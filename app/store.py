import hashlib
import json
from datetime import timedelta

from sqlalchemy import or_, select, text, update
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.db import ACTIVE, Artifact, Decision, Event, MemoryItem, SessionLocal, Task, utcnow
from app.schemas import (
    CLEARANCE,
    DECISION_PHRASES,
    OUTCOME_STATE,
    DecisionState,
    MemoryState,
    MemoryView,
    MemoryWrite,
)
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

    async def create(
        self,
        requirement,
        project="lab",
        chat_id=None,
        user_id=None,
        idempotency_key=None,
        kind="engineering",
        brief=None,
    ):
        requirement = requirement.strip()
        if not 5 <= len(requirement) <= 20000:
            raise ValueError("Requirement must contain 5–20000 characters")
        if redact(requirement) != requirement:
            raise ValueError("Remove credentials from the requirement; configure them outside the task")
        policy = settings.projects().get(project)
        if policy is None:
            raise ValueError("Unknown project alias; use /projects")
        if kind == "self_improvement" and brief is None:
            raise ValueError("self_improvement tasks require a SelfImprovementBrief")
        async with self.sessions() as s:
            # An idempotent retry must return the original task even while it is active.
            if idempotency_key:
                existing = await s.scalar(select(Task).where(Task.idempotency_key == idempotency_key))
                if existing is not None:
                    if (
                        existing.requirement != requirement
                        or existing.project != project
                        or existing.user_id != user_id
                    ):
                        raise ValueError("Idempotency key already belongs to a different request")
                    return existing
            # One active task per repository is the default safety policy.
            running = await s.scalar(
                select(Task)
                .where(Task.repo == policy.repo, Task.status.in_(ACTIVE), Task.cancel_requested.is_(False))
                .limit(1)
            )
            if running:
                raise ValueError(
                    f"One active task per repository; task #{running.id} is still {running.status} on {policy.repo}"
                )
            task = Task(
                requirement=requirement,
                kind=kind,
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
            if brief is not None:
                s.add(
                    Artifact(task_id=task.id, kind="self_improvement_brief", content=brief.model_dump_json())
                )
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

    async def create_decision(self, card):
        """Persist a decision card. It never lives only in a channel message."""
        async with self.sessions() as s:
            decision = Decision(
                task_id=card.task_id,
                project=card.project,
                kind=card.kind,
                title=card.title,
                situation=redact(card.situation),
                why_now=redact(card.why_now),
                options_json=json.dumps([o.model_dump() for o in card.options]),
                impact_json=json.dumps([o.impact for o in card.options]),
                risk_json=json.dumps([o.risk for o in card.options]),
                recommendation=redact(card.recommendation),
                evidence_json=json.dumps([redact(e) for e in card.evidence]),
                missing_information=redact(card.missing_information),
                rollback=redact(card.rollback),
                required_action=card.required_action,
                priority=card.priority,
                risk_level=card.risk_level,
                expires_at=card.expires_at,
            )
            s.add(decision)
            await s.commit()
            await s.refresh(decision)
            if card.task_id:
                s.add(
                    Event(
                        task_id=card.task_id,
                        kind=card.kind,
                        message=redact(f"Decision #{decision.id} requested: {card.title}"),
                    )
                )
                await s.commit()
            return decision

    async def inbox(self, state=None, project=None, limit=50):
        """List decisions newest first, filtered by state/project for the operator inbox."""
        async with self.sessions() as s:
            query = select(Decision).order_by(Decision.id.desc()).limit(limit)
            if state:
                query = query.where(Decision.state == str(state))
            if project:
                query = query.where(Decision.project == project)
            return list(await s.scalars(query))

    async def expire_stale_decisions(self):
        """Mark past-expiry open decisions expired. Expiry is not a decision."""
        now = utcnow()
        async with self.sessions() as s, s.begin():
            rows = list(
                await s.scalars(
                    select(Decision).where(Decision.state == DecisionState.OPEN, Decision.expires_at < now)
                )
            )
            for row in rows:
                row.state = DecisionState.EXPIRED
            if rows:
                await s.flush()
            return len(rows)

    async def resolve_decision(self, decision_id, phrase, user_id=None):
        """Apply one decision outcome. Idempotent and auditable.

        A repeated identical answer returns the stored decision without changing
        state again. A conflicting answer after resolution is rejected.
        """
        outcome = DECISION_PHRASES.get(phrase.strip().lower())
        if not outcome:
            raise ValueError("Answer with one of: approve, reject, ask, defer")
        async with self.sessions() as s:
            decision = await s.get(Decision, decision_id, with_for_update=True)
            if not decision:
                raise ValueError("Decision not found")
            target = OUTCOME_STATE[outcome]
            if decision.state != DecisionState.OPEN:
                if decision.state == target:
                    return decision
                raise ValueError(
                    f"Decision #{decision_id} is already {decision.state}; start a new decision instead"
                )
            if decision.expires_at and decision.expires_at < utcnow():
                # Persist the expiry before raising, otherwise the rollback keeps it open forever.
                decision.state = DecisionState.EXPIRED
                await s.commit()
                raise ValueError("Decision expired; request a new one")
            decision.state = target
            decision.decided_by = user_id
            decision.decided_at = utcnow()
            if decision.task_id:
                s.add(
                    Event(
                        task_id=decision.task_id,
                        kind="decision",
                        message=redact(f"Decision #{decision_id} -> {target} by {user_id}"),
                    )
                )
            await s.commit()
            await s.refresh(decision)
            return decision

    # --- Context and Memory Plane ---

    async def remember(self, item: MemoryWrite, owner=None, task_id=None):
        """Store one memory, superseding any earlier active version of the same key.

        A correction never overwrites history: the old row is kept and marked superseded,
        so what was previously believed stays auditable.
        """
        if redact(item.value) != item.value:
            raise ValueError("Remove credentials before storing this as memory")
        async with self.sessions() as s:
            previous = list(
                await s.scalars(
                    select(MemoryItem)
                    .where(MemoryItem.key == item.key, MemoryItem.state == MemoryState.ACTIVE)
                    .with_for_update()
                )
            )
            # A contradictory value for the same key is a conflict, not a silent overwrite.
            if any(p.value != item.value for p in previous):
                raise ValueError(
                    f"Memory '{item.key}' already holds a different value; correct it explicitly"
                )
            if previous:
                for row in previous:
                    row.state = MemoryState.SUPERSEDED
                version = max(p.version for p in previous) + 1
                supersedes = previous[0].id
                # Emit the SUPERSEDED update before inserting the replacement so the
                # partial unique index never sees two active rows for one key.
                await s.flush()
            else:
                version, supersedes = 1, None
            row = MemoryItem(
                key=item.key,
                value=item.value,
                scope=item.scope,
                sensitivity=item.sensitivity,
                source=item.source,
                evidence_ref=item.evidence_ref,
                owner=owner,
                confidence=item.confidence,
                version=version,
                supersedes=supersedes,
                locked=item.locked,
                expires_at=item.expires_at,
            )
            s.add(row)
            if task_id:
                s.add(
                    Event(
                        task_id=task_id,
                        kind="memory",
                        message=redact(f"Memory '{item.key}' v{version} stored ({item.source})"),
                    )
                )
            try:
                await s.commit()
            except IntegrityError as exc:
                await s.rollback()
                raise ValueError(f"Memory '{item.key}' was stored concurrently; retry") from exc
            await s.refresh(row)
            return row

    async def recall(self, keys=None, role=None, scope=None, limit=25):
        """Return a permission-aware slice. Never returns a whole-table dump.

        `role` is an agent role resolved through the audited clearance config. An
        unknown role gets nothing, so a typo cannot widen access.
        """
        clearance = CLEARANCE.get(settings.role_clearance().get(role, "none"), -1)
        async with self.sessions() as s:
            query = select(MemoryItem).where(MemoryItem.state == MemoryState.ACTIVE)
            if keys:
                query = query.where(MemoryItem.key.in_(keys))
            if scope is not None:
                query = query.where(or_(MemoryItem.scope == scope, MemoryItem.scope == ""))
            rows = list(await s.scalars(query.order_by(MemoryItem.id.desc())))
        allowed = [r for r in rows if CLEARANCE.get(r.sensitivity, 0) <= clearance]
        now = utcnow()
        views = [self._view(r, now) for r in allowed[:limit]]
        return views, len(allowed) > limit

    @staticmethod
    def _view(row, now):
        stale = bool(row.expires_at and row.expires_at < now)
        label = "stale" if stale else ("unverified" if row.confidence < 0.5 else "current")
        return MemoryView(
            id=row.id,
            key=row.key,
            value=row.value,
            scope=row.scope,
            sensitivity=row.sensitivity,
            source=row.source,
            evidence_ref=row.evidence_ref,
            confidence=row.confidence,
            version=row.version,
            state=row.state,
            stale=stale,
            label=label,
            created_at=row.created_at.isoformat(),
            expires_at=row.expires_at.isoformat() if row.expires_at else None,
        )

    async def correct_memory(self, memory_id, value, source, owner=None, task_id=None):
        """Replace a memory's value with a new version, keeping the previous one auditable."""
        async with self.sessions() as s:
            row = await s.get(MemoryItem, memory_id, with_for_update=True)
            if not row:
                raise ValueError("Memory not found")
            if row.locked:
                raise ValueError("Memory is locked by Dedi and cannot be corrected automatically")
            if row.state != MemoryState.ACTIVE:
                raise ValueError(f"Memory is {row.state}; correct the active version instead")
            if redact(value) != value:
                raise ValueError("Remove credentials before storing this as memory")
            replacement = MemoryItem(
                key=row.key,
                value=value,
                scope=row.scope,
                sensitivity=row.sensitivity,
                source=source,
                evidence_ref=row.evidence_ref,
                owner=owner if owner is not None else row.owner,
                confidence=row.confidence,
                version=row.version + 1,
                supersedes=row.id,
                locked=row.locked,
                expires_at=row.expires_at,
            )
            row.state = MemoryState.SUPERSEDED
            # Emit the SUPERSEDED update before inserting the replacement so the
            # partial unique index never sees two active rows for one key.
            await s.flush()
            # Captured before commit: a rollback expires the row, so the error
            # message below must not touch it again.
            key = row.key
            s.add(replacement)
            if task_id:
                s.add(
                    Event(
                        task_id=task_id,
                        kind="memory",
                        message=redact(f"Memory '{row.key}' corrected to v{row.version + 1} ({source})"),
                    )
                )
            try:
                await s.commit()
            except IntegrityError as exc:
                await s.rollback()
                raise ValueError(f"Memory '{key}' was modified concurrently; retry the correction") from exc
            await s.refresh(replacement)
            return replacement

    async def retract_memory(self, memory_id, reason, owner=None, task_id=None):
        """Close a memory without deleting it. The row and its reason remain auditable."""
        async with self.sessions() as s:
            row = await s.get(MemoryItem, memory_id, with_for_update=True)
            if not row:
                raise ValueError("Memory not found")
            if row.locked and owner is None:
                raise ValueError("Locked memories can only be retracted by their owner")
            row.state = MemoryState.RETRACTED
            if task_id:
                s.add(
                    Event(
                        task_id=task_id,
                        kind="memory",
                        message=redact(f"Memory '{row.key}' retracted: {reason}"),
                    )
                )
            await s.commit()
            await s.refresh(row)
            return row

    async def memory_history(self, key):
        """Every version of a key, newest first, so a correction can be inspected."""
        async with self.sessions() as s:
            rows = list(
                await s.scalars(
                    select(MemoryItem).where(MemoryItem.key == key).order_by(MemoryItem.version.desc())
                )
            )
            now = utcnow()
            return [self._view(r, now) for r in rows]


store = Store()
