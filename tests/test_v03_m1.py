import pytest

from app import main
from app.agents import normalize_lead_plan
from app.config import Settings, settings
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


def test_technical_requirements_files_do_not_force_product_research():
    assert select_skills(
        "Review telegram-lab. Baca README.md dan file requirements atau test yang paling relevan."
    ) == ["engineering"]


def test_explicit_product_requirements_still_select_product_research():
    assert select_skills("Review product requirements untuk onboarding flow") == ["product_research"]


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
    monkeypatch.setattr(settings, "model_aliases_json", '{"team-default":"provider/model-v1"}')
    monkeypatch.setattr(settings, "lead_model", "team-default")
    assert settings.model_for("lead") == "provider/model-v1"


def test_blank_api_operator_user_id_means_unset_not_a_parse_error():
    """Coolify/Compose forward an empty variable as ""; Settings must still start."""
    config = Settings(_env_file=None, api_token="", api_operator_user_id="")
    assert config.api_operator_user_id is None
    assert Settings(_env_file=None, api_operator_user_id=" 7 ").api_operator_user_id == 7


def test_blank_principal_still_fails_closed_when_the_api_token_is_set():
    config = Settings(_env_file=None, worker_enabled=False, api_token="t", api_operator_user_id="   ")
    with pytest.raises(ValueError, match="API_OPERATOR_USER_ID"):
        config.validate_runtime()


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


async def test_health_reports_build_metadata(monkeypatch):
    monkeypatch.setattr(settings, "app_version", "0.4.0-rc1")
    monkeypatch.setattr(settings, "release_sha", "abc123")
    assert await main.health() == {
        "status": "ok",
        "version": "0.4.0-rc1",
        "release_sha": "abc123",
    }


async def test_health_omits_blank_release_sha(monkeypatch):
    monkeypatch.setattr(settings, "app_version", "0.4.0-rc1")
    monkeypatch.setattr(settings, "release_sha", "")
    assert await main.health() == {
        "status": "ok",
        "version": "0.4.0-rc1",
        "release_sha": None,
    }


async def test_budget_warnings_are_durable_and_actionable(db, monkeypatch):
    monkeypatch.setattr(settings, "max_llm_calls", 10)
    task = await db.create("Track budget warnings")
    await db.claim("worker")
    for _ in range(6):
        await db.reserve_call(task.id, "worker", token_reserve=1)
    warnings = [event for event in await db.events(task.id) if event.kind == "budget_warning"]
    assert any("Budget warning 60%" in event.message for event in warnings)
    assert any("No automatic reset" in event.message for event in warnings)


@pytest.mark.parametrize("role", ["lead", "developer", "reviewer"])
def test_all_agent_roles_use_the_same_default_model(role):
    assert settings.model_for(role) == "deepseek/deepseek-v4-flash-0731"
