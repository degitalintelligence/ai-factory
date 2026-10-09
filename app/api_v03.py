"""Operator APIs for chat, shared console, knowledge and explicit recovery."""

import json
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import or_, select

from app.chat import converse
from app.config import settings
from app.conversations import _effective_task, history
from app.db import (
    Artifact,
    AuditLog,
    ChatTurn,
    Conversation,
    Decision,
    ImprovementProposal,
    MemoryConflict,
    MemoryItem,
    ModelRun,
    Task,
)
from app.deployment import DeploymentService
from app.github_api import GitHubAPI
from app.metrics import task_outcomes
from app.schemas import ClarificationRequest, ImprovementOutcome, MemoryWrite, SelfImprovementBrief
from app.staff import audit, detect_improvements
from app.staff_schemas import ChatRequest, DeploymentReconciliation, RollbackConfirmation
from app.store import store
from app.version import VERSION


class MemoryCorrection(BaseModel):
    value: str = Field(min_length=1, max_length=8000)
    source: str = Field(min_length=1, max_length=300)


class ReasonRequest(BaseModel):
    reason: str = Field(min_length=5, max_length=2000)


def build_router(authorize, task_view, decision_view) -> APIRouter:
    router = APIRouter()

    async def owned(task_id, actor):
        task = await store.get(task_id)
        if not task or task.tenant != settings.tenant_id or task.user_id != actor:
            raise HTTPException(404, "Task not found")
        return task

    @router.get("/dashboard")
    async def dashboard():
        return FileResponse(
            Path(__file__).parent / "console" / "index.html",
            headers={
                "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'",
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @router.get("/dashboard/app.js")
    async def script():
        return FileResponse(Path(__file__).parent / "console" / "app.js", media_type="text/javascript")

    @router.get("/dashboard/style.css")
    async def style():
        return FileResponse(Path(__file__).parent / "console" / "style.css", media_type="text/css")

    @router.post("/v1/chat", status_code=202)
    async def chat(request: ChatRequest, actor=Depends(authorize)):
        try:
            return await converse(request, actor)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.get("/v1/release-manifest")
    async def release_manifest(actor=Depends(authorize)):
        return {
            "version": VERSION,
            "release_sha": settings.release_sha or None,
            "sha_authority": "Operator-configured attestation; not independent image verification.",
            "tenant": settings.tenant_id,
            "models": {
                role: {"alias": settings.configured_model(role), "resolved": settings.model_for(role)}
                for role in ("lead", "developer", "reviewer")
            },
            "projects": {alias: policy.model_dump() for alias, policy in settings.projects().items()},
            "self_project": settings.self_project,
            "self_improvement_enabled": settings.self_improvement_enabled,
            "operator_budget": {
                "calls": settings.max_llm_calls,
                "tokens": settings.max_total_tokens,
                "cost_usd": settings.max_cost_usd,
            },
            "daily_budget": {
                "calls": settings.global_max_llm_calls_per_day,
                "reserved_tokens": settings.global_max_tokens_per_day,
                "cost_usd": settings.global_max_cost_usd_per_day,
            },
            "release_status": "implementation_candidate",
            "live_gates": [
                "same-SHA CI",
                "10 bounded live objectives with >=8 operator-accepted outcomes",
                "live channel decisions/memory parity",
                "restart persistence",
                "measured self-improvement and staging rollback",
                "exact-SHA release approval",
            ],
        }

    @router.get("/v1/overview")
    async def overview(project: str = "", actor=Depends(authorize)):
        async with store.sessions() as s:
            query = select(Task).where(Task.tenant == settings.tenant_id, Task.user_id == actor)
            if project:
                query = query.where(Task.project == project)
            tasks = list(await s.scalars(query.order_by(Task.id.desc()).limit(100)))
            dq = select(Decision).where(
                Decision.tenant == settings.tenant_id,
                or_(
                    Decision.owner == actor,
                    (Decision.owner.is_(None) & Decision.task_id.in_([t.id for t in tasks])),
                ),
            )
            if project:
                dq = dq.where(Decision.project == project)
            decisions = list(await s.scalars(dq.order_by(Decision.id.desc()).limit(100)))
            receipts = await s.execute(
                select(ChatTurn.task_id, ChatTurn.conversation_id, Conversation.project)
                .join(Conversation, ChatTurn.conversation_id == Conversation.id)
                .where(
                    Conversation.tenant == settings.tenant_id,
                    Conversation.owner == actor,
                    ChatTurn.task_id.in_([task.id for task in tasks]),
                )
                .order_by(ChatTurn.id)
            )
            threads = {task_id: (thread_id, project) for task_id, thread_id, project in receipts}
            views = []
            for task in tasks:
                view = task_view(task)
                view["conversation_id"], view["conversation_project"] = threads.get(task.id, (None, None))
                try:
                    effective = await _effective_task(s, task, actor)
                except ValueError:
                    view.update(
                        effective_status="handoff_unverified",
                        summary="Engineering handoff unavailable in this scope",
                        next_action="Verifikasi referensi handoff.",
                    )
                    views.append(view)
                    continue
                if effective.id != task.id:
                    view.update(
                        handoff_task_id=effective.id,
                        effective_status=effective.status,
                        summary=effective.last_message,
                        next_action=f"Pantau task engineering #{effective.id}.",
                    )
                views.append(view)
        return {
            "tasks": views,
            "decisions": [decision_view(d) for d in decisions],
            "projects": [{"id": k, "repo": v.repo} for k, v in settings.projects().items()],
            "summary": f"{sum(d.state == 'open' for d in decisions)} keputusan terbuka; {sum(t.status == 'failed' for t in tasks)} pekerjaan perlu ditinjau.",
            "next_action": "Prioritaskan kartu terbuka dan pekerjaan yang blocked.",
            "decision_required": any(d.state == "open" for d in decisions),
            "status": "current",
            "evidence_refs": [f"task:{t.id}" for t in tasks[:10]],
            "risk": "unknown",
        }

    @router.get("/v1/conversations/{conversation_id}")
    async def conversation_history(conversation_id: str, actor=Depends(authorize)):
        try:
            return await history(conversation_id, actor)
        except ValueError as exc:
            raise HTTPException(404, "Conversation not found") from exc

    @router.post("/v1/decisions/{decision_id}/clarification")
    async def clarification_answer(decision_id: int, request: ClarificationRequest, actor=Depends(authorize)):
        try:
            task = await store.answer_clarification(decision_id, request.answer, actor)
            return task_view(task)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.get("/v1/intents/{task_id}/result")
    async def result(task_id: int, actor=Depends(authorize)):
        task = await owned(task_id, actor)
        artifacts = {a.kind: a.content for a in await store.artifacts(task.id)}
        return {
            "task": task_view(task),
            "result": json.loads(artifacts.get("staff_result", "null")),
            "handoff": json.loads(artifacts.get("engineering_handoff", "null")),
        }

    @router.get("/v1/metrics")
    async def metrics(actor=Depends(authorize)):
        async with store.sessions() as s:
            tasks = list(
                await s.scalars(select(Task).where(Task.tenant == settings.tenant_id, Task.user_id == actor))
            )
            ids = [t.id for t in tasks]
            handoff_ids = set(
                await s.scalars(
                    select(Artifact.task_id).where(
                        Artifact.task_id.in_(ids),
                        Artifact.kind == "engineering_handoff",
                    )
                )
            )
            runs = list(await s.scalars(select(ModelRun).where(ModelRun.task_id.in_(ids))))
            artifacts = list(
                await s.scalars(
                    select(Artifact).where(
                        Artifact.task_id.in_(ids),
                        Artifact.kind.in_({"engineering_handoff", "improvement_outcome"}),
                    )
                )
            )
            audits = list(
                await s.scalars(
                    select(AuditLog)
                    .where(AuditLog.tenant == settings.tenant_id, AuditLog.actor == actor)
                    .order_by(AuditLog.id.desc())
                )
            )
            decisions = list(
                await s.scalars(
                    select(Decision).where(Decision.tenant == settings.tenant_id, Decision.owner == actor)
                )
            )
            decision_seconds = [
                (d.decided_at - d.created_at).total_seconds() for d in decisions if d.decided_at
            ]
        terminal_statuses = {
            "completed",
            "reviewed",
            "deployed",
            "deployment_failed",
            "failed",
            "cancelled",
            "superseded",
        }
        successful_final_statuses = {"completed", "reviewed", "deployed"}
        handoff_tasks = [t for t in tasks if t.id in handoff_ids]
        final_tasks = [t for t in tasks if t.status in terminal_statuses and t.id not in handoff_ids]
        return {
            "tasks": len(tasks),
            **task_outcomes(tasks, artifacts),
            "active_tasks": sum(t.status not in terminal_statuses for t in tasks),
            "handoff_tasks": len(handoff_tasks),
            "final_tasks": len(final_tasks),
            "successful_final_tasks": sum(t.status in successful_final_statuses for t in final_tasks),
            "success_rate": sum(t.status in successful_final_statuses for t in final_tasks)
            / max(1, len(final_tasks)),
            **task_outcomes(tasks, artifacts),
            "calls": len(runs),
            "tokens": sum(r.tokens for r in runs),
            "reported_cost_usd": sum(r.cost_usd or 0 for r in runs),
            "cost_incomplete": any(not r.cost_reported for r in runs),
            "mean_model_latency_ms": sum(r.latency_ms for r in runs) / max(1, len(runs)),
            "model_error_rate": sum(r.outcome != "ok" for r in runs) / max(1, len(runs)),
            "retry_attempts": sum(r.attempt > 1 for r in runs),
            "rollback_confirmations": sum(a.action == "rollback_confirmed" for a in audits),
            "mean_time_to_decision_seconds": sum(decision_seconds) / max(1, len(decision_seconds)),
            "human_overrides": sum(d.state in {"rejected", "changes_requested"} for d in decisions),
            "review_rejections": sum("evaluation failed" in (a.detail or "").lower() for a in audits),
            "audit": [
                {
                    "id": a.id,
                    "action": a.action,
                    "actor": a.actor,
                    "correlation_id": a.correlation_id,
                    "scope": a.scope,
                    "detail": a.detail,
                    "created_at": a.created_at,
                }
                for a in audits[:100]
            ],
        }

    @router.post("/v1/memory", status_code=201)
    async def write_memory(request: MemoryWrite, actor=Depends(authorize)):
        try:
            row = await store.remember(request, owner=actor, tenant=settings.tenant_id)
            return {"id": row.id, "key": row.key, "version": row.version, "state": row.state}
        except ValueError as exc:
            # Retain the contradictory proposal as a visible conflict, never replace the active value.
            if "conflict" in str(exc).lower():
                async with store.sessions() as s, s.begin():
                    previous = await s.scalar(
                        select(MemoryItem).where(
                            MemoryItem.tenant == settings.tenant_id,
                            MemoryItem.key == request.key,
                            MemoryItem.scope == request.scope,
                            MemoryItem.state == "active",
                            or_(MemoryItem.owner == actor, MemoryItem.owner.is_(None)),
                        )
                    )
                    if previous:
                        conflict = MemoryConflict(
                            tenant=settings.tenant_id,
                            owner=actor,
                            memory_id=previous.id,
                            proposed_value=request.value,
                            source=request.source,
                        )
                        s.add(conflict)
                        await s.flush()
                        audit(
                            s,
                            "memory_conflict",
                            actor,
                            tenant=settings.tenant_id,
                            detail=f"conflict:{conflict.id}",
                        )
                raise HTTPException(409, "Conflicting memory retained for operator review") from exc
            raise HTTPException(409, str(exc)) from exc

    @router.get("/v1/memory/conflicts")
    async def conflicts(actor=Depends(authorize)):
        async with store.sessions() as s:
            rows = await s.scalars(
                select(MemoryConflict).where(
                    MemoryConflict.tenant == settings.tenant_id,
                    MemoryConflict.owner == actor,
                    MemoryConflict.state == "open",
                )
            )
            return [
                {"id": r.id, "memory_id": r.memory_id, "proposed_value": r.proposed_value, "source": r.source}
                for r in rows
            ]

    @router.post("/v1/memory/{memory_id}/correction")
    async def correct(memory_id: int, request: MemoryCorrection, actor=Depends(authorize)):
        try:
            row = await store.correct_memory(
                memory_id, request.value, request.source, owner=actor, tenant=settings.tenant_id
            )
            return {"id": row.id, "version": row.version, "state": row.state}
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.post("/v1/memory/{memory_id}/retract")
    async def retract(memory_id: int, request: ReasonRequest, actor=Depends(authorize)):
        try:
            row = await store.retract_memory(
                memory_id, request.reason, owner=actor, tenant=settings.tenant_id
            )
            return {"id": row.id, "state": row.state}
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.post("/v1/deployments/{task_id}/reconcile")
    async def reconcile(task_id: int, request: DeploymentReconciliation, actor=Depends(authorize)):
        await owned(task_id, actor)
        try:
            row = await DeploymentService().reconcile(task_id, request, actor)
            return {
                "status": row.status,
                "commit_sha": row.commit_sha,
                "deployment_uuid": row.deployment_uuid,
                "message": row.message,
            }
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.get("/v1/improvement-proposals")
    async def proposals(actor=Depends(authorize)):
        async with store.sessions() as session:
            rows = list(
                await session.scalars(
                    select(ImprovementProposal)
                    .where(
                        ImprovementProposal.tenant == settings.tenant_id, ImprovementProposal.owner == actor
                    )
                    .order_by(ImprovementProposal.id.desc())
                    .limit(100)
                )
            )
        return [
            {"id": row.id, "status": row.status, "brief": json.loads(row.brief_json), "task_id": row.task_id}
            for row in rows
        ]

    @router.post("/v1/improvement-proposals/detect")
    async def detect(actor=Depends(authorize)):
        rows = await detect_improvements(actor)
        return [
            {"id": r.id, "status": r.status, "brief": json.loads(r.brief_json), "task_id": r.task_id}
            for r in rows
        ]

    @router.post("/v1/improvement-proposals/{proposal_id}/start")
    async def start(proposal_id: int, actor=Depends(authorize)):
        if not settings.self_improvement_enabled:
            raise HTTPException(409, "Self-improvement kill switch is active")
        async with store.sessions() as s:
            row = await s.get(ImprovementProposal, proposal_id)
            if not row or row.owner != actor or row.tenant != settings.tenant_id:
                raise HTTPException(404, "Proposal not found")
            if row.task_id:
                return {"task_id": row.task_id, "status": row.status}
        try:
            task = await store.create(
                f"Self-improvement: {json.loads(row.brief_json)['problem']}",
                project=settings.self_project,
                user_id=actor,
                kind="self_improvement",
                brief=SelfImprovementBrief.model_validate_json(row.brief_json),
                idempotency_key=f"proposal:{row.tenant}:{row.id}",
            )
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        async with store.sessions() as s, s.begin():
            current = await s.get(ImprovementProposal, proposal_id, with_for_update=True)
            current.task_id = task.id
            current.status = "executing"
            audit(s, "improvement_started", actor, task, detail=f"proposal:{proposal_id}")
        return {"task_id": task.id, "status": "executing", "approval_required": True}

    @router.post("/v1/improvements/{task_id}/rollback/confirm")
    async def confirm_rollback(task_id: int, request: RollbackConfirmation, actor=Depends(authorize)):
        task = await owned(task_id, actor)
        if not request.compatibility_checked:
            raise HTTPException(409, "Assess migration/data compatibility before confirming rollback")
        artifacts = {a.kind: a.content for a in await store.artifacts(task.id)}
        if "rollback_requested" not in artifacts:
            raise HTTPException(409, "No rollback requested")
        async with store.sessions() as session:
            approval = await session.scalar(
                select(Decision).where(
                    Decision.task_id == task_id,
                    Decision.title == "Approve production rollback",
                    Decision.state == "approved",
                    Decision.decided_by == actor,
                )
            )
        if not approval:
            raise HTTPException(409, "Explicit rollback decision approval is required")
        # Verify the immutable revision exists and is the current registered branch tip.
        if await GitHubAPI().branch_sha(task.repo, task.base_branch) != request.revision:
            raise HTTPException(409, "Rollback revision is not the current registered branch tip")
        if task.status == "deployed":
            if not request.deployment_uuid:
                raise HTTPException(
                    409, "A finished rollback deployment UUID is required for a deployed task"
                )
            service = DeploymentService()
            policy = settings.projects().get(task.project)
            if not policy or policy.repo != task.repo or not policy.coolify_uuid:
                raise HTTPException(409, "Registered deployment policy is required")
            remote = await service.coolify("GET", f"deployments/{request.deployment_uuid}")
            application = await service.coolify("GET", f"applications/{policy.coolify_uuid}")
            identity = remote.get("application_uuid") == policy.coolify_uuid or (
                application.get("id") is not None
                and str(remote.get("application_id")) == str(application["id"])
            )
            if remote.get("status") != "finished" or remote.get("commit") != request.revision or not identity:
                raise HTTPException(
                    409, "Rollback deployment must prove the application, finished status and exact revision"
                )
        outcome = ImprovementOutcome.model_validate_json(artifacts["rollback_requested"])
        try:
            lesson = await store.record_outcome(
                task.id,
                outcome,
                tenant=task.tenant,
                rollback_confirmation=request.model_dump()
                | {
                    "actor": actor,
                    "verification": "branch tip, finished deployment when required, and operator smoke/compatibility attestation",
                },
                actor=actor,
            )
            return {
                "status": "rollback_confirmed",
                "lesson_memory_id": lesson.id,
                "revision": request.revision,
            }
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    return router


async def request_rollback(task_id: int, outcome: ImprovementOutcome, actor: int):
    """The measurement requests rollback; no final lesson claims it happened yet."""
    async with store.sessions() as s, s.begin():
        task = await s.get(Task, task_id, with_for_update=True)
        if not task or task.tenant != settings.tenant_id or task.user_id != actor:
            raise ValueError("Task not found")
        if task.kind != "self_improvement" or task.status not in {"completed", "deployed"}:
            raise ValueError("Rollback measurement requires a released self-improvement")
        previous = await s.scalar(
            select(Artifact).where(Artifact.task_id == task_id, Artifact.kind == "improvement_outcome")
        )
        if previous:
            raise ValueError("An outcome is already recorded for this task")
        pending = await s.scalar(
            select(Artifact).where(Artifact.task_id == task_id, Artifact.kind == "rollback_requested")
        )
        if pending and pending.content != outcome.model_dump_json():
            raise ValueError("A different rollback measurement is already requested")
        if not pending:
            from app.security import secret_present

            if secret_present(outcome.model_dump_json()):
                raise ValueError("Remove credentials from the measurement")
            s.add(Artifact(task_id=task.id, kind="rollback_requested", content=outcome.model_dump_json()))
            s.add(
                Decision(
                    task_id=task.id,
                    tenant=task.tenant,
                    owner=actor,
                    category="approval_required",
                    title="Approve production rollback",
                    situation=outcome.notes or outcome.after,
                    why_now="Measured regression requires explicit human rollback approval.",
                    options_json=json.dumps(
                        [
                            {"id": "rollback", "label": "Approve compatible rollback"},
                            {"id": "stop", "label": "Reject rollback"},
                        ]
                    ),
                    evidence_json=json.dumps(outcome.evidence),
                    recommendation="rollback",
                    risk_level="high",
                    required_action="Approve, perform rollback externally, verify compatibility and smoke tests, then confirm.",
                    rollback="Preserve all data and evidence; assess migrations before reverting.",
                )
            )
            audit(s, "rollback_requested", actor, task, detail=outcome.model_dump_json())
    return {
        "task_id": task_id,
        "status": "rollback_pending",
        "conclusion": "rollback",
        "decision_required": True,
        "next_action": "Perform an explicitly approved compatible rollback, run smoke tests, then submit revision and evidence to /rollback/confirm.",
        "evidence_refs": [f"task:{task_id}"],
        "risk": "high",
    }
