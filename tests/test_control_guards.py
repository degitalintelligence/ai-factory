import json
from types import SimpleNamespace

import pytest

from app import agents, orchestrator
from app.config import settings
from app.schemas import DeveloperAction, LeadPlan, ReviewResult
from app.store import BudgetExceeded
from app.workspace import WorkspaceError


class FakeContextWorkspace:
    def list_files(self):
        return "\n".join(
            [
                "README.md",
                "AGENTS.md",
                "docs/ARCHITECTURE.md",
                "app/deployment.py",
                "app/deployment_preflight.py",
                "tests/test_deployment_preflight.py",
            ]
        )

    def read_file(self, path):
        content = {
            "README.md": "# LioBot\n",
            "AGENTS.md": "A" * 12000,
            "docs/ARCHITECTURE.md": "D" * 12000,
            "app/deployment.py": "P" * 12000,
            "app/deployment_preflight.py": "new module\n",
            "tests/test_deployment_preflight.py": "new test\n",
        }
        if path not in content:
            raise ValueError(path)
        return content[path]


@pytest.mark.asyncio
async def test_self_context_is_compact_and_targeted(db, monkeypatch):
    async def fail_if_previous_tasks_are_loaded(_limit):
        raise AssertionError("self-improvement context must not load prior task transcripts")

    monkeypatch.setattr(orchestrator.store, "list", fail_if_previous_tasks_are_loaded)
    task = SimpleNamespace(
        policy_json='{"repo":"owner/repo"}',
        requirement=("Implement app/deployment_preflight.py and tests/test_deployment_preflight.py"),
        repo="owner/repo",
        id=1,
        kind="self_improvement",
        base_sha="a" * 40,
        base_branch="main",
        project="lab",
        user_id=None,
    )

    context, raw = await orchestrator.repository_context(FakeContextWorkspace(), task)
    baseline = json.loads(raw)
    inspected_paths = [entry["path"] for entry in baseline["inspected"]]

    assert baseline["context_mode"] == "self_improvement_compact"
    assert baseline["context_char_limit"] == settings.self_task_context_chars
    assert len(context) <= settings.self_task_context_chars
    assert inspected_paths[:2] == [
        "app/deployment_preflight.py",
        "tests/test_deployment_preflight.py",
    ]
    assert "README.md" in inspected_paths
    assert "docs/ARCHITECTURE.md" not in inspected_paths
    assert baseline["inspected"][inspected_paths.index("AGENTS.md")]["truncated"] is True


class FakeDeveloperWorkspace:
    def list_files(self):
        return "README.md\n"

    def read_file(self, path):
        assert path == "README.md"
        return "# Demo\n"


@pytest.mark.asyncio
async def test_developer_loop_stops_repeated_no_progress(monkeypatch):
    async def repeated_read(**_kwargs):
        return DeveloperAction(action="read_file", path="README.md")

    monkeypatch.setattr(agents, "json_completion", repeated_read)
    monkeypatch.setattr(settings, "max_developer_stall_steps", 3)
    plan = LeadPlan(objective="Read only", acceptance_criteria=["The file is inspected"])

    with pytest.raises(RuntimeError, match="Developer stalled"):
        await agents.developer_loop(
            workspace=FakeDeveloperWorkspace(),
            requirement="Read README.md",
            plan=plan,
        )


async def test_large_failed_replacement_keeps_error_visible_without_replaying_source(monkeypatch):
    class Workspace(FakeDeveloperWorkspace):
        def replace_text(self, *args):
            raise WorkspaceError("old_text must match exactly once (matches=0); read the current file")

        def diff(self):
            return "reviewed diff"

    payload = "private stale source " * 1000
    script = iter(
        [
            DeveloperAction(action="read_file", path="README.md"),
            DeveloperAction(action="replace_text", path="README.md", old_text=payload, content="new"),
            DeveloperAction(action="git_diff"),
            DeveloperAction(action="finish"),
        ]
    )
    prompts, traces = [], []

    async def model(**kwargs):
        prompts.append(kwargs["user"])
        return next(script)

    async def trace(step, record):
        traces.append(record)

    monkeypatch.setattr(agents, "json_completion", model)
    await agents.developer_loop(
        workspace=Workspace(),
        requirement="Small edit",
        plan=LeadPlan(objective="Edit", acceptance_criteria=["Edited"]),
        trace=trace,
    )
    assert "matches=0" in traces[1][:600]
    assert payload not in "\n".join(traces + prompts)
    assert "old_text" in traces[1] and "characters" in traces[1]


async def test_finish_requires_successful_diff_after_the_latest_mutation(monkeypatch):
    class Workspace(FakeDeveloperWorkspace):
        def diff(self):
            return "reviewable diff"

        def write_file(self, *args):
            return "Wrote README.md"

    script = iter(
        [
            DeveloperAction(action="read_file", path="README.md"),
            DeveloperAction(action="git_diff"),
            DeveloperAction(action="write_file", path="README.md", content="changed"),
            DeveloperAction(action="finish"),
            DeveloperAction(action="git_diff"),
            DeveloperAction(action="finish", note="final diff inspected"),
        ]
    )
    prompts = []

    async def model(**kwargs):
        prompts.append(kwargs["user"])
        return next(script)

    monkeypatch.setattr(agents, "json_completion", model)
    result = await agents.developer_loop(
        workspace=Workspace(),
        requirement="Small edit",
        plan=LeadPlan(objective="Edit", acceptance_criteria=["Edited"]),
    )
    assert result == "final diff inspected" and len(prompts) == 6
    assert "Inspect existing files and git_diff before finish" in prompts[4]


async def test_developer_cannot_consume_the_last_independent_review_call(db, monkeypatch):
    task = await db.create("Small edit")
    await db.claim("w")
    await db.update(task.id, "w", llm_calls=2, plan_json='{"budget":{"max_llm_calls":3}}')

    async def never(**kwargs):
        raise AssertionError("The last call belongs to independent review")

    monkeypatch.setattr(agents, "json_completion", never)
    token = agents.run_context.set((task.id, "w"))
    try:
        with pytest.raises(BudgetExceeded, match="reserving independent review"):
            await agents.developer_loop(
                workspace=FakeDeveloperWorkspace(),
                requirement="Small edit",
                plan=LeadPlan(objective="Edit", acceptance_criteria=["Edited"]),
            )
    finally:
        agents.run_context.reset(token)
    assert (await db.get(task.id)).llm_calls == 2


async def test_gateway_retries_leave_review_and_review_fits_remaining_calls(db, monkeypatch):
    task = await db.create("Small edit")
    await db.claim("w")
    await db.update(task.id, "w", plan_json='{"budget":{"max_llm_calls":4}}')

    class Workspace(FakeDeveloperWorkspace):
        def diff(self):
            return "reviewable diff"

    script = iter(
        [
            DeveloperAction(action="read_file", path="README.md"),
            DeveloperAction(action="git_diff"),
            DeveloperAction(action="finish"),
        ]
    )
    attempts = []

    async def model(**kwargs):
        attempts.append((kwargs["role"], kwargs["max_attempts"]))
        await db.reserve_call(task.id, "w", token_reserve=1)
        return (
            ReviewResult(approved=True, summary="Reviewed") if kwargs["role"] == "reviewer" else next(script)
        )

    monkeypatch.setattr(agents, "json_completion", model)
    token = agents.run_context.set((task.id, "w"))
    try:
        plan = LeadPlan(objective="Edit", acceptance_criteria=["Edited"])
        await agents.developer_loop(workspace=Workspace(), requirement="Small edit", plan=plan)
        result = await agents.review_change(
            requirement="Small edit",
            plan=plan,
            diff="reviewable diff",
            test_output="passing tests",
            hygiene_issues=[],
        )
    finally:
        agents.run_context.reset(token)
    assert result.approved
    assert attempts == [("developer", 3), ("developer", 2), ("developer", 1), ("reviewer", 1)]
    assert (await db.get(task.id)).llm_calls == 4
