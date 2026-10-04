import pytest

from app import main
from app.agents import normalize_lead_plan
from app.config import settings
from app.schemas import LeadPlan
from app.skills import SKILLS, select_skills


def test_skill_registry_has_bounded_contracts_and_future_domains():
    ids = set(SKILLS.ids())
    assert {"engineering", "product_research", "marketing", "admin_ops", "finance", "sales_cs"} <= ids
    for skill in SKILLS.all():
        assert skill.version and skill.allowed_tools is not None
        assert skill.evaluator and skill.rollback
        assert skill.execution_state in {"verified", "draft_only", "disabled"}
        assert all("." in tool for tool in skill.allowed_tools)


def test_cross_functional_objective_selects_relevant_skills():
    assert select_skills("Implement a marketing funnel landing page and test its API") == [
        "engineering",
        "marketing",
    ]


def test_lead_plan_normalization_makes_budget_gates_and_rollback_explicit():
    plan = normalize_lead_plan(
        LeadPlan(
            objective="Build the marketing funnel",
            acceptance_criteria=["The funnel is tested"],
            risk="high",
            deployment_required=True,
        ),
        "Implement a marketing funnel landing page and test its API",
    )
    assert plan.skills == ["engineering", "marketing"]
    assert plan.budget.max_llm_calls > 0 and plan.budget.max_tokens > 0
    assert plan.approval_gates == [
        "Dedi approval before implementation",
        "Dedi approval before production deployment",
    ]
    assert plan.rollback_plan


def test_model_alias_resolves_without_hardcoding_it_in_agent_calls(monkeypatch):
    monkeypatch.setattr(settings, "model_aliases_json", '{"bunny-alpha":"provider/model-v1"}')
    monkeypatch.setattr(settings, "lead_model", "bunny-alpha")
    assert settings.model_for("lead") == "provider/model-v1"


def test_v03_channel_neutral_routes_exist():
    paths = {route.path for route in main.app.routes}
    assert {
        "/v1/intents",
        "/v1/intents/{intent_id}/clarification",
        "/v1/intents/{task_id}",
        "/v1/plans/{plan_id}/approve",
        "/v1/decisions",
        "/v1/decisions/{decision_id}/action",
        "/v1/tasks/{task_id}/evidence",
        "/v1/memory/search",
        "/v1/improvements",
        "/v1/health",
        "/v1/ready",
    } <= paths


async def test_budget_warnings_are_durable_and_actionable(db, monkeypatch):
    monkeypatch.setattr(settings, "max_llm_calls", 10)
    task = await db.create("Track budget warnings")
    await db.claim("worker")
    for _ in range(6):
        await db.reserve_call(task.id, "worker", token_reserve=1)
    warnings = [event for event in await db.events(task.id) if event.kind == "budget_warning"]
    assert any("Budget warning 60%" in event.message for event in warnings)
    assert any("no automatic reset" in event.message for event in warnings)


@pytest.mark.parametrize("role", ["lead", "developer", "reviewer"])
def test_all_agent_roles_use_the_same_default_alias(role):
    assert settings.model_for(role) == "stealth/space-bunny-alpha"
