import json
from datetime import timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import settings
from app.db import Task, _default_clause, init_db, utcnow
from app.schemas import DecisionState, SelfImprovementBrief
from app.store import BudgetExceeded, TaskStopped, plan_hash


async def test_idempotency_and_project_routing(db):
    first = await db.create("Create persistent todo feature", idempotency_key="telegram:1")
    again = await db.create("Create persistent todo feature", idempotency_key="telegram:1")
    assert first.id == again.id
    assert first.repo == "owner/repo" and first.branch == "ai-factory/task-1"
    with pytest.raises(ValueError, match="different request"):
        await db.create("Different feature", idempotency_key="telegram:1")
    with pytest.raises(ValueError, match="Unknown project"):
        await db.create("Edit another repository", "unregistered")


async def test_queued_task_already_reserves_its_repository(db):
    first = await db.create("First queued feature")
    with pytest.raises(ValueError, match="One active task per repository"):
        await db.create("Second feature on the same repository")
    assert (await db.claim("worker-a")).id == first.id
    assert await db.claim("worker-b") is None


async def test_serializes_same_repo_but_allows_other_repos(db):
    a = await db.create("First feature")
    c = await db.create("Other feature", "other")
    assert (await db.claim("worker-a")).id == a.id
    assert (await db.claim("worker-b")).id == c.id
    assert await db.claim("worker-c") is None
    # An open PR keeps the repository reserved: a later branch must not race it.
    await db.update(a.id, "worker-a", status="pr_created", lease_owner=None, lease_until=None)
    assert await db.claim("worker-c") is None


@pytest.mark.parametrize("status", ["waiting_input", "awaiting_approval", "pr_created"])
async def test_paused_or_published_task_blocks_a_claim_on_the_same_repository(db, status):
    first = await db.create("First gated feature")
    await db.update(first.id, status=status, cancel_requested=False)
    # A stale queued row from before the reservation rule must still be refused.
    async with db.sessions() as s, s.begin():
        s.add(
            Task(
                requirement="Queued before the reservation rule",
                status="received",
                repo=first.repo,
                base_branch="main",
                project="lab",
                branch="ai-factory/task-legacy",
            )
        )
    assert await db.claim("worker") is None


async def test_claim_refuses_a_task_with_no_branch(db):
    task = await db.create("Half-written intake")
    await db.update(task.id, branch=None)
    assert await db.claim("worker") is None


async def test_intake_rolls_back_when_the_brief_cannot_be_stored(db):
    class ExplodingBrief:
        def model_dump_json(self):
            raise RuntimeError("brief serialization failed")

    with pytest.raises(RuntimeError, match="brief serialization failed"):
        await db.create("Improve the parser", "self", kind="self_improvement", brief=ExplodingBrief())
    assert await db.list() == []
    assert await db.claim("worker") is None


async def test_expired_lease_recovers_and_fences_old_worker(db):
    task = await db.create("Feature needing recovery")
    await db.claim("old")
    await db.update(
        task.id, "old", plan_json='{"objective":"preserved"}', lease_until=utcnow() - timedelta(seconds=1)
    )
    recovered = await db.claim("new")
    assert recovered.id == task.id and recovered.recoveries == 1
    assert recovered.plan_json == '{"objective":"preserved"}'
    with pytest.raises(TaskStopped):
        await db.update(task.id, "old", status="pr_created")


async def test_recovery_limit_and_cancel(db, monkeypatch):
    monkeypatch.setattr(settings, "max_recoveries", 0)
    task = await db.create("Feature to cancel")
    await db.cancel(task.id)
    assert (await db.get(task.id)).status == "cancelled"
    assert await db.claim("w") is None
    await db.resume(task.id, "retry")
    await db.claim("w")
    await db.update(task.id, "w", lease_until=utcnow() - timedelta(seconds=1))
    assert await db.claim("next") is None
    assert (await db.get(task.id)).status == "failed"


async def test_approval_is_bound_to_plan_and_clarification_resets_it(db):
    task = await db.create("Migrate stored data")
    plan = json.dumps({"objective": "Migrate", "acceptance_criteria": ["Data preserved"]})
    await db.update(task.id, status="awaiting_approval", plan_json=plan)
    with pytest.raises(ValueError, match="current plan hash"):
        await db.resume(task.id, "approve", "wrong")
    await db.resume(task.id, "approve", plan_hash(plan)[:12])
    assert (await db.get(task.id)).approved_plan_hash == plan_hash(plan)
    await db.update(task.id, status="waiting_input")
    await db.resume(task.id, "answer", "Use a backup")
    assert (await db.get(task.id)).plan_json is None
    assert (await db.get(task.id)).approved_plan_hash is None


async def test_waiting_input_clarification_card_is_durable_and_closes_on_answer(db):
    task = await db.create("Clarify the target repository", user_id=7)
    await db.update(task.id, status="waiting_input")
    card = await db.ensure_task_clarification_decision(task, "Perlu informasi: repositori target mana?")
    assert card.category == "clarification_needed"
    assert card.kind == "NEED_INFO"
    assert card.state == DecisionState.OPEN

    same = await db.ensure_task_clarification_decision(task, "Perlu informasi: repositori target mana?")
    assert same.id == card.id

    await db.resume(task.id, "answer", "Gunakan repo lab", user_id=7)
    closed = (await db.inbox(state="approved", owner=7))[0]
    assert closed.id == card.id
    assert closed.decided_by == 7
    assert closed.decision_note == "Gunakan repo lab"


async def test_budget_survives_retry(db, monkeypatch):
    monkeypatch.setattr(settings, "max_llm_calls", 1)
    task = await db.create("Budgeted feature")
    await db.claim("w")
    await db.reserve_call(task.id, "w")
    await db.record_usage(task.id, "w", 123, None)
    assert (await db.get(task.id)).cost_incomplete
    with pytest.raises(BudgetExceeded):
        await db.reserve_call(task.id, "w")
    await db.update(task.id, "w", status="failed", lease_owner=None, lease_until=None)
    await db.resume(task.id, "retry")
    await db.claim("w2")
    with pytest.raises(BudgetExceeded):
        await db.reserve_call(task.id, "w2")


async def test_additive_migration_preserves_v01_rows(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'old.db'}")
    async with engine.begin() as c:
        await c.execute(
            text(
                "CREATE TABLE tasks (id INTEGER PRIMARY KEY, requirement TEXT, status VARCHAR(32), branch VARCHAR(200), pr_url TEXT, last_message TEXT, created_at DATETIME, updated_at DATETIME)"
            )
        )
        await c.execute(
            text("INSERT INTO tasks (id, requirement, status) VALUES (6, 'old todo task', 'pr_created')")
        )
    await init_db(engine)
    await init_db(engine)
    async with engine.connect() as c:
        row = (
            await c.execute(text("SELECT requirement, status, iteration, project FROM tasks WHERE id=6"))
        ).one()
        assert tuple(row) == ("old todo task", "pr_created", 0, "lab")
    await engine.dispose()


def test_default_clause_quotes_literals_and_keeps_sql_raw():
    # The V0.1 upgrade path must quote string defaults (DEFAULT 'lab'); unquoted,
    # PostgreSQL treats them as identifiers and rejects the additive ALTER TABLE.
    assert _default_clause(Task.__table__.c.project.server_default) == " DEFAULT 'lab'"
    assert _default_clause(Task.__table__.c.cost_incomplete.server_default) == " DEFAULT false"
    assert _default_clause(Task.__table__.c.requirement.server_default) == ""


async def test_only_one_active_task_per_repository(db):
    first = await db.create("First feature")
    assert (await db.claim("worker-a")).id == first.id  # planning is an active state
    with pytest.raises(ValueError, match="One active task per repository"):
        await db.create("Second feature on the same repository")
    other = await db.create("Feature on another repository", "other")
    assert other.repo == "owner/other"


@pytest.mark.parametrize("status", ["waiting_input", "awaiting_approval", "pr_created"])
async def test_paused_or_published_task_reserves_its_repository(db, status):
    first = await db.create("First gated feature")
    await db.update(first.id, status=status)
    with pytest.raises(ValueError, match="One active task per repository"):
        await db.create("Second feature on the same repository")


async def test_idempotent_retry_returns_the_original_task_while_active(db):
    first = await db.create("Same requirement", idempotency_key="telegram:9")
    await db.claim("worker-a")  # the task is active, yet a retry is still idempotent
    again = await db.create("Same requirement", idempotency_key="telegram:9")
    assert again.id == first.id and again.status == "planning"
    with pytest.raises(ValueError, match="different request"):
        await db.create("Different requirement", idempotency_key="telegram:9")


async def test_idempotency_key_binds_the_whole_request_not_only_the_requirement(db):
    brief = SelfImprovementBrief(
        problem="Parser drops unknown fields",
        evidence=["test_parser failed 3 times"],
        hypothesis="Tolerate unknown keys",
        scope="app/agents.py",
        baseline="Strict schema",
        rollback_plan="Revert the schema change",
    )
    first = await db.create(
        "Improve the parser",
        "self",
        chat_id=42,
        user_id=7,
        idempotency_key="api:1",
        kind="self_improvement",
        brief=brief,
    )
    # The same key with the same request stays idempotent, even while the task is active.
    await db.claim("worker")
    again = await db.create(
        "Improve the parser",
        "self",
        chat_id=42,
        user_id=7,
        idempotency_key="api:1",
        kind="self_improvement",
        brief=brief,
    )
    assert again.id == first.id
    # The self-target alias gate fires before idempotency, so a wrong target is refused
    # deterministically instead of colliding with the bound request.
    with pytest.raises(ValueError, match="must target the registered self-target alias"):
        await db.create(
            "Improve the parser",
            "other",
            chat_id=42,
            user_id=7,
            idempotency_key="api:1",
            kind="self_improvement",
            brief=brief,
        )
    for changed in (
        {"chat_id": 43},
        {"user_id": 8},
        {"kind": "engineering", "brief": None},
    ):
        with pytest.raises(ValueError, match="different request"):
            await db.create(
                "Improve the parser",
                changed.get("project", "self"),
                chat_id=changed.get("chat_id", 42),
                user_id=changed.get("user_id", 7),
                idempotency_key="api:1",
                kind=changed.get("kind", "self_improvement"),
                brief=changed.get("brief", brief),
            )
    other_brief = brief.model_copy(update={"hypothesis": "Reject unknown keys"})
    with pytest.raises(ValueError, match="different request"):
        await db.create(
            "Improve the parser",
            "self",
            chat_id=42,
            user_id=7,
            idempotency_key="api:1",
            kind="self_improvement",
            brief=other_brief,
        )
