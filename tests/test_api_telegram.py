from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.config import settings
from app.main import app
from app.telegram_control import new_handler, status_handler


async def test_api_auth_and_idempotent_creation(db, monkeypatch):
    monkeypatch.setattr(settings, "api_token", "operator-test-token")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://factory") as client:
        assert (await client.get("/health")).status_code == 200
        assert (await client.get("/tasks")).status_code == 401
        headers = {"Authorization": "Bearer operator-test-token"}
        body = {"requirement": "Create feature", "idempotency_key": "api:1"}
        a = await client.post("/tasks", headers=headers, json=body)
        b = await client.post("/tasks", headers=headers, json=body)
        assert a.status_code == b.status_code == 202
        assert a.json()["id"] == b.json()["id"]
        assert (await client.get("/tasks", headers=headers)).json()[0]["repo"] == "owner/repo"
        response = await client.post("/tasks/1/cancel", headers=headers, json={})
        assert response.status_code == 200
        assert (await db.get(1)).status == "cancelled"


@pytest.mark.parametrize("token", ["", "wrong"])
async def test_api_disabled_or_wrong_token_denies_access(db, monkeypatch, token):
    monkeypatch.setattr(settings, "api_token", token)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://factory") as client:
        assert (await client.get("/tasks", headers={"Authorization": "Bearer x"})).status_code == 401


def update(user=7, kind="private"):
    message = SimpleNamespace(reply_text=AsyncMock())
    return SimpleNamespace(
        update_id=100,
        effective_user=SimpleNamespace(id=user),
        effective_chat=SimpleNamespace(id=10, type=kind),
        effective_message=message,
    )


async def test_telegram_denies_unknown_users_and_groups(db, monkeypatch):
    monkeypatch.setattr(settings, "telegram_allowed_user_ids", "7")
    context = SimpleNamespace(args=["Build", "feature"])
    for event in (update(8), update(7, "group")):
        await new_handler(event, context)
        assert "Access denied" in event.effective_message.reply_text.call_args.args[0]
    assert await db.list() == []


async def test_telegram_deduplicates_updates_and_hides_other_users_tasks(db, monkeypatch):
    monkeypatch.setattr(settings, "telegram_allowed_user_ids", "7,8")
    event = update()
    await new_handler(event, SimpleNamespace(args=["Build", "feature"]))
    await new_handler(event, SimpleNamespace(args=["Build", "feature"]))
    assert len(await db.list()) == 1
    intruder = update(8)
    await status_handler(intruder, SimpleNamespace(args=["1"]))
    assert "not owned" in intruder.effective_message.reply_text.call_args.args[0]
