import hmac
import json
import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.config import settings
from app.contracts import (
    create_self_improvement,
    decision_inbox,
    perform_action,
    raise_decision,
    resolve_decision,
)
from app.db import engine, init_db, utcnow
from app.schemas import (
    ClarificationRequest,
    DecisionRequest,
    ImprovementRequest,
    IntentRequest,
    PlanApprovalRequest,
    TaskRequest,
)
from app.store import store
from app.telegram_control import build_telegram_app
from app.worker import WorkerPool

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
# HTTP client URLs can include Telegram bot tokens.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
telegram_app = None
worker = None


@asynccontextmanager
async def lifespan(app):
    global telegram_app, worker
    settings.validate_runtime()
    await init_db()
    try:
        if settings.telegram_bot_token:
            telegram_app = build_telegram_app()
            await telegram_app.initialize()
            await telegram_app.updater.start_polling(drop_pending_updates=False)
            await telegram_app.start()

        async def notify(chat_id, message):
            if telegram_app:
                for n in range(0, len(message), 3500):
                    await telegram_app.bot.send_message(chat_id=chat_id, text=message[n : n + 3500])

        if settings.worker_enabled:
            worker = WorkerPool(notify)
            worker.start()
        yield
    finally:
        if worker:
            await worker.stop()
        if telegram_app:
            if telegram_app.updater.running:
                await telegram_app.updater.stop()
            if telegram_app.running:
                await telegram_app.stop()
            await telegram_app.shutdown()
        await engine.dispose()


app = FastAPI(title="LioBot by AI Factory", version="0.3.0", lifespan=lifespan, docs_url=None, redoc_url=None)


def authorize(authorization: str = Header(default="")):
    if not settings.api_token or not hmac.compare_digest(
        authorization.encode(), f"Bearer {settings.api_token}".encode()
    ):
        raise HTTPException(401, "Unauthorized")
    return settings.api_operator_user_id


@app.get("/health")
@app.get("/v1/health")
async def health():
    return {"status": "ok", "version": "0.3.0"}


@app.get("/ready")
@app.get("/v1/ready")
async def ready():
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        if settings.telegram_bot_token and (
            not telegram_app or not telegram_app.running or not telegram_app.updater.running
        ):
            raise RuntimeError("Telegram not running")
        if settings.worker_enabled:
            if (
                not worker
                or not worker.last_tick
                or (utcnow() - worker.last_tick).total_seconds() > 30
                or worker.last_error
            ):
                raise RuntimeError("Worker unavailable")
            async with httpx.AsyncClient(timeout=12, trust_env=False) as client:
                response = await client.get(settings.sandbox_url.rstrip("/") + "/health")
                response.raise_for_status()
    except Exception:
        raise HTTPException(503, "A required dependency is not ready") from None
    return {"status": "ready", "version": "0.3.0"}


@app.get("/health/telegram", dependencies=[Depends(authorize)])
async def telegram_health():
    return {
        "application_running": bool(telegram_app and telegram_app.running),
        "updater_running": bool(telegram_app and telegram_app.updater.running),
    }


def task_view(t):
    try:
        plan = json.loads(t.plan_json) if t.plan_json else None
    except (TypeError, json.JSONDecodeError):
        plan = None
    next_actions = {
        "received": "LioBot will resolve intent and prepare a plan.",
        "planning": "Review the plan when it appears; answer only blocking questions.",
        "waiting_input": "Answer the blocking clarification.",
        "awaiting_approval": "Review the exact plan and approve, reject, ask, or defer.",
        "developing": "LioBot is executing the approved bounded plan.",
        "testing": "LioBot is running isolated verification.",
        "reviewing": "LioBot is independently reviewing evidence.",
        "publishing": "LioBot is reconciling the approved publication.",
        "pr_created": "Review the PR and decide whether to merge/deploy.",
        "completed": "Publication is acknowledged; no further action is required.",
        "superseded": "This stale merged PR was closed without release; create a fresh bounded re-review task.",
        "failed": "Inspect the report/logs, then create a new bounded retry if appropriate.",
        "cancelled": "No action is running; create a new intent if the work is still needed.",
    }
    decision_required = t.status in {"waiting_input", "awaiting_approval"}
    return {
        key: getattr(t, key)
        for key in (
            "id",
            "project",
            "repo",
            "status",
            "requirement",
            "branch",
            "pr_url",
            "last_message",
            "iteration",
            "llm_calls",
            "tokens",
            "cost_usd",
            "cost_incomplete",
            "created_at",
            "updated_at",
        )
    } | {
        "intent_id": t.id,
        "kind": t.kind,
        "summary": t.last_message or "Intent accepted; waiting for the next durable state transition.",
        "next_action": next_actions.get(t.status, "Inspect the current evidence and task state."),
        "decision_required": decision_required,
        "evidence_refs": [f"/v1/tasks/{t.id}/evidence"],
        "risk": (plan or {}).get("risk", "unknown"),
        "plan": plan,
    }


@app.post("/tasks", dependencies=[Depends(authorize)], status_code=202)
async def create(request: TaskRequest):
    try:
        return task_view(await store.create(**request.model_dump()))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/tasks", dependencies=[Depends(authorize)])
async def tasks():
    return [task_view(t) for t in await store.list()]


@app.get("/tasks/{task_id}", dependencies=[Depends(authorize)])
@app.get("/v1/tasks/{task_id}", dependencies=[Depends(authorize)])
@app.get("/v1/intents/{task_id}", dependencies=[Depends(authorize)])
async def task(task_id: int):
    t = await store.get(task_id)
    if not t:
        raise HTTPException(404, "Task not found")
    return task_view(t)


@app.get("/tasks/{task_id}/artifacts", dependencies=[Depends(authorize)])
@app.get("/v1/tasks/{task_id}/evidence", dependencies=[Depends(authorize)])
@app.get("/v1/tasks/{task_id}/artifacts", dependencies=[Depends(authorize)])
async def artifacts(task_id: int):
    return [
        {"kind": a.kind, "content": a.content, "created_at": a.created_at}
        for a in await store.artifacts(task_id)
    ]


@app.get("/tasks/{task_id}/events", dependencies=[Depends(authorize)])
async def events(task_id: int):
    return [
        {"kind": e.kind, "message": e.message, "created_at": e.created_at}
        for e in await store.events(task_id)
    ]


class ActionRequest(BaseModel):
    message: str = Field(default="", max_length=10000)


@app.post("/tasks/{task_id}/{action}", dependencies=[Depends(authorize)])
@app.post("/v1/tasks/{task_id}/{action}", dependencies=[Depends(authorize)])
async def action(task_id: int, action: str, request: ActionRequest):
    try:
        return {"message": await perform_action(task_id, action, request.message)}
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


def decision_view(d):
    return {
        "id": d.id,
        "task_id": d.task_id,
        "project": d.project,
        "kind": d.kind,
        "title": d.title,
        "situation": d.situation,
        "why_now": d.why_now,
        "options": json.loads(d.options_json or "[]"),
        "impact": json.loads(d.impact_json or "[]"),
        "risk": json.loads(d.risk_json or "[]"),
        "recommendation": d.recommendation,
        "evidence": json.loads(d.evidence_json or "[]"),
        "missing_information": d.missing_information,
        "rollback": d.rollback,
        "required_action": d.required_action,
        "priority": d.priority,
        "risk_level": d.risk_level,
        "state": d.state,
        "expires_at": d.expires_at,
        "decided_by": d.decided_by,
        "decided_at": d.decided_at,
        "created_at": d.created_at,
    }


@app.post("/decisions", dependencies=[Depends(authorize)], status_code=201)
@app.post("/v1/decisions", dependencies=[Depends(authorize)], status_code=201)
async def new_decision(card: DecisionRequest):
    return decision_view(await raise_decision(card))


@app.get("/decisions", dependencies=[Depends(authorize)])
@app.get("/v1/decisions", dependencies=[Depends(authorize)])
async def decisions(state: str | None = None, project: str | None = None):
    return [decision_view(d) for d in await decision_inbox(state=state, project=project)]


class DecisionAnswer(BaseModel):
    model_config = {"extra": "forbid"}
    answer: str = Field(min_length=1, max_length=200)


@app.post("/decisions/{decision_id}", dependencies=[Depends(authorize)])
@app.post("/v1/decisions/{decision_id}/action", dependencies=[Depends(authorize)])
async def answer_decision(decision_id: int, request: DecisionAnswer, operator_id=Depends(authorize)):
    try:
        return decision_view(await resolve_decision(decision_id, request.answer, operator_id))
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/v1/intents", dependencies=[Depends(authorize)], status_code=202)
async def create_intent(request: IntentRequest, operator_id=Depends(authorize)):
    try:
        task = await store.create(
            request.objective,
            project=request.project,
            user_id=operator_id,
            idempotency_key=request.idempotency_key,
        )
        return task_view(task)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/v1/intents/{intent_id}/clarification", dependencies=[Depends(authorize)])
async def clarify_intent(intent_id: int, request: ClarificationRequest):
    try:
        await perform_action(intent_id, "answer", request.answer)
        task = await store.get(intent_id)
        if not task:
            raise HTTPException(404, "Intent not found")
        return task_view(task)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/v1/plans/{plan_id}/approve", dependencies=[Depends(authorize)])
async def approve_plan(plan_id: int, request: PlanApprovalRequest):
    try:
        await perform_action(plan_id, "approve", request.plan_hash[:12])
        task = await store.get(plan_id)
        if not task:
            raise HTTPException(404, "Plan not found")
        return task_view(task)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/v1/improvements", dependencies=[Depends(authorize)], status_code=202)
async def create_improvement(request: ImprovementRequest, operator_id=Depends(authorize)):
    try:
        task, needs_approval, sensitive_areas = await create_self_improvement(
            request.brief,
            request.project,
            user_id=operator_id,
            idempotency_key=request.idempotency_key,
        )
        result = task_view(task)
        result.update(
            {
                "approval_required": needs_approval,
                "sensitive_areas": sensitive_areas,
                "next_action": (
                    "Review the self-improvement plan and decision card before execution."
                    if needs_approval
                    else "LioBot will prepare the bounded self-improvement plan."
                ),
            }
        )
        return result
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/v1/memory/search", dependencies=[Depends(authorize)])
async def search_memory(
    keys: str = "",
    role: str = "",
    scope: str | None = None,
    limit: int = 25,
):
    if not 1 <= limit <= 100:
        raise HTTPException(422, "limit must be between 1 and 100")
    selected = [key.strip() for key in keys.split(",") if key.strip()] or None
    items, truncated = await store.recall(selected, role=role or None, scope=scope, limit=limit)
    return {"items": [item.model_dump() for item in items], "truncated": truncated}
