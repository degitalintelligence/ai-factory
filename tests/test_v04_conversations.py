"""Real SQL routing contracts; no live model, channel, or production action."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import func, select

from app.chat import converse
from app.config import settings
from app.conversations import bind_telegram_message, history, telegram_target
from app.db import ChatTurn, Conversation, utcnow
from app.main import app
from app.metrics import task_outcomes
from app.staff_schemas import ChatRequest
from app.store import TaskStopped


def request(message, key, **kwargs):
    return ChatRequest(message=message, idempotency_key=key, **kwargs)


async def goal(db, project="lab"):
    return await converse(request("Review product requirements", "initial", project=project), 7)


@pytest.mark.parametrize(
    "message",
    [
        "status",
        "gimana progresnya",
        "sudah sampai mana",
        "kenapa gagal",
        "why failed",
        "hasilnya",
        "lihat hasil",
    ],
)
async def test_status_followups_return_persisted_state_without_a_new_dag(db, message):
    initial = await goal(db)
    await db.update(initial["intent_id"], status="failed", last_message="Recorded budget stop")
    result = await converse(request(message, "followup", conversation_id=initial["conversation_id"]), 7)
    assert result["intent_id"] == initial["intent_id"] and result["status"] == "failed"
    assert result["summary"] == "Recorded budget stop"
    assert len(await db.list(user_id=7)) == 1


@pytest.mark.parametrize(
    "message",
    [
        "lanjut",
        "lanjutkan",
        "lanjut yang tadi",
        "continue",
        "setuju",
        "approve",
        "oke",
        "ok",
        "gas",
        "yes",
        "ya",
    ],
)
async def test_continuation_never_infers_retry_or_approval(db, message):
    initial = await goal(db)
    await db.update(initial["intent_id"], status="awaiting_approval", plan_json='{"budget":{}}')
    result = await converse(request(message, "followup", conversation_id=initial["conversation_id"]), 7)
    task = await db.get(initial["intent_id"])
    assert task.status == "awaiting_approval" and task.approved_plan_hash is None
    assert len(await db.list(user_id=7)) == 1
    if message in {"setuju", "approve", "oke", "ok", "gas", "yes", "ya"}:
        assert result["status"] == "needs_target"


async def test_multiple_thread_tasks_require_selection_not_latest(db):
    initial = await goal(db)
    second = await converse(
        request("Review another product goal", "second", conversation_id=initial["conversation_id"]), 7
    )
    result = await converse(
        request("lanjut yang tadi", "third", conversation_id=initial["conversation_id"]), 7
    )
    assert result["status"] == "needs_target"
    assert set(result["candidates"]) == {initial["intent_id"], second["intent_id"]}


@pytest.mark.parametrize(
    "case", ["other_owner", "other_tenant", "other_project", "unknown_thread", "conflicting_id"]
)
async def test_followup_scope_never_widens(db, case, monkeypatch):
    initial = await goal(db)
    req = request(
        "status",
        "followup",
        conversation_id=initial["conversation_id"],
        reply_to_intent_id=initial["intent_id"],
    )
    actor = 7
    if case == "other_owner":
        actor = 8
    elif case == "other_tenant":
        monkeypatch.setattr(settings, "tenant_id", "other")
    elif case == "other_project":
        await db.update(initial["intent_id"], project="other")
    elif case == "unknown_thread":
        req.conversation_id = "missing"
    else:
        req.message = "status task #999999"
    with pytest.raises(ValueError):
        await converse(req, actor)


async def test_duplicate_delivery_and_changed_payload(db):
    req = request("Review product requirements", "duplicate", project="lab")
    first = await converse(req, 7)
    assert await converse(req, 7) == first
    with pytest.raises(ValueError, match="Idempotency"):
        await converse(req.model_copy(update={"message": "A different objective"}), 7)
    async with db.sessions() as s:
        assert await s.scalar(select(func.count()).select_from(ChatTurn)) == 1
        assert await s.scalar(select(func.count()).select_from(Conversation)) == 1


async def test_trivial_one_word_request_uses_quick_reply_without_creating_a_task(db, monkeypatch):
    async def fake_quick_reply(request, actor):
        assert actor == 7
        return {
            "summary": "Sehat",
            "status": "completed",
            "intent_id": None,
            "next_action": "Ajukan tujuan baru bila perlu.",
            "decision_required": False,
            "evidence_refs": [],
            "risk": "low",
            "quick_reply": True,
        }

    monkeypatch.setattr("app.conversations.quick_reply", fake_quick_reply)
    result = await converse(request("Jawab satu kata saja: sehat.", "quick-one-word"), 7)
    assert result["status"] == "completed"
    assert result["intent_id"] is None
    assert result["summary"] == "Sehat"
    assert result["quick_reply"] is True
    assert await db.list(user_id=7) == []


async def test_review_request_still_creates_a_durable_intent(db):
    result = await converse(request("Review product requirements", "durable-review", project="lab"), 7)
    assert result["intent_id"] is not None
    assert result["status"] == "received"


async def test_project_switch_requires_new_thread(db):
    initial = await goal(db)
    with pytest.raises(ValueError, match="Project changed"):
        await converse(
            request(
                "Review other project", "switch", project="other", conversation_id=initial["conversation_id"]
            ),
            7,
        )
    fresh = await converse(request("Review other project", "new", project="other"), 7)
    assert fresh["conversation_id"] != initial["conversation_id"]


async def test_clarification_and_card_are_atomic_fenced_and_idempotent(db):
    initial = await goal(db)
    task = await db.claim("worker")
    card = await db.wait_for_clarification(task.id, "worker", "Which outcome is required?")
    repeated = await db.wait_for_clarification(task.id, "worker", "Which outcome is required?")
    assert repeated.id == card.id and (await db.get(task.id)).status == "waiting_input"
    with pytest.raises(TaskStopped):
        await db.wait_for_clarification(task.id, "stale", "Another question")
    await db.update(task.id, "worker", lease_owner=None, lease_until=utcnow())
    req = request(
        "Measurable product priorities",
        "answer",
        conversation_id=initial["conversation_id"],
        reply_to_intent_id=task.id,
    )
    result = await converse(req, 7)
    assert result["status"] == "received"
    assert await converse(req, 7) == result
    await db.answer_clarification(card.id, req.message, 7)
    updated = await db.get(task.id)
    assert updated.requirement.count(req.message) == 1 and updated.approved_plan_hash is None
    assert not await db.inbox(state="open")


async def test_stale_clarification_cannot_resume_new_requirement(db):
    await goal(db)
    task = await db.claim("worker")
    card = await db.wait_for_clarification(task.id, "worker", "Which outcome?")
    await db.update(task.id, "worker", lease_owner=None, lease_until=utcnow())
    await db.update(task.id, requirement="Changed objective")
    with pytest.raises(ValueError, match="different requirement"):
        await db.answer_clarification(card.id, "A specific outcome", 7)
    assert (await db.get(task.id)).status == "waiting_input"


async def test_approval_is_not_an_answer_and_foreign_answer_is_rejected(db):
    await goal(db)
    task = await db.claim("worker")
    card = await db.wait_for_clarification(task.id, "worker", "Which outcome?")
    await db.update(task.id, "worker", lease_owner=None, lease_until=utcnow())
    with pytest.raises(ValueError, match="approval is not an answer"):
        await db.resolve_decision(card.id, "approve", user_id=7)
    with pytest.raises(ValueError, match="not found"):
        await db.answer_clarification(card.id, "Specific outcome", 8)


async def test_status_tracks_actual_child_not_completed_parent(db):
    initial = await goal(db)
    parent = initial["intent_id"]
    await db.update(parent, status="completed")
    child = await db.create("Implement bounded feature", user_id=7)
    await db.update(child.id, status="failed", last_message="Child failed tests")
    await db.artifact(parent, "engineering_handoff", json.dumps({"task_id": child.id, "project": "lab"}))
    result = await converse(request("status", "child", conversation_id=initial["conversation_id"]), 7)
    assert result["status"] == "failed" and result["child_task_id"] == child.id


async def test_completed_draft_revision_preserves_original_and_links_task(db):
    initial = await goal(db)
    await db.update(initial["intent_id"], status="completed")
    result = await converse(
        request(
            "revisi rekomendasi kedua: prioritaskan usability",
            "revision",
            conversation_id=initial["conversation_id"],
            reply_to_intent_id=initial["intent_id"],
        ),
        7,
    )
    assert result["related_task_id"] == initial["intent_id"] and result["intent_id"] != initial["intent_id"]
    assert (await db.get(initial["intent_id"])).status == "completed"


async def test_telegram_reply_mapping_is_owner_bound_and_durable(db):
    initial = await converse(request("Review product requirements", "telegram"), 7, 77)
    await bind_telegram_message(77, 123, initial["intent_id"], 7)
    assert await telegram_target(77, 123, 7) == initial["intent_id"]
    with pytest.raises(ValueError):
        await telegram_target(77, 123, 8)
    assert (await history(initial["conversation_id"], 7))["turns"][0]["task_id"] == initial["intent_id"]


async def test_restart_keeps_thread_and_idempotent_receipt(db, tmp_path, monkeypatch):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.db import init_db

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'restart.db'}")
    await init_db(engine)
    monkeypatch.setattr(db, "sessions", async_sessionmaker(engine, expire_on_commit=False))
    req = request("Review product requirements", "persist", project="lab")
    initial = await converse(req, 7)
    await engine.dispose()
    restarted = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'restart.db'}")
    await init_db(restarted)
    await init_db(restarted)
    monkeypatch.setattr(db, "sessions", async_sessionmaker(restarted, expire_on_commit=False))
    assert await converse(req, 7) == initial
    assert (await history(initial["conversation_id"], 7))["turns"]
    await restarted.dispose()


def test_metrics_exclude_handoff_pending_and_stopped_from_success_denominator():
    tasks = [
        SimpleNamespace(id=i, status=status, tenant="default", user_id=7, project="lab")
        for i, status in enumerate(
            ["completed", "failed", "received", "awaiting_approval", "cancelled", "pr_created", "reviewed"], 1
        )
    ]
    artifacts = [SimpleNamespace(task_id=1, kind="engineering_handoff", content='{"task_id":2}')]
    result = task_outcomes(tasks, artifacts)
    assert result["success_rate"] == 0.5 and result["terminal_evaluated_tasks"] == 2
    assert result["outcomes"]["handoffs"] == 1 and result["outcomes"]["reviewed_prs"] == 1
    assert task_outcomes([], [])["success_rate"] is None


async def test_http_clarification_and_history_share_core(db, monkeypatch):
    monkeypatch.setattr(settings, "api_token", "test-token")
    monkeypatch.setattr(settings, "api_operator_user_id", 7)
    headers = {"Authorization": "Bearer test-token"}
    initial = await goal(db)
    task = await db.claim("worker")
    card = await db.wait_for_clarification(task.id, "worker", "Which outcome?")
    await db.update(task.id, "worker", lease_owner=None, lease_until=utcnow())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://factory") as client:
        assert (await client.get(f"/v1/conversations/{initial['conversation_id']}")).status_code == 401
        assert (
            await client.get(f"/v1/conversations/{initial['conversation_id']}", headers=headers)
        ).status_code == 200
        answer = await client.post(
            f"/v1/decisions/{card.id}/clarification",
            json={"answer": "Prioritize reliability"},
            headers=headers,
        )
        assert answer.status_code == 200 and answer.json()["status"] == "received"
        assert (
            await client.post(
                f"/v1/decisions/{card.id}/clarification",
                json={"answer": "Prioritize reliability"},
                headers=headers,
            )
        ).status_code == 200


async def test_telegram_reply_uses_stored_task_not_text_injection(db, monkeypatch):
    from app.telegram_control import chat_handler

    initial = await converse(request("Review product requirements", "initial-telegram"), 7, 77)
    await db.update(initial["intent_id"], status="failed", last_message="Recorded failure")
    await bind_telegram_message(77, 100, initial["intent_id"], 7)
    monkeypatch.setattr(settings, "telegram_allowed_user_ids", "7")
    sent = SimpleNamespace(message_id=200)
    message = SimpleNamespace(
        text="kenapa gagal",
        message_id=150,
        reply_to_message=SimpleNamespace(message_id=100),
        reply_text=AsyncMock(return_value=sent),
    )
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=7),
        effective_chat=SimpleNamespace(id=77, type="private"),
        effective_message=message,
        update_id=900,
    )
    await chat_handler(update, SimpleNamespace())
    assert "Recorded failure" in message.reply_text.call_args.args[0]
    assert await telegram_target(77, 200, 7) == initial["intent_id"]
