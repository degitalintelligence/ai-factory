import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.config import settings
from app.contracts import decision_inbox
from app.main import app
from app.schemas import MemoryWrite, SelfImprovementBrief
from app.store import plan_hash
from app.telegram_control import (
    decide_handler,
    improve_handler,
    inbox_handler,
    new_handler,
    status_handler,
)

PLAN = json.dumps({"steps": ["do the thing"], "risk": "high"})


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
            "project:self",
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
            "project:self",
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


async def test_improve_requires_the_registered_self_target_alias(db, monkeypatch):
    """Self-improvement never falls back to the lab harness; the caller names the target."""
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
    event = update_new(101)
    await improve_handler(event, SimpleNamespace(args=[base]))  # no project field at all
    rendered = "\n".join(texts(event))
    assert "Missing fields" in rendered and "project" in rendered

    event = update_new(102)
    await improve_handler(event, SimpleNamespace(args=[base + "; project:other"]))
    assert "must target the registered self-target alias" in "\n".join(texts(event))

    event = update_new(103)
    await improve_handler(event, SimpleNamespace(args=[base + "; project:ghost"]))
    assert "Unknown project alias" in "\n".join(texts(event))
    assert await db.list() == []

    await improve_handler(update_new(104), SimpleNamespace(args=[base + "; project:self"]))
    assert [(t.project, t.repo) for t in await db.list()] == [("self", "owner/self")]


def update_new(update_id):
    event = update()
    event.update_id = update_id
    return event


async def test_memory_lock_endpoint_freezes_for_the_operator(db, monkeypatch):
    monkeypatch.setattr(settings, "api_token", "operator-test-token")
    monkeypatch.setattr(settings, "api_operator_user_id", 7)
    stored = await db.remember(
        MemoryWrite(key="deploy.target", value="prod-web", source="operator note", scope="lab")
    )
    headers = {"Authorization": "Bearer operator-test-token"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://factory") as client:
        wrong = {"Authorization": "Bearer not-the-token"}
        assert (
            await client.post(f"/v1/memory/{stored.id}/lock", headers=wrong, json={})
        ).status_code == 401  # wrong token never reaches the store
        ok = await client.post(
            f"/v1/memory/{stored.id}/lock", headers=headers, json={"reason": "verified SOP"}
        )
        assert ok.status_code == 200
        body = ok.json()
        assert body["locked"] is True and body["owner"] == 7 and body["state"] == "active"
        # A repeated request is the same one action, not a new event.
        again = await client.post(f"/v1/memory/{stored.id}/lock", headers=headers, json={})
        assert again.status_code == 200 and again.json()["locked"] is True
        missing = await client.post("/v1/memory/99999/lock", headers=headers, json={})
        assert missing.status_code == 404


async def test_improvement_outcome_endpoint_closes_the_loop(db, monkeypatch):
    monkeypatch.setattr(settings, "api_token", "operator-test-token")
    monkeypatch.setattr(settings, "api_operator_user_id", 7)
    retry_brief = {
        "problem": "Prompt retries loop three times on malformed JSON",
        "evidence": ["events for task 41 show 3 retries"],
        "hypothesis": "Tightening the schema will cut retries",
        "scope": "Reword the developer prompt only",
        "baseline": "3 retries per malformed response, 41 tasks observed",
        "touched_areas": ["app/agents.py"],
        "rollback_plan": "Revert the prompt commit",
    }
    task = await db.create(
        "Improve the retry parser",
        "self",
        kind="self_improvement",
        brief=SelfImprovementBrief(**retry_brief),
    )
    await db.update(task.id, status="completed")
    headers = {"Authorization": "Bearer operator-test-token"}
    measurement = {
        "before": "3 retries per malformed response",
        "after": "1 retry per malformed response across 20 tasks",
        "evidence": ["model_runs show retries 3->1"],
        "conclusion": "retain",
    }
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://factory") as client:
        wrong = {"Authorization": "Bearer not-the-token"}
        assert (
            await client.post(f"/v1/improvements/{task.id}/outcome", headers=wrong, json=measurement)
        ).status_code == 401
        ok = await client.post(f"/v1/improvements/{task.id}/outcome", headers=headers, json=measurement)
        assert ok.status_code == 200
        body = ok.json()
        assert body["conclusion"] == "retain"
        assert body["memory_key"] == f"improvement.task-{task.id}"
        assert body["lesson_version"] == 1
        # A repeated identical measurement is the same one action.
        again = await client.post(f"/v1/improvements/{task.id}/outcome", headers=headers, json=measurement)
        assert again.status_code == 200 and again.json()["lesson_memory_id"] == body["lesson_memory_id"]
        # A task that never shipped cannot be measured.
        pending = await db.create(
            "Improve the retry parser again",
            "self",
            kind="self_improvement",
            brief=SelfImprovementBrief(**retry_brief),
        )
        response = await client.post(
            f"/v1/improvements/{pending.id}/outcome", headers=headers, json=measurement
        )
        assert response.status_code == 409
        assert "measure the outcome after completion" in response.json()["detail"]
        missing = await client.post("/v1/improvements/99999/outcome", headers=headers, json=measurement)
        assert missing.status_code == 404


async def test_plan_approval_endpoint_closes_the_card_and_records_the_actor(db, monkeypatch):
    """A direct plan-hash approval is the same decision as resolving the card.

    The open APPROVAL_REQUIRED card must close in the same transaction as the
    resume, and the approval event must carry the acting principal (review of
    a64e469: the endpoint left a stale open gate and recorded no actor).
    """
    monkeypatch.setattr(settings, "api_token", "operator-test-token")
    monkeypatch.setattr(settings, "api_operator_user_id", 7)
    task = await db.create("Ship a risky change")
    await db.update(task.id, status="awaiting_approval", plan_json=PLAN)
    task = await db.get(task.id)
    digest = plan_hash(PLAN)
    card = await db.ensure_task_approval_decision(task, "High-risk plan", digest)

    headers = {"Authorization": "Bearer operator-test-token"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://factory") as client:
        response = await client.post(
            f"/v1/plans/{task.id}/approve", headers=headers, json={"plan_hash": digest}
        )
    assert response.status_code == 200
    assert response.json()["status"] == "received"

    # The card closed with the resume in one transaction: no stale open gate
    # survives, and the closing principal is the operator that called the endpoint.
    assert await decision_inbox(state="open") == []
    closed = (await decision_inbox(state="approved"))[0]
    assert closed.id == card.id
    assert closed.decided_by == 7 and closed.decided_at is not None
    events = await db.events(task.id)
    assert any(e.kind == "approve" and "by user 7" in e.message for e in events)
