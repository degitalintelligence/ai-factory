import hashlib
import json
from datetime import timedelta

from sqlalchemy import or_, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import aliased

from app.config import settings
from app.db import (
    ACTIVE,
    REPOSITORY_RESERVED,
    Artifact,
    AuditLog,
    DailyBudget,
    Decision,
    Event,
    ImprovementProposal,
    MemoryConflict,
    MemoryItem,
    ModelRun,
    SessionLocal,
    Subtask,
    Task,
    utcnow,
)
from app.schemas import (
    CLEARANCE,
    DECISION_PHRASES,
    OUTCOME_STATE,
    DecisionMessageType,
    DecisionState,
    ImprovementOutcome,
    MemoryState,
    MemoryView,
    MemoryWrite,
)
from app.security import redact, secret_present

# The single tenant a deployment owns. Memory is isolated per tenant; this value is the
# boundary a read must match, never a wildcard.
DEFAULT_TENANT = "default"

# Utilization of the binding budget envelope that raises a durable, actionable warning
# before the hard stop. The envelope, not the raw policy ceiling, is what these measure.
BUDGET_WARNING_THRESHOLDS = (0.60, 0.80, 0.95)


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

    @staticmethod
    async def _require_same_request(session, existing, requirement, project, chat_id, user_id, kind, brief):
        """An idempotency key must describe exactly one request.

        The policy snapshot is deliberately not compared: it is server-side state that
        may legitimately change between two identical retries, and a stale task is
        stopped by the policy-revocation check rather than by refusing the retry.
        """
        same = (
            existing.requirement == requirement
            and existing.project == project
            and existing.chat_id == chat_id
            and existing.user_id == user_id
            and existing.kind == kind
        )
        if same:
            stored = await session.scalar(
                select(Artifact.content).where(
                    Artifact.task_id == existing.id, Artifact.kind == "self_improvement_brief"
                )
            )
            same = stored == (brief.model_dump_json() if brief is not None else None)
        if not same:
            raise ValueError("Idempotency key already belongs to a different request")

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
        from app.audit_scope import target_project

        project = target_project(requirement, project, settings.projects())
        policy = settings.projects().get(project)
        if policy is None:
            raise ValueError("Unknown project alias; use /projects")
        if kind == "self_improvement":
            if brief is None:
                raise ValueError("self_improvement tasks require a SelfImprovementBrief")
            # Self-improvement must target the explicitly registered self-hosting
            # alias, never a default fallback such as the lab harness (AGENTS.md §15).
            self_target = settings.self_project
            if not self_target:
                raise ValueError(
                    "Self-improvement has no registered self-target alias; "
                    "set SELF_PROJECT to one before creating self_improvement tasks"
                )
            if project != self_target:
                raise ValueError(
                    f"Self-improvement must target the registered self-target alias "
                    f"'{self_target}', not '{project}'"
                )
        try:
            async with self.sessions() as s, s.begin():
                # Same lock as claim(): a queued task must not appear between a worker's
                # blocker check and its lease write.
                if s.bind.dialect.name == "postgresql":
                    await s.execute(text("SELECT pg_advisory_xact_lock(72803103)"))
                # An idempotent retry must return the original task even while it is active.
                if idempotency_key:
                    existing = await s.scalar(select(Task).where(Task.idempotency_key == idempotency_key))
                    if existing is not None:
                        await self._require_same_request(
                            s, existing, requirement, project, chat_id, user_id, kind, brief
                        )
                        return existing
                # One reserved task per repository is the default safety policy. A task that
                # is only queued still owns the repository, otherwise repeated /new calls
                # stack unclaimed work that later claims race to execute.
                running = await s.scalar(
                    select(Task)
                    .where(
                        Task.repo == policy.repo,
                        Task.status.in_(REPOSITORY_RESERVED),
                        Task.cancel_requested.is_(False),
                    )
                    .limit(1)
                )
                if running:
                    raise ValueError(
                        f"One active task per repository; task #{running.id} is still "
                        f"{running.status} on {policy.repo}"
                    )
                task = Task(
                    tenant=settings.tenant_id,
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
                # Intake is one transaction: a task row is never visible without its branch,
                # its received event, and its brief. Any failure rolls the whole intake back
                # instead of leaving an unclaimable half-task behind.
                await s.flush()
                task.branch = f"ai-factory/task-{task.id}"
                s.add(Event(task_id=task.id, kind="received", message=f"Queued for {task.repo}"))
                if brief is not None:
                    s.add(
                        Artifact(
                            task_id=task.id,
                            kind="self_improvement_brief",
                            content=brief.model_dump_json(),
                        )
                    )
                return task
        except IntegrityError:
            # The unique idempotency key is the only unique constraint an intake can
            # violate, so a collision means a concurrent identical request. The advisory
            # lock already prevents this on PostgreSQL; this is the remaining fallback.
            if not idempotency_key:
                raise
            async with self.sessions() as s:
                existing = await s.scalar(select(Task).where(Task.idempotency_key == idempotency_key))
                if existing is None:
                    raise
                await self._require_same_request(
                    s, existing, requirement, project, chat_id, user_id, kind, brief
                )
                return existing

    async def list(self, limit=20, user_id=None):
        async with self.sessions() as s:
            query = (
                select(Task).where(Task.tenant == settings.tenant_id).order_by(Task.id.desc()).limit(limit)
            )
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

    async def artifact(self, task_id, kind, content, owner=None):
        async with self.sessions() as s:
            if owner:
                task = await s.get(Task, task_id, with_for_update=True)
                if (
                    not task
                    or task.lease_owner != owner
                    or task.lease_until <= utcnow()
                    or task.cancel_requested
                ):
                    raise TaskStopped("Worker lease lost")
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
        if task.kind == "self_improvement" and not settings.self_improvement_enabled:
            raise TaskStopped("Self-improvement kill switch is active")
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
            candidate, blocker = aliased(Task), aliased(Task)
            # A repository stays reserved from intake until it reaches a terminal state,
            # so a paused task or an open PR is never bypassed by a second branch. Only a
            # lower-id task blocks the candidate: the oldest task in a repository always
            # wins, which keeps a queued task claimable and legacy stacked rows progressing
            # instead of deadlocking each other.
            reserved = (
                select(blocker.id)
                .where(
                    blocker.repo == candidate.repo,
                    blocker.id < candidate.id,
                    blocker.status.in_(REPOSITORY_RESERVED),
                    blocker.cancel_requested.is_(False),
                )
                .correlate(candidate)
                .exists()
            )
            task = await s.scalar(
                select(candidate)
                .where(
                    candidate.status == "received",
                    or_(candidate.kind != "self_improvement", settings.self_improvement_enabled),
                    candidate.cancel_requested.is_(False),
                    # A task without a branch is a half-written intake; refuse to execute it.
                    candidate.branch.is_not(None),
                    ~reserved,
                )
                .order_by(candidate.id)
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

    @staticmethod
    def _plan_budget(task):
        """The budget the Lead requested for this task, or {} when there is no plan yet."""
        try:
            plan = json.loads(task.plan_json or "{}")
        except json.JSONDecodeError:
            return {}
        budget = plan.get("budget") if isinstance(plan, dict) else None
        return budget if isinstance(budget, dict) else {}

    @classmethod
    def budget_envelope(cls, task):
        """The effective ceiling for one task: the tighter of policy and requested budget.

        The planner's budget is a request, not an authority, so it can only lower the
        operator-configured ceiling — a plan can never raise it. Returning the binding
        limit explicitly is what makes a budget number explainable after the fact instead
        of a ratio against an unknown bound.
        """
        plan = cls._plan_budget(task)
        return {
            "max_llm_calls": int(
                min(settings.max_llm_calls, plan.get("max_llm_calls") or settings.max_llm_calls)
            ),
            "max_total_tokens": int(
                min(settings.max_total_tokens, plan.get("max_tokens") or settings.max_total_tokens)
            ),
            "max_cost_usd": float(
                min(settings.max_cost_usd, plan.get("max_cost_usd") or settings.max_cost_usd)
            ),
        }

    @classmethod
    def _budget_ratio(cls, task, envelope=None):
        limits = envelope or cls.budget_envelope(task)
        return max(
            task.llm_calls / limits["max_llm_calls"],
            task.tokens / limits["max_total_tokens"],
            task.cost_usd / limits["max_cost_usd"],
        )

    @classmethod
    def _degradation_plan(cls, task, envelope=None):
        """What to do next when a budget is running out, before the hard stop.

        v0.3 asks for automatic degradation instead of a bare error, so the closest
        dimension names its own remedy: shrink the input that drives it.
        """
        limits = envelope or cls.budget_envelope(task)
        if task.tokens / limits["max_total_tokens"] >= 0.80:
            return (
                "Degrade now: narrow the inspected file set and the tool-history window, or "
                "split the requirement into a smaller bounded task."
            )
        if task.llm_calls / limits["max_llm_calls"] >= 0.80:
            return (
                "Degrade now: reduce parallelism to one worker step at a time, lower "
                "max_dev_steps, or request an approved budget increase before continuing."
            )
        return (
            "Degrade now: request an approved budget increase, or create a new bounded "
            "task; usage is never reset by /retry."
        )

    async def _emit_budget_warnings(self, task_id, owner, before, after):
        envelope = self.budget_envelope(after)
        for threshold in BUDGET_WARNING_THRESHOLDS:
            if before < threshold <= self._budget_ratio(after, envelope):
                percent = int(threshold * 100)
                message = (
                    f"Budget warning {percent}%: calls={after.llm_calls}/{envelope['max_llm_calls']}, "
                    f"tokens={after.tokens}/{envelope['max_total_tokens']}, "
                    f"reported_cost=${after.cost_usd:.4f}/${envelope['max_cost_usd']:.2f}. "
                    f"{self._degradation_plan(after, envelope)} "
                    "No automatic reset: /retry retains lifetime usage."
                )
                await self.event(task_id, "budget_warning", message)

    async def budget_status(self, task_id):
        """Durable budget envelope, consumption, and the next action for /status and /report."""
        task = await self.get(task_id)
        if not task:
            raise ValueError("Task not found")
        envelope = self.budget_envelope(task)
        return {
            "envelope": envelope,
            "requested": self._plan_budget(task),
            "used": {"llm_calls": task.llm_calls, "tokens": task.tokens, "cost_usd": task.cost_usd},
            "utilization": self._budget_ratio(task, envelope),
            "cost_incomplete": task.cost_incomplete,
            "warning_thresholds": list(BUDGET_WARNING_THRESHOLDS),
            "exhausted": (
                task.llm_calls >= envelope["max_llm_calls"]
                or task.tokens >= envelope["max_total_tokens"]
                or task.cost_usd >= envelope["max_cost_usd"]
            ),
            "degradation": self._degradation_plan(task, envelope),
        }

    async def reserve_call(self, task_id, owner, token_reserve=None, subtask=None):
        task = await self.check(task_id, owner)
        envelope = self.budget_envelope(task)
        reserve = token_reserve or settings.max_output_tokens
        blockers = []
        if task.llm_calls >= envelope["max_llm_calls"]:
            blockers.append(f"calls={task.llm_calls}/{envelope['max_llm_calls']}")
        if task.tokens + reserve > envelope["max_total_tokens"]:
            blockers.append(
                f"token_reservation: used={task.tokens}, next_reserve={reserve}, "
                f"limit={envelope['max_total_tokens']}"
            )
        if task.cost_usd >= envelope["max_cost_usd"]:
            blockers.append(f"reported_cost={task.cost_usd:.4f}/{envelope['max_cost_usd']}")
        if blockers:
            raise BudgetExceeded(
                "Task LLM budget exhausted (" + "; ".join(blockers) + "); "
                "use /report and /logs, then create a new bounded task "
                "or obtain an approved policy change. /retry does not reset lifetime usage."
            )
        before = self._budget_ratio(task, envelope)
        async with self.sessions() as session, session.begin():
            # The tenant lock serializes daily accounting across different tasks/workers.
            if session.bind.dialect.name == "postgresql":
                lock = int.from_bytes(hashlib.sha256(task.tenant.encode()).digest()[:8], "big", signed=True)
                await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock})
            current = await session.get(Task, task_id, with_for_update=True)
            if current.lease_owner != owner or current.lease_until <= utcnow() or current.cancel_requested:
                raise TaskStopped("Worker lease lost")
            if current.llm_calls >= envelope["max_llm_calls"]:
                raise BudgetExceeded("Task lifetime model-call budget exhausted")
            key = f"{task.tenant}:{utcnow().date().isoformat()}"
            daily = await session.get(DailyBudget, key, with_for_update=True)
            if not daily:
                daily = DailyBudget(id=key, calls=0, reserved_tokens=0, cost_usd=0.0)
                session.add(daily)
            reserve = token_reserve or settings.max_output_tokens
            if (
                daily.calls >= settings.global_max_llm_calls_per_day
                or daily.reserved_tokens + reserve > settings.global_max_tokens_per_day
                or daily.cost_usd >= settings.global_max_cost_usd_per_day
            ):
                raise BudgetExceeded("Tenant daily model budget exhausted")
            if subtask:
                row = await session.get(Subtask, subtask[0], with_for_update=True)
                if not row or row.task_id != task_id or row.calls >= subtask[1]:
                    raise BudgetExceeded("Subtask lifetime model-call budget exhausted")
                row.calls += 1
            global_before = max(
                daily.calls / settings.global_max_llm_calls_per_day,
                daily.reserved_tokens / settings.global_max_tokens_per_day,
                daily.cost_usd / settings.global_max_cost_usd_per_day,
            )
            daily.calls += 1
            daily.reserved_tokens += reserve
            current.llm_calls += 1
            global_after = max(
                daily.calls / settings.global_max_llm_calls_per_day,
                daily.reserved_tokens / settings.global_max_tokens_per_day,
                daily.cost_usd / settings.global_max_cost_usd_per_day,
            )
            for threshold in BUDGET_WARNING_THRESHOLDS:
                if global_before < threshold <= global_after:
                    message = f"Tenant daily budget warning {int(threshold * 100)}%: narrow new goals; every attempt consumes conservative token reservation. No budget reset."
                    session.add(Event(task_id=task_id, kind="budget_warning", message=message))
                    session.add(
                        AuditLog(
                            tenant=task.tenant,
                            actor=task.user_id,
                            task_id=task_id,
                            scope=task.project,
                            action="global_budget_warning",
                            correlation_id=key,
                            detail=message,
                        )
                    )
        await self._emit_budget_warnings(task_id, owner, before, await self.get(task_id))

    async def record_usage(self, task_id, owner, tokens, cost):
        task = await self.check(task_id, owner)
        envelope = self.budget_envelope(task)
        before = self._budget_ratio(task, envelope)
        await self.update(
            task_id,
            owner,
            tokens=task.tokens + tokens,
            cost_usd=task.cost_usd + (cost or 0),
            cost_incomplete=task.cost_incomplete or cost is None,
        )
        async with self.sessions() as session, session.begin():
            daily = await session.get(
                DailyBudget, f"{task.tenant}:{utcnow().date().isoformat()}", with_for_update=True
            )
            if daily:
                daily.cost_usd += cost or 0
        await self._emit_budget_warnings(task_id, owner, before, await self.get(task_id))

    async def record_model_run(
        self,
        task_id,
        *,
        role="",
        model_alias="",
        model="",
        prompt_version="",
        prompt_sha256="",
        schema_name="",
        attempt=1,
        outcome="ok",
        detail="",
        tokens=0,
        cost_usd=None,
        latency_ms=0,
    ):
        """Append one immutable per-call record.

        The task counters are lifetime aggregates that survive retries but cannot be
        decomposed afterwards, so this is where the per-call evidence lives: which role
        ran, which alias resolved to which provider model, which prompt version was sent,
        how long it took, and how it ended. Rows are only ever inserted.
        """
        async with self.sessions() as s:
            s.add(
                ModelRun(
                    task_id=task_id,
                    role=role,
                    model_alias=model_alias,
                    model=model,
                    prompt_version=prompt_version,
                    prompt_sha256=prompt_sha256,
                    schema_name=schema_name,
                    attempt=attempt,
                    outcome=outcome,
                    detail=redact(detail)[:2000],
                    tokens=tokens,
                    cost_usd=cost_usd,
                    cost_reported=cost_usd is not None,
                    latency_ms=latency_ms,
                )
            )
            task = await s.get(Task, task_id)
            if task:
                s.add(
                    AuditLog(
                        tenant=task.tenant,
                        actor=task.user_id,
                        task_id=task_id,
                        scope=task.project,
                        action="model_call",
                        correlation_id=f"intent-{task_id}",
                        detail=json.dumps(
                            {
                                "role": role,
                                "model": model,
                                "alias": model_alias,
                                "prompt_sha256": prompt_sha256,
                                "attempt": attempt,
                                "outcome": outcome,
                            }
                        ),
                    )
                )
            await s.commit()

    async def model_runs(self, task_id, limit=200):
        async with self.sessions() as s:
            return list(
                await s.scalars(
                    select(ModelRun).where(ModelRun.task_id == task_id).order_by(ModelRun.id).limit(limit)
                )
            )

    async def cancel(self, task_id):
        """Request cooperative cancellation. Repeating it changes nothing.

        The flag is the state, so a second request from another channel must not append a
        second event: the audit trail would then claim two distinct operator decisions where
        only one was ever made.
        """
        async with self.sessions() as s, s.begin():
            task = await s.get(Task, task_id, with_for_update=True)
            if not task:
                raise ValueError("Task not found")
            if task.status == "reviewed":
                raise ValueError("Reviewed task is terminal; create a fresh task")
            if task.status == "superseded":
                raise ValueError("Superseded task is terminal; create a fresh task")
            if task.status in {"pr_created", "completed"}:
                raise ValueError("Published task is terminal; cancellation cannot undo publication")
            if task.status in {"deployment_pending", "deploying", "deployment_unknown"}:
                raise ValueError(
                    "Deployment was already submitted; cancellation cannot stop Coolify. "
                    "Use /deployment to reconcile it."
                )
            if task.status == "deployed":
                raise ValueError("Deployed task is terminal; cancellation cannot undo a deployment")
            if task.status == "deployment_failed":
                raise ValueError(
                    "Deployment-failed task is terminal; inspect Coolify and create a fresh task"
                )
            if task.cancel_requested:
                # Already requested: keep the original flag and event, refresh nothing.
                return
            task.cancel_requested = True
            if task.status not in ACTIVE:
                task.status = "cancelled"
            s.add(Event(task_id=task_id, kind="cancel", message="Cancellation requested"))

    async def supersede(self, task_id, message):
        """Release a stale merged-PR checkpoint after explicit validation."""
        message = redact(message.strip())
        if not message:
            raise ValueError("Supersede requires a reason")
        async with self.sessions() as s, s.begin():
            task = await s.get(Task, task_id, with_for_update=True)
            if not task:
                raise ValueError("Task not found")
            if task.status != "pr_created":
                raise ValueError("Only a stale pr_created task can be superseded")
            if task.lease_until and task.lease_until > utcnow():
                raise ValueError("Task is still running; wait until its worker stops")
            task.status = "superseded"
            task.last_message = f"Superseded: {message[:12000]}"
            task.cancel_requested = False
            task.lease_owner = None
            task.lease_until = None
            s.add(Event(task_id=task_id, kind="superseded", message=message[:24000]))
            await s.flush()
            return task

    async def _apply_resume(self, session, task_id, action, message="", user_id=None, close_card=True):
        """Apply one resume transition inside a caller-owned transaction.

        resolve_decision must reuse this exact logic so a decision and the task
        transition it authorises commit together or not at all.
        """
        task = await session.get(Task, task_id, with_for_update=True)
        if not task:
            raise ValueError("Task not found")
        if task.lease_until and task.lease_until > utcnow():
            raise ValueError("Task is still running; wait until its worker stops")
        digest = None
        if action == "approve":
            if task.status != "awaiting_approval" or not task.plan_json:
                raise ValueError("Task has no plan awaiting approval")
            digest = plan_hash(task.plan_json)
            if message != digest[:12]:
                raise ValueError("Approval must include the current plan hash shown by /plan")
            task.approved_plan_hash = digest
            if close_card:
                # A direct plan-hash approval (/approve, HTTP) is the same decision as
                # resolving the card, so both commit together and the inbox never keeps
                # a stale open gate for a task that already resumed. The acting
                # principal is recorded on the card either way.
                card = await session.scalar(
                    select(Decision)
                    .where(
                        Decision.task_id == task_id,
                        Decision.state == DecisionState.OPEN,
                        Decision.kind == DecisionMessageType.APPROVAL_REQUIRED,
                    )
                    .order_by(Decision.id.desc())
                    .with_for_update()
                )
                if card is not None:
                    if self._card_plan_digest(card) not in (None, digest):
                        raise ValueError(
                            "This approval card is for a different plan version; "
                            "reject it and run planning again"
                        )
                    self._close_decision_row(session, card, DecisionState.APPROVED, user_id)
        elif action == "answer":
            if task.status != "waiting_input" or not message.strip():
                raise ValueError("Task is not waiting for input or answer is empty")
            task.requirement += "\n\nUser clarification:\n" + message[:10000]
            task.plan_json = None
            task.approved_plan_hash = None
            if task.kind == "orchestration" and message.strip() in settings.projects():
                task.project = message.strip()
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
        event_message = message or action
        if action == "approve" and user_id is not None:
            # The approval event carries the acting principal, not only the plan hash.
            event_message = f"{message} by user {user_id}"
        session.add(Event(task_id=task_id, kind=action, message=redact(event_message)))
        return task

    async def resume(self, task_id, action, message="", user_id=None):
        async with self.sessions() as s, s.begin():
            await self._apply_resume(s, task_id, action, message, user_id=user_id)

    async def create_decision(self, card, owner=None):
        """Persist a decision card. It never lives only in a channel message."""
        async with self.sessions() as s, s.begin():
            decision = Decision(
                task_id=card.task_id,
                tenant=settings.tenant_id,
                owner=owner,
                category=card.category,
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
            await s.flush()
            if card.task_id:
                s.add(
                    Event(
                        task_id=card.task_id,
                        kind=card.kind,
                        message=redact(f"Decision #{decision.id} requested: {card.title}"),
                    )
                )
            await s.refresh(decision)
            return decision

    async def ensure_task_approval_decision(self, task, reason, plan_digest):
        """Create one durable approval card for a task waiting on its plan.

        Retries and worker recovery must not create duplicate inbox items for the
        same task, so the task row is locked before the open-card check. The plan
        hash is included in the evidence so the approval is bound to the exact plan
        that will be resumed. When a concurrent request wins the insert, the
        partial unique index rejects the loser; the loser then re-reads the
        winner's card instead of surfacing an integrity error.
        """
        try:
            return await self._create_approval_card(task, reason, plan_digest)
        except IntegrityError:
            return await self._open_approval_card(task.id)

    async def _open_approval_card(self, task_id):
        async with self.sessions() as s, s.begin():
            return await s.scalar(
                select(Decision)
                .where(
                    Decision.task_id == task_id,
                    Decision.state == DecisionState.OPEN,
                    Decision.kind == DecisionMessageType.APPROVAL_REQUIRED,
                )
                .order_by(Decision.id.desc())
            )

    async def _create_approval_card(self, task, reason, plan_digest):
        async with self.sessions() as s, s.begin():
            await s.scalar(select(Task.id).where(Task.id == task.id).with_for_update())
            existing = await s.scalar(
                select(Decision)
                .where(
                    Decision.task_id == task.id,
                    Decision.state == DecisionState.OPEN,
                    Decision.kind == DecisionMessageType.APPROVAL_REQUIRED,
                )
                .order_by(Decision.id.desc())
            )
            if existing:
                return existing
            decision = Decision(
                task_id=task.id,
                project=task.project,
                kind=DecisionMessageType.APPROVAL_REQUIRED,
                category="approval_required",
                tenant=task.tenant,
                owner=task.user_id,
                title=f"Approve task #{task.id}: {reason}",
                situation=redact(task.requirement),
                why_now="The task is gated before code execution because its plan requires Dedi approval.",
                options_json=json.dumps(
                    [
                        {
                            "id": "approve",
                            "label": "Approve this exact plan",
                            "impact": "The worker may continue to implementation.",
                            "risk": "The approved plan will be executed in the registered repository.",
                        },
                        {
                            "id": "reject",
                            "label": "Reject and stop the task",
                            "impact": "No code changes will be executed.",
                            "risk": "The requested work remains incomplete.",
                        },
                    ]
                ),
                impact_json=json.dumps(
                    ["The worker may continue to implementation.", "No code changes will be executed."]
                ),
                risk_json=json.dumps(
                    [
                        "The approved plan will be executed in the registered repository.",
                        "The requested work remains incomplete.",
                    ]
                ),
                recommendation="approve",
                evidence_json=json.dumps([f"plan_sha256={plan_digest}", f"task_id={task.id}"]),
                missing_information="Review the plan and exact hash shown in /plan before approving.",
                rollback="Reject the task before execution; code changes are still gated by review and tests.",
                required_action=f"approve with plan hash {plan_digest[:12]}, or reject",
                priority="high",
                risk_level="high" if reason == "Self-improvement" else "medium",
            )
            s.add(decision)
            await s.flush()
            s.add(
                Event(
                    task_id=task.id,
                    kind="decision",
                    message=redact(f"Decision #{decision.id} requested: {decision.title}"),
                )
            )
            await s.refresh(decision)
            return decision

    async def inbox(self, state=None, project=None, limit=50, owner=None):
        """List decisions newest first, filtered by state/project for the operator inbox."""
        async with self.sessions() as s:
            query = (
                select(Decision)
                .where(Decision.tenant == settings.tenant_id)
                .order_by(Decision.id.desc())
                .limit(limit)
            )
            if owner is not None:
                query = query.where(
                    or_(
                        Decision.owner == owner,
                        (
                            Decision.owner.is_(None)
                            & Decision.task_id.in_(select(Task.id).where(Task.user_id == owner))
                        ),
                        (Decision.owner.is_(None) & Decision.task_id.is_(None)),
                    )
                )
            if state:
                query = query.where(Decision.state == str(state))
            if project:
                query = query.where(Decision.project == project)
            return list(await s.scalars(query))

    async def expire_stale_decisions(self):
        """Mark past-expiry open decisions expired. Expiry is not a decision.

        An expired approval card must release the task it gated, otherwise the task
        would sit in awaiting_approval forever and hold its repository.
        """
        now = utcnow()
        async with self.sessions() as s, s.begin():
            rows = list(
                await s.scalars(
                    select(Decision)
                    .where(Decision.state == DecisionState.OPEN, Decision.expires_at < now)
                    .with_for_update()
                )
            )
            for row in rows:
                await self._close_decision(s, row, DecisionState.EXPIRED, None)
            if rows:
                await s.flush()
            return len(rows)

    async def resolve_decision(self, decision_id, phrase, user_id=None, reason="", delegate_to=None):
        """Apply one decision outcome. Idempotent and auditable.

        A repeated identical answer returns the stored decision without changing
        state again. A conflicting answer after resolution is rejected.

        The decision row, its audit event, and the task transition commit in one
        transaction: a crash can never leave an approved decision on a task that is
        still awaiting approval, and a rejected one can never strand the repository.
        """
        outcome = DECISION_PHRASES.get(phrase.strip().lower())
        if not outcome:
            raise ValueError("Answer with one of: approve, reject, ask, defer")
        target = OUTCOME_STATE[outcome]
        if outcome.value in {"request_changes", "delegate"} and not reason.strip():
            raise ValueError("Request changes and delegation require a reason")
        if outcome.value == "delegate" and delegate_to not in settings.allowed_users():
            raise ValueError("Delegation target must be an authorized operator")
        if secret_present(reason):
            raise ValueError("Remove credentials from the decision reason")
        async with self.sessions() as s:
            decision = await s.get(Decision, decision_id, with_for_update=True)
            if not decision:
                raise ValueError("Decision not found")
            if user_id is not None and decision.owner is not None and decision.owner != user_id:
                raise ValueError("Decision not found or not owned by you")
            if decision.tenant != settings.tenant_id:
                raise ValueError("Decision not found")
            if decision.task_id and user_id is not None:
                linked = await s.get(Task, decision.task_id)
                if (
                    not linked
                    or linked.tenant != settings.tenant_id
                    or (linked.user_id is not None and linked.user_id != user_id)
                ):
                    raise ValueError("Decision not found or not owned by you")
            if decision.state != DecisionState.OPEN:
                if decision.state == target:
                    return decision
                raise ValueError(
                    f"Decision #{decision_id} is already {decision.state}; start a new decision instead"
                )
            if decision.expires_at and decision.expires_at < utcnow():
                # Expiry is not a decision, but it must also release whatever it blocked.
                await self._close_decision(s, decision, DecisionState.EXPIRED, user_id)
                await s.commit()
                raise ValueError("Decision expired; request a new one")
            if target == DecisionState.DELEGATED and decision.kind == "APPROVAL_REQUIRED":
                raise ValueError("Execution approvals cannot be delegated")
            decision.decision_note = reason or None
            await self._close_decision(s, decision, target, user_id)
            if target == DecisionState.DELEGATED:
                # Hand the question to another operator, never widen execution authority.
                clone = Decision(
                    tenant=decision.tenant,
                    owner=delegate_to,
                    project=decision.project,
                    category=decision.category,
                    title=decision.title,
                    situation=decision.situation,
                    why_now=decision.why_now,
                    options_json=decision.options_json,
                    evidence_json=decision.evidence_json,
                    recommendation=decision.recommendation,
                    required_action=decision.required_action,
                    rollback=decision.rollback,
                    risk_level=decision.risk_level,
                    priority=decision.priority,
                    decision_note=f"Delegated from decision:{decision.id}: {reason}",
                )
                s.add(clone)
            await s.commit()
            await s.refresh(decision)
            return decision

    @staticmethod
    def _close_decision_row(session, decision, target, user_id):
        """Record the decision state change and its audit event, without task effects.

        Shared by resolve_decision, expiry, and direct plan-hash approvals so every
        closer records the same decided_by/decided_at fields and the same event.
        """
        session.add(
            AuditLog(
                tenant=decision.tenant,
                actor=user_id,
                task_id=decision.task_id,
                scope=decision.project,
                action="decision_action",
                correlation_id=f"decision-{decision.id}",
                detail=redact(
                    json.dumps(
                        {"decision_id": decision.id, "outcome": str(target), "reason": decision.decision_note}
                    )
                ),
            )
        )
        decision.state = target
        decision.decided_by = user_id
        decision.decided_at = utcnow()
        if not decision.task_id:
            return
        session.add(
            Event(
                task_id=decision.task_id,
                kind="decision",
                message=redact(f"Decision #{decision.id} -> {target} by {user_id}"),
            )
        )

    async def _close_decision(self, session, decision, target, user_id):
        """Record one decision outcome and apply its exact effect on the linked task.

        Only an APPROVAL_REQUIRED card may move a task forward, and only while the task
        is still parked on the plan digest the card carries. Every other outcome stops a
        gated task so the repository reservation is released instead of held forever.
        """
        self._close_decision_row(session, decision, target, user_id)
        task_id = decision.task_id
        if not task_id:
            return
        if decision.kind != DecisionMessageType.APPROVAL_REQUIRED:
            # A generic DECISION_REQUIRED card is a question about the work, never an
            # authorisation to execute it.
            return
        task = await session.get(Task, task_id, with_for_update=True)
        if not task or task.status != "awaiting_approval":
            return
        if target != DecisionState.APPROVED:
            task.status = "cancelled" if target == DecisionState.REJECTED else "failed"
            task.last_message = redact(
                f"Decision #{decision.id} {target}; stop and inspect /logs before retrying"
            )
            task.cancel_requested = False
            task.lease_owner = None
            task.lease_until = None
            return
        if not task.plan_json:
            raise ValueError("Task has no plan awaiting approval")
        digest = plan_hash(task.plan_json)
        if self._card_plan_digest(decision) not in (None, digest):
            raise ValueError(
                "This approval card is for a different plan version; reject it and run planning again"
            )
        # close_card=False: this card is the one being resolved; re-running the
        # card close here would duplicate the decision event.
        await self._apply_resume(session, task_id, "approve", digest[:12], user_id=user_id, close_card=False)

    @staticmethod
    def _card_plan_digest(decision):
        """The plan sha256 an APPROVAL_REQUIRED card was created against, if recorded."""
        try:
            evidence = json.loads(decision.evidence_json or "[]")
        except json.JSONDecodeError:
            return None
        for item in evidence:
            if isinstance(item, str) and item.startswith("plan_sha256="):
                return item.split("=", 1)[1]
        return None

    # --- Context and Memory Plane ---

    async def record_outcome(
        self,
        task_id,
        outcome: ImprovementOutcome,
        tenant=DEFAULT_TENANT,
        rollback_confirmation=None,
        actor=None,
    ):
        """Measure a completed self-improvement and retain the lesson (requirement v0.3 §9).

        The improvement cycle ends with Observe outcome → Rollback atau retain, not at the
        merged PR. One measurement is exactly one outcome artifact, one timeline event, and
        one shared lesson in memory keyed to the task. The task row is locked and the
        artifact, event, and lesson commit in ONE transaction: two concurrent requests
        serialise on the task lock, and the event can never claim the lesson was stored
        when the memory write failed. A different measurement for the same change is
        refused — the numbers of one change are history, and a revised measurement
        belongs to a fresh self-improvement task.
        """
        key = f"improvement.task-{task_id}"
        value = (
            f"{outcome.conclusion} | before: {outcome.before[:1500]} | after: {outcome.after[:1500]}"
            f" | evidence: {'; '.join(outcome.evidence)[:600]}"
            + (f" | window: {outcome.window[:200]}" if outcome.window else "")
            + (f" | notes: {outcome.notes[:600]}" if outcome.notes else "")
        )
        if secret_present(value):
            raise ValueError("Remove credentials from the measurement; store them outside memory")
        payload = redact(outcome.model_dump_json())
        lesson = MemoryWrite(
            key=key,
            value=value,
            scope="lesson",
            source="self-improvement outcome",
            evidence_ref=f"task:{task_id}",
        )
        async with self.sessions() as s, s.begin():
            # Lock the task row so two concurrent measurements of the same task
            # serialise here: the loser sees the committed artifact instead of
            # racing to create a duplicate outcome/event pair.
            task = await s.get(Task, task_id, with_for_update=True)
            if not task:
                raise ValueError("Task not found")
            if task.tenant != tenant or (
                actor is not None and task.user_id is not None and task.user_id != actor
            ):
                raise ValueError("Task not found")
            if task.kind != "self_improvement":
                raise ValueError("Outcomes are recorded on self_improvement tasks only")
            if task.status not in {"completed", "deployed"}:
                raise ValueError(f"Task #{task_id} is {task.status}; measure the outcome after completion")
            existing = await s.scalar(
                select(Artifact).where(Artifact.task_id == task_id, Artifact.kind == "improvement_outcome")
            )
            if existing is not None and existing.content != payload:
                raise ValueError(
                    "An outcome is already recorded for this task; a revised measurement "
                    "belongs in a new self-improvement task"
                )
            if outcome.conclusion == "rollback" and rollback_confirmation is None:
                raise ValueError("Rollback confirmation required before recording final outcome")
            if outcome.conclusion == "rollback" and existing is None:
                s.add(
                    Artifact(
                        task_id=task_id,
                        kind="rollback_confirmation",
                        content=redact(json.dumps(rollback_confirmation)),
                    )
                )
                s.add(
                    AuditLog(
                        tenant=tenant,
                        actor=actor,
                        task_id=task_id,
                        scope=task.project,
                        action="rollback_confirmed",
                        correlation_id=f"intent-{task_id}",
                        detail=redact(json.dumps(rollback_confirmation)),
                    )
                )
            if existing is not None:
                # A repeated identical measurement is the same one action: return the
                # retained lesson instead of superseding it with a v2 of itself. The
                # task lock has already serialised any competing write.
                retained = await s.scalar(
                    select(MemoryItem).where(
                        MemoryItem.key == key,
                        MemoryItem.tenant == tenant,
                        MemoryItem.scope == "lesson",
                        MemoryItem.state == MemoryState.ACTIVE,
                    )
                )
                if retained is not None and retained.value == value:
                    return retained
            try:
                row = await self._remember(s, lesson, owner=task.user_id, task_id=task_id, tenant=tenant)
            except IntegrityError as exc:
                raise ValueError(f"Memory '{key}' was stored concurrently; retry") from exc
            if existing is None:
                s.add(Artifact(task_id=task_id, kind="improvement_outcome", content=payload))
                s.add(
                    Event(
                        task_id=task_id,
                        kind="outcome",
                        message=redact(
                            f"Outcome recorded: {outcome.conclusion}; lesson stored as memory '{key}'"
                        ),
                    )
                )
            proposals = await s.scalars(
                select(ImprovementProposal).where(
                    ImprovementProposal.task_id == task_id, ImprovementProposal.tenant == tenant
                )
            )
            for proposal in proposals:
                proposal.status = "retained" if outcome.conclusion == "retain" else "rolled_back"
            s.add(
                AuditLog(
                    tenant=tenant,
                    actor=actor,
                    task_id=task_id,
                    scope=task.project,
                    action="improvement_outcome",
                    correlation_id=f"intent-{task_id}",
                    detail=payload,
                )
            )
            # Falling through with an existing artifact means the lesson was left
            # missing by an older partial write; _remember repairs it in this same
            # transaction without adding a duplicate artifact or event.
            return row

    async def remember(self, item: MemoryWrite, owner=None, task_id=None, tenant=DEFAULT_TENANT):
        """Store one memory, superseding any earlier active version of the same key.

        A correction never overwrites history: the old row is kept and marked superseded,
        so what was previously believed stays auditable. The unique active key is scoped to
        (tenant, key, scope), so one tenant never supersedes another tenant's memory.
        """
        async with self.sessions() as s:
            try:
                row = await self._remember(s, item, owner=owner, task_id=task_id, tenant=tenant)
                await s.commit()
            except IntegrityError as exc:
                await s.rollback()
                raise ValueError(f"Memory '{item.key}' was stored concurrently; retry") from exc
            await s.refresh(row)
            return row

    async def _remember(self, session, item: MemoryWrite, owner=None, task_id=None, tenant=DEFAULT_TENANT):
        """Store one memory inside a caller-owned transaction.

        remember() wraps this with its own transaction; record_outcome() reuses it so
        the lesson and the outcome artifact commit together or not at all.
        """
        if item.locked and owner is None:
            raise ValueError("Locked memories require an owner")
        if secret_present(item.value) or secret_present(item.source) or secret_present(item.evidence_ref):
            raise ValueError("Remove credentials before storing this as memory")
        previous = list(
            await session.scalars(
                select(MemoryItem)
                .where(
                    MemoryItem.key == item.key,
                    MemoryItem.tenant == tenant,
                    MemoryItem.scope == item.scope,
                    MemoryItem.state == MemoryState.ACTIVE,
                )
                .with_for_update()
            )
        )
        # A contradictory value for the same key is a conflict, not a silent overwrite.
        if any(p.value != item.value for p in previous):
            raise ValueError(
                f"Memory '{item.key}' Memory conflict: already holds a different value; correct it explicitly"
            )
        if any(p.owner is not None and p.owner != owner for p in previous):
            raise ValueError("Memory not owned by you")
        if any(p.locked for p in previous):
            raise ValueError("Memory is locked; use explicit correction")
        if previous:
            for row in previous:
                row.state = MemoryState.SUPERSEDED
            version = max(p.version for p in previous) + 1
            supersedes = previous[0].id
            # Emit the SUPERSEDED update before inserting the replacement so the
            # partial unique index never sees two active rows for one key.
            await session.flush()
        else:
            version, supersedes = 1, None
        row = MemoryItem(
            key=item.key,
            value=item.value,
            tenant=tenant,
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
        session.add(row)
        session.add(
            AuditLog(
                tenant=tenant,
                actor=owner,
                task_id=task_id,
                scope=item.scope,
                action="memory_written",
                correlation_id=f"memory:{item.key}:v{version}",
                detail=redact(item.source),
            )
        )
        if task_id:
            session.add(
                Event(
                    task_id=task_id,
                    kind="memory",
                    message=redact(f"Memory '{item.key}' v{version} stored ({item.source})"),
                )
            )
        await session.flush()
        return row

    async def recall(self, keys=None, role=None, scope=None, limit=25, owner=None, tenant=DEFAULT_TENANT):
        """Return a permission-aware slice. Never returns a whole-table dump.

        `role` is an agent role resolved through the audited clearance config. An
        unknown role gets nothing, so a typo cannot widen access.

        `tenant` is the hard isolation boundary and is always applied. `owner` narrows
        further: a memory owned by a person is visible only to that owner, while an
        unowned memory is shared inside the tenant. A read for one owner can never
        return another owner's memory, whatever the role clearance allows.
        """
        clearance = CLEARANCE.get(settings.role_clearance().get(role, "none"), -1)
        async with self.sessions() as s:
            query = select(MemoryItem).where(
                MemoryItem.state == MemoryState.ACTIVE,
                MemoryItem.tenant == tenant,
            )
            if keys:
                query = query.where(MemoryItem.key.in_(keys))
            if scope is not None:
                query = query.where(or_(MemoryItem.scope == scope, MemoryItem.scope == ""))
            if owner is not None:
                query = query.where(or_(MemoryItem.owner == owner, MemoryItem.owner.is_(None)))
            else:
                # Without a principal, only unowned shared memory is readable.
                query = query.where(MemoryItem.owner.is_(None))
            rows = list(await s.scalars(query.order_by(MemoryItem.id.desc())))
        allowed = [r for r in rows if CLEARANCE.get(r.sensitivity, 0) <= clearance]
        now = utcnow()
        views = [self._view(r, now) for r in allowed[:limit]]
        async with self.sessions() as session:
            conflicts = set(
                await session.scalars(
                    select(MemoryConflict.memory_id).where(
                        MemoryConflict.tenant == tenant,
                        MemoryConflict.state == "open",
                        MemoryConflict.memory_id.in_([v.id for v in views]),
                        or_(MemoryConflict.owner == owner, MemoryConflict.owner.is_(None)),
                    )
                )
            )
        for view in views:
            if view.id in conflicts:
                view.label = "conflict"
                view.confidence = min(view.confidence, 0.49)
        return views, len(allowed) > limit

    @staticmethod
    def _view(row, now):
        stale = bool(row.expires_at and row.expires_at < now)
        label = "stale" if stale else ("unverified" if row.confidence < 0.5 else "current")
        return MemoryView(
            id=row.id,
            owner=row.owner,
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

    async def correct_memory(self, memory_id, value, source, owner=None, task_id=None, tenant=DEFAULT_TENANT):
        """Replace a memory's value with a new version, keeping the previous one auditable."""
        if secret_present(value) or secret_present(source):
            raise ValueError("Remove credentials before storing this as memory")
        async with self.sessions() as s:
            row = await s.get(MemoryItem, memory_id, with_for_update=True)
            if not row or row.tenant != tenant:
                raise ValueError("Memory not found")
            if row.locked:
                if owner != row.owner:
                    raise ValueError("Memory is locked by Dedi and cannot be corrected automatically")
            elif row.owner is not None and owner != row.owner:
                raise ValueError("Only the memory owner can correct this memory")
            if row.state != MemoryState.ACTIVE:
                raise ValueError(f"Memory is {row.state}; correct the active version instead")
            replacement = MemoryItem(
                key=row.key,
                value=value,
                tenant=row.tenant,
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
            conflicts = await s.scalars(
                select(MemoryConflict).where(
                    MemoryConflict.memory_id == memory_id,
                    MemoryConflict.tenant == tenant,
                    MemoryConflict.owner == owner,
                    MemoryConflict.state == "open",
                )
            )
            for conflict in conflicts:
                conflict.state = "resolved"
            s.add(
                AuditLog(
                    tenant=tenant,
                    actor=owner,
                    task_id=task_id,
                    scope=row.scope,
                    action="memory_corrected",
                    correlation_id=f"memory:{memory_id}",
                    detail=redact(source),
                )
            )
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

    async def retract_memory(self, memory_id, reason, owner=None, task_id=None, tenant=DEFAULT_TENANT):
        """Close a memory without deleting it. The row and its reason remain auditable."""
        async with self.sessions() as s:
            row = await s.get(MemoryItem, memory_id, with_for_update=True)
            if not row or row.tenant != tenant:
                raise ValueError("Memory not found")
            if row.locked and owner != row.owner:
                raise ValueError("Locked memories can only be retracted by their owner")
            if row.owner is not None and owner != row.owner:
                raise ValueError("Only the memory owner can retract this memory")
            row.state = MemoryState.RETRACTED
            s.add(
                AuditLog(
                    tenant=tenant,
                    actor=owner,
                    task_id=task_id,
                    scope=row.scope,
                    action="memory_retracted",
                    correlation_id=f"memory:{memory_id}",
                    detail=redact(reason),
                )
            )
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

    async def lock_memory(self, memory_id, reason, owner=None, task_id=None, tenant=DEFAULT_TENANT):
        """Freeze one memory so later corrections and retractions need its owner.

        Dedi's lock action is the human review gate on shared knowledge: a locked memory
        can no longer be edited by agents or other principals, only by the person who
        holds it. Because the isolation model makes every owned row private, locking an
        unowned shared row adopts the requesting principal as its owner — the same
        adoption a correction already performs — so the freeze always stays reachable by
        exactly one accountable person.
        """
        async with self.sessions() as s:
            row = await s.get(MemoryItem, memory_id, with_for_update=True)
            if not row or row.tenant != tenant:
                raise ValueError("Memory not found")
            if row.state != MemoryState.ACTIVE:
                raise ValueError(f"Memory is {row.state}; lock the active version instead")
            if row.owner is not None and owner != row.owner:
                raise ValueError("Only the memory owner can lock this memory")
            if row.owner is None and owner is None:
                raise ValueError("Locking requires an owner; no principal was supplied")
            if row.locked:
                return row  # A repeated lock request is one action, not an error.
            row.locked = True
            s.add(
                AuditLog(
                    tenant=tenant,
                    actor=owner,
                    task_id=task_id,
                    scope=row.scope,
                    action="memory_locked",
                    correlation_id=f"memory:{memory_id}",
                    detail=redact(reason),
                )
            )
            if row.owner is None:
                row.owner = owner
            if task_id:
                s.add(
                    Event(
                        task_id=task_id,
                        kind="memory",
                        message=redact(f"Memory '{row.key}' locked: {reason}"),
                    )
                )
            await s.commit()
            await s.refresh(row)
            return row

    async def memory_history(self, key, owner=None, tenant=DEFAULT_TENANT):
        """Every version of a key, newest first, so a correction can be inspected.

        Scoped exactly like recall: another tenant's key is invisible, and within the
        tenant only the owner and shared memory are returned.
        """
        async with self.sessions() as s:
            query = select(MemoryItem).where(
                MemoryItem.key == key,
                MemoryItem.tenant == tenant,
            )
            if owner is not None:
                query = query.where(or_(MemoryItem.owner == owner, MemoryItem.owner.is_(None)))
            else:
                query = query.where(MemoryItem.owner.is_(None))
            rows = list(await s.scalars(query.order_by(MemoryItem.version.desc())))
            now = utcnow()
            return [self._view(r, now) for r in rows]


store = Store()
