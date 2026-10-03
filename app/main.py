import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.db import init_db
from app.telegram_control import build_telegram_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("ai_factory")

telegram_app = build_telegram_app()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()

    # Follow python-telegram-bot's documented lifecycle:
    # initialize -> updater.start_polling -> application.start
    await telegram_app.initialize()

    me = await telegram_app.bot.get_me()
    logger.info("Telegram bot authenticated as @%s (id=%s)", me.username, me.id)

    if not telegram_app.updater:
        raise RuntimeError("Telegram updater is unavailable")

    await telegram_app.updater.start_polling(
        drop_pending_updates=False,
        allowed_updates=None,
    )
    await telegram_app.start()

    logger.info("Telegram polling started")

    try:
        yield
    finally:
        logger.info("Stopping Telegram polling")
        await telegram_app.updater.stop()
        await telegram_app.stop()
        await telegram_app.shutdown()


app = FastAPI(title="AI Factory", version="0.1.1", lifespan=lifespan)


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "version": "0.1.1",
        "telegram_application_running": telegram_app.running,
        "telegram_updater_running": bool(
            telegram_app.updater and telegram_app.updater.running
        ),
    }


@app.get("/health/telegram")
async def telegram_health():
    me = await telegram_app.bot.get_me()
    return {
        "ok": True,
        "bot_username": me.username,
        "bot_id": me.id,
        "application_running": telegram_app.running,
        "updater_running": bool(
            telegram_app.updater and telegram_app.updater.running
        ),
    }
