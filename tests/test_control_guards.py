import json
from types import SimpleNamespace

import pytest

from app import agents, orchestrator
from app.config import settings
from app.schemas import DeveloperAction, LeadPlan


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
