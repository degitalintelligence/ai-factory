import json
from pathlib import Path
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
    assert "CURRENT FILE AFTER FAILED REPLACEMENT:\n# Demo" in traces[1]
    assert payload not in "\n".join(traces + prompts)
    assert "old_text" in traces[1] and "characters" in traces[1]


async def test_recovery_read_after_failed_replacements_prevents_false_stall(monkeypatch):
    fixture = json.loads((Path(__file__).parent / "fixtures/task_57_stall.json").read_text())
    assert fixture["repository"] == "degitalintelligence/telegram-lab"
    assert fixture["base_sha"] == "455ea8620a49356386aa91df43d8b5595a6d587f"
    assert fixture["failure_stage"] == "developing"

    class Workspace(FakeDeveloperWorkspace):
        def __init__(self):
            self.source = "first edit\n"

        def read_file(self, path):
            if path == "README.md":
                return self.source
            if path == "tests/test_todo.py":
                return "existing tests\n"
            raise ValueError(path)

        def write_file(self, path, content):
            self.source = content
            return f"Wrote {path}"

        def replace_text(self, path, old_text, content):
            if old_text not in self.source:
                raise WorkspaceError("old_text must match exactly once (matches=0); read the current file")
            self.source = self.source.replace(old_text, content)
            return f"Wrote {path}"

        def diff(self):
            return "diff --git a/README.md b/README.md\n"

    stale_actions = [
        DeveloperAction(
            action="replace_text",
            path="README.md",
            old_text=f"stale whole function {index}",
            content="replacement",
        )
        for index, _step in enumerate(fixture["failed_replacement_steps"])
    ]
    script = iter(
        [
            DeveloperAction(action="read_file", path="README.md"),
            DeveloperAction(action="write_file", path="README.md", content="first edit\n"),
            *stale_actions,
            DeveloperAction(action="read_file", path="README.md"),
            DeveloperAction(action="read_file", path="tests/test_todo.py"),
            DeveloperAction(
                action="replace_text",
                path="README.md",
                old_text="first edit",
                content="recovered edit",
            ),
            DeveloperAction(action="git_diff"),
            DeveloperAction(action="finish"),
        ]
    )
    prompts = []

    async def model(**kwargs):
        prompts.append(kwargs["user"])
        return next(script)

    monkeypatch.setattr(agents, "json_completion", model)
    monkeypatch.setattr(settings, "max_developer_stall_steps", 8)
    monkeypatch.setattr(settings, "max_dev_steps", 20)
    workspace = Workspace()

    result = await agents.developer_loop(
        workspace=workspace,
        requirement="Recover from stale replacements",
        plan=LeadPlan(objective="Edit", acceptance_criteria=["Edited"]),
    )

    assert result == "Implementation completed"
    assert workspace.source == "recovered edit\n"
    assert len(fixture["recovery_read_steps"]) == 2
    # The next model turn gets current source without spending another tool call.
    assert "CURRENT FILE AFTER FAILED REPLACEMENT:\nfirst edit" in prompts[3]


async def test_finish_collects_successful_diff_after_the_latest_mutation(monkeypatch):
    class Workspace(FakeDeveloperWorkspace):
        diffs = 0

        def diff(self):
            self.diffs += 1
            return "reviewable diff"

        def write_file(self, *args):
            return "Wrote README.md"

    script = iter(
        [
            DeveloperAction(action="read_file", path="README.md"),
            DeveloperAction(action="git_diff"),
            DeveloperAction(action="write_file", path="README.md", content="changed"),
            DeveloperAction(action="finish", note="ready for mandatory checks"),
        ]
    )
    prompts = []

    async def model(**kwargs):
        prompts.append(kwargs["user"])
        return next(script)

    monkeypatch.setattr(agents, "json_completion", model)
    workspace = Workspace()
    traces = []

    async def trace(step, record):
        traces.append(record)

    result = await agents.developer_loop(
        workspace=workspace,
        trace=trace,
        requirement="Small edit",
        plan=LeadPlan(objective="Edit", acceptance_criteria=["Edited"]),
    )
    assert result == "ready for mandatory checks" and len(prompts) == 4
    assert workspace.diffs == 2
    assert "controller finish checkpoint" in traces[-2]
    assert "ACTION: finish" in traces[-1]


async def test_existing_file_whole_write_requires_reading_that_exact_path(monkeypatch):
    class Workspace(FakeDeveloperWorkspace):
        def __init__(self):
            self.test_source = "def test_existing(): pass\n"

        def list_files(self):
            return "README.md\ntests/test_todo.py\n"

        def read_file(self, path):
            if path == "README.md":
                return "# Demo\n"
            if path == "tests/test_todo.py":
                return self.test_source
            raise ValueError(path)

        def write_file(self, path, content):
            assert path == "tests/test_todo.py"
            self.test_source = content
            return f"Wrote {path}"

        def diff(self):
            return "diff --git a/tests/test_todo.py b/tests/test_todo.py\n"

    script = iter(
        [
            DeveloperAction(action="read_file", path="README.md"),
            DeveloperAction(action="write_file", path="tests/test_todo.py", content="new tests\n"),
            DeveloperAction(action="read_file", path="tests/test_todo.py"),
            DeveloperAction(
                action="write_file",
                path="tests/test_todo.py",
                content="def test_existing(): pass\ndef test_count(): pass\n",
            ),
            DeveloperAction(action="git_diff"),
            DeveloperAction(action="finish"),
        ]
    )
    traces = []

    async def model(**_kwargs):
        return next(script)

    async def trace(_step, record):
        traces.append(record)

    monkeypatch.setattr(agents, "json_completion", model)
    workspace = Workspace()
    await agents.developer_loop(
        workspace=workspace,
        requirement="Add todo count tests",
        plan=LeadPlan(objective="Edit tests", acceptance_criteria=["Coverage preserved"]),
        trace=trace,
    )

    assert "Existing file must be read before whole-file write" in traces[1]
    assert "test_existing" in workspace.test_source
    assert "test_count" in workspace.test_source


async def test_repair_iteration_cannot_finish_without_a_new_mutation(monkeypatch):
    fixture = json.loads((Path(__file__).parent / "fixtures/task_60_repair.json").read_text())
    assert fixture["task_id"] == 60
    assert fixture["developer_allocations"] == [30, 8, 11]
    assert fixture["usage"]["calls"] == 50

    class Workspace(FakeDeveloperWorkspace):
        def __init__(self):
            self.source = "old\n"

        def read_file(self, path):
            assert path == "README.md"
            return self.source

        def replace_text(self, path, old_text, content):
            assert path == "README.md" and old_text in self.source
            self.source = self.source.replace(old_text, content)
            return "Wrote README.md"

        def diff(self):
            return "diff --git a/README.md b/README.md\n"

    script = iter(
        [
            DeveloperAction(action="read_file", path="README.md"),
            DeveloperAction(action="git_diff"),
            DeveloperAction(action="finish"),
            DeveloperAction(action="replace_text", path="README.md", old_text="old", content="fixed"),
            DeveloperAction(action="finish"),
        ]
    )
    traces = []

    async def model(**_kwargs):
        return next(script)

    async def trace(_step, record):
        traces.append(record)

    monkeypatch.setattr(agents, "json_completion", model)
    workspace = Workspace()
    result = await agents.developer_loop(
        workspace=workspace,
        requirement="Repair rejected change",
        plan=LeadPlan(objective="Repair", acceptance_criteria=["Issue fixed"]),
        reviewer_feedback=["Tests still fail"],
        trace=trace,
    )

    assert result == "Implementation completed"
    assert any("repair feedback requires at least one successful file mutation" in item for item in traces)
    assert workspace.source == "fixed\n"
    assert agents.PROMPT_VERSION_DEVELOPER == "developer-v9"


def test_tracked_runtime_artifact_is_binding_initial_feedback():
    fixture = json.loads((Path(__file__).parent / "fixtures/task_60_repair.json").read_text())
    feedback = orchestrator.initial_source_hygiene_feedback(
        "README.md\nbot.py\ntests/test_todo.py\ntodos.db\n",
        [],
    )

    assert any("Deterministic repair required" in item for item in feedback)
    assert any("todos.db" in item for item in feedback)
    assert fixture["base_sha"] == "455ea8620a49356386aa91df43d8b5595a6d587f"


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


async def test_finish_checkpoint_failure_requires_repair_and_never_bypasses_hygiene(monkeypatch):
    class Workspace(FakeDeveloperWorkspace):
        unsafe = True

        def diff(self):
            if self.unsafe:
                raise WorkspaceError("Remove generated/runtime artifact: todos.db")
            return "clean complete diff"

        def delete_file(self, path):
            assert path == "todos.db"
            self.unsafe = False
            return "Deleted todos.db"

    script = iter(
        [
            DeveloperAction(action="read_file", path="README.md"),
            DeveloperAction(action="finish"),
            DeveloperAction(action="delete_file", path="todos.db"),
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
        requirement="Edit",
        plan=LeadPlan(objective="Edit", acceptance_criteria=["Edited"]),
        trace=trace,
    )
    assert "Final diff collection failed" in prompts[2]
    assert "todos.db" in prompts[2]
    assert sum("controller finish checkpoint" in item for item in traces) == 1
    assert "ACTION: finish" in traces[-1]


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


async def test_step_limit_collects_latest_diff_without_an_extra_model_call(monkeypatch):
    class Workspace(FakeDeveloperWorkspace):
        def write_file(self, *args):
            return "Wrote README.md"

        def diff(self):
            return "complete latest source diff"

    actions = iter(
        [
            DeveloperAction(action="read_file", path="README.md"),
            DeveloperAction(action="write_file", path="README.md", content="latest repair"),
        ]
    )
    prompts, traces = [], []

    async def model(**kwargs):
        prompts.append(kwargs["user"])
        return next(actions)

    async def trace(step, record):
        traces.append((step, record))

    monkeypatch.setattr(settings, "max_dev_steps", 2)
    monkeypatch.setattr(agents, "json_completion", model)
    note = await agents.developer_loop(
        workspace=Workspace(),
        requirement="Edit",
        plan=LeadPlan(objective="Edit", acceptance_criteria=["Edited"]),
        trace=trace,
    )
    assert "Completion has not been established" in note
    assert len(prompts) == 2
    assert "CONTROLLER STEP: 2/2" in prompts[-1]
    assert "controller step-limit checkpoint" in traces[-2][1]
    assert "complete latest source diff" in traces[-2][1]
    assert traces[-1][0] == 2 and "ACTION: handoff" in traces[-1][1]


@pytest.mark.parametrize("unsafe", [False, True])
async def test_step_limit_cannot_handoff_empty_or_unsafe_source(monkeypatch, unsafe):
    class Workspace(FakeDeveloperWorkspace):
        def write_file(self, *args):
            return "Wrote README.md"

        def diff(self):
            if unsafe:
                raise WorkspaceError("Remove generated/runtime artifact: todos.db")
            return ""

    actions = iter(
        [
            DeveloperAction(action="read_file", path="README.md"),
            DeveloperAction(action="write_file", path="README.md", content="unchanged"),
        ]
    )

    async def model(**kwargs):
        return next(actions)

    monkeypatch.setattr(settings, "max_dev_steps", 2)
    monkeypatch.setattr(agents, "json_completion", model)
    with pytest.raises(RuntimeError, match="runtime artifact" if unsafe else "without an evaluable diff"):
        await agents.developer_loop(
            workspace=Workspace(),
            requirement="Edit",
            plan=LeadPlan(objective="Edit", acceptance_criteria=["Edited"]),
        )


async def test_developer_zero_allocation_fails_before_model_call(monkeypatch):
    async def never(**_kwargs):
        raise AssertionError("No model call is allowed")

    monkeypatch.setattr(agents, "json_completion", never)
    with pytest.raises(BudgetExceeded, match="No Developer call allocation remains"):
        await agents.developer_loop(
            workspace=FakeDeveloperWorkspace(),
            requirement="Edit",
            plan=LeadPlan(objective="Edit", acceptance_criteria=["Edited"]),
            step_limit=0,
        )


async def test_step_limit_does_not_handoff_read_only_exhaustion(monkeypatch):
    async def model(**kwargs):
        return DeveloperAction(action="read_file", path="README.md")

    monkeypatch.setattr(settings, "max_dev_steps", 1)
    monkeypatch.setattr(agents, "json_completion", model)
    with pytest.raises(RuntimeError, match="without an evaluable diff"):
        await agents.developer_loop(
            workspace=FakeDeveloperWorkspace(),
            requirement="Edit",
            plan=LeadPlan(objective="Edit", acceptance_criteria=["Edited"]),
        )


def test_reviewer_prompt_treats_tracked_runtime_artifact_deletion_as_corrective():
    system, _ = agents.reviewer_request(
        requirement="Small edit",
        plan=LeadPlan(objective="Edit", acceptance_criteria=["Edited"]),
        diff="diff --git a/todos.db b/todos.db\ndeleted file mode 100644",
        test_output='{"issues":["Commands modified source or left test artifacts: todos.db"]}',
        hygiene_issues=["Commands modified source or left test artifacts: todos.db"],
    )
    assert "deletion is corrective" in system
    assert "Do not recommend restoring or ignoring it" in system
    assert "No PR exists at this review stage" in system
    assert agents.PROMPT_VERSION_REVIEWER == "reviewer-v3"


async def test_identical_stale_replacement_rolls_over_before_generic_stall(monkeypatch):
    fixture = json.loads((Path(__file__).parent / "fixtures/task_61_stale_replace.json").read_text())
    assert fixture["task_id"] == 61
    assert fixture["successful_mutation_step"] == 3
    assert len(fixture["stale_replace_steps"]) == 8

    class Workspace(FakeDeveloperWorkspace):
        def __init__(self):
            self.source = "current source\n"

        def read_file(self, path):
            assert path == "README.md"
            return self.source

        def write_file(self, path, content):
            self.source = content
            return f"Wrote {path}"

        def replace_text(self, path, old_text, content):
            raise WorkspaceError("old_text must match exactly once (matches=0); read current file")

    stale = DeveloperAction(
        action="replace_text",
        path="README.md",
        old_text="stale whole function",
        content="replacement",
    )
    script = iter(
        [
            DeveloperAction(action="read_file", path="README.md"),
            DeveloperAction(action="write_file", path="README.md", content="partial edit\n"),
            stale,
            stale,
        ]
    )
    prompts = []

    async def model(**kwargs):
        prompts.append(kwargs["user"])
        return next(script)

    monkeypatch.setattr(agents, "json_completion", model)
    monkeypatch.setattr(settings, "max_developer_stall_steps", 8)

    with pytest.raises(agents.DeveloperStalled, match="same stale replace_text anchor"):
        await agents.developer_loop(
            workspace=Workspace(),
            requirement="Repair a feature",
            plan=LeadPlan(objective="Edit", acceptance_criteria=["Edited"]),
        )

    assert len(prompts) == 4
    assert "CURRENT FILE AFTER FAILED REPLACEMENT:\npartial edit" in prompts[-1]
    assert agents.PROMPT_VERSION_DEVELOPER == "developer-v9"


async def test_different_ambiguous_replacements_roll_over_on_the_same_path(monkeypatch):
    class Workspace(FakeDeveloperWorkspace):
        def replace_text(self, path, old_text, content):
            raise WorkspaceError("old_text must match exactly once (matches=2); read the current file")

    script = iter(
        [
            DeveloperAction(action="read_file", path="README.md"),
            DeveloperAction(
                action="replace_text",
                path="README.md",
                old_text="first ambiguous anchor",
                content="replacement",
            ),
            DeveloperAction(
                action="replace_text",
                path="README.md",
                old_text="second ambiguous anchor",
                content="replacement",
            ),
        ]
    )
    prompts = []

    async def model(**kwargs):
        prompts.append(kwargs["user"])
        return next(script)

    monkeypatch.setattr(agents, "json_completion", model)

    with pytest.raises(agents.DeveloperStalled, match="ambiguous or stale replace_text anchors"):
        await agents.developer_loop(
            workspace=Workspace(),
            requirement="Repair duplicated tests",
            plan=LeadPlan(objective="Repair", acceptance_criteria=["Tests remain unique"]),
        )

    assert len(prompts) == 3
    assert "CURRENT FILE AFTER FAILED REPLACEMENT:\n# Demo" in prompts[-1]


async def test_tracked_runtime_artifact_forces_cleanup_before_edits_or_tests(monkeypatch):
    class Workspace(FakeDeveloperWorkspace):
        writes = 0

        def list_files(self):
            return "README.md\ntodos.db\n"

        def run_command(self, command):
            raise WorkspaceError("Remove generated/runtime artifact: todos.db")

        def replace_text(self, *args):
            self.writes += 1
            return "Wrote README.md"

    script = iter(
        [
            DeveloperAction(action="read_file", path="README.md"),
            DeveloperAction(action="replace_text", path="README.md", old_text="# Demo", content="# Changed"),
            DeveloperAction(action="run_command", command="python -m pytest -q"),
        ]
    )
    prompts = []

    async def model(**kwargs):
        prompts.append(kwargs["user"])
        return next(script)

    monkeypatch.setattr(agents, "json_completion", model)
    workspace = Workspace()

    with pytest.raises(agents.DeveloperStalled, match="artifact cleanup was not performed"):
        await agents.developer_loop(
            workspace=workspace,
            requirement="Repair tests",
            plan=LeadPlan(objective="Repair", acceptance_criteria=["Clean tests"]),
        )

    assert workspace.writes == 0
    assert "CONTROLLER BLOCKER" in prompts[-1]
    assert "delete_file" in prompts[-1] and "todos.db" in prompts[-1]


async def test_explicit_runtime_artifact_cleanup_unblocks_repair(monkeypatch):
    class Workspace(FakeDeveloperWorkspace):
        def __init__(self):
            self.source = "# Demo\n"
            self.artifact_exists = True

        def list_files(self):
            return "README.md\ntodos.db\n" if self.artifact_exists else "README.md\n"

        def read_file(self, path):
            assert path == "README.md"
            return self.source

        def run_command(self, command):
            if self.artifact_exists:
                raise WorkspaceError("Remove generated/runtime artifact: todos.db")
            return "tests passed"

        def delete_file(self, path):
            assert path == "todos.db"
            self.artifact_exists = False
            return "Deleted todos.db"

        def replace_text(self, path, old_text, content):
            assert path == "README.md"
            self.source = self.source.replace(old_text, content)
            return "Wrote README.md"

        def diff(self):
            if self.artifact_exists:
                raise WorkspaceError("Remove generated/runtime artifact: todos.db")
            return "diff --git a/README.md b/README.md\n"

    script = iter(
        [
            DeveloperAction(action="read_file", path="README.md"),
            DeveloperAction(action="delete_file", path="todos.db"),
            DeveloperAction(
                action="replace_text",
                path="README.md",
                old_text="# Demo",
                content="# Repaired",
            ),
            DeveloperAction(action="run_command", command="python -m pytest -q"),
            DeveloperAction(action="git_diff"),
            DeveloperAction(action="finish"),
        ]
    )

    async def model(**kwargs):
        return next(script)

    monkeypatch.setattr(agents, "json_completion", model)
    workspace = Workspace()
    result = await agents.developer_loop(
        workspace=workspace,
        requirement="Repair tests",
        plan=LeadPlan(objective="Repair", acceptance_criteria=["Clean tests"]),
    )

    assert result == "Implementation completed"
    assert workspace.artifact_exists is False
    assert workspace.source == "# Repaired\n"
