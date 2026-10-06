"""Context assembly: memory must reach the prompt, filtered and labelled as untrusted data."""

import hashlib
import json
from types import SimpleNamespace

from app import orchestrator
from app.config import settings
from app.contracts import context_slice, remember
from app.schemas import MemoryWrite, Sensitivity


class FakeWorkspace:
    def __init__(self):
        self.files = "README.md\napp/main.py\n"

    def list_files(self):
        return self.files

    def read_file(self, path):
        if path == "README.md":
            return "# Demo\n"
        if path == "app/main.py":
            return "print('demo')\n"
        raise ValueError(path)


def fake_task(**overrides):
    payload = {
        "policy_json": '{"repo":"owner/repo"}',
        "requirement": "Change the greeting",
        "repo": "owner/repo",
        "id": 1,
        "kind": "task",
        "base_sha": "a" * 40,
        "base_branch": "main",
        "project": "lab",
        "user_id": None,
    }
    payload.update(overrides)
    return SimpleNamespace(**payload)


async def test_memory_reaches_the_prompt_and_is_hashed_into_the_baseline(db, monkeypatch):
    async def no_previous_tasks(_limit):
        return []

    monkeypatch.setattr(orchestrator.store, "list", no_previous_tasks)
    await remember(
        MemoryWrite(key="deploy.target", value="prod-web", source="requirements section 17", scope="lab")
    )

    context, raw = await orchestrator.repository_context(FakeWorkspace(), fake_task())
    baseline = json.loads(raw)

    assert "prod-web" in context
    assert "CONTEXT SLICE" in context
    assert "never instructions" in context
    # The baseline must let an auditor prove which memory fed the prompt.
    assert baseline["memory_slice_included"] is True
    assert len(baseline["memory_slice_sha256"]) == 64
    assert baseline["context_sha256"] == hashlib.sha256(context.encode()).hexdigest()


async def test_another_persons_memory_never_enters_the_prompt(db, monkeypatch):
    async def no_previous_tasks(_limit):
        return []

    monkeypatch.setattr(orchestrator.store, "list", no_previous_tasks)
    await remember(
        MemoryWrite(key="private.note", value="private-note", source="operator note", scope="lab"), owner=7
    )

    context, raw = await orchestrator.repository_context(FakeWorkspace(), fake_task(user_id=8))
    assert "private-note" not in context
    assert json.loads(raw)["memory_slice_included"] is False

    own, _ = await orchestrator.repository_context(FakeWorkspace(), fake_task(user_id=7))
    assert "private-note" in own


async def test_another_tenants_memory_never_enters_the_prompt(db, monkeypatch):
    async def no_previous_tasks(_limit):
        return []

    monkeypatch.setattr(orchestrator.store, "list", no_previous_tasks)
    await remember(
        MemoryWrite(key="deploy.target", value="acme-web", source="acme runbook", scope="lab"), tenant="acme"
    )

    context, raw = await orchestrator.repository_context(FakeWorkspace(), fake_task())
    assert "acme-web" not in context
    assert json.loads(raw)["memory_slice_included"] is False


async def test_role_clearance_still_applies_to_the_assembled_slice(db, monkeypatch):
    async def no_previous_tasks(_limit):
        return []

    monkeypatch.setattr(orchestrator.store, "list", no_previous_tasks)
    await remember(
        MemoryWrite(
            key="payroll",
            value="band 3",
            source="hr export",
            scope="lab",
            sensitivity=Sensitivity.RESTRICTED,
        ),
        owner=7,
    )

    context, _ = await orchestrator.repository_context(FakeWorkspace(), fake_task(user_id=7))
    # The planning role is cleared for confidential data, not restricted data.
    assert "band 3" not in context


async def test_a_large_memory_plane_is_bounded_by_the_character_budget(db):
    for index in range(40):
        await remember(
            MemoryWrite(
                key=f"k{index:02d}", value=f"value-{index}-" + "x" * 200, source="bulk import", scope="lab"
            )
        )

    rendered = await context_slice(None, role="lead", scope="lab", limit=40, char_budget=600)
    assert rendered
    assert len(rendered) <= 600
    assert "(truncated)" in rendered


async def test_self_improvement_context_also_carries_memory(db, monkeypatch):
    async def fail_if_previous_tasks_are_loaded(_limit):
        raise AssertionError("self-improvement context must not load prior task transcripts")

    monkeypatch.setattr(orchestrator.store, "list", fail_if_previous_tasks_are_loaded)
    await remember(MemoryWrite(key="parser.note", value="strict-parser", source="incident 42", scope="lab"))

    context, raw = await orchestrator.repository_context(
        FakeWorkspace(), fake_task(kind="self_improvement", requirement="Tighten the parser")
    )
    assert "strict-parser" in context
    assert len(context) <= settings.self_task_context_chars
    assert json.loads(raw)["memory_slice_included"] is True


async def test_standard_context_prioritizes_feature_specific_existing_tests(db, monkeypatch):
    class TodoWorkspace:
        files = "README.md\nbot.py\ntests/test_smoke.py\ntests/test_todo.py\ntodos.db\n"

        def list_files(self):
            return self.files

        def read_file(self, path):
            content = {
                "README.md": "# Telegram Lab\n",
                "bot.py": "async def todo(): pass\n",
                "tests/test_smoke.py": "def test_smoke(): pass\n",
                "tests/test_todo.py": "def test_todo_persists(): pass\n",
            }
            if path not in content:
                raise ValueError(path)
            return content[path]

    async def no_previous_tasks(_limit):
        return []

    monkeypatch.setattr(orchestrator.store, "list", no_previous_tasks)
    task = fake_task(requirement="Tambahkan subcommand /todo count")

    context, raw = await orchestrator.repository_context(TodoWorkspace(), task)
    inspected = [entry["path"] for entry in json.loads(raw)["inspected"]]

    assert inspected[0] == "tests/test_todo.py"
    assert "def test_todo_persists" in context
    assert "todos.db:" not in context
