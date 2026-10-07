import hashlib
import json
from pathlib import Path

import pytest

from app import agents, orchestrator
from app.config import settings
from app.contracts import decision_inbox, resolve_decision
from app.schemas import CommandResult, CriterionEvidence, DeveloperAction, LeadPlan, ReviewResult
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
async def test_inadequate_explicit_or_approved_plan_stops_before_developer(
    db, engine_fakes, monkeypatch, saved
):
    state, _ = engine_fakes
    plan = LeadPlan(objective="Feature", acceptance_criteria=["Returns value"], budget={"max_tokens": 30000})

    async def lead(*args):
        return plan

    monkeypatch.setattr(orchestrator, "lead_plan", lead)
    task = await db.create("Change the return value" + ("" if saved else " Budget: 30000 tokens."))
    if saved:
        # An approved plan is never re-funded: readmission only re-funds an
        # unapproved saved plan (retry), so the exact inadequate budget fails closed.
        await db.update(
            task.id,
            plan_json=plan.model_dump_json(),
            tokens=13088,
            llm_calls=1,
            approved_plan_hash=orchestrator.plan_hash(plan.model_dump_json()),
        )
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


async def test_task_53_real_developer_loop_repairs_tool_failures_then_reviews_once(
    db, repo, engine_fakes, monkeypatch
):
    """Script equivalent budget/tool failures, exercising real tools and loop.

    Models, sandbox responses and GitHub remain mocked. No lab code or production
    runtime is executed. Final SQL/Git/test/review/publication gates are real.
    """
    fixture = json.loads((Path(__file__).parent / "fixtures/task_53_budget.json").read_text())
    workspace, git = repo
    state, refs = engine_fakes
    (workspace.path / "todos.db").write_text("disposable test artifact")
    git("add", "-f", "todos.db")
    git("commit", "-m", "Fixture with tracked runtime artifact")
    workspace.base_sha = git("rev-parse", "HEAD")
    refs["main"] = workspace.base_sha
    script = [
        {"action": "read_file", "path": "app.py"},
        {"action": "delete_file", "path": "todos.db"},
        {"action": "write_file", "path": "app.py", "content": "def value(): return 2\n"},
        {"action": "replace_text", "path": "app.py", "old_text": "stale anchor", "content": "new"},
        {"action": "read_file", "path": "app.py"},
        {"action": "replace_text", "path": "app.py", "old_text": "return 2", "content": "return 3"},
        {"action": "read_file", "path": "README.md"},
        {
            "action": "write_file",
            "path": "tests/test_app.py",
            "content": "from app import value\nasync def test_value(): assert value() == 3\n",
        },
        {"action": "read_file", "path": "tests/test_app.py"},
        {"action": "git_diff"},
        {"action": "run_command", "command": "python -m pytest -q"},
        {"action": "read_file", "path": "app.py"},
        {"action": "run_command", "command": "python -m pytest -q"},
        {
            "action": "replace_text",
            "path": "tests/test_app.py",
            "old_text": "stale test anchor",
            "content": "new",
        },
        {"action": "read_file", "path": "tests/test_app.py"},
        {"action": "replace_text", "path": "tests/test_app.py", "old_text": "async def", "content": "def"},
        {"action": "run_command", "command": "python -m pytest -q"},
        {"action": "git_diff"},
        {"action": "read_file", "path": "app.py"},
        {"action": "replace_text", "path": "app.py", "old_text": "return 3", "content": "return 4"},
        {"action": "replace_text", "path": "tests/test_app.py", "old_text": "== 3", "content": "== 4"},
        {"action": "run_command", "command": "python -m pytest -q"},
        {"action": "git_diff"},
        {"action": "finish", "note": "Scoped implementation and test repair completed"},
    ]
    seen = []
    reviewer = orchestrator.review_change

    async def lead(*args):
        task_id, owner = agents.run_context.get()
        await db.reserve_call(task_id, owner, token_reserve=24000)
        await db.record_usage(task_id, owner, 5239, 0.002)
        return LeadPlan(
            objective="Feature",
            acceptance_criteria=["Returns the requested value"],
            budget=fixture["plan_budget_accounting"]["estimate"],
        )

    async def developer_model(**kwargs):
        assert kwargs["role"] == "developer" and 1 <= kwargs["max_attempts"] <= 3
        task_id, owner = agents.run_context.get()
        await db.reserve_call(task_id, owner, token_reserve=24000)
        await db.record_usage(task_id, owner, 6000, 0.002)
        seen.append(kwargs["user"])
        return DeveloperAction.model_validate(script[len(seen) - 1])

    def sandbox_command(self, command):
        self.snapshot()  # Real source hygiene gate, mocked sandbox execution only.
        asynchronous = "async def" in self.read_file("tests/test_app.py")
        return Report(
            results=[
                CommandResult(
                    command=["python", "-m", "pytest"],
                    exit_code=1 if asynchronous else 0,
                    output="async def functions are not natively supported"
                    if asynchronous
                    else "test evidence",
                )
            ]
        ).model_dump_json()

    async def review(**kwargs):
        task_id, owner = agents.run_context.get()
        current = await db.get(task_id)
        assert current.llm_calls == 25 > 20
        await db.reserve_call(task_id, owner, token_reserve=30000)
        await db.record_usage(task_id, owner, 6000, 0.002)
        return await reviewer(**kwargs)

    monkeypatch.setattr(orchestrator, "lead_plan", lead)
    monkeypatch.setattr(orchestrator, "developer_loop", agents.developer_loop)
    monkeypatch.setattr(agents, "json_completion", developer_model)
    monkeypatch.setattr(orchestrator, "review_change", review)
    monkeypatch.setattr(Workspace, "run_command", sandbox_command)
    task = await db.create(fixture["requirement"])
    result = await execute(db, task.id)
    assert result.status == "pr_created" and result.llm_calls == 26
    assert len(seen) == 24 and state["prs"] == state["pushes"] == 1
    traces = [json.loads(a.content) for a in await db.artifacts(task.id) if a.kind == "developer_trace"]
    assert len(traces) == 24
    assert "matches=0" in traces[3]["record"][:600]
    assert "Deleted todos.db" in traces[1]["record"][:600]
    assert "async def functions are not natively supported" in traces[10]["record"][:600]
    assert "async def functions are not natively supported" in traces[12]["record"][:600]
    assert any("use a smaller unique exact anchor" in prompt for prompt in seen)
    assert not (workspace.path / "todos.db").exists()
    assert "def value(): return 4" in workspace.read_file("app.py")
    assert git("status", "--porcelain") == ""


async def test_task_54_finish_is_blocked_by_reported_artifacts_then_repairs(
    db, repo, engine_fakes, monkeypatch
):
    """Real controller/tools/Git/SQL; model, sandbox and GitHub boundaries mocked.

    Reproduces Task 54's passing pytest with sandbox issues. Since the Task 65
    guard fix the serialized run_command report (production shape) is parsed, so
    the controller blocks the first finish itself; the developer then repairs,
    reruns the suite clean, and only then finishes into review.
    """
    fixture = json.loads((Path(__file__).parent / "fixtures/task_54_finish.json").read_text())
    workspace, git = repo
    state, _ = engine_fakes
    monkeypatch.setattr(settings, "max_iterations", 2)
    actions = iter(
        [
            {"action": "read_file", "path": "app.py"},
            {"action": "write_file", "path": "app.py", "content": "def value(): return 2\n"},
            {
                "action": "write_file",
                "path": "tests/test_app.py",
                "content": "from app import value\ndef test_value(): assert value() == 2\n",
            },
            {"action": "run_command", "command": "python -m pytest -q"},
            fixture["first_finish"],
            {"action": "read_file", "path": "tests/test_app.py"},
            {
                "action": "write_file",
                "path": "tests/test_app.py",
                "content": "from app import value\ndef test_value(tmp_path): assert value() == 2\n",
            },
            {"action": "run_command", "command": "python -m pytest -q"},
            {"action": "finish", "note": "Temporary storage fixed; ready for independent review"},
        ]
    )
    prompts, reviews = [], []
    reviewer = orchestrator.review_change

    def sandbox_report(self):
        isolated = "tmp_path" in self.read_file("tests/test_app.py")
        return Report(
            results=Report.model_validate(fixture["sandbox_report"]).results,
            issues=[] if isolated else fixture["sandbox_report"]["issues"],
            environment=fixture["sandbox_report"]["environment"],
        )

    async def model(**kwargs):
        prompts.append(kwargs["user"])
        return DeveloperAction.model_validate(next(actions))

    async def review(**kwargs):
        # Even an approving model cannot override the artifact gate.
        assert state["prs"] == state["pushes"] == 0
        reviews.append(kwargs)
        return await reviewer(**kwargs)

    monkeypatch.setattr(agents, "json_completion", model)
    monkeypatch.setattr(orchestrator, "developer_loop", agents.developer_loop)
    monkeypatch.setattr(orchestrator, "review_change", review)
    monkeypatch.setattr(
        Workspace, "run_command", lambda self, command: sandbox_report(self).model_dump_json()
    )
    monkeypatch.setattr(Workspace, "default_tests", sandbox_report)
    monkeypatch.setattr(Workspace, "standalone_tests", lambda self, paths: sandbox_report(self))
    task = await db.create("Small scoped feature with persistent user data tests")
    result = await execute(db, task.id)
    assert result.status == "pr_created" and result.iteration == 1
    assert len(prompts) == 9 and len(reviews) == 1
    assert "test artifacts: todos.db" in prompts[5]
    assert "todos.db" not in reviews[0]["test_output"]
    artifacts = await db.artifacts(task.id)
    gates = [json.loads(a.content) for a in artifacts if a.kind == "gates"]
    assert len(gates) == 1 and gates[0]["passed"]
    traces = [json.loads(a.content) for a in artifacts if a.kind == "developer_trace"]
    assert sum("controller finish checkpoint" in item["record"] for item in traces) == 1
    assert state["prs"] == state["pushes"] == 1
    assert git("status", "--porcelain") == ""


@pytest.mark.parametrize("repair_succeeds", [True, False])
async def test_task_55_last_step_repair_is_evaluated_without_extra_developer_calls(
    db, repo, engine_fakes, monkeypatch, repair_succeeds
):
    """Script an equivalent supplied tail, with a synthetic unavailable prefix.

    Real tools/Git/SQL/gates; models, sandbox execution and GitHub are mocked.
    No assertion about the unsupplied actual source patch or provider quality.
    """
    fixture = json.loads((Path(__file__).parent / "fixtures/task_55_step_limit.json").read_text())
    workspace, _ = repo
    state, _ = engine_fakes
    monkeypatch.setattr(settings, "max_dev_steps", fixture["max_dev_steps"])
    broken_test = "from app import value\ndef test_value(): assert value() == 999\n"
    fixture_test = (
        "from app import value\n# test environment missing\ndef test_value(): assert value() == 2\n"
    )
    script = [
        {"action": "read_file", "path": "app.py"},
        {"action": "write_file", "path": "app.py", "content": "def value(): return 2\n"},
        {"action": "write_file", "path": "tests/test_app.py", "content": broken_test},
    ]
    # Only steps 21--30 were supplied. This synthetic prefix spends the same
    # remaining steps with valid tools; it is not presented as real task history.
    while len(script) < 20:
        script.append({"action": "write_file", "path": "app.py", "content": "def value(): return 2\n"})
    script.extend(
        [
            {
                "action": "replace_text",
                "path": "tests/test_app.py",
                "old_text": "== 999",
                "content": "== 998",
            },
            {"action": "run_command", "command": "python -m pytest -q --tb=short"},
            {"action": "write_file", "path": "tests/test_app.py", "content": fixture_test},
            {"action": "run_command", "command": "python -m pytest -q --tb=short"},
            {
                "action": "replace_text",
                "path": "tests/test_app.py",
                "old_text": "absent anchor",
                "content": "fixed",
            },
            {"action": "read_file", "path": "tests/test_app.py"},
            {
                "action": "run_command",
                "command": "export TELEGRAM_BOT_TOKEN=dummy_token && python -m pytest -q --tb=short",
            },
            {
                "action": "run_command",
                "command": "TELEGRAM_BOT_TOKEN=dummy_token python -m pytest -q --tb=short",
            },
            {"action": "run_command", "command": "python -m pytest -q --tb=short"},
            {
                "action": "replace_text",
                "path": "tests/test_app.py",
                "old_text": "# test environment missing",
                "content": "# test environment configured",
            },
        ]
    )
    seen, reviews = [], []
    reviewer = orchestrator.review_change

    async def lead(*args):
        task_id, owner = agents.run_context.get()
        await db.reserve_call(task_id, owner, token_reserve=1000)
        await db.record_usage(task_id, owner, 1000, 0.002)
        return LeadPlan(objective="Feature", acceptance_criteria=["Returns the requested value"])

    async def model(**kwargs):
        task_id, owner = agents.run_context.get()
        await db.reserve_call(task_id, owner, token_reserve=1000)
        await db.record_usage(task_id, owner, 1000, 0.001)
        seen.append(kwargs["user"])
        return DeveloperAction.model_validate(script[len(seen) - 1])

    def report(self):
        source = self.read_file("tests/test_app.py")
        passed = repair_succeeds and "# test environment configured" in source
        return Report(
            results=[
                CommandResult(
                    command=["pytest"],
                    exit_code=0 if passed else 1,
                    output="15 passed" if passed else "RuntimeError: TELEGRAM_BOT_TOKEN is required",
                )
            ]
        )

    async def review(**kwargs):
        task_id, owner = agents.run_context.get()
        assert (await db.get(task_id)).llm_calls == 31
        assert state["prs"] == state["pushes"] == 0
        await db.reserve_call(task_id, owner, token_reserve=1000)
        await db.record_usage(task_id, owner, 1000, 0.001)
        reviews.append(kwargs)
        return await reviewer(**kwargs)  # Approving fake cannot override a failed check.

    monkeypatch.setattr(orchestrator, "lead_plan", lead)
    monkeypatch.setattr(orchestrator, "developer_loop", agents.developer_loop)
    monkeypatch.setattr(orchestrator, "review_change", review)
    monkeypatch.setattr(agents, "json_completion", model)
    monkeypatch.setattr(Workspace, "run_commands", lambda self, commands: report(self))
    monkeypatch.setattr(Workspace, "default_tests", report)
    monkeypatch.setattr(Workspace, "standalone_tests", lambda self, paths: report(self))
    task = await db.create("Scoped feature with meaningful regression tests")
    if repair_succeeds:
        result = await execute(db, task.id)
        assert result.status == "pr_created"
    else:
        result = await execute(db, task.id)
        assert result.status == "failed"
        assert "Review iteration limit" in result.last_message
    assert len(seen) == 30 and len(reviews) == 1 and result.llm_calls == 32
    assert state["prs"] == state["pushes"] == int(repair_succeeds)
    assert "CONTROLLER STEP: 30/30" in seen[-1]
    traces = [json.loads(a.content) for a in await db.artifacts(task.id) if a.kind == "developer_trace"]
    assert "matches=0" in traces[24]["record"][:600]
    assert "monkeypatch.setenv" in traces[26]["record"][:600]
    assert "controller step-limit checkpoint" in traces[-2]["record"]
    assert "ACTION: handoff" in traces[-1]["record"]


async def test_task_56_artifact_feedback_drives_test_storage_repair_then_one_pr(
    db, repo, engine_fakes, monkeypatch
):
    fixture = json.loads((Path(__file__).parent / "fixtures/task_56_review.json").read_text())
    workspace, git = repo
    state, refs = engine_fakes
    (workspace.path / "todos.db").write_text("disposable runtime state")
    git("add", "-f", "todos.db")
    git("commit", "-m", "Fixture with tracked runtime database")
    workspace.base_sha = git("rev-parse", "HEAD")
    refs["main"] = workspace.base_sha
    monkeypatch.setattr(settings, "max_iterations", 2)
    feedback_seen = []
    reviews = 0

    async def develop(*, workspace, reviewer_feedback, **kwargs):
        feedback_seen.append(reviewer_feedback)
        workspace.write_file("app.py", "def value(): return 2\n")
        if len(feedback_seen) == 1:
            workspace.write_file(
                "tests/test_app.py",
                "from app import value\n# default-storage\ndef test_value(): assert value() == 2\n",
            )
            workspace.delete_file("todos.db")
            return "Feature implemented; ready for mandatory checks"
        joined = "\n".join(reviewer_feedback)
        assert "Deterministic repair required" in joined
        assert "smoke and application-registration tests" in joined
        assert "tmp_path" in joined and "todos.db" in joined
        assert fixture["review_issue"] not in reviewer_feedback
        workspace.write_file(
            "tests/test_app.py",
            "from app import value\n# temporary-storage\ndef test_value(): assert value() == 2\n",
        )
        return "Test storage repaired; ready for mandatory checks"

    def report(self):
        repaired = "temporary-storage" in self.read_file("tests/test_app.py")
        return Report(
            results=[CommandResult(command=["pytest"], exit_code=0, output=fixture["test_result"])],
            issues=[] if repaired else [fixture["sandbox_issue"]],
        )

    async def review(**kwargs):
        nonlocal reviews
        reviews += 1
        return ReviewResult(
            approved=True,
            summary="Acceptance criteria verified",
            issues=[fixture["review_issue"]] if reviews == 1 else [],
            criteria=[CriterionEvidence(criterion=1, satisfied=True, evidence="tests/test_app.py")],
        )

    monkeypatch.setattr(orchestrator, "developer_loop", develop)
    monkeypatch.setattr(orchestrator, "review_change", review)
    monkeypatch.setattr(Workspace, "default_tests", report)
    monkeypatch.setattr(Workspace, "standalone_tests", lambda self, paths: report(self))
    task = await db.create("Small feature")
    result = await execute(db, task.id)
    assert result.status == "pr_created" and result.iteration == 2
    assert reviews == 2 and state["prs"] == state["pushes"] == 1
    gates = [json.loads(a.content) for a in await db.artifacts(task.id) if a.kind == "gates"]
    assert not gates[0]["passed"] and gates[1]["passed"]
    assert not (workspace.path / "todos.db").exists()
    assert git("status", "--porcelain") == ""


async def test_task_63_reviewer_deletion_complaint_never_reaches_developer_feedback(
    db, repo, engine_fakes, monkeypatch
):
    complaint = "The todos.db file was deleted during testing as a corrective cleanup of test artifacts."
    workspace, git = repo
    state, refs = engine_fakes
    (workspace.path / "todos.db").write_text("disposable runtime state")
    git("add", "-f", "todos.db")
    git("commit", "-m", "Fixture with tracked runtime database")
    workspace.base_sha = git("rev-parse", "HEAD")
    refs["main"] = workspace.base_sha
    monkeypatch.setattr(settings, "max_iterations", 2)
    feedback_seen = []
    reviews = 0

    async def develop(*, workspace, reviewer_feedback, **kwargs):
        feedback_seen.append(list(reviewer_feedback))
        workspace.write_file("app.py", "def value(): return 2\n")
        if len(feedback_seen) == 1:
            workspace.write_file(
                "tests/test_app.py",
                "from app import value\n# default-storage\ndef test_value(): assert value() == 2\n",
            )
            workspace.delete_file("todos.db")
            return "Feature implemented; ready for mandatory checks"
        joined = "\n".join(reviewer_feedback)
        assert "Deterministic repair required" in joined
        assert "Reviewer cannot approve while reporting unresolved issues" in joined
        assert complaint not in joined
        workspace.write_file(
            "tests/test_app.py",
            "from app import value\n# temporary-storage\ndef test_value(): assert value() == 2\n",
        )
        return "Test storage repaired; ready for mandatory checks"

    def report(self):
        repaired = "temporary-storage" in self.read_file("tests/test_app.py")
        return Report(
            results=[CommandResult(command=["pytest"], exit_code=0, output="15 passed in 1.0s")],
            issues=[] if repaired else ["Commands modified source or left test artifacts: todos.db"],
        )

    async def review(**kwargs):
        nonlocal reviews
        reviews += 1
        return ReviewResult(
            approved=True,
            summary="Acceptance criteria verified",
            issues=[complaint] if reviews == 1 else [],
            criteria=[CriterionEvidence(criterion=1, satisfied=True, evidence="tests/test_app.py")],
        )

    monkeypatch.setattr(orchestrator, "developer_loop", develop)
    monkeypatch.setattr(orchestrator, "review_change", review)
    monkeypatch.setattr(Workspace, "default_tests", report)
    monkeypatch.setattr(Workspace, "standalone_tests", lambda self, paths: report(self))
    task = await db.create("Small feature")
    result = await execute(db, task.id)
    assert result.status == "pr_created" and result.iteration == 2
    assert reviews == 2 and state["prs"] == state["pushes"] == 1
    assert complaint not in "\n".join(feedback_seen[1])
    gates = [json.loads(a.content) for a in await db.artifacts(task.id) if a.kind == "gates"]
    assert not gates[0]["passed"] and gates[1]["passed"]
    assert not (workspace.path / "todos.db").exists()
    assert git("status", "--porcelain") == ""


def test_task_56_feedback_drops_only_the_conflicting_reviewer_advice():
    fixture = json.loads((Path(__file__).parent / "fixtures/task_56_review.json").read_text())
    issues = [
        "Mandatory test gate failed (including missing tests, timeout, or test artifacts)",
        fixture["sandbox_issue"],
        "Reviewer cannot approve while reporting unresolved issues",
    ]
    feedback = orchestrator.engineering_repair_feedback(
        issues,
        [fixture["review_issue"], "Keep this unrelated correctness issue"],
    )
    assert feedback[0].startswith("Deterministic repair required")
    assert "smoke and application-registration tests" in feedback[0]
    assert "tmp_path" in feedback[0] and "todos.db" in feedback[0]
    assert fixture["review_issue"] not in feedback
    assert "Keep this unrelated correctness issue" in feedback


def test_task_63_feedback_drops_non_actionable_artifact_deletion_complaints():
    issues = [
        "Mandatory test gate failed (including missing tests, timeout, or test artifacts)",
        "Commands modified source or left test artifacts: todos.db",
        "Reviewer cannot approve while reporting unresolved issues",
    ]
    review_issues = [
        "The todos.db file was deleted during testing as a corrective cleanup of test artifacts.",
        "The todos.db file was deleted as a cleanup step to avoid test artifacts, which is appropriate.",
        "tests/test_app.py recreates todos.db through the default store path; route build_app through tmp_path",
    ]
    feedback = orchestrator.engineering_repair_feedback(issues, review_issues)
    assert feedback[0].startswith("Deterministic repair required")
    assert all("corrective cleanup" not in item and "cleanup step" not in item for item in feedback)
    assert "Reviewer cannot approve while reporting unresolved issues" in feedback
    assert any("route build_app through tmp_path" in item for item in feedback)


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


async def test_retry_warns_the_developer_about_resumed_workspace_state(db, engine_fakes, monkeypatch):
    """Readmitted workspaces (retry) carry uncommitted edits from the previous
    attempt: the developer prompt must say so instead of assuming baseline
    content (Task 66 inherited a corrupted test file silently)."""
    state, refs = engine_fakes
    wrapped = orchestrator.developer_loop

    async def develop(**kwargs):
        state.setdefault("resume_notes", []).append(kwargs.get("resume_note", ""))
        return await wrapped(**kwargs)

    monkeypatch.setattr(orchestrator, "developer_loop", develop)
    state["test_exit"] = 5  # pytest: no tests collected
    task = await execute(db, (await db.create("Feature with a resumed workspace warning")).id)
    assert task.status == "failed" and state["develop"] == 1
    await db.resume(task.id, "retry")
    state["test_exit"] = 0
    task = await execute(db, task.id, "second")
    assert task.status == "pr_created" and state["prs"] == 1
    notes = state["resume_notes"]
    assert len(notes) == 2
    assert notes[0] == ""
    assert "WORKSPACE RESUME WARNING" in notes[1]


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


async def test_task_61_stale_replace_rolls_into_repair_iteration_then_publishes(
    db, repo, engine_fakes, monkeypatch
):
    fixture = json.loads((Path(__file__).parent / "fixtures/task_61_stale_replace.json").read_text())
    workspace, _ = repo
    state, _ = engine_fakes
    monkeypatch.setattr(settings, "max_iterations", 2)
    monkeypatch.setattr(orchestrator, "developer_loop", agents.developer_loop)

    stale = {
        "action": "replace_text",
        "path": "app.py",
        "old_text": "def value(): return 1\n",
        "content": "def value(): return 3\n",
    }
    script = iter(
        [
            {"action": "read_file", "path": "app.py"},
            {
                "action": "replace_text",
                "path": "app.py",
                "old_text": "return 1",
                "content": "return 2",
            },
            stale,
            stale,
            {"action": "read_file", "path": "app.py"},
            {
                "action": "write_file",
                "path": "tests/test_app.py",
                "content": "from app import value\ndef test_value(): assert value() == 2\n",
            },
            {"action": "git_diff"},
            {"action": "finish", "note": "Recovered after stale anchor"},
        ]
    )
    prompts = []

    async def model(**kwargs):
        prompts.append(kwargs["user"])
        return DeveloperAction.model_validate(next(script))

    monkeypatch.setattr(agents, "json_completion", model)
    task = await db.create("Change the return value and add a regression test")
    result = await execute(db, task.id)

    assert fixture["observed_failure"] == "Developer stalled: repeated replace_text on bot.py"
    assert result.status == "pr_created" and result.iteration == 2
    assert state["prs"] == state["pushes"] == 1
    assert "return 2" in workspace.read_file("app.py")
    artifacts = await db.artifacts(task.id)
    stalls = [json.loads(item.content) for item in artifacts if item.kind == "developer_stall"]
    assert len(stalls) == 1 and stalls[0]["recoverable"] is True
    assert any("Recovery iteration required" in prompt for prompt in prompts[4:])


async def test_task_62_budget_reserve_hands_safe_partial_diff_to_tests_and_review(
    db, repo, engine_fakes, monkeypatch
):
    fixture = json.loads((Path(__file__).parent / "fixtures/task_62_budget.json").read_text())
    workspace, git = repo
    state, refs = engine_fakes
    (workspace.path / "todos.db").write_text("disposable runtime state")
    git("add", "-f", "todos.db")
    git("commit", "-m", "Fixture with tracked runtime database")
    workspace.base_sha = git("rev-parse", "HEAD")
    refs["main"] = workspace.base_sha
    monkeypatch.setattr(settings, "max_iterations", 1)
    monkeypatch.setattr(orchestrator, "developer_loop", agents.developer_loop)

    async def lead(*args):
        return LeadPlan(
            objective="Feature",
            acceptance_criteria=["Returns the requested value"],
            budget=fixture["budget"],
        )

    monkeypatch.setattr(orchestrator, "lead_plan", lead)

    script = [
        {"action": "read_file", "path": "app.py"},
        {"action": "delete_file", "path": "todos.db"},
        {
            "action": "replace_text",
            "path": "app.py",
            "old_text": "return 1",
            "content": (
                "return 2\n"
                "# ambiguous marker one\n"
                "# ambiguous marker one\n"
                "# ambiguous marker two\n"
                "# ambiguous marker two"
            ),
        },
        {
            "action": "write_file",
            "path": "tests/test_app.py",
            "content": "from app import value\ndef test_value(): assert value() == 2\n",
        },
        {
            "action": "write_file",
            "path": "tests/test_app.py",
            "content": (
                "from app import value\n"
                "def test_value(): assert value() == 2\n"
                "def test_value(): assert value() == 3\n"
            ),
        },
        {
            "action": "replace_text",
            "path": "app.py",
            "old_text": "# ambiguous marker one",
            "content": "replacement",
        },
        {"action": "read_file", "path": "app.py"},
        {
            "action": "replace_text",
            "path": "app.py",
            "old_text": "# ambiguous marker two",
            "content": "replacement",
        },
    ]
    developer_calls = 0

    async def model(**kwargs):
        nonlocal developer_calls
        action = DeveloperAction.model_validate(script[developer_calls])
        developer_calls += 1
        if developer_calls == len(script):
            task_id, owner = agents.run_context.get()
            current = await db.get(task_id)
            envelope = db.budget_envelope(current)
            assert envelope["max_llm_calls"] == fixture["budget"]["max_llm_calls"] == 50
            await db.update(task_id, owner, llm_calls=49)
        return action

    monkeypatch.setattr(agents, "json_completion", model)
    base_review = orchestrator.review_change

    async def review(**kwargs):
        current = await db.get(task.id)
        assert current.llm_calls == 49
        return await base_review(**kwargs)

    monkeypatch.setattr(orchestrator, "review_change", review)
    task = await db.create("Change the return value with bounded recovery")
    result = await execute(db, task.id)

    assert fixture["usage"]["calls"] == 49
    assert fixture["failure_stage"] == "developing"
    assert result.status == "pr_created" and result.iteration == 1, result.last_message
    assert state["prs"] == state["pushes"] == 1
    assert "return 2" in workspace.read_file("app.py")
    assert workspace.read_file("tests/test_app.py").count("def test_value") == 1
    assert not (workspace.path / "todos.db").exists()
    artifacts = await db.artifacts(task.id)
    stalls = [json.loads(item.content) for item in artifacts if item.kind == "developer_stall"]
    assert stalls[-1]["reason"] == "budget_reserved" and stalls[-1]["recoverable"] is False
    events = await db.events(task.id)
    assert any(event.kind == "developer_budget_reserved" for event in events)
    assert any(event.kind == "developer_handoff" for event in events)
