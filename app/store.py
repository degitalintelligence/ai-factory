import hashlib
import json
from datetime import timedelta

from sqlalchemy import or_, select, text, update
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.db import ACTIVE, Artifact, Event, SessionLocal, Task, utcnow
from app.security import redact


class TaskStopped(RuntimeError):
    pass


class BudgetExceeded(RuntimeError):
    pass


def plan_hash(plan_json: str) -> str:
    return hashlib.sha256(plan_json.encode()).hexdigest()


class Store:
    def __init__(self, sessions=SessionLocal):
        self.sessions = sessions

    async def get(self, task_id):
        async with self.sessions() as s:
            return await s.get(Task, task_id)

    async def create(self, requirement, project="lab", chat_id=None, user_id=None, idempotency_key=None):
        requirement = requirement.strip()
        if not 5 <= len(requirement) <= 20000:
            raise ValueError("Requirement must contain 5–20000 characters")
        if redact(requirement) != requirement:
            raise ValueError("Remove credentials from the requirement; configure them outside the task")
        policy = settings.projects().get(project)
        if policy is None:
            raise ValueError("Unknown project alias; use /projects")
        async with self.sessions() as s:
            task = Task(
                requirement=requirement,
                project=project,
                repo=policy.repo,
                base_branch=policy.base_branch,
                policy_json=policy.model_dump_json(),
                chat_id=chat_id,
                user_id=user_id,
                idempotency_key=idempotency_key,
            )
            s.add(task)
            try:
                await s.commit()
            except IntegrityError:
                await s.rollback()
                task = await s.scalar(select(Task).where(Task.idempotency_key == idempotency_key))
                if not idempotency_key or task is None:
                    raise
                if task.requirement != requirement or task.project != project or task.user_id != user_id:
                    raise ValueError("Idempotency key already belongs to a different request")
                return task
            await s.refresh(task)
            task.branch = f"ai-factory/task-{task.id}"
            s.add(Event(task_id=task.id, kind="received", message=f"Queued for {task.repo}"))
            await s.commit()
            return task

    async def list(self, limit=20, user_id=None):
        async with self.sessions() as s:
            query = select(Task).order_by(Task.id.desc()).limit(limit)
            if user_id is not None:
                query = query.where(Task.user_id == user_id)
            return list(await s.scalars(query))

    async def events(self, task_id, limit=30):
        async with self.sessions() as s:
            return list(
                await s.scalars(
                    select(Event).where(Event.task_id == task_id).order_by(Event.id.desc()).limit(limit)
                )
            )

    async def artifacts(self, task_id):
        async with self.sessions() as s:
            return list(
                await s.scalars(select(Artifact).where(Artifact.task_id == task_id).order_by(Artifact.id))
            )

    async def artifact(self, task_id, kind, content):
        async with self.sessions() as s:
            s.add(Artifact(task_id=task_id, kind=kind, content=redact(content)))
            await s.commit()

    async def event(self, task_id, kind, message):
        async with self.sessions() as s:
            s.add(Event(task_id=task_id, kind=kind, message=redact(message)[:24000]))
            await s.commit()

    async def update(self, task_id, owner=None, **values):
        async with self.sessions() as s:
            query = update(Task).where(Task.id == task_id)
            if owner:
                query = query.where(Task.lease_owner == owner, Task.lease_until > utcnow())
            values["updated_at"] = utcnow()
            if "last_message" in values:
                values["last_message"] = redact(values["last_message"])
            result = await s.execute(query.values(**values))
            await s.commit()
            if not result.rowcount:
                raise TaskStopped("Task missing or worker lease lost")

    async def check(self, task_id, owner):
        task = await self.get(task_id)
        if (
            not task
            or task.cancel_requested
            or task.lease_owner != owner
            or not task.lease_until
            or task.lease_until <= utcnow()
        ):
            raise TaskStopped("Task cancelled or worker lease lost")
        return task

    async def claim(self, owner):
        now = utcnow()
        async with self.sessions() as s, s.begin():
            # Serialize short claims (not execution); prevents two workers claiming the same repo.
            if s.bind.dialect.name == "postgresql":
                await s.execute(text("SELECT pg_advisory_xact_lock(72803103)"))
            stale = list(
                await s.scalars(
                    select(Task)
                    .where(Task.status.in_(ACTIVE), or_(Task.lease_until < now, Task.lease_until.is_(None)))
                    .with_for_update(skip_locked=True)
                )
            )
            for task in stale:
                task.recoveries += 1
                task.lease_owner = None
                task.lease_until = None
                if task.cancel_requested:
                    task.status = "cancelled"
                elif task.recoveries > settings.max_recoveries:
                    task.status = "failed"
                    task.last_message = "Recovery limit reached; inspect /logs then /retry"
                else:
                    # Publishing remains a checkpoint; reconcile its existing branch/PR first.
                    task.status = "received"
                s.add(
                    Event(
                        task_id=task.id,
                        kind="recovery",
                        message=f"Expired lease recovered; state={task.status}",
                    )
                )
            await s.flush()
            busy = select(Task.repo).where(Task.lease_until > now, Task.status.in_(ACTIVE))
            task = await s.scalar(
                select(Task)
                .where(Task.status == "received", Task.cancel_requested.is_(False), Task.repo.not_in(busy))
                .order_by(Task.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if not task:
                return None
            # CAS also makes SQLite's development mode safe for duplicate claims.
            result = await s.execute(
                update(Task)
                .where(Task.id == task.id, Task.status == "received")
                .values(
                    status="planning",
                    lease_owner=owner,
                    lease_until=now + timedelta(seconds=settings.lease_seconds),
                    updated_at=now,
                )
            )
            if not result.rowcount:
                return None
            await s.refresh(task)
            return task

    async def heartbeat(self, task_id, owner):
        await self.update(task_id, owner, lease_until=utcnow() + timedelta(seconds=settings.lease_seconds))

    async def reserve_call(self, task_id, owner, token_reserve=None):
        task = await self.check(task_id, owner)
        if (
            task.llm_calls >= settings.max_llm_calls
            or task.tokens + (token_reserve or settings.max_output_tokens) > settings.max_total_tokens
            or task.cost_usd >= settings.max_cost_usd
        ):
            raise BudgetExceeded("Task LLM budget reached; inspect usage before starting another task")
        await self.update(task_id, owner, llm_calls=task.llm_calls + 1)

    async def record_usage(self, task_id, owner, tokens, cost):
        task = await self.check(task_id, owner)
        await self.update(
            task_id,
            owner,
            tokens=task.tokens + tokens,
            cost_usd=task.cost_usd + (cost or 0),
            cost_incomplete=task.cost_incomplete or cost is None,
        )

    async def cancel(self, task_id):
        async with self.sessions() as s, s.begin():
            task = await s.get(Task, task_id, with_for_update=True)
            if not task:
                raise ValueError("Task not found")
            if task.status == "pr_created":
                raise ValueError("PR already published; cancellation cannot undo publication")
            task.cancel_requested = True
            if task.status not in ACTIVE:
                task.status = "cancelled"
            s.add(Event(task_id=task_id, kind="cancel", message="Cancellation requested"))

    async def resume(self, task_id, action, message=""):
        async with self.sessions() as s, s.begin():
            task = await s.get(Task, task_id, with_for_update=True)
            if not task:
                raise ValueError("Task not found")
            if task.lease_until and task.lease_until > utcnow():
                raise ValueError("Task is still running; wait until its worker stops")
            if action == "approve":
                if task.status != "awaiting_approval" or not task.plan_json:
                    raise ValueError("Task has no plan awaiting approval")
                digest = plan_hash(task.plan_json)
                if message != digest[:12]:
                    raise ValueError("Approval must include the current plan hash shown by /plan")
                task.approved_plan_hash = digest
            elif action == "answer":
                if task.status != "waiting_input" or not message.strip():
                    raise ValueError("Task is not waiting for input or answer is empty")
                task.requirement += "\n\nUser clarification:\n" + message[:10000]
                task.plan_json = None
                task.approved_plan_hash = None
            elif action == "feedback":
                if task.status != "pr_created" or not message.strip():
                    raise ValueError("Feedback requires a published PR and a message")
                task.requirement += "\n\nRequested revision:\n" + message[:10000]
                task.plan_json = None
                task.approved_plan_hash = None
                task.review_digest = None
                task.head_sha = None
                task.iteration = 0
                task.feedback_json = json.dumps([message])
            elif action == "retry":
                if task.status not in {"failed", "cancelled"}:
                    raise ValueError("Only failed/cancelled tasks can be retried")
                task.iteration = 0
            else:
                raise ValueError("Unknown resume action")
            if redact(task.requirement) != task.requirement:
                raise ValueError("Remove credentials from the message")
            task.status = "received"
            task.cancel_requested = False
            task.lease_owner = None
            task.lease_until = None
            task.recoveries = 0
            s.add(Event(task_id=task_id, kind=action, message=redact(message or action)))


store = Store()
