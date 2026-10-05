"""Task #44: a repository audit must not become an unscoped staff goal."""

import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import staff
from app.audit_scope import SELF_REPOSITORY
from app.config import settings
from app.schemas import MemoryWrite
from app.staff_schemas import ChatRequest, OutputEvaluation, ResolvedIntent, StaffOutput, StaffPlan
from app.telegram_control import logs_handler, new_handler, report_handler, status_handler

REQUIREMENT = (
    "Audit current main repository ai-factory setelah deployment terbaru. "
    "Verifikasi konfigurasi model, evidence-audit-v1, budget diagnostics, readiness dan test suite. "
    "Bedakan bukti repository dari hal yang belum dapat diverifikasi di runtime. Jangan mengubah apa pun."
)
SHA = "a" * 40


def event(number=44):
    return SimpleNamespace(
        update_id=number,
        effective_user=SimpleNamespace(id=7),
        effective_chat=SimpleNamespace(id=7, type="private"),
        effective_message=SimpleNamespace(reply_text=AsyncMock(), reply_document=AsyncMock()),
    )


@pytest.mark.parametrize("repo", ["liobot/123", "degitalintelligence/telegram-lab"])
def test_self_alias_cannot_be_repointed(monkeypatch, repo):
    monkeypatch.setattr(settings, "projects_json", json.dumps({"self": {"repo": repo}}))
    with pytest.raises(ValueError, match="Alias self must"):
        settings.projects()


async def test_missing_self_never_falls_back_to_lab(db, monkeypatch):
    monkeypatch.setattr(settings, "projects_json", '{"lab":{"repo":"degitalintelligence/telegram-lab"}}')
    with pytest.raises(ValueError):
        await staff.create_intent(ChatRequest(message=REQUIREMENT, project="self", idempotency_key="44"), 7)
    assert not await db.list()


@pytest.mark.parametrize("project", ["", "self"])
async def test_task_44_locks_target_before_context_and_enters_audit_workflow(db, monkeypatch, project):
    monkeypatch.setattr(settings, "github_token", "test-placeholder")
    lab = await db.create("Unrelated lab history", user_id=7)
    await db.update(lab.id, status="failed")
    old = await db.create("Unreferenced self history", project="self", user_id=7)
    await db.update(old.id, status="failed")
    await db.remember(MemoryWrite(key="lab.note", value="LAB-POISON", scope="lab", source="fixture"), owner=7)
    await db.remember(MemoryWrite(key="global.note", value="GLOBAL-POISON", source="fixture"), owner=7)
    goal = await staff.create_intent(
        ChatRequest(message=REQUIREMENT, project=project, idempotency_key="44"), 7
    )
    assert goal.repo == SELF_REPOSITORY and goal.project == "self"
    assert (await db.claim("worker")).id == goal.id
    reads = []

    async def branch(repo, branch):
        assert repo == SELF_REPOSITORY and branch == "main"
        return SHA

    async def request(method, path, **kwargs):
        assert path.startswith(SELF_REPOSITORY + "/")
        reads.append(path)
        if "/commits/" in path:
            return {"sha": SHA}
        locked = await db.get(goal.id)
        assert locked.base_sha == SHA  # persisted before any source retrieval
        assert kwargs["params"]["ref"] == SHA
        return {
            "type": "file",
            "encoding": "base64",
            "size": 10,
            "content": base64.b64encode(b"# AI Factory source").decode(),
        }

    monkeypatch.setattr(staff.GitHubAPI, "branch_sha", AsyncMock(side_effect=branch))
    monkeypatch.setattr(staff.GitHubAPI, "request", AsyncMock(side_effect=request))
    seen = []

    async def complete(*, schema, role, user, **kwargs):
        seen.append((schema, role))
        assert "telegram-lab" not in user and "LAB-POISON" not in user and "GLOBAL-POISON" not in user
        assert f'"ref": "task:{lab.id}"' not in user
        assert f'"ref": "task:{old.id}"' not in user
        if schema is ResolvedIntent:
            assert role == "lead" and SHA in user
            return ResolvedIntent(objective=REQUIREMENT, desired_outcome="Audit sumber repository")
        assert schema is not StaffPlan  # deterministic composite audit, no keyword-derived plan
        if schema is StaffOutput:
            return StaffOutput(
                summary="Bukti repository tersedia; runtime belum diverifikasi.",
                findings=[],
                next_action="Periksa runtime secara terpisah.",
            )
        assert schema is OutputEvaluation
        return OutputEvaluation(approved=True, summary="Bukti dan batas cakupan sesuai.")

    monkeypatch.setattr(staff, "complete", complete)
    await staff.run_staff_task(goal.id, "worker")
    saved = await db.get(goal.id)
    artifacts = {a.kind: a.content for a in await db.artifacts(goal.id)}
    assert saved.status == "completed", saved.last_message
    assert saved.repo == SELF_REPOSITORY and saved.project == "self" and saved.base_sha == SHA
    assert artifacts["output_contract"] == "evidence-audit-v1"
    assert json.loads(artifacts["resolved_skills"])["steps"] == ["engineering", "product_research"]
    assert all(item["scope"] == "self" for item in json.loads(artifacts["context"]))
    assert (ResolvedIntent, "lead") in seen
    assert len(reads) == 6 and "staff_failure" not in artifacts


async def test_audit_requires_locked_sha_and_scope_before_context(db):
    goal = await staff.create_intent(ChatRequest(message=REQUIREMENT, idempotency_key="44"), 7)
    with pytest.raises(ValueError, match="exact 40-character"):
        await staff.assemble_context(goal)


async def test_failed_baseline_never_calls_context_or_lead_and_is_reported(db, monkeypatch):
    goal = await staff.create_intent(ChatRequest(message=REQUIREMENT, idempotency_key="44"), 7)
    await db.claim("worker")
    monkeypatch.setattr(staff.GitHubAPI, "branch_sha", AsyncMock(return_value=None))
    context = AsyncMock(side_effect=AssertionError("No context before baseline"))
    lead = AsyncMock(side_effect=AssertionError("No Lead before baseline"))
    monkeypatch.setattr(staff, "assemble_context", context)
    monkeypatch.setattr(staff, "complete", lead)
    await staff.run_staff_task(goal.id, "worker")
    assert not context.called and not lead.called
    artifacts = {a.kind: a.content for a in await db.artifacts(goal.id)}
    assert json.loads(artifacts["staff_failure"])["stage"] == "baseline_lock"
    monkeypatch.setattr(settings, "telegram_allowed_user_ids", "7")
    for handler in [status_handler, logs_handler, report_handler]:
        update = event()
        await handler(update, SimpleNamespace(args=[str(goal.id)]))
        if handler is report_handler:
            rendered = (
                update.effective_message.reply_document.call_args.kwargs["document"].getvalue().decode()
            )
        else:
            rendered = "".join(c.args[0] for c in update.effective_message.reply_text.call_args_list)
        for value in [
            SELF_REPOSITORY,
            "Base SHA:",
            "Resolved skills:",
            "Context references:",
            "Failure stage: baseline_lock",
        ]:
            assert value in rendered


@pytest.mark.parametrize("separator", [" ", " | "])
async def test_telegram_explicit_self_with_or_without_pipe(db, monkeypatch, separator):
    monkeypatch.setattr(settings, "telegram_allowed_user_ids", "7")
    update = event()
    await new_handler(update, SimpleNamespace(args=("self" + separator + REQUIREMENT).split()))
    [task] = await db.list()
    assert task.project == "self" and task.repo == SELF_REPOSITORY


async def test_explicit_conflicting_target_is_rejected(db):
    with pytest.raises(ValueError, match="conflicts"):
        await db.create(REQUIREMENT, project="lab", user_id=7)


def test_repository_audit_skill_mapping_excludes_finance_and_sales_keywords():
    from app.skills import select_skills

    assert select_skills(REQUIREMENT + " Lead model dan budget diagnostics.") == [
        "engineering",
        "product_research",
    ]


async def test_explicit_historical_reference_stays_within_target_owner_and_tenant(db, monkeypatch):
    source = await db.create("Referenced self task", project="self", user_id=7)
    await db.update(source.id, status="failed")
    foreign = await db.create("Foreign owner self task", project="self", user_id=8)
    await db.update(foreign.id, status="failed")
    lab = await db.create("Explicit but out-of-scope lab task", user_id=7)
    await db.update(lab.id, status="failed")
    goal = await staff.create_intent(
        ChatRequest(
            message=REQUIREMENT + f" Evidence: Task #{source.id}, Task #{foreign.id}, Task #{lab.id}.",
            idempotency_key="refs",
        ),
        7,
    )
    await db.update(goal.id, base_sha=SHA)
    goal = await db.get(goal.id)
    monkeypatch.setattr(settings, "github_token", "")
    context = await staff.assemble_context(goal)
    refs = {item.ref for item in context}
    assert f"task:{source.id}" in refs and f"task:{source.id}:evidence" in refs
    assert f"task:{foreign.id}" not in refs and f"task:{lab.id}" not in refs


async def test_retry_keeps_locked_sha_even_when_main_advances(db, monkeypatch):
    goal = await staff.create_intent(ChatRequest(message=REQUIREMENT, idempotency_key="retry"), 7)
    await db.update(goal.id, base_sha=SHA)
    await db.claim("worker")
    branch = AsyncMock(side_effect=AssertionError("Never move a locked audit baseline"))
    monkeypatch.setattr(staff.GitHubAPI, "branch_sha", branch)
    monkeypatch.setattr(settings, "github_token", "test-placeholder")

    async def request(method, path, **kwargs):
        assert kwargs["params"]["ref"] == SHA
        return {"type": "file", "encoding": "base64", "content": base64.b64encode(b"source").decode()}

    monkeypatch.setattr(staff.GitHubAPI, "request", AsyncMock(side_effect=request))
    monkeypatch.setattr(staff, "complete", AsyncMock(side_effect=ValueError("Stop after context")))
    await staff.run_staff_task(goal.id, "worker")
    assert (await db.get(goal.id)).base_sha == SHA and not branch.called
    artifacts = {a.kind: a.content for a in await db.artifacts(goal.id)}
    assert all(SHA in item["ref"] for item in json.loads(artifacts["context"]))
    assert json.loads(artifacts["staff_failure"])["stage"] == "intent_resolution"
