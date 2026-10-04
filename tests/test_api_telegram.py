from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.config import settings
from app.contracts import decision_inbox
from app.main import app
from app.telegram_control import (
    decide_handler,
    improve_handler,
    inbox_handler,
    new_handler,
    status_handler,
)


async def test_api_auth_and_idempotent_creation(db, monkeypatch):
    monkeypatch.setattr(settings, "api_token", "operator-test-token")
    monkeypatch.setattr(settings, "api_operator_user_id", 7)
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


async def test_api_refuses_a_valid_token_with_no_principal(db, monkeypatch):
    """A decision recorded with no principal is unattributable, so it fails closed."""
    monkeypatch.setattr(settings, "api_token", "t")
    monkeypatch.setattr(settings, "api_operator_user_id", None)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://factory") as client:
        response = await client.get("/tasks", headers={"Authorization": "Bearer t"})
    assert response.status_code == 401


def test_startup_refuses_an_api_token_without_a_principal(monkeypatch):
    monkeypatch.setattr(settings, "api_token", "t")
    monkeypatch.setattr(settings, "api_operator_user_id", None)
    monkeypatch.setattr(settings, "telegram_bot_token", "")
    monkeypatch.setattr(settings, "worker_enabled", False)
    with pytest.raises(ValueError, match="API_OPERATOR_USER_ID"):
        settings.validate_runtime()


def test_startup_refuses_invalid_role_clearance(monkeypatch):
    """Bad clearance JSON must fail at startup, not on the first memory read."""
    monkeypatch.setattr(settings, "role_clearance_json", '{"lead":"top-secret"}')
    monkeypatch.setattr(settings, "telegram_bot_token", "")
    monkeypatch.setattr(settings, "worker_enabled", False)
    with pytest.raises(ValueError, match="ROLE_CLEARANCE_JSON"):
        settings.validate_runtime()


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


CARD = {
    "title": "Register ai-factory as a self-target alias",
    "situation": "Self-improvement cannot target the engine repository yet.",
    "why_now": "Blocks the self-building loop on core.",
    "options": [{"id": "register", "label": "register", "impact": "Unblocks", "risk": "Scope grows"}],
    "recommendation": "register",
    "evidence": ["requirements section 15"],
    "rollback": "Remove the alias",
    "priority": "high",
    "risk_level": "high",
}


def texts(event):
    return [call.args[0] for call in event.effective_message.reply_text.call_args_list]


async def test_decision_lifecycle_is_identical_in_both_channels(db, monkeypatch):
    monkeypatch.setattr(settings, "api_token", "t")
    monkeypatch.setattr(settings, "api_operator_user_id", 7)
    monkeypatch.setattr(settings, "telegram_allowed_user_ids", "7")
    headers = {"Authorization": "Bearer t"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://factory") as client:
        created = await client.post("/decisions", headers=headers, json=CARD)
        assert created.status_code == 201
        decision_id = created.json()["id"]
        assert created.json()["state"] == "open"

        # Both channels read the same inbox row.
        inbox = await client.get("/decisions", headers=headers)
        assert [d["id"] for d in inbox.json()] == [decision_id]
        event = update()
        await inbox_handler(event, SimpleNamespace(args=[]))
        rendered = "\n".join(texts(event))
        assert f"Decision #{decision_id}" in rendered
        assert "register" in rendered and "requirements section 15" in rendered

        # Answering via Telegram is visible to the API, and vice versa.
        await decide_handler(update(), SimpleNamespace(args=[str(decision_id), "setujui"]))
        assert (await client.get("/decisions", headers=headers, params={"state": "approved"})).json()[0][
            "id"
        ] == decision_id
        answered = await client.post(f"/decisions/{decision_id}", headers=headers, json={"answer": "approve"})
        assert answered.status_code == 200 and answered.json()["state"] == "approved"


async def test_an_unparseable_decision_answer_is_refused_in_both_channels(db, monkeypatch):
    monkeypatch.setattr(settings, "api_token", "t")
    monkeypatch.setattr(settings, "api_operator_user_id", 7)
    monkeypatch.setattr(settings, "telegram_allowed_user_ids", "7")
    headers = {"Authorization": "Bearer t"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://factory") as client:
        decision_id = (await client.post("/decisions", headers=headers, json=CARD)).json()["id"]
    event = update()
    await decide_handler(event, SimpleNamespace(args=[str(decision_id), "maybe"]))
    assert "approve, reject, ask, defer" in "\n".join(texts(event))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://factory") as client:
        response = await client.post(f"/decisions/{decision_id}", headers=headers, json={"answer": "maybe"})
    assert response.status_code == 409
    assert [d.state for d in await decision_inbox(state="open")] == ["open"]


async def test_improve_requires_every_mandatory_field_then_queues(db, monkeypatch):
    monkeypatch.setattr(settings, "telegram_allowed_user_ids", "7")
    event = update()
    await improve_handler(event, SimpleNamespace(args=["problem:Only the problem was given"]))
    joined = "\n".join(texts(event))
    assert "Missing fields" in joined and "hypothesis" in joined
    assert await db.list() == []

    fields = "; ".join(
        [
            "problem:Retries loop on malformed JSON",
            "evidence:task 41 events",
            "hypothesis:Tighter schema cuts retries",
            "scope:Reword the developer prompt",
            "baseline:3 retries per response",
            "rollback:Revert the prompt commit",
            "areas:app/agents.py",
        ]
    )
    await improve_handler(update(), SimpleNamespace(args=[fields]))
    tasks = await db.list()
    assert len(tasks) == 1
    assert tasks[0].kind == "self_improvement"
    assert tasks[0].branch != tasks[0].base_branch
    stored = {a.kind: a.content for a in await db.artifacts(tasks[0].id)}
    assert '"hypothesis"' in stored["self_improvement_brief"]


async def test_improve_warns_when_sensitive_areas_are_touched(db, monkeypatch):
    monkeypatch.setattr(settings, "telegram_allowed_user_ids", "7")
    fields = "; ".join(
        [
            "problem:Relax the review gate",
            "evidence:three rejections last week",
            "hypothesis:One pass is enough",
            "scope:Lower the approval threshold in app/gates.py",
            "baseline:3 rejections per task",
            "rollback:Revert the gate change",
        ]
    )
    event = update()
    await improve_handler(event, SimpleNamespace(args=[fields]))
    rendered = "\n".join(texts(event))
    assert "Sensitive areas detected" in rendered
    assert "stops at /approve" in rendered
    assert (await db.list())[0].kind == "self_improvement"


async def test_api_decision_records_the_answering_operator(db, monkeypatch):
    monkeypatch.setattr(settings, "api_token", "t")
    monkeypatch.setattr(settings, "api_operator_user_id", 42)
    headers = {"Authorization": "Bearer t"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://factory") as client:
        first = (await client.post("/decisions", headers=headers, json=CARD)).json()["id"]
        second = (await client.post("/decisions", headers=headers, json=CARD)).json()["id"]
        answered = await client.post(
            f"/decisions/{first}", headers=headers, json={"answer": "approve", "user_id": 42}
        )
        assert answered.status_code == 422
        answered = await client.post(f"/decisions/{first}", headers=headers, json={"answer": "approve"})
        assert answered.status_code == 200 and answered.json()["decided_by"] == 42
        anonymous = await client.post(f"/decisions/{second}", headers=headers, json={"answer": "approve"})
        assert anonymous.status_code == 200 and anonymous.json()["decided_by"] == 42


async def test_improve_can_target_a_registered_project(db, monkeypatch):
    monkeypatch.setattr(settings, "telegram_allowed_user_ids", "7")
    base = "; ".join(
        [
            "problem:Retries loop on malformed JSON",
            "evidence:task 41 events",
            "hypothesis:Tighter schema cuts retries",
            "scope:Reword the developer prompt",
            "baseline:3 retries per response",
            "rollback:Revert the prompt commit",
        ]
    )
    await improve_handler(update(), SimpleNamespace(args=[base + "; project:other"]))
    await improve_handler(update_new(101), SimpleNamespace(args=[base]))  # no project field → lab
    assert [(t.project, t.repo) for t in await db.list()] == [
        ("lab", "owner/repo"),
        ("other", "owner/other"),
    ]

    event = update_new(102)
    await improve_handler(event, SimpleNamespace(args=[base + "; project:ghost"]))
    assert "Unknown project alias" in "\n".join(texts(event))
    assert len(await db.list()) == 2


def update_new(update_id):
    event = update()
    event.update_id = update_id
    return event
