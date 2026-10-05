"""Budget envelope, warning thresholds, and the degradation message v0.3 asks for."""

import json

import pytest

from app.config import settings
from app.store import BUDGET_WARNING_THRESHOLDS, BudgetExceeded


async def test_reservation_rejection_reports_next_call_not_actual_exhaustion(db, monkeypatch):
    monkeypatch.setattr(settings, "max_total_tokens", 120000)
    task = await db.create("Read-only audit")
    await db.claim("w")
    await db.update(task.id, "w", tokens=83281)
    with pytest.raises(BudgetExceeded, match="used=83281, next_reserve=40000, limit=120000"):
        await db.reserve_call(task.id, "w", token_reserve=40000)
    assert (await db.get(task.id)).llm_calls == 0
    await db.reserve_call(task.id, "w", token_reserve=30000)
    assert (await db.get(task.id)).llm_calls == 1


async def test_plan_budget_can_only_lower_the_operator_ceiling(db, monkeypatch):
    monkeypatch.setattr(settings, "max_llm_calls", 20)
    monkeypatch.setattr(settings, "max_total_tokens", 200000)
    monkeypatch.setattr(settings, "max_cost_usd", 5.0)
    task = await db.create("Budgeted feature")
    await db.claim("w")

    assert db.budget_envelope(await db.get(task.id)) == {
        "max_llm_calls": 20,
        "max_total_tokens": 200000,
        "max_cost_usd": 5.0,
    }

    await db.update(
        task.id,
        "w",
        plan_json=json.dumps({"budget": {"max_llm_calls": 4, "max_tokens": 1000, "max_cost_usd": 1.0}}),
    )
    assert db.budget_envelope(await db.get(task.id)) == {
        "max_llm_calls": 4,
        "max_total_tokens": 1000,
        "max_cost_usd": 1.0,
    }

    # A plan asking for more than policy allows must not raise the ceiling.
    await db.update(
        task.id,
        "w",
        plan_json=json.dumps({"budget": {"max_llm_calls": 9999, "max_tokens": 10**9, "max_cost_usd": 999.0}}),
    )
    assert db.budget_envelope(await db.get(task.id))["max_llm_calls"] == 20


async def test_a_malformed_plan_budget_falls_back_to_policy(db, monkeypatch):
    monkeypatch.setattr(settings, "max_llm_calls", 7)
    task = await db.create("Budgeted feature")
    await db.claim("w")
    await db.update(task.id, "w", plan_json="not json at all")
    assert db.budget_envelope(await db.get(task.id))["max_llm_calls"] == 7


async def test_every_documented_threshold_fires_exactly_once(db, monkeypatch):
    monkeypatch.setattr(settings, "max_llm_calls", 100)
    task = await db.create("Track thresholds")
    await db.claim("w")

    for _ in range(96):
        await db.reserve_call(task.id, "w", token_reserve=1)

    warnings = [event.message for event in await db.events(task.id) if event.kind == "budget_warning"]
    # Events are newest first; each threshold must be reported exactly once.
    assert [m.split()[2].rstrip(":") for m in warnings] == [
        f"{int(t * 100)}%" for t in reversed(BUDGET_WARNING_THRESHOLDS)
    ]
    assert BUDGET_WARNING_THRESHOLDS == (0.60, 0.80, 0.95)


async def test_the_plan_budget_drives_the_threshold_not_only_the_policy_ceiling(db, monkeypatch):
    """A tight plan budget must warn against the binding limit, not the global one."""
    monkeypatch.setattr(settings, "max_llm_calls", 1000)
    task = await db.create("Tight plan budget")
    await db.claim("w")
    await db.update(task.id, "w", plan_json=json.dumps({"budget": {"max_llm_calls": 10}}))

    for _ in range(6):
        await db.reserve_call(task.id, "w", token_reserve=1)

    warning = next(e.message for e in await db.events(task.id) if e.kind == "budget_warning")
    assert "calls=6/10" in warning
    for _ in range(4):
        await db.reserve_call(task.id, "w", token_reserve=1)
    with pytest.raises(BudgetExceeded):
        await db.reserve_call(task.id, "w", token_reserve=1)


async def test_budget_status_reports_the_binding_envelope_and_next_action(db, monkeypatch):
    monkeypatch.setattr(settings, "max_llm_calls", 4)
    monkeypatch.setattr(settings, "max_total_tokens", 10000)
    monkeypatch.setattr(settings, "max_cost_usd", 1.0)
    task = await db.create("Report the budget")
    await db.claim("w")
    await db.update(task.id, "w", plan_json=json.dumps({"budget": {"max_llm_calls": 2}}))
    await db.reserve_call(task.id, "w", token_reserve=1)
    await db.record_usage(task.id, "w", 8000, None)

    status = await db.budget_status(task.id)
    assert status["envelope"]["max_llm_calls"] == 2
    assert status["requested"] == {"max_llm_calls": 2}
    assert status["used"]["llm_calls"] == 1
    assert status["utilization"] == pytest.approx(0.8)
    assert status["cost_incomplete"] is True
    assert status["exhausted"] is False
    assert status["warning_thresholds"] == list(BUDGET_WARNING_THRESHOLDS)
    # The running dimension names its own remedy.
    assert "narrow the inspected file set" in status["degradation"]


async def test_call_pressure_names_the_parallelism_remedy(db, monkeypatch):
    monkeypatch.setattr(settings, "max_llm_calls", 4)
    monkeypatch.setattr(settings, "max_total_tokens", 10**9)
    task = await db.create("Call pressure")
    await db.claim("w")
    for _ in range(4):
        await db.record_usage(task.id, "w", 0, None)
        await db.reserve_call(task.id, "w", token_reserve=1)

    status = await db.budget_status(task.id)
    assert status["utilization"] == pytest.approx(1.0)
    assert "reduce parallelism" in status["degradation"]


async def test_an_exhausted_task_is_never_silently_reset_by_retry(db, monkeypatch):
    monkeypatch.setattr(settings, "max_llm_calls", 2)
    task = await db.create("Exhausted feature")
    await db.claim("w")
    await db.reserve_call(task.id, "w", token_reserve=1)
    await db.record_usage(task.id, "w", 500, 0.25)
    await db.reserve_call(task.id, "w", token_reserve=1)
    with pytest.raises(BudgetExceeded, match="does not reset lifetime usage"):
        await db.reserve_call(task.id, "w", token_reserve=1)

    await db.update(task.id, "w", status="failed", lease_owner=None, lease_until=None)
    await db.resume(task.id, "retry")
    await db.claim("w2")

    resumed = await db.get(task.id)
    assert (resumed.llm_calls, resumed.tokens, resumed.cost_usd) == (2, 500, 0.25)
    with pytest.raises(BudgetExceeded):
        await db.reserve_call(task.id, "w2", token_reserve=1)


async def test_token_reserve_alone_can_exhaust_a_task(db, monkeypatch):
    monkeypatch.setattr(settings, "max_total_tokens", 1000)
    task = await db.create("Token pressure")
    await db.claim("w")
    await db.record_usage(task.id, "w", 990, None)
    with pytest.raises(BudgetExceeded):
        await db.reserve_call(task.id, "w", token_reserve=100)


async def test_a_warning_names_the_binding_numbers_and_the_recovery_route(db, monkeypatch):
    monkeypatch.setattr(settings, "max_llm_calls", 10)
    monkeypatch.setattr(settings, "max_total_tokens", 100000)
    monkeypatch.setattr(settings, "max_cost_usd", 2.0)
    task = await db.create("Readable warning")
    await db.claim("w")
    for _ in range(6):
        await db.reserve_call(task.id, "w", token_reserve=1)

    warning = next(e.message for e in await db.events(task.id) if e.kind == "budget_warning")
    assert "Budget warning 60%" in warning
    assert "calls=6/10" in warning
    assert "tokens=0/100000" in warning
    assert "reported_cost=$0.0000/$2.00" in warning
    assert "/report" in warning or "/retry" in warning
    assert "Degrade now" in warning
    assert "No automatic reset" in warning
