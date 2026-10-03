from contextlib import asynccontextmanager
from fastapi import FastAPI
from app.db import init_db
from app.telegram_control import build_telegram_app

telegram_app = build_telegram_app()

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await telegram_app.initialize()
    await telegram_app.start()
    if telegram_app.updater:
        await telegram_app.updater.start_polling()
    yield
    if telegram_app.updater:
        await telegram_app.updater.stop()
    await telegram_app.stop()
    await telegram_app.shutdown()

app = FastAPI(title="AI Factory", version="0.1.0", lifespan=lifespan)

@app.get("/health")
async def health():
    return {"status": "ok", "version": "0.1.0"}
