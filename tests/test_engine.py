import hashlib
import json
from pathlib import Path

import pytest

from app import orchestrator
from app.config import settings
from app.contracts import decision_inbox, resolve_decision
from app.schemas import CommandResult, CriterionEvidence, LeadPlan, ReviewResult
from app.schemas import TestReport as Report
from app.workspace import Workspace


@pytest.fixture
def engine_fakes(repo, monkeypatch):
    workspace, git = repo
    state = {
        "develop": 0,
        "pushes": 0,
        "prs": 0,
        "pr_calls": 0,
        "fail_publish": False,
        "mangle_body": 0,
        "test_exit": 0,
        "risk": "low",
        "questions": [],
    }
    refs = {"main": workspace.base_sha}

    class GitHub:
        async def branch_sha(self, repo, branch):
            return refs.get(branch)

        async def find_pr(self, repo, branch):
            return {"state": "open", "head": {"sha": refs.get(branch)}} if state["prs"] else None

        async def create_pr(self, **kwargs):
            if state["fail_publish"]:
                state["fail_publish"] = False
                raise RuntimeError("Lost GitHub connection after push")
            state["prs"] = 1
            state["pr_calls"] += 1
            body = kwargs["body"]
            if state["mangle_body"]:
                state["mangle_body"] -= 1
                # Simulate GitHub publishing a body that lost a required evidence section.
                body = body.replace("## Test evidence", "## Tests")
            state["pr_body"] = body
            return "https://github.com/owner/repo/pull/1"

        async def pull(self, repo, number):
            return {
                "state": "open",
                "body": state.get("pr_body", ""),
                "head": {"sha": refs.get("feature")},
            }

    def prepare(self, **kwargs):
        self.base_sha = workspace.base_sha
        return self.base_sha

    def push(self, sha):
        refs[self.branch] = sha
        state["pushes"] += 1

    async def plan(*args):
        return LeadPlan(
            objective="Feature",
            acceptance_criteria=["Returns the requested value"],
            risk=state["risk"],
            questions=state["questions"],
        )

    async def develop(*, workspace, **kwargs):
        state["develop"] += 1
        workspace.write_file("app.py", f"def value(): return {state['develop'] + 1}\n")
        workspace.write_file(
            "tests/test_app.py",
            f"from app import value\ndef test_value(): assert value() == {state['develop'] + 1}\n",
        )

    async def review(**kwargs):
        return ReviewResult(
            approved=True,
            summary="Criterion verified",
            criteria=[
                CriterionEvidence(
                    criterion=1, satisfied=True, evidence="tests/test_app.py checks the return value"
                )
            ],
        )

    monkeypatch.setattr(Workspace, "prepare", prepare)
    monkeypatch.setattr(Workspace, "push", push)
    monkeypatch.setattr(
        Workspace,
        "default_tests",
        lambda self: Report(
            results=[CommandResult(command=["pytest"], exit_code=state["test_exit"], output="test evidence")]
        ),
    )
    monkeypatch.setattr(
        Workspace,
        "standalone_tests",
        lambda self, paths: (
            Report(results=[CommandResult(command=["pytest", *paths], exit_code=state["test_exit"])])
            if paths
            else None
        ),
    )
    monkeypatch.setattr(orchestrator, "GitHubAPI", GitHub)
    monkeypatch.setattr(orchestrator, "lead_plan", plan)
    monkeypatch.setattr(orchestrator, "developer_loop", develop)
    monkeypatch.setattr(orchestrator, "review_change", review)
    monkeypatch.setattr(settings, "max_iterations", 1)
    return state, refs


async def execute(db, task_id, owner="w"):
    claimed = await db.claim(owner)
    assert claimed.id == task_id
    try:
        await orchestrator.run_task(task_id, owner=owner)
    finally:
        await db.update(task_id, owner, lease_owner=None, lease_until=None)
    return await db.get(task_id)


async def test_baseline_artifact_redacts_secrets(db, repo, engine_fakes, monkeypatch):
    workspace, git = repo
    secret = "ghp_" + "A" * 30
    monkeypatch.setattr(settings, "github_token", secret)
    # Committed before the task starts, so the sandbox write guard cannot reject it.
    (workspace.path / "README.md").write_text(f"# Demo\ntoken: {secret}\n")
    git("add", ".")
    git("commit", "-m", "Add readme")
    task = await db.create("Change the return value")
    task = await execute(db, task.id)
    baseline = next(a for a in await db.artifacts(task.id) if a.kind == "baseline")
    assert secret not in baseline.content
    assert "[REDACTED]" in baseline.content


async def test_end_to_end_real_git_mocked_external_services(db, repo, engine_fakes):
    state, refs = engine_fakes
    task = await db.create("Change return value and add tests")
    task = await execute(db, task.id)
    assert task.status == "pr_created" and state["prs"] == state["pushes"] == 1
    assert task.head_sha == refs[task.branch]
    assert len(task.review_digest) == 64
    artifacts = await db.artifacts(task.id)
    assert {a.kind for a in artifacts} >= {
        "baseline",
        "plan",
        "tests",
        "standalone_tests",
        "diff",
        "review",
        "gates",
        "post_publication",
    }
    assert "tests/test_app.py" in next(a.content for a in artifacts if a.kind == "diff")
    baseline = json.loads(next(a.content for a in artifacts if a.kind == "baseline"))
    assert baseline["base_sha"] == refs["main"] and len(baseline["context_sha256"]) == 64
    assert "app.py" in baseline["file_inventory"]
    readme = next(d for d in baseline["inspected"] if d["path"] == "README.md")
    assert readme["content"] == "# Demo\n"
    assert hashlib.sha256(readme["content"].encode()).hexdigest() == readme["sha256"]
    standalone = json.loads(next(a.content for a in artifacts if a.kind == "standalone_tests"))
    assert standalone["test_files"] == ["tests/test_app.py"]
    assert json.loads(next(a.content for a in artifacts if a.kind == "post_publication"))["issues"] == []
    assert repo[1]("status", "--porcelain") == ""


async def test_task_52_budget_is_admitted_before_developer_and_reaches_review_and_pr(
    db, repo, engine_fakes, monkeypatch
):
    # Replay the real plan budget/usage; source edits, sandbox and model review use
    # the existing engine harness. This verifies admission, not /todo implementation.
    fixture = json.loads((Path(__file__).parent / "fixtures/task_52_budget.json").read_text())
    state, refs = engine_fakes
    original_develop = orchestrator.developer_loop
    original_review = orchestrator.review_change
    phases = []

    async def plan(*args):
        task_id, owner = orchestrator.run_context.get()
        await db.reserve_call(task_id, owner, token_reserve=22402)
        await db.record_usage(task_id, owner, 13088, 0.01)
        return LeadPlan(
            objective="Feature",
            acceptance_criteria=["Returns the requested value"],
            budget=fixture["plan"]["budget"],
        )

    async def develop(**kwargs):
        task_id, owner = orchestrator.run_context.get()
        current = await db.get(task_id)
        accounting = next(a for a in await db.artifacts(task_id) if a.kind == "plan_budget_accounting")
        assert json.loads(accounting.content)["estimate"]["max_tokens"] == 30000
        assert json.loads(current.plan_json)["budget"]["max_tokens"] > 35490
        phases.append("developing")
        await db.reserve_call(task_id, owner, token_reserve=22402)
        await db.record_usage(task_id, owner, 10000, 0.001)
        return await original_develop(**kwargs)

    async def review(**kwargs):
        task_id, owner = orchestrator.run_context.get()
        phases.append("reviewing")
        await db.reserve_call(task_id, owner, token_reserve=30000)
        await db.record_usage(task_id, owner, 10000, 0.001)
        return await original_review(**kwargs)

    monkeypatch.setattr(orchestrator, "lead_plan", plan)
    monkeypatch.setattr(orchestrator, "developer_loop", develop)
    monkeypatch.setattr(orchestrator, "review_change", review)
    task = await db.create(fixture["requirement"])
    result = await execute(db, task.id)
    assert result.status == "pr_created"
    assert result.project == "lab" and result.repo == "owner/repo"
    assert result.base_sha == refs["main"]
    assert phases == ["developing", "reviewing"]
    assert (result.llm_calls, result.tokens) == (3, 33088)
    assert state["prs"] == state["pushes"] == 1
    gates = [json.loads(a.content) for a in await db.artifacts(task.id) if a.kind == "gates"]
    assert gates and all(g["passed"] for g in gates)


@pytest.mark.parametrize("saved", [False, True])
async def test_inadequate_explicit_or_saved_plan_stops_before_developer(db, engine_fakes, monkeypatch, saved):
    state, _ = engine_fakes
    plan = LeadPlan(objective="Feature", acceptance_criteria=["Returns value"], budget={"max_tokens": 30000})

    async def lead(*args):
        return plan

    monkeypatch.setattr(orchestrator, "lead_plan", lead)
    task = await db.create("Change the return value" + ("" if saved else " Budget: 30000 tokens."))
    if saved:
        await db.update(task.id, plan_json=plan.model_dump_json(), tokens=13088, llm_calls=1)
    result = await execute(db, task.id)
    assert result.status == "failed" and state["develop"] == state["prs"] == 0
    assert LeadPlan.model_validate_json(result.plan_json).budget.max_tokens == 30000
    failure = json.loads(next(a.content for a in await db.artifacts(task.id) if a.kind == "failure"))
    assert failure["stage"] == "plan_budget_admission"
    if saved:
        assert (result.llm_calls, result.tokens) == (1, 13088)


async def test_high_risk_approval_binds_the_admitted_budget(db, engine_fakes, monkeypatch):
    state, _ = engine_fakes

    async def lead(*args):
        return LeadPlan(
            objective="Feature",
            acceptance_criteria=["Returns value"],
            risk="high",
            budget={"max_tokens": 30000},
        )

    monkeypatch.setattr(orchestrator, "lead_plan", lead)
    task = await db.create("Change the return value")
    result = await execute(db, task.id)
    assert result.status == "awaiting_approval" and state["develop"] == 0
    admitted = result.plan_json
    assert LeadPlan.model_validate_json(admitted).budget.max_tokens > 30000
    items = await decision_inbox(state="open", project=task.project)
    item = next(item for item in items if item.task_id == task.id)
    assert f"plan_sha256={orchestrator.plan_hash(admitted)}" in json.loads(item.evidence_json)
    await resolve_decision(item.id, "approve", user_id=7)
    result = await execute(db, task.id, owner="w2")
    assert result.status == "pr_created" and result.plan_json == admitted


async def test_review_only_completes_without_mutation_or_pr(db, engine_fakes, monkeypatch):
    state, refs = engine_fakes

    async def review_plan(*args):
        return LeadPlan(
            objective="Review current behavior",
            acceptance_criteria=["Existing behavior remains valid"],
            risk="low",
            review_only=False,
        )

    async def developer_should_not_run(**kwargs):
        raise AssertionError("developer loop must not run for review-only tasks")

    monkeypatch.setattr(orchestrator, "lead_plan", review_plan)
    monkeypatch.setattr(orchestrator, "developer_loop", developer_should_not_run)
    task = await execute(
        db,
        (
            await db.create(
                "Re-review current main after the merge. "
                "Create a PR only if a correction is required. Do not change application behavior."
            )
        ).id,
    )

    assert task.status == "reviewed"
    assert state["prs"] == state["pushes"] == state["develop"] == 0
    assert task.head_sha is None
    artifacts = await db.artifacts(task.id)
    assert {a.kind for a in artifacts} >= {
        "baseline",
        "plan",
        "tests",
        "test_environment",
        "standalone_tests",
        "diff",
        "review_only",
        "review",
        "gates",
    }


async def test_test_failure_cannot_be_overruled_by_reviewer(db, engine_fakes):
    state, refs = engine_fakes
    state["test_exit"] = 5  # pytest: no tests collected
    task = await execute(db, (await db.create("Feature without real tests")).id)
    assert task.status == "failed"
    assert state["prs"] == state["pushes"] == 0


async def test_publish_checkpoint_recovers_without_recoding_or_duplicate_push(db, engine_fakes):
    state, refs = engine_fakes
    state["fail_publish"] = True
    task = await execute(db, (await db.create("Feature with transient publish outage")).id)
    assert task.status == "failed" and task.head_sha
    assert state["pushes"] == state["develop"] == 1
    await db.resume(task.id, "retry")
    task = await execute(db, task.id, "second")
    assert task.status == "pr_created" and state["prs"] == 1
    assert state["pushes"] == state["develop"] == 1


async def test_feedback_updates_the_existing_pr(db, engine_fakes):
    state, refs = engine_fakes
    task = await execute(db, (await db.create("Add a tested feature")).id)
    first_sha = task.head_sha
    await db.resume(task.id, "feedback", "Return three instead of two")
    task = await execute(db, task.id, "second")
    assert task.status == "pr_created" and task.head_sha != first_sha
    assert state["prs"] == 1 and state["develop"] == 2


@pytest.mark.parametrize("reason,status", [("high", "awaiting_approval"), ("question", "waiting_input")])
async def test_plan_waits_before_execution_when_needed(db, engine_fakes, reason, status):
    state, refs = engine_fakes
    if reason == "high":
        state["risk"] = "high"
    else:
        state["questions"] = ["Which database must be preserved?"]
    task = await execute(db, (await db.create("Migrate user storage safely")).id)
    assert task.status == status and state["develop"] == 0
    if reason == "high":
        cards = await decision_inbox(state="open", project=task.project)
        assert len(cards) == 1 and cards[0].task_id == task.id
        await resolve_decision(cards[0].id, "approve", user_id=7)
        assert (await db.get(task.id)).status == "received"


async def test_post_publication_failure_is_not_reported_as_a_pass(db, engine_fakes):
    state, refs = engine_fakes
    state["mangle_body"] = 1
    task = await execute(db, (await db.create("Publish with incomplete PR evidence")).id)

    assert task.status == "failed"
    assert "Post-publication verification failed" in task.last_message
    assert "## Test evidence" in task.last_message
    assert "Passed gates" not in task.last_message
    # The PR exists and must stay reconcilable, but the task never claimed success.
    assert task.pr_url == "https://github.com/owner/repo/pull/1"
    artifact = json.loads(
        next(a.content for a in await db.artifacts(task.id) if a.kind == "post_publication")
    )
    assert artifact["pr_url"] == task.pr_url
    assert artifact["issues"]


async def test_post_publication_failure_reconciles_the_same_pr_on_retry(db, engine_fakes):
    state, refs = engine_fakes
    state["mangle_body"] = 1
    task = await execute(db, (await db.create("Publish then correct the PR")).id)
    assert task.status == "failed" and task.head_sha

    await db.resume(task.id, "retry")
    task = await execute(db, task.id, "second")

    assert task.status == "pr_created"
    assert task.pr_url == "https://github.com/owner/repo/pull/1"
    assert state["pr_calls"] == 2  # create_pr reconciles the existing PR instead of duplicating it
    assert state["pushes"] == state["develop"] == 1


async def test_base_drift_prevents_publication(db, engine_fakes):
    state, refs = engine_fakes
    refs["main"] = "b" * 40
    task = await execute(db, (await db.create("Test against an outdated base")).id)
    assert task.status == "failed" and "Base branch changed" in task.last_message
    assert state["pushes"] == 0


async def test_revoked_project_policy_stops_task(db, engine_fakes, monkeypatch):
    task = await db.create("Work in registered repository")
    monkeypatch.setattr(settings, "projects_json", '{"lab":{"repo":"other/repository"}}')
    task = await execute(db, task.id)
    assert task.status == "failed" and "policy changed" in task.last_message


async def test_task_44_command_reaches_lead_audit_without_lab_or_old_task_context(
    db, repo, engine_fakes, monkeypatch
):
    from app.audit_scope import SELF_REPOSITORY
    from app.schemas import MemoryWrite

    state, refs = engine_fakes
    requirement = (
        "Audit current main repository ai-factory setelah deployment terbaru. "
        "Verifikasi konfigurasi model, evidence-audit-v1, budget diagnostics, readiness dan test suite. "
        "Bedakan bukti repository dari hal yang belum dapat diverifikasi di runtime. Jangan mengubah apa pun."
    )
    task = await db.create(requirement, project="self", user_id=7)
    lab = await db.create("LAB-POISON old lab task", user_id=7)
    await db.update(lab.id, status="pr_created", last_message="LAB-POISON")
    await db.remember(MemoryWrite(key="global", value="GLOBAL-POISON", source="fixture"), owner=7)
    seen = []

    async def lead(requirement, context):
        saved = await db.get(task.id)
        assert saved.base_sha == refs["main"] and saved.repo == SELF_REPOSITORY
        assert "LAB-POISON" not in context and "GLOBAL-POISON" not in context
        seen.append("lead")
        return LeadPlan(
            objective="Audit repository",
            acceptance_criteria=["Existing behavior remains valid"],
            risk="low",
            review_only=True,
        )

    monkeypatch.setattr(orchestrator, "lead_plan", lead)
    saved = await execute(db, task.id)
    assert saved.status == "reviewed", saved.last_message
    assert saved.project == "self" and saved.repo == SELF_REPOSITORY
    assert seen == ["lead"] and state["develop"] == state["prs"] == state["pushes"] == 0
    artifacts = {a.kind: a.content for a in await db.artifacts(task.id)}
    assert json.loads(artifacts["audit_target"])["base_sha"] == refs["main"]
    assert json.loads(artifacts["resolved_skills"])["workflow"] == "lead/audit"
    assert "tests" in artifacts and "review" in artifacts
