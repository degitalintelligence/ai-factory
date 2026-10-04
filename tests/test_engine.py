import hashlib
import json

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
        "fail_publish": False,
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
            state["pr_body"] = kwargs["body"]
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
