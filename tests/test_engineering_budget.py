import json
from pathlib import Path

import pytest

from app.config import settings
from app.engineering_budget import engineering_budget_admission
from app.schemas import LeadPlan
from app.store import BudgetExceeded


def task_52() -> dict:
    return json.loads((Path(__file__).parent / "fixtures/task_52_budget.json").read_text())


def admit(fixture: dict, *, fresh: bool = True, requirement: str | None = None):
    return engineering_budget_admission(
        LeadPlan.model_validate(fixture["plan"]),
        requirement=requirement or fixture["requirement"],
        file_index=fixture["file_index"],
        context=fixture["context"],
        used_calls=1,
        used_tokens=fixture["used_tokens"],
        used_cost=0.01,
        fresh=fresh,
        reviewer_feedback=[],
    )


async def test_task_52_original_budget_blocks_exact_next_reserve(db):
    fixture = task_52()
    task = await db.create(fixture["requirement"])
    await db.claim("w")
    await db.update(task.id, "w", plan_json=json.dumps(fixture["plan"]), tokens=13088)
    with pytest.raises(BudgetExceeded, match="used=13088, next_reserve=22402, limit=30000"):
        await db.reserve_call(task.id, "w", token_reserve=22402)


async def test_task_52_fresh_allocation_funds_development_and_review(db):
    fixture = task_52()
    original = LeadPlan.model_validate(fixture["plan"])
    plan, accounting = admit(fixture)
    assert original.budget.max_tokens == 30000
    assert plan.budget.max_tokens >= accounting["minimum_tokens"] > 35490
    assert plan.budget.max_tokens <= settings.max_total_tokens
    assert not accounting["issues"]
    assert plan.skills == ["engineering"]
    assert plan.constraints == original.constraints
    task = await db.create(fixture["requirement"])
    await db.claim("w")
    await db.update(task.id, "w", plan_json=plan.model_dump_json(), tokens=13088, llm_calls=1)
    # Reserve each baseline action and the independent review against the real store.
    for _ in range(accounting["developer_baseline_calls"]):
        await db.reserve_call(task.id, "w", token_reserve=22402)
        await db.record_usage(task.id, "w", 12000, 0.001)
    await db.reserve_call(task.id, "w", token_reserve=accounting["reviewer_reserve"])
    assert (await db.get(task.id)).llm_calls == 9


@pytest.mark.parametrize("constraint", ["Budget maksimal 30000 token.", "At most 20 calls, $0.50."])
def test_explicit_budget_is_preserved_and_inadequacy_reported(constraint):
    fixture = task_52()
    plan, accounting = admit(fixture, requirement=fixture["requirement"] + " " + constraint)
    assert plan.budget.model_dump() == fixture["plan"]["budget"]
    assert accounting["explicit_budget"] and accounting["issues"]


def test_saved_plan_remains_exact_and_is_not_reallocated():
    fixture = task_52()
    original = LeadPlan.model_validate(fixture["plan"])
    plan, accounting = admit(fixture, fresh=False)
    assert plan.model_dump_json() == original.model_dump_json()
    assert accounting["used"]["tokens"] == 13088
    assert accounting["issues"] and accounting["fresh"] is False


def test_fresh_call_estimate_covers_intake_developer_and_review():
    fixture = task_52()
    fixture["plan"]["budget"]["max_llm_calls"] = 2
    plan, accounting = admit(fixture)
    assert accounting["minimum_calls"] == 9
    assert plan.budget.max_llm_calls == accounting["execution_calls"] == 1 + settings.max_dev_steps + 3
    assert not accounting["issues"]
    saved, accounting = admit(fixture, fresh=False)
    assert saved.budget.max_llm_calls == 2
    assert any("calls:" in issue for issue in accounting["issues"])


@pytest.mark.parametrize("limit,field", [(30000, "max_total_tokens"), (4, "max_llm_calls")])
def test_operator_ceiling_blocks_admission_without_raising_policy(monkeypatch, limit, field):
    monkeypatch.setattr(settings, field, limit)
    fixture = task_52()
    plan, accounting = admit(fixture)
    assert getattr(settings, field) == limit
    assert accounting["effective_limits"][field] <= limit
    assert accounting["issues"]
    if field == "max_total_tokens":
        assert plan.budget.max_tokens == limit


def test_utf8_and_output_allowance_contribute_to_admission(monkeypatch):
    fixture = task_52()
    _, normal = admit(fixture)
    multibyte = dict(fixture, file_index=fixture["file_index"] + "測試.py\n")
    _, unicode = admit(multibyte)
    assert unicode["developer_reserve"] >= normal["developer_reserve"] + len("測試.py\n".encode())
    monkeypatch.setattr(settings, "max_output_tokens", settings.max_output_tokens + 1024)
    _, larger = admit(fixture)
    assert larger["minimum_tokens"] >= normal["minimum_tokens"] + 8 * 1024


def test_already_exhausted_cost_is_blocked_without_new_dollar_authority():
    fixture = task_52()
    _, accounting = engineering_budget_admission(
        LeadPlan.model_validate(fixture["plan"]),
        requirement=fixture["requirement"],
        file_index=fixture["file_index"],
        context=fixture["context"],
        used_calls=1,
        used_tokens=13088,
        used_cost=0.5,
        fresh=True,
        reviewer_feedback=[],
    )
    assert any("reported_cost" in issue for issue in accounting["issues"])
    assert accounting["admitted"]["max_cost_usd"] == 0.5


async def test_task_53_exact_call_failure_and_fresh_iteration_allowance(db):
    actual = json.loads((Path(__file__).parent / "fixtures/task_53_budget.json").read_text())
    task = await db.create(actual["requirement"])
    await db.claim("w")
    await db.update(task.id, "w", plan_json=json.dumps(actual["plan"]), llm_calls=20, tokens=118775)
    with pytest.raises(BudgetExceeded, match="calls=20/20"):
        await db.reserve_call(task.id, "w", token_reserve=22402)
    fixture = task_52()
    fixture["plan"] = actual["plan"]
    fixture["plan"]["budget"] = actual["plan_budget_accounting"]["estimate"]
    fixture["used_tokens"] = actual["plan_budget_accounting"]["used"]["tokens"]
    fresh, accounting = admit(fixture)
    assert fresh.budget.max_llm_calls >= 1 + settings.max_dev_steps + 3 > 20
    assert fresh.budget.max_tokens <= settings.max_total_tokens
    assert not accounting["issues"]
    saved, _ = admit(fixture, fresh=False)
    assert saved.budget.max_llm_calls == 20
