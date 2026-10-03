import hmac
import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.config import settings
from app.db import engine, init_db, utcnow
from app.schemas import TaskRequest
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


app = FastAPI(title="AI Factory", version="0.2.0", lifespan=lifespan, docs_url=None, redoc_url=None)


def authorize(authorization: str = Header(default="")):
    if not settings.api_token or not hmac.compare_digest(authorization, f"Bearer {settings.api_token}"):
        raise HTTPException(401, "Unauthorized")


@app.get("/health")
async def health():
    return {"status": "ok", "version": "0.2.0"}


@app.get("/ready")
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
    return {"status": "ready", "version": "0.2.0"}


@app.get("/health/telegram", dependencies=[Depends(authorize)])
async def telegram_health():
    return {
        "application_running": bool(telegram_app and telegram_app.running),
        "updater_running": bool(telegram_app and telegram_app.updater.running),
    }


def task_view(t):
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
async def task(task_id: int):
    t = await store.get(task_id)
    if not t:
        raise HTTPException(404, "Task not found")
    return task_view(t)


@app.get("/tasks/{task_id}/artifacts", dependencies=[Depends(authorize)])
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
async def action(task_id: int, action: str, request: ActionRequest):
    try:
        if action == "cancel":
            await store.cancel(task_id)
        elif action in {"retry", "approve", "answer", "feedback"}:
            await store.resume(task_id, action, request.message)
        else:
            raise ValueError("Unknown action")
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"accepted": True}
