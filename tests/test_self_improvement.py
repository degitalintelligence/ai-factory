"""Self-improvement task type: brief is mandatory, sensitive scope always stops for approval."""

import pytest
from pydantic import ValidationError

from app.contracts import create_self_improvement
from app.schemas import ImprovementOutcome, SelfImprovementBrief, TaskKind, TaskRequest


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


def outcome(**overrides):
    payload = {
        "before": "3 retries per malformed response",
        "after": "1 retry per malformed response across 20 tasks",
        "evidence": ["model_runs show retries 3->1", "task events show no schema failures"],
        "conclusion": "retain",
        "window": "20 tasks after merge",
    }
    return ImprovementOutcome(**{**payload, **overrides})


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


# --- Outcome measurement closes the loop (requirement v0.3 §9) -------------------


async def completed_self_improvement(db):
    task = await db.create("Improve the retry parser", kind="self_improvement", brief=brief())
    await db.update(task.id, status="completed")
    return task


async def test_outcome_measurement_is_recorded_after_completion(db):
    task = await completed_self_improvement(db)
    lesson = await db.record_outcome(task.id, outcome())
    # One artifact, one timeline event, one shared retained lesson.
    stored = {a.kind: a.content for a in await db.artifacts(task.id)}
    assert '"conclusion":"retain"' in stored["improvement_outcome"]
    assert any("Outcome recorded: retain" in e.message for e in await db.events(task.id))
    assert lesson.key == f"improvement.task-{task.id}" and lesson.scope == "lesson"
    assert lesson.state == "active" and lesson.version == 1
    assert lesson.owner is None  # retained learning is shared inside the tenant
    assert lesson.evidence_ref == f"task:{task.id}"


async def test_outcome_requires_a_completed_task(db):
    task = await db.create("Improve the retry parser", kind="self_improvement", brief=brief())
    with pytest.raises(ValueError, match="measure the outcome after completion"):
        await db.record_outcome(task.id, outcome())


async def test_outcome_applies_only_to_self_improvement(db):
    task = await db.create("Normal engineering work")
    await db.update(task.id, status="completed")
    with pytest.raises(ValueError, match="self_improvement tasks only"):
        await db.record_outcome(task.id, outcome())


async def test_a_repeated_outcome_is_one_action_and_a_revised_one_is_refused(db):
    task = await completed_self_improvement(db)
    first = await db.record_outcome(task.id, outcome())
    again = await db.record_outcome(task.id, outcome())
    assert again.id == first.id and again.version == 1  # no supersede churn
    with pytest.raises(ValueError, match="already recorded"):
        await db.record_outcome(task.id, outcome(conclusion="rollback"))


async def test_outcome_rejects_a_secret_in_the_measurement(db):
    task = await completed_self_improvement(db)
    leak = outcome(after="token ghp_ABCDEFGHIJKLMNOPQRSTUVWX leaked into logs")
    with pytest.raises(ValueError, match="credentials"):
        await db.record_outcome(task.id, leak)
    # Nothing was recorded: no artifact, no event, no lesson.
    assert not [a for a in await db.artifacts(task.id) if a.kind == "improvement_outcome"]
