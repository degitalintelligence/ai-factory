"""Decision Request / Decision Inbox: one durable row, one state machine, every channel."""

import json
from datetime import timedelta

import pytest
from pydantic import ValidationError

from app.contracts import decision_inbox, raise_decision, resolve_decision
from app.db import Decision, utcnow
from app.schemas import DecisionMessageType, DecisionRequest, Option
from app.store import plan_hash

PLAN = json.dumps({"steps": ["do the thing"], "risk": "high"})


def card(**overrides):
    payload = {
        "task_id": None,
        "project": "lab",
        "kind": DecisionMessageType.DECISION_REQUIRED,
        "title": "Register ai-factory as a self-target alias",
        "situation": "Self-improvement cannot target the engine repository yet.",
        "why_now": "Blocks the self-building loop from operating on core.",
        "options": [
            Option(id="register", label="register", impact="Unblocks self-build", risk="Scope grows"),
            Option(id="defer", label="defer", impact="No change", risk="Loop stays blocked"),
        ],
        "recommendation": "register",
        "evidence": ["requirements section 15"],
        "rollback": "Remove the alias entry",
        "priority": "high",
        "risk_level": "high",
    }
    return DecisionRequest(**{**payload, **overrides})


async def gated_task(db, requirement="Ship a risky change"):
    """A task parked on a high-risk plan, exactly as the orchestrator leaves it."""
    task = await db.create(requirement)
    await db.update(task.id, status="awaiting_approval", plan_json=PLAN)
    return await db.get(task.id)


async def expire(db, decision_id):
    async with db.sessions() as s, s.begin():
        row = await s.get(Decision, decision_id)
        row.expires_at = utcnow() - timedelta(hours=1)


async def test_decision_card_requires_a_recommendation_that_exists(db):
    with pytest.raises(ValidationError, match="recommendation must reference one of the offered options"):
        card(recommendation="something invented")
    # Referencing by label also satisfies the contract.
    assert card(recommendation="defer").recommendation == "defer"


async def test_decision_requires_evidence_and_a_reason(db):
    with pytest.raises(ValidationError):
        card(evidence=[])
    with pytest.raises(ValidationError):
        card(why_now="")


async def test_card_persists_every_field_the_operator_needs(db):
    await raise_decision(card())
    stored = (await decision_inbox())[0]
    assert stored.state == "open"
    assert stored.priority == "high" and stored.risk_level == "high"
    assert stored.required_action == "" or isinstance(stored.required_action, str)
    assert "requirements section 15" in stored.evidence_json
    assert "unblocks" in stored.impact_json.lower()
    assert "scope grows" in stored.risk_json.lower()
    assert stored.rollback == "Remove the alias entry"
    assert stored.missing_information == ""


async def test_natural_language_answers_map_to_the_same_state_machine(db):
    for phrase, expected in [
        ("approve", "approved"),
        ("APPROVE", "approved"),
        ("setujui", "approved"),
        ("reject", "rejected"),
        ("tolak", "rejected"),
        ("ask", "needs_info"),
        ("tanya", "needs_info"),
        ("defer", "deferred"),
        ("nanti", "deferred"),
    ]:
        decision = await raise_decision(card())
        resolved = await resolve_decision(decision.id, phrase, user_id=42)
        assert resolved.state == expected, phrase
        assert resolved.decided_by == 42
        assert resolved.decided_at is not None


async def test_unknown_answer_never_mutates_state(db):
    decision = await raise_decision(card())
    for junk in ("maybe", "merge to main", "", "APPROVE NOW", "yes"):
        with pytest.raises(ValueError, match="approve, reject, ask, defer"):
            await resolve_decision(decision.id, junk)
    assert (await decision_inbox(state="open"))[0].id == decision.id


async def test_resolution_is_idempotent_but_conflicts_are_refused(db):
    decision = await raise_decision(card())
    first = await resolve_decision(decision.id, "approve", user_id=7)
    again = await resolve_decision(decision.id, "approve", user_id=7)
    assert first.state == again.state == "approved"
    assert again.decided_by == 7
    with pytest.raises(ValueError, match="is already approved"):
        await resolve_decision(decision.id, "reject", user_id=7)


async def test_expired_decisions_cannot_be_answered_and_leave_the_open_queue(db):
    task = await db.create("Add an alias")
    decision = await raise_decision(card(task_id=task.id, expires_at=utcnow() - timedelta(hours=1)))
    with pytest.raises(ValueError, match="Decision expired"):
        await resolve_decision(decision.id, "approve")
    # Expiry is persisted, so the card stops blocking the inbox.
    assert await decision_inbox(state="open") == []
    expired = await decision_inbox(state="expired")
    assert [d.id for d in expired] == [decision.id]


async def test_future_expiry_stays_answerable(db):
    decision = await raise_decision(card(expires_at=utcnow() + timedelta(hours=6)))
    assert (await resolve_decision(decision.id, "defer")).state == "deferred"


async def test_decision_is_audited_on_the_task_timeline(db):
    task = await db.create("Add an alias")
    decision = await raise_decision(card(task_id=task.id))
    await resolve_decision(decision.id, "approve", user_id=99)
    kinds = [e.kind for e in await db.events(task.id)]
    messages = [e.message for e in await db.events(task.id)]
    assert "DECISION_REQUIRED" in kinds
    assert "decision" in kinds
    assert any(f"Decision #{decision.id} -> approved by 99" in m for m in messages)


async def test_inbox_filters_by_state_and_project_across_tasks(db):
    await raise_decision(card(project="lab", title="lab question"))
    other = await raise_decision(card(project="quant", title="quant question"))
    assert len(await decision_inbox(state="open")) == 2
    assert [d.title for d in await decision_inbox(project="lab")] == ["lab question"]
    await resolve_decision(other.id, "approve")
    assert [d.title for d in await decision_inbox(state="open")] == ["lab question"]
    assert await decision_inbox(project="nobody") == []


async def test_secrets_in_a_card_are_redacted_before_storage(db):
    await raise_decision(
        card(
            situation="token ghp_abcdefghijklmnopqrstuvwxyz0123456789 leaked",
            evidence=["key sk-proj-1234567890abcdef"],
        )
    )
    stored = (await decision_inbox())[0]
    assert "ghp_abcdefghijklmnopqrstuvwxyz0123456789" not in stored.situation
    assert "sk-proj-1234567890abcdef" not in stored.evidence_json
    assert "[REDACTED]" in stored.situation


async def test_approval_card_releases_the_task_with_the_exact_plan_hash(db):
    task = await gated_task(db)
    decision = await db.ensure_task_approval_decision(task, "High-risk plan", plan_hash(PLAN))

    resolved = await resolve_decision(decision.id, "approve", user_id=7)

    assert resolved.state == "approved"
    after = await db.get(task.id)
    assert after.status == "received"
    assert after.approved_plan_hash == plan_hash(PLAN)


async def test_a_generic_decision_never_resumes_an_approval_task(db):
    task = await gated_task(db)
    decision = await raise_decision(card(task_id=task.id, kind=DecisionMessageType.DECISION_REQUIRED))

    await resolve_decision(decision.id, "approve", user_id=7)

    # DECISION_REQUIRED is a question about the work, not an authorisation to execute it.
    assert (await db.get(task.id)).status == "awaiting_approval"


@pytest.mark.parametrize(
    ("phrase", "expected"),
    [("reject", "cancelled"), ("ask", "failed"), ("defer", "failed")],
)
async def test_a_non_approval_outcome_releases_the_gated_task_and_its_repository(db, phrase, expected):
    task = await gated_task(db)
    decision = await db.ensure_task_approval_decision(task, "High-risk plan", plan_hash(PLAN))

    await resolve_decision(decision.id, phrase, user_id=7)

    assert (await db.get(task.id)).status == expected
    # The repository reservation must not outlive the decision.
    assert await db.claim("worker") is None
    replacement = await db.create("Unrelated next task")
    assert replacement.id != task.id


async def test_expiry_releases_the_gated_task_instead_of_stranding_it(db):
    task = await gated_task(db)
    decision = await db.ensure_task_approval_decision(task, "High-risk plan", plan_hash(PLAN))
    await expire(db, decision.id)
    await db.expire_stale_decisions()

    assert (await db.get(task.id)).status == "failed"
    assert await db.claim("worker") is None


async def test_expired_approval_card_is_recorded_and_the_task_is_released(db):
    task = await gated_task(db)
    decision = await db.ensure_task_approval_decision(task, "High-risk plan", plan_hash(PLAN))
    await expire(db, decision.id)

    with pytest.raises(ValueError, match="Decision expired"):
        await resolve_decision(decision.id, "approve")

    assert [d.state for d in await decision_inbox(state="expired")] == ["expired"]
    assert (await db.get(task.id)).status == "failed"


async def test_an_approval_card_for_an_old_plan_cannot_approve_the_current_one(db):
    task = await gated_task(db)
    decision = await db.ensure_task_approval_decision(task, "High-risk plan", plan_hash(PLAN))
    await db.update(task.id, plan_json=json.dumps({"steps": ["a different plan"], "risk": "high"}))

    with pytest.raises(ValueError, match="different plan version"):
        await resolve_decision(decision.id, "approve")

    # Nothing was authorised: the card stays open and the task stays gated.
    assert (await db.get(task.id)).status == "awaiting_approval"
    assert (await db.get(task.id)).approved_plan_hash is None
    assert [d.state for d in await decision_inbox(state="open")] == ["open"]


async def test_a_failed_task_transition_rolls_the_decision_back(db):
    task = await gated_task(db)
    decision = await db.ensure_task_approval_decision(task, "High-risk plan", plan_hash(PLAN))
    await db.update(task.id, plan_json=json.dumps({"steps": ["moved on"], "risk": "high"}))

    with pytest.raises(ValueError):
        await resolve_decision(decision.id, "approve")

    reopened = (await decision_inbox(state="open"))[0]
    assert reopened.id == decision.id
    assert reopened.decided_at is None


async def test_repeated_identical_answer_does_not_duplicate_the_task_event(db):
    task = await gated_task(db)
    decision = await db.ensure_task_approval_decision(task, "High-risk plan", plan_hash(PLAN))

    await resolve_decision(decision.id, "reject", user_id=7)
    await resolve_decision(decision.id, "reject", user_id=7)

    messages = [e.message for e in await db.events(task.id) if e.kind == "decision"]
    assert sum(f"Decision #{decision.id} -> rejected" in m for m in messages) == 1
