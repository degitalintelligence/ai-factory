"""Task #47: successful orchestration must still answer and substantiate its scope."""

import base64
import json
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from app import audit_evidence, staff
from app.audit_scope import SELF_REPOSITORY
from app.config import settings
from app.db import DailyBudget
from app.staff_schemas import AuditCheck, ChatRequest, OutputEvaluation, ResolvedIntent, StaffOutput

SHA = "a" * 40
REQUIREMENT = (
    "Audit current main repository ai-factory setelah deployment terbaru. Verifikasi konfigurasi model, "
    "evidence-audit-v1, budget diagnostics, readiness dan test suite. "
    "Bedakan bukti repository dari hal yang belum dapat diverifikasi di runtime. Jangan mengubah apa pun."
)


async def goal(db):
    task = await staff.create_intent(
        ChatRequest(message=REQUIREMENT, project="self", idempotency_key="47"), 7
    )
    await db.update(task.id, base_sha=SHA)
    return await db.get(task.id)


def fake_github(monkeypatch, *, ci_sha=SHA, ci_result="success"):
    reads = []

    async def request(method, path, **kwargs):
        assert path.startswith(SELF_REPOSITORY + "/")
        reads.append((path, kwargs))
        if path.endswith("/actions/runs"):
            return {
                "workflow_runs": [
                    {"id": 99, "head_sha": ci_sha, "status": "completed", "conclusion": ci_result}
                ]
            }
        if path.endswith("/jobs"):
            return {
                "jobs": [
                    {
                        "name": "engine",
                        "status": "completed",
                        "conclusion": ci_result,
                        "steps": [{"name": "Run python -m pytest -q", "conclusion": ci_result}],
                    }
                ]
            }
        assert kwargs["params"]["ref"] == SHA
        local = path.split("/contents/", 1)[1]
        raw = Path(local).read_bytes()
        return {
            "type": "file",
            "size": len(raw),
            "encoding": "base64",
            "content": base64.b64encode(raw).decode(),
        }

    monkeypatch.setattr(settings, "github_token", "fixture-only")
    monkeypatch.setattr(staff.GitHubAPI, "request", AsyncMock(side_effect=request))
    monkeypatch.setattr(
        audit_evidence,
        "readiness_observation",
        AsyncMock(return_value={"status": "ready", "scope": "fixture control process"}),
    )
    return reads


def covered_output(context):
    refs = {item.source: item.ref for item in context}
    mapping = {
        "models": ("runtime", "factory_runtime_configuration"),
        "workflow": ("repository", "registered_repository_source"),
        "budget": ("runtime", "factory_runtime_budget"),
        "readiness": ("runtime", "factory_runtime_readiness"),
        "tests": ("ci", "repository_ci"),
    }
    workflow = next(item.ref for item in context if item.ref.endswith("app/staff.py"))
    return StaffOutput(
        summary="Konfigurasi aktif dan bukti pada commit terkunci diperiksa; CI terpisah dari runtime.",
        audit_checks=[
            AuditCheck(
                topic=topic,
                verification=kind,
                observation=f"Pemeriksaan {topic} sesuai bukti.",
                observed_values=audit_evidence.audit_facts(context).get(topic, {}),
                evidence_refs=[workflow if topic == "workflow" else refs[source]],
                limitation="Pengamatan terbatas pada sumber dan waktu yang dicatat.",
            )
            for topic, (kind, source) in mapping.items()
        ],
        next_action="Tidak ada perubahan; pantau sesuai temuan.",
    )


async def test_pinned_code_runtime_config_budget_readiness_and_ci_all_reach_context(db, monkeypatch):
    task = await goal(db)
    reads = fake_github(monkeypatch)
    for role in ["lead", "developer", "reviewer"]:
        monkeypatch.setattr(settings, role + "_model", "openai/gpt-4.1-mini")
    async with db.sessions() as session, session.begin():
        session.add(
            DailyBudget(
                id=f"default:{audit_evidence.utcnow().date().isoformat()}",
                calls=12,
                reserved_tokens=765432,
                cost_usd=0.12,
            )
        )
        session.add(
            DailyBudget(
                id=f"other:{audit_evidence.utcnow().date().isoformat()}",
                calls=99999,
                reserved_tokens=1,
                cost_usd=99,
            )
        )
    context = await staff.assemble_context(task)
    sources = {item.source for item in context}
    assert {
        "registered_repository_source",
        "factory_runtime_configuration",
        "factory_runtime_budget",
        "factory_runtime_readiness",
        "repository_ci",
        "repository_audit_scope",
    } <= sources
    assert all(item.scope == "self" for item in context)
    assert sum(len(item.model_dump_json()) for item in context) <= min(32000, settings.max_prompt_chars // 3)
    assert any(item.ref.endswith("app/staff.py") for item in context)
    config = json.loads(
        next(item.content for item in context if item.source == "factory_runtime_configuration")
    )
    assert {role["provider_model"] for role in config["roles"].values()} == {"openai/gpt-4.1-mini"}
    assert "bunny-alpha" not in json.dumps(config)
    budget = json.loads(next(item.content for item in context if item.source == "factory_runtime_budget"))
    assert budget["daily_usage"]["calls"] == 12 and budget["daily_usage"]["reserved_tokens"] == 765432
    assert budget["period"] == "UTC calendar day" and "99999" not in json.dumps(budget)
    assert all(args["params"]["ref"] == SHA for path, args in reads if "/contents/" in path)
    assert all(item.source != "tasks" for item in context)
    assert staff.validate_output(covered_output(context), context) == []
    rendered = staff.render_result(task.id, covered_output(context))
    for label in [
        "Konfigurasi model",
        "Workflow audit",
        "Budget diagnostics",
        "Readiness",
        "Test suite",
        "bukti CI",
    ]:
        assert label in rendered


async def test_task_47_doc_only_report_is_rejected_even_if_model_reviewer_approves(db, monkeypatch):
    task = await goal(db)
    fake_github(monkeypatch)
    await db.claim("worker")
    seen = []

    async def complete(*, schema, **kwargs):
        seen.append(schema)
        if schema is ResolvedIntent:
            return ResolvedIntent(objective=REQUIREMENT, desired_outcome="Audit lima pemeriksaan")
        if schema is StaffOutput:
            context = await staff.assemble_context(await db.get(task.id))
            docs = next(item.ref for item in context if item.source == "registered_repository_excerpt")
            return StaffOutput(
                summary="Audit mengonfirmasi mekanisme antrean yang andal dan keamanan yang ketat.",
                findings=[
                    {
                        "title": "Mekanisme antrean yang andal",
                        "situation": "Dokumentasi membuktikan sistem andal.",
                        "priority": "high",
                        "why_now": "Operasional",
                        "recommendation": "Pantau",
                        "alternative": "Tunda",
                        "risk": "Kegagalan",
                        "evidence_refs": [docs],
                        "confidence": 1,
                    }
                ],
                missing_information=[
                    "Tidak tersedia bukti langsung kegagalan sistem dari narasi tersembunyi."
                ],
                next_action="Lakukan tes manual ARM.",
            )
        return OutputEvaluation(approved=True, summary="Disetujui model")

    monkeypatch.setattr(staff, "complete", complete)
    await staff.run_staff_task(task.id, "worker")
    saved = await db.get(task.id)
    assert saved.status == "failed"
    artifacts = {a.kind: a.content for a in await db.artifacts(task.id)}
    assert "staff_result" not in artifacts
    assert "scope coverage" in artifacts["staff_failure"]
    assert "Documentation-only" in artifacts["staff_failure"]
    assert "leaks internal" in artifacts["staff_failure"]


async def test_task_47_complete_scoped_report_publishes_exact_reviewed_checks(db, monkeypatch):
    task = await goal(db)
    fake_github(monkeypatch)
    await db.claim("worker")
    seen = []
    outputs = []

    async def complete(*, schema, role, user, **kwargs):
        seen.append((schema, role))
        if schema is ResolvedIntent:
            return ResolvedIntent(objective=REQUIREMENT, desired_outcome="Audit lima pemeriksaan")
        if schema is StaffOutput:
            assert "Required audit_checks topics:" in user
            context = await staff.assemble_context(await db.get(task.id))
            result = covered_output(context)
            outputs.append(result)
            return result
        assert schema is OutputEvaluation and role == "reviewer"
        assert "AUDIT" in user.upper()
        return OutputEvaluation(approved=True, summary="Cakupan lengkap dan bukti sesuai jenisnya")

    monkeypatch.setattr(staff, "complete", complete)
    await staff.run_staff_task(task.id, "worker")
    saved = await db.get(task.id)
    assert saved.status == "completed", saved.last_message
    assert len(seen) == 5 and len(outputs) == 2
    artifacts = {a.kind: a.content for a in await db.artifacts(task.id)}
    assert StaffOutput.model_validate_json(artifacts["staff_result"]).audit_checks == outputs[-1].audit_checks
    assert "staff_final_draft" not in artifacts


@pytest.mark.parametrize("ci_sha,conclusion", [("b" * 40, "success"), (SHA, "failure")])
async def test_ci_for_wrong_commit_or_failed_test_never_verifies_suite(db, monkeypatch, ci_sha, conclusion):
    task = await goal(db)
    fake_github(monkeypatch, ci_sha=ci_sha, ci_result=conclusion)
    context = await staff.assemble_context(task)
    assert any(
        "CI verification requires" in issue
        for issue in staff.validate_output(covered_output(context), context)
    )


@pytest.mark.parametrize("topic", ["models", "workflow", "budget", "readiness", "tests"])
async def test_documentation_cannot_be_used_as_runtime_or_repository_verification(db, monkeypatch, topic):
    task = await goal(db)
    fake_github(monkeypatch)
    context = await staff.assemble_context(task)
    result = covered_output(context)
    check = next(check for check in result.audit_checks if check.topic == topic)
    check.evidence_refs = [
        next(item.ref for item in context if item.source == "registered_repository_excerpt")
    ]
    assert staff.validate_output(result, context)


async def test_unavailable_readiness_is_a_gap_not_success(db, monkeypatch):
    task = await goal(db)
    fake_github(monkeypatch)
    monkeypatch.setattr(
        audit_evidence, "readiness_observation", AsyncMock(return_value={"status": "unavailable"})
    )
    context = await staff.assemble_context(task)
    result = covered_output(context)
    assert "Readiness cannot be verified" in ";".join(staff.validate_output(result, context))
    check = next(c for c in result.audit_checks if c.topic == "readiness")
    check.verification = "unverified"
    check.limitation = "Probe /ready tidak menghasilkan readiness yang sukses."
    assert staff.validate_output(result, context) == []


def test_line_numbered_excerpts_find_relevant_functions_deep_in_large_files():
    text = "# unrelated\n" * 1000 + "async def ready():\n    return {'status': 'ready'}\n"
    excerpt = audit_evidence.source_excerpt("app/main.py", text, ["readiness"])
    assert "L1001: async def ready" in excerpt and "source_sha256=" in excerpt
    assert len(excerpt) <= 4000 and "not execution evidence" in excerpt


async def test_other_repository_does_not_receive_factory_process_observations(db, monkeypatch):
    task = await staff.create_intent(
        ChatRequest(
            message="Read-only audit repository owner/other: model, budget, readiness and tests.",
            project="other",
            idempotency_key="other",
        ),
        7,
    )
    await db.update(task.id, base_sha=SHA)
    task = await db.get(task.id)
    monkeypatch.setattr(settings, "github_token", "")
    probe = AsyncMock(side_effect=AssertionError("No factory runtime for another product"))
    monkeypatch.setattr(audit_evidence, "readiness_observation", probe)
    context = await staff.assemble_context(task)
    assert not probe.called
    assert all(not item.source.startswith("factory_runtime") for item in context)


async def test_ci_unavailable_is_explicit_and_no_target_code_is_executed(db, monkeypatch):
    task = await goal(db)
    monkeypatch.setattr(settings, "github_token", "fixture-only")
    monkeypatch.setattr(
        audit_evidence, "readiness_observation", AsyncMock(return_value={"status": "unavailable"})
    )
    monkeypatch.setattr(staff.GitHubAPI, "request", AsyncMock(side_effect=httpx.ConnectError("unavailable")))
    context = await staff.assemble_context(task)
    ci = json.loads(next(item.content for item in context if item.source == "repository_ci"))
    assert ci["status"] == "unavailable" and ci["runs"] == []
    assert any(item.source == "repository_read_status" for item in context)


async def test_invented_intent_gaps_are_not_copied_to_scoped_audit_answer(db, monkeypatch):
    task = await goal(db)
    fake_github(monkeypatch)
    await db.claim("worker")

    async def complete(*, schema, **kwargs):
        if schema is ResolvedIntent:
            return ResolvedIntent(
                objective=REQUIREMENT,
                desired_outcome="Audit lengkap",
                evidence_gaps=["Tidak ada bukti terkait penanganan ValueError dan narasi tersembunyi."],
            )
        if schema is StaffOutput:
            return covered_output(await staff.assemble_context(await db.get(task.id)))
        return OutputEvaluation(approved=True, summary="Periksa bukti per topik")

    monkeypatch.setattr(staff, "complete", complete)
    await staff.run_staff_task(task.id, "worker")
    assert (await db.get(task.id)).status == "completed"
    artifacts = {a.kind: a.content for a in await db.artifacts(task.id)}
    assert StaffOutput.model_validate_json(artifacts["staff_result"]).missing_information == []


async def test_verified_checks_cannot_reuse_irrelevant_source_files(db, monkeypatch):
    task = await goal(db)
    fake_github(monkeypatch)
    context = await staff.assemble_context(task)
    result = covered_output(context)
    check = next(c for c in result.audit_checks if c.topic == "workflow")
    check.evidence_refs = [next(item.ref for item in context if item.ref.endswith("app/config.py"))]
    assert any("does not cover" in issue for issue in staff.validate_output(result, context))


async def test_legacy_outputs_remain_readable_and_missing_check_fails_closed(db, monkeypatch):
    task = await goal(db)
    fake_github(monkeypatch)
    context = await staff.assemble_context(task)
    legacy = StaffOutput.model_validate(
        {"summary": "Dokumentasi tersedia", "findings": [], "next_action": "Periksa bukti"}
    )
    assert legacy.audit_checks == []
    assert any("scope coverage" in issue for issue in staff.validate_output(legacy, context))
    result = covered_output(context)
    result.audit_checks[-1].verification = "repository"
    result.audit_checks[-1].evidence_refs = [
        next(item.ref for item in context if item.ref.endswith(".github/workflows/ci.yml"))
    ]
    result.audit_checks[-1].observation = "Test suite lulus."
    assert "Test pass claim requires" in ";".join(staff.validate_output(result, context))


async def test_readiness_handler_failure_is_recorded_without_leaking_exception_details(monkeypatch):
    from app import main

    monkeypatch.setattr(main, "ready", AsyncMock(side_effect=RuntimeError("private deployment details")))
    observation = await audit_evidence.readiness_observation()
    assert observation["status"] == "unavailable" and observation["error_category"] == "RuntimeError"
    assert "private deployment details" not in json.dumps(observation)


async def test_scope_defect_gets_one_correction_and_fresh_review_before_completion(db, monkeypatch):
    task = await goal(db)
    fake_github(monkeypatch)
    await db.claim("worker")
    drafts = []
    reviews = []

    async def complete(*, schema, user, **kwargs):
        if schema is ResolvedIntent:
            return ResolvedIntent(objective=REQUIREMENT, desired_outcome="Audit lengkap")
        if schema is StaffOutput:
            result = covered_output(await staff.assemble_context(await db.get(task.id)))
            if not drafts:
                result.audit_checks = result.audit_checks[:-1]
            drafts.append(result)
            return result
        reviews.append(user)
        return OutputEvaluation(approved=True, summary="Disetujui model; tetap tunduk pada gate lokal")

    monkeypatch.setattr(staff, "complete", complete)
    await staff.run_staff_task(task.id, "worker")
    assert (await db.get(task.id)).status == "completed"
    artifacts = {a.kind: a.content for a in await db.artifacts(task.id)}
    assert "review_repair_evaluation" in artifacts
    assert len(drafts) == 3 and len(reviews) == 3
    assert len(StaffOutput.model_validate_json(artifacts["staff_result"]).audit_checks) == 5


@pytest.mark.parametrize(
    "defect", ["summary", "missing", "models", "budget", "workflow", "production", "wrong_value"]
)
async def test_task_48_consistency_defects_rejected_despite_approved_review(db, monkeypatch, defect):
    task = await goal(db)
    fake_github(monkeypatch)
    context = await staff.assemble_context(task)
    result = covered_output(context)
    if defect == "summary":
        result.summary = "Bukti runtime langsung untuk workflow dan readiness tidak tersedia."
    elif defect == "missing":
        result.missing_information = ["Bukti runtime readiness tidak tersedia."]
    elif defect == "production":
        result.next_action = "Pertimbangkan pengujian di lingkungan produksi untuk validasi tes."
    else:
        topic = "budget" if defect == "wrong_value" else defect
        check = next(c for c in result.audit_checks if c.topic == topic)
        if defect == "wrong_value":
            check.observed_values["daily_limits_calls"] = "999999"
        else:
            check.observed_values = {}
    assert any(issue.startswith("Audit consistency") for issue in staff.validate_output(result, context))
    # Reviewer approval cannot remove local issues; repeated same answer fails closed.
    await db.claim("worker")

    async def complete(*, schema, **kwargs):
        if schema is ResolvedIntent:
            return ResolvedIntent(objective=REQUIREMENT, desired_outcome="Audit evidence")
        if schema is StaffOutput:
            return result
        return OutputEvaluation(approved=True, summary="Model approved")

    monkeypatch.setattr(staff, "complete", complete)
    await staff.run_staff_task(task.id, "worker")
    saved = await db.get(task.id)
    assert saved.status == "failed" and "Audit consistency" in saved.last_message


async def test_task_48_exact_active_values_render_and_legitimate_gaps_remain(db, monkeypatch):
    task = await goal(db)
    reads = fake_github(monkeypatch)
    for role in ("lead", "developer", "reviewer"):
        monkeypatch.setattr(settings, role + "_model", "openai/gpt-4.1-mini")
    monkeypatch.setattr(settings, "global_max_tokens_per_day", 20_000_000)
    monkeypatch.setattr(settings, "global_max_cost_usd_per_day", 50.0)
    context = await staff.assemble_context(task)
    result = covered_output(context)
    result.missing_information = [
        "Bukti panggilan model aktual oleh semua peran belum tersedia.",
        "Budget berkelanjutan belum tersedia, hanya snapshot.",
        "Readiness ARM/business acceptance belum tersedia.",
        "Attestation deployed runtime SHA tidak tersedia.",
    ]
    assert staff.validate_output(result, context) == []
    result.next_action = "Jangan lakukan pengujian di produksi; gunakan staging bila diperlukan."
    assert staff.validate_output(result, context) == []
    rendered = staff.render_result(task.id, result)
    assert "lead=openai/gpt-4.1-mini" in rendered
    assert "daily_limits_reserved_tokens=20000000" in rendered
    assert "daily_limits_reported_cost_usd=50.0" in rendered
    assert "contract=evidence-audit-v1" in rendered
    assert "status=ready" in rendered
    assert not any("/contents/docs/" in path for path, _ in reads)
    assert len(context) == 14  # five observations, eight unique code paths, README


async def test_task_48_compact_prompts_preserve_authorized_evidence_and_full_artifact(db, monkeypatch):
    task = await goal(db)
    fake_github(monkeypatch)
    await db.claim("worker")
    prompts = []

    async def complete(*, schema, user, **kwargs):
        prompts.append(user)
        if schema is ResolvedIntent:
            return ResolvedIntent(objective=REQUIREMENT, desired_outcome="Audit evidence")
        if schema is StaffOutput:
            return covered_output(await staff.assemble_context(await db.get(task.id)))
        return OutputEvaluation(approved=True, summary="Checked")

    monkeypatch.setattr(staff, "complete", complete)
    await staff.run_staff_task(task.id, "worker")
    assert (await db.get(task.id)).status == "completed"
    artifacts = {a.kind: a.content for a in await db.artifacts(task.id)}
    archived = json.loads(artifacts["context"])
    assert all("owner" in item and "created_at" in item for item in archived)
    assert all('"created_at"' not in prompt and '"owner"' not in prompt for prompt in prompts)
    assert all(all(item["ref"] in prompt for item in archived) for prompt in prompts)
    full = json.dumps(archived, ensure_ascii=False)
    compact = json.dumps(
        [{k: item[k] for k in ("ref", "source", "content", "label")} for item in archived],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    assert len(compact) < len(full) * 0.95
