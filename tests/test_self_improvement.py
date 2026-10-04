"""Self-improvement task type: brief is mandatory, sensitive scope always stops for approval."""

import pytest
from pydantic import ValidationError

from app.contracts import create_self_improvement
from app.schemas import SelfImprovementBrief, TaskKind, TaskRequest


def brief(**overrides):
    payload = {
        "problem": "Prompt retries loop three times on malformed JSON",
        "evidence": ["events for task 41 show 3 retries"],
        "hypothesis": "Tightening the schema will cut retries",
        "scope": "Reword the developer prompt only",
        "baseline": "3 retries per malformed response, 41 tasks observed",
        "touched_areas": ["app/agents.py"],
        "rollback_plan": "Revert the prompt commit",
    }
    return SelfImprovementBrief(**{**payload, **overrides})


def test_brief_rejects_every_missing_mandatory_field():
    for field in ("problem", "evidence", "hypothesis", "scope", "baseline", "rollback_plan"):
        with pytest.raises(ValidationError):
            brief(**{field: "" if field != "evidence" else []})


def test_task_request_requires_a_brief_for_self_improvement():
    with pytest.raises(ValidationError, match="require a SelfImprovementBrief"):
        TaskRequest(requirement="Improve the parser", kind=TaskKind.SELF_IMPROVEMENT)
    # A brief on a normal engineering task is a category error, not a silent no-op.
    with pytest.raises(ValidationError, match="only valid for self_improvement"):
        TaskRequest(requirement="Improve the parser", kind=TaskKind.ENGINEERING, brief=brief())
    ok = TaskRequest(requirement="Improve the parser", kind=TaskKind.SELF_IMPROVEMENT, brief=brief())
    assert ok.kind == TaskKind.SELF_IMPROVEMENT


@pytest.mark.parametrize(
    "area",
    ["app/config.py", "sandbox/server.py", ".github/workflows/ci.yml", "Dockerfile", "app/gates.py"],
)
def test_policy_and_boundary_areas_are_flagged_sensitive(area):
    assert brief(touched_areas=[area]).requires_explicit_approval
    assert brief(touched_areas=[area]).sensitive_areas


@pytest.mark.parametrize("area", ["app/agents.py", "tests/test_agents.py", "README.md"])
def test_ordinary_code_areas_stay_on_the_normal_path(area):
    assert not brief(touched_areas=[area]).requires_explicit_approval
    assert brief(touched_areas=[area]).sensitive_areas == []


def test_sensitive_scope_in_words_counts_even_without_paths():
    # A prompt can describe a credential change without naming the file.
    result = brief(touched_areas=["app/llm.py"], scope="Rotate the provider credential scope")
    assert result.requires_explicit_approval
    assert "credential" in result.sensitive_areas


def test_a_sensitive_file_named_only_in_scope_is_still_detected():
    # Regression: paths were matched against touched_areas only, so naming app/gates.py
    # inside the scope text slipped through the approval floor.
    result = brief(touched_areas=[], scope="Lower the approval threshold in app/gates.py")
    assert result.requires_explicit_approval
    assert "app/gates.py" in result.sensitive_areas


def test_sensitive_files_are_detected_through_a_path_prefix():
    # A branch or nested path must not hide a sensitive file.
    assert brief(touched_areas=["packages/core/app/security.py"]).requires_explicit_approval
    assert brief(scope="Edit .github/workflows/ci.yml to skip the gate").requires_explicit_approval


async def test_self_improvement_task_is_created_isolated_with_durable_brief(db):
    task, needs_approval, areas = await create_self_improvement(brief(), "lab")
    assert task.kind == "self_improvement"
    # Isolated branch, never main.
    assert task.branch == f"ai-factory/task-{task.id}"
    assert task.branch != task.base_branch
    stored = {a.kind: a.content for a in await db.artifacts(task.id)}
    assert '"hypothesis"' in stored["self_improvement_brief"]
    assert needs_approval is False and areas == []


async def test_sensitive_self_improvement_reports_that_approval_is_required(db):
    task, needs_approval, areas = await create_self_improvement(brief(touched_areas=["app/gates.py"]), "lab")
    assert needs_approval is True
    assert "app/gates.py" in areas
    assert task.kind == "self_improvement"


async def test_store_refuses_self_improvement_without_a_brief(db):
    with pytest.raises(ValueError, match="require a SelfImprovementBrief"):
        await db.create("Improve the parser", "lab", kind="self_improvement")


async def test_normal_tasks_default_to_engineering(db):
    assert (await db.create("Change the return value")).kind == "engineering"
