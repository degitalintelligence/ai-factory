"""Acceptance of the shared leased workflow, using deterministic provider replies."""

from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app import staff
from app.config import settings
from app.db import Artifact, AuditLog, DailyBudget, Decision, Deployment, Event, ModelRun, Subtask
from app.deployment import DeploymentService
from app.llm import run_context, subtask_context
from app.main import app
from app.schemas import DecisionRequest, ImprovementOutcome, MemoryWrite, SelfImprovementBrief
from app.staff_schemas import (
    ChatRequest,
    DeploymentReconciliation,
    OutputEvaluation,
    ResolvedIntent,
    StaffOutput,
    StaffPlan,
)
from app.store import BudgetExceeded


def plan():
    return StaffPlan(
        objective="Audit AI Factory",
        success_criteria=["Three prioritized decisions with evidence"],
        steps=[
            {"id": "audit", "skill": "engineering", "objective": "Audit reliability"},
            {
                "id": "prioritize",
                "skill": "product_research",
                "objective": "Prioritize work",
                "dependencies": ["audit"],
            },
        ],
        risks=["Insufficient source data"],
        approval_gates=["No external actions"],
        rollback_plan="Discard drafts",
    )


def output(ref):
    return StaffOutput(
        summary="Tiga masalah membutuhkan keputusan minggu ini.",
        findings=[
            {
                "title": f"Reliability issue {i}",
                "situation": "Recorded failure needs investigation",
                "priority": "high",
                "why_now": "Avoid repeated failed work",
                "recommendation": "Investigate and add a regression gate",
                "alternative": "Defer investigation",
                "risk": "Cause remains uncertain",
                "evidence_refs": [ref],
                "confidence": 0.7,
                "decision_required": True,
            }
            for i in range(3)
        ],
        next_action="Review the decision cards",
    )


async def setup_goal(db):
    source = await db.create("Evidence for failed engineering task", user_id=7)
    await db.update(source.id, status="failed", last_message="Recorded test failure")
    task = await staff.create_intent(
        ChatRequest(
            message="Audit AI Factory. Tunjukkan tiga masalah paling penting, rekomendasi perbaikan, dan keputusan minggu ini.",
            idempotency_key="golden",
        ),
        7,
    )
    await db.claim("worker")
    return task, source


def provider(db, ref, calls):
    async def complete(*, schema, role, user):
        calls.append(schema.__name__)
        await db.reserve_call(*run_context.get(), token_reserve=1, subtask=subtask_context.get())
        if schema is ResolvedIntent:
            return ResolvedIntent(
                objective="Audit AI Factory", desired_outcome="Three evidence-backed recommendations"
            )
        if schema is StaffPlan:
            return plan()
        if schema is StaffOutput:
            return output(ref)
        return OutputEvaluation(approved=True, summary="Claims grounded in supplied task evidence")

    return complete


async def test_golden_goal_to_dag_evidence_and_decision_inbox(db, monkeypatch):
    task, source = await setup_goal(db)
    calls = []
    monkeypatch.setattr(staff, "complete", provider(db, f"task:{source.id}", calls))
    await staff.run_staff_task(task.id, "worker")
    assert (await db.get(task.id)).status == "completed"
    async with db.sessions() as session:
        steps = list(await session.scalars(select(Subtask).where(Subtask.task_id == task.id)))
        decisions = list(await session.scalars(select(Decision).where(Decision.task_id == task.id)))
        assert [r.status for r in steps] == ["completed", "completed"]
        assert all(r.calls == 2 for r in steps)
        assert len(decisions) == 3 and all(r.owner == 7 for r in decisions)
    assert len(calls) == 8
    # A recovered final checkpoint never repeats provider calls or decision cards.
    await staff.run_staff_task(task.id, "worker")
    assert len(calls) == 8
    assert any(a.kind == "staff_result" for a in await db.artifacts(task.id))


async def test_unauthorized_or_invented_evidence_fails_closed(db, monkeypatch):
    task, _ = await setup_goal(db)
    monkeypatch.setattr(staff, "complete", provider(db, "task:999999", []))
    await staff.run_staff_task(task.id, "worker")
    assert (await db.get(task.id)).status == "failed"
    assert not any(a.kind == "staff_result" for a in await db.artifacts(task.id))
    assert any(d.category == "blocked" for d in await db.inbox())


async def test_context_excludes_other_owners_tenants_and_role_secrets(db):
    await db.remember(MemoryWrite(key="mine", value="Internal strategy", source="Dedi"), owner=7)
    await db.remember(MemoryWrite(key="other", value="Other private strategy", source="other"), owner=8)
    await db.remember(
        MemoryWrite(
            key="secret", value="Confidential board strategy", source="board", sensitivity="confidential"
        ),
        owner=7,
    )
    await db.remember(
        MemoryWrite(key="foreign", value="Foreign strategy", source="other"), owner=7, tenant="foreign"
    )
    task = await staff.create_intent(
        ChatRequest(message="Review business strategy", idempotency_key="context"), 7
    )
    context = await staff.assemble_context(task)
    assert [item.content for item in context] == ["Internal strategy"]


async def test_per_subtask_budget_is_reserved_before_attempt_and_survives_retry(db):
    task, _ = await setup_goal(db)
    async with db.sessions() as session, session.begin():
        row = Subtask(task_id=task.id, key="bounded", skill="engineering", skill_version="1")
        session.add(row)
        await session.flush()
        row_id = row.id
    await db.reserve_call(task.id, "worker", token_reserve=1, subtask=(row_id, 1))
    with pytest.raises(BudgetExceeded, match="Subtask"):
        await db.reserve_call(task.id, "worker", token_reserve=1, subtask=(row_id, 1))
    assert (await db.get(task.id)).llm_calls == 1


async def test_tenant_daily_budget_caps_multiple_tasks(db, monkeypatch):
    monkeypatch.setattr(settings, "global_max_llm_calls_per_day", 1)
    first, _ = await setup_goal(db)
    second = await staff.create_intent(
        ChatRequest(message="Review another objective", idempotency_key="second"), 7
    )
    await db.claim("worker2")
    await db.reserve_call(first.id, "worker", token_reserve=1)
    with pytest.raises(BudgetExceeded, match="Tenant daily"):
        await db.reserve_call(second.id, "worker2", token_reserve=1)
    async with db.sessions() as session:
        daily = await session.scalar(select(DailyBudget))
        assert daily.calls == 1


async def test_chat_api_dashboard_and_decisions_share_durable_state(db, monkeypatch):
    monkeypatch.setattr(settings, "api_token", "test-token")
    monkeypatch.setattr(settings, "api_operator_user_id", 7)
    headers = {"Authorization": "Bearer test-token"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://factory") as client:
        assert (await client.get("/dashboard")).status_code == 200
        assert (await client.get("/dashboard/app.js")).status_code == 200
        assert (await client.get("/v1/overview")).status_code == 401
        body = {"message": "Audit AI Factory this week", "idempotency_key": "api-goal"}
        first = await client.post("/v1/chat", json=body, headers=headers)
        again = await client.post("/v1/chat", json=body, headers=headers)
        assert first.status_code == 202 and first.json()["intent_id"] == again.json()["intent_id"]
        decision = await db.create_decision(
            DecisionRequest(
                title="Prioritize reliability",
                situation="Failure recorded",
                why_now="Repeated failures",
                options=[{"id": "fix", "label": "Investigate"}],
                recommendation="fix",
                evidence=["task:1"],
            ),
            owner=7,
        )
        answer = await client.post(
            "/v1/chat",
            json={"message": f"setujui keputusan #{decision.id}", "idempotency_key": "action"},
            headers=headers,
        )
        assert answer.status_code == 202 and answer.json()["status"] == "approved"
        overview = (await client.get("/v1/overview", headers=headers)).json()
        assert overview["decisions"][0]["state"] == "approved"
        assert len(overview["tasks"]) == 1


async def test_delegation_preserves_original_action_and_never_delegates_execution(db, monkeypatch):
    monkeypatch.setattr(settings, "telegram_allowed_user_ids", "7,8")
    decision = await db.create_decision(
        DecisionRequest(
            title="Research choice",
            situation="Question",
            why_now="Now",
            options=[{"id": "a", "label": "A"}],
            recommendation="a",
            evidence=["task:1"],
        ),
        owner=7,
    )
    resolved = await db.resolve_decision(decision.id, "delegate", 7, reason="Ask specialist", delegate_to=8)
    assert resolved.state == "delegated" and resolved.owner == 7
    assert (
        await db.resolve_decision(decision.id, "delegate", 7, reason="Ask specialist", delegate_to=8)
    ).id == decision.id
    rows = await db.inbox(state="open")
    assert len(rows) == 1 and rows[0].owner == 8


async def test_audit_cannot_be_changed_even_with_bulk_sql(db):
    await staff.create_intent(ChatRequest(message="Audit history invariants", idempotency_key="audit"), 7)
    async with db.sessions() as session:
        with pytest.raises(DBAPIError, match="append-only"):
            await session.execute(text("DELETE FROM audit_log"))
        await session.rollback()
        assert await session.scalar(select(AuditLog.id))


def test_plan_rejects_cycles_and_unregistered_skills():
    data = plan().model_dump()
    data["steps"][0]["dependencies"] = ["prioritize"]
    with pytest.raises(ValidationError, match="cycle"):
        StaffPlan.model_validate(data)
    data["steps"][0]["dependencies"] = []
    data["steps"][0]["skill"] = "unregistered"
    with pytest.raises(ValidationError):
        StaffPlan.model_validate(data)


async def test_unknown_deployment_can_be_recovered_without_resubmission(db):
    task = await db.create("Deploy approved feature", user_id=7)
    await db.update(task.id, status="deployment_unknown")
    async with db.sessions() as session, session.begin():
        session.add(Deployment(task_id=task.id, commit_sha="a" * 40, status="unknown"))
    service = DeploymentService(sessions=db.sessions)
    service.coolify = AsyncMock(side_effect=AssertionError("Must not submit deployment"))
    result = await service.reconcile(
        task.id,
        DeploymentReconciliation(
            approved_sha="a" * 40,
            no_submission_confirmed=True,
            evidence="Operator inspected Coolify and confirmed there was no submission",
        ),
        7,
    )
    assert result.status == "not_submitted" and (await db.get(task.id)).status == "deployment_failed"
    assert not service.coolify.called
    assert (await db.create("Next safe task", user_id=7)).id != task.id


async def test_rollback_measurement_is_pending_without_final_lesson(db, monkeypatch):
    from app.api_v03 import request_rollback

    brief = SelfImprovementBrief(
        problem="Repeated failures",
        evidence=["task:1"],
        hypothesis="Improve diagnostics",
        scope="app",
        baseline="3 failures",
        rollback_plan="Revert compatible code change",
    )
    task = await db.create(
        "Improve diagnostics", project="self", user_id=7, kind="self_improvement", brief=brief
    )
    await db.update(task.id, status="completed")
    measurement = ImprovementOutcome(
        before="Three failures", after="Four failures", evidence=["model-run records"], conclusion="rollback"
    )
    result = await request_rollback(task.id, measurement, 7)
    assert result["status"] == "rollback_pending"
    assert not any(a.kind == "improvement_outcome" for a in await db.artifacts(task.id))
    assert any(d.title == "Approve production rollback" for d in await db.inbox(state="open"))
    with pytest.raises(ValueError, match="confirmation"):
        await db.record_outcome(task.id, measurement, actor=7)


async def test_chat_and_telegram_share_intake_and_reject_wrong_principal(db, monkeypatch):
    from types import SimpleNamespace

    from app.telegram_control import chat_handler

    monkeypatch.setattr(settings, "telegram_allowed_user_ids", "7")
    message = SimpleNamespace(text="Review operations this week", reply_text=AsyncMock())
    update = SimpleNamespace(
        update_id=876,
        effective_user=SimpleNamespace(id=7),
        effective_chat=SimpleNamespace(id=77, type="private"),
        effective_message=message,
    )
    await chat_handler(update, SimpleNamespace())
    tasks = await db.list(user_id=7)
    assert len(tasks) == 1 and tasks[0].kind == "orchestration" and tasks[0].chat_id == 77
    assert f"tujuan #{tasks[0].id} [received]" in message.reply_text.call_args.args[0]
    await chat_handler(update, SimpleNamespace())
    assert len(await db.list(user_id=7)) == 1
    update.effective_user.id = 8
    await chat_handler(update, SimpleNamespace())
    assert not await db.list(user_id=8)


async def test_memory_conflict_visible_then_explicit_correction(db, monkeypatch):
    monkeypatch.setattr(settings, "api_token", "test-token")
    monkeypatch.setattr(settings, "api_operator_user_id", 7)
    headers = {"Authorization": "Bearer test-token"}
    body = {"key": "strategy", "value": "Prioritize reliability", "source": "Dedi"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://factory") as client:
        first = await client.post("/v1/memory", headers=headers, json=body)
        conflict = await client.post(
            "/v1/memory", headers=headers, json=body | {"value": "Prioritize growth"}
        )
        assert first.status_code == 201 and conflict.status_code == 409
        items, _ = await db.recall(role="lead", owner=7)
        assert items[0].label == "conflict" and items[0].value == "Prioritize reliability"
        assert len((await client.get("/v1/memory/conflicts", headers=headers)).json()) == 1
        corrected = await client.post(
            f"/v1/memory/{first.json()['id']}/correction",
            headers=headers,
            json={"value": "Prioritize growth", "source": "Dedi resolved conflict"},
        )
        assert corrected.status_code == 200
        assert not (await client.get("/v1/memory/conflicts", headers=headers)).json()
        assert len(await db.memory_history("strategy", owner=7)) == 2


async def test_task_evidence_and_action_do_not_leak_other_owner(db, monkeypatch):
    monkeypatch.setattr(settings, "api_token", "test-token")
    monkeypatch.setattr(settings, "api_operator_user_id", 7)
    task = await db.create("Private objective for another operator", user_id=8)
    await db.artifact(task.id, "private", "Other operator evidence")
    headers = {"Authorization": "Bearer test-token"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://factory") as client:
        assert (await client.get(f"/v1/tasks/{task.id}/evidence", headers=headers)).status_code == 404
        assert (await client.get(f"/v1/tasks/{task.id}", headers=headers)).status_code == 404
        assert (await client.post(f"/v1/tasks/{task.id}/cancel", headers=headers, json={})).status_code == 409
        assert not (await client.get("/tasks", headers=headers)).json()
    assert (await db.get(task.id)).status == "received"


async def test_l3_goal_creates_blocked_card_and_no_execution(db, monkeypatch):
    task, _ = await setup_goal(db)

    async def complete(*, schema, role, user):
        if schema is ResolvedIntent:
            return ResolvedIntent(
                objective="Deploy to production",
                desired_outcome="Production release",
                requested_authority="L3",
            )
        return plan()

    monkeypatch.setattr(staff, "complete", complete)
    await staff.run_staff_task(task.id, "worker")
    assert (await db.get(task.id)).status == "failed"
    assert any(d.category == "blocked" and d.owner == 7 for d in await db.inbox())
    async with db.sessions() as session:
        assert await session.scalar(select(Subtask.id)) is None
        assert await session.scalar(select(Deployment.id)) is None


async def test_repeated_failures_create_gated_evidence_backed_proposal(db):
    for index in range(3):
        task = await staff.create_intent(
            ChatRequest(message=f"Failure observation {index}", idempotency_key=f"failed-{index}"), 7
        )
        await db.update(task.id, status="failed", last_message="Task budget exhausted")
    proposals = await staff.detect_improvements(7)
    again = await staff.detect_improvements(7)
    assert len(proposals) == 1 and proposals[0].id == again[0].id
    brief = SelfImprovementBrief.model_validate_json(proposals[0].brief_json)
    assert len(brief.evidence) == 3 and brief.test_plan and brief.risk and brief.rollback_plan
    assert proposals[0].task_id is None
    assert not await staff.detect_improvements(8)


async def test_recovery_preserves_completed_subtasks_without_replay(db, monkeypatch):
    task, source = await setup_goal(db)
    calls = []
    real = provider(db, f"task:{source.id}", calls)
    failures = 0

    async def crash(*, schema, role, user):
        nonlocal failures
        if schema is StaffOutput and role == "lead" and not failures:
            failures += 1
            raise RuntimeError("Simulated final synthesis outage")
        return await real(schema=schema, role=role, user=user)

    monkeypatch.setattr(staff, "complete", crash)
    await staff.run_staff_task(task.id, "worker")
    assert (await db.get(task.id)).status == "failed"
    assert len(calls) == 6
    await db.update(task.id, status="received", lease_owner=None, lease_until=None)
    await db.claim("recovery")
    await staff.run_staff_task(task.id, "recovery")
    assert (await db.get(task.id)).status == "completed" and len(calls) == 8


async def test_high_risk_draft_requires_exact_plan_approval(db, monkeypatch):
    task, source = await setup_goal(db)
    real = provider(db, f"task:{source.id}", [])

    async def finance(*, schema, role, user):
        if schema is StaffPlan:
            data = plan().model_dump()
            data["steps"][0]["skill"] = "finance"
            return StaffPlan.model_validate(data)
        return await real(schema=schema, role=role, user=user)

    monkeypatch.setattr(staff, "complete", finance)
    await staff.run_staff_task(task.id, "worker")
    assert (await db.get(task.id)).status == "awaiting_approval"
    decisions = await db.inbox(state="open")
    assert len(decisions) == 1 and decisions[0].kind == "APPROVAL_REQUIRED"
    message = (await db.get(task.id)).last_message
    assert f"setujui keputusan #{decisions[0].id}" in message
    assert "finance: Audit reliability" in message
    assert "Batas efektif:" in message and "Insufficient source data" in message
    # WorkerPool.supervise releases the lease after the approval checkpoint.
    await db.update(task.id, lease_owner=None, lease_until=None)
    await db.resolve_decision(decisions[0].id, "approve", 7, reason="Review bounded finance draft")
    await db.claim("approved-worker")
    await staff.run_staff_task(task.id, "approved-worker")
    assert (await db.get(task.id)).status == "completed"


async def test_reconciliation_rejects_wrong_remote_application(db, monkeypatch):
    monkeypatch.setattr(
        settings, "projects_json", '{"lab":{"repo":"owner/repo","coolify_uuid":"registered-app"}}'
    )
    task = await db.create("Recover ambiguous deployment", user_id=7)
    await db.update(task.id, status="deployment_unknown")
    async with db.sessions() as session, session.begin():
        session.add(Deployment(task_id=task.id, commit_sha="a" * 40, status="unknown"))
    service = DeploymentService(sessions=db.sessions)
    service.coolify = AsyncMock(
        side_effect=[{"application_id": "999", "commit": "a" * 40, "status": "finished"}, {"id": 1}]
    )
    with pytest.raises(ValueError, match="application"):
        await service.reconcile(
            task.id,
            DeploymentReconciliation(
                approved_sha="a" * 40,
                deployment_uuid="candidate",
                evidence="Operator found this candidate deployment",
            ),
            7,
        )
    assert (await db.get(task.id)).status == "deployment_unknown"


async def test_old_decision_table_upgrade_preserves_history(tmp_path):
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.db import init_db

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'legacy.db'}")
    async with engine.begin() as connection:
        await connection.execute(text("CREATE TABLE decisions (id INTEGER PRIMARY KEY, title TEXT)"))
        await connection.execute(text("INSERT INTO decisions(id,title) VALUES(3,'Historical decision')"))
    await init_db(engine)
    await init_db(engine)
    async with engine.connect() as connection:
        row = (await connection.execute(text("SELECT title,tenant,category FROM decisions WHERE id=3"))).one()
        assert tuple(row) == ("Historical decision", "default", "recommendation")
    await engine.dispose()


async def test_withdrawn_memory_is_not_replayed_from_checkpoint(db, monkeypatch):
    memory = await db.remember(
        MemoryWrite(key="knowledge", value="Recorded internal source", source="Dedi"), owner=7
    )
    task = await staff.create_intent(
        ChatRequest(message="Review business knowledge", idempotency_key="withdrawn"), 7
    )
    await db.claim("worker")

    async def clarify(*, schema, role, user):
        return ResolvedIntent(
            objective="Review business knowledge",
            desired_outcome="Grounded review",
            missing_information=["Which observation window?"],
        )

    monkeypatch.setattr(staff, "complete", clarify)
    await staff.run_staff_task(task.id, "worker")
    await db.retract_memory(memory.id, "Withdraw incorrect source", owner=7)
    await db.update(task.id, status="received", lease_owner=None, lease_until=None)
    await db.claim("recovery")
    calls = AsyncMock(side_effect=AssertionError("Withdrawn source must not reach provider"))
    monkeypatch.setattr(staff, "complete", calls)
    await staff.run_staff_task(task.id, "recovery")
    assert (await db.get(task.id)).status == "failed" and not calls.called


async def test_bounded_context_retains_knowledge_alongside_large_task_history(db):
    for index in range(20):
        task = await db.create(f"Recorded engineering history {index} " + ("x" * 1500), user_id=7)
        await db.update(task.id, status="failed")
    memory = await db.remember(
        MemoryWrite(key="business.strategy", value="Reliability is the weekly priority", source="Dedi"),
        owner=7,
    )
    task = await staff.create_intent(
        ChatRequest(message="Audit business reliability this week", idempotency_key="balanced"), 7
    )
    context = await staff.assemble_context(task)
    assert any(item.ref == f"memory:{memory.id}:v1" for item in context)
    assert any(item.ref.startswith("task:") for item in context)
    assert sum(len(item.model_dump_json()) for item in context) <= 24000


async def test_staff_sends_start_before_slow_context_or_provider(db, monkeypatch):
    task, _ = await setup_goal(db)
    notify = AsyncMock()

    async def slow_context(task):
        assert notify.await_count == 1
        assert "Analisis dimulai" in notify.call_args.args[0]
        raise RuntimeError("Simulated source failure")

    monkeypatch.setattr(staff, "assemble_context", slow_context)
    complete = AsyncMock()
    monkeypatch.setattr(staff, "complete", complete)
    await staff.run_staff_task(task.id, "worker", notify)
    assert (await db.get(task.id)).status == "failed"
    assert not complete.called
    assert notify.await_count == 2


async def test_staff_delivery_failures_preserve_completed_result(db, monkeypatch):
    task, source = await setup_goal(db)
    monkeypatch.setattr(staff, "complete", provider(db, f"task:{source.id}", []))
    notify = AsyncMock(side_effect=RuntimeError("Simulated delivery outage"))
    await staff.run_staff_task(task.id, "worker", notify)
    assert (await db.get(task.id)).status == "completed"
    assert any(a.kind == "staff_result" for a in await db.artifacts(task.id))
    assert any(e.kind == "notification_failed" for e in await db.events(task.id))
    assert len([d for d in await db.inbox() if d.task_id == task.id]) == 3


async def test_audit_evidence_gaps_do_not_block_and_survive_final_result(db, monkeypatch):
    task, source = await setup_goal(db)
    real = provider(db, f"task:{source.id}", [])

    async def incomplete(*, schema, role, user):
        result = await real(schema=schema, role=role, user=user)
        if schema is ResolvedIntent:
            result.evidence_gaps = ["Live acceptance has no supplied evidence"]
        return result

    monkeypatch.setattr(staff, "complete", incomplete)
    await staff.run_staff_task(task.id, "worker")
    assert (await db.get(task.id)).status == "completed"
    artifact = next(a for a in await db.artifacts(task.id) if a.kind == "staff_result")
    result = StaffOutput.model_validate_json(artifact.content)
    assert result.missing_information == ["Live acceptance has no supplied evidence"]


async def test_task_diagnostics_are_bounded_and_owner_tenant_scoped(db):
    task, source = await setup_goal(db)
    other = await db.create("Private objective", user_id=8)
    await db.update(other.id, status="failed")
    foreign = await db.create("Foreign objective", user_id=7)
    await db.update(foreign.id, tenant="foreign")
    await db.update(source.id, plan_json='{"budget":{"max_llm_calls":2}}', cost_incomplete=True)
    async with db.sessions() as session, session.begin():
        for row, marker in ((source, "allowed"), (other, "private"), (foreign, "foreign")):
            for index in range(6):
                session.add(Event(task_id=row.id, kind="diagnostic", message=f"{marker}-event-{index}"))
            session.add(Artifact(task_id=row.id, kind="test_result", content=f"{marker}-artifact"))
            session.add(
                ModelRun(
                    task_id=row.id,
                    role="developer",
                    model_alias="configured-alias",
                    model="configured-model",
                    prompt_version="test-version",
                    outcome="timeout",
                    detail=f"{marker}-model",
                )
            )
    context = await staff.assemble_context(task)
    diagnostic = next(item.content for item in context if item.ref == f"task:{source.id}:evidence")
    assert "allowed-event-5" in diagnostic and "allowed-event-0" not in diagnostic
    assert "allowed-artifact" in diagnostic and "allowed-model" in diagnostic
    assert '"max_llm_calls": 2' in diagnostic and '"cost_incomplete": true' in diagnostic
    assert "configured-alias" in diagnostic and "test-version" in diagnostic
    combined = " ".join(item.content for item in context)
    assert "private-event" not in combined and "foreign-event" not in combined
    assert "private-artifact" not in combined and "foreign-model" not in combined


async def test_oversized_fresh_plan_is_repaired_once_within_original_budget(db, monkeypatch):
    task, source = await setup_goal(db)
    real = provider(db, f"task:{source.id}", [])
    plans = []

    async def oversized(*, schema, role, user):
        result = await real(schema=schema, role=role, user=user)
        if schema is StaffPlan:
            plans.append(user)
            data = result.model_dump()
            data["budget"]["max_llm_calls"] = 10 if len(plans) == 1 else 100
            if len(plans) == 1:
                data["steps"] += [
                    {"id": f"extra{i}", "skill": "engineering", "objective": "Overlapping audit"}
                    for i in range(3)
                ]
                data["risk"] = "high"
            return StaffPlan.model_validate(data)
        return result

    monkeypatch.setattr(staff, "complete", oversized)
    await staff.run_staff_task(task.id, "worker")
    saved = await db.get(task.id)
    assert len(plans) == 2 and saved.status == "awaiting_approval"
    revised = StaffPlan.model_validate_json(saved.plan_json)
    assert len(revised.steps) == 2 and revised.budget.max_llm_calls == 10
    assert revised.risk == "high" and saved.llm_calls == 3
    assert "minimal 6 tambahan" in saved.last_message and "cadangan retry 1" in saved.last_message
    assert any(a.kind == "infeasible_plan" for a in await db.artifacts(task.id))
    card = next(d for d in await db.inbox(state="open") if d.kind == "APPROVAL_REQUIRED")
    await db.update(task.id, lease_owner=None, lease_until=None)
    await db.resolve_decision(card.id, "approve", 7)
    await db.claim("approved-worker")
    await staff.run_staff_task(task.id, "approved-worker")
    assert (await db.get(task.id)).status == "completed"
    assert (await db.get(task.id)).llm_calls == 9 and len(plans) == 2


async def test_still_infeasible_plan_never_requests_execution_approval(db, monkeypatch):
    task, source = await setup_goal(db)
    real = provider(db, f"task:{source.id}", [])
    planning_calls = []

    async def oversized(*, schema, role, user):
        result = await real(schema=schema, role=role, user=user)
        if schema is StaffPlan:
            planning_calls.append(user)
            data = result.model_dump()
            data["budget"]["max_llm_calls"] = 10
            data["risk"] = "high"
            data["steps"] += [
                {"id": f"extra{i}", "skill": "engineering", "objective": "Overlapping audit"}
                for i in range(3)
            ]
            return StaffPlan.model_validate(data)
        return result

    monkeypatch.setattr(staff, "complete", oversized)
    await staff.run_staff_task(task.id, "worker")
    assert len(planning_calls) == 2 and (await db.get(task.id)).status == "failed"
    assert not any(d.kind == "APPROVAL_REQUIRED" for d in await db.inbox(state="open"))
    async with db.sessions() as session:
        assert not list(await session.scalars(select(Subtask).where(Subtask.task_id == task.id)))


async def test_saved_infeasible_plan_is_not_silently_rewritten(db, monkeypatch):
    task, source = await setup_goal(db)
    data = plan().model_dump()
    data["budget"]["max_llm_calls"] = 5
    await db.update(task.id, plan_json=StaffPlan.model_validate(data).model_dump_json())
    calls = []
    monkeypatch.setattr(staff, "complete", provider(db, f"task:{source.id}", calls))
    await staff.run_staff_task(task.id, "worker")
    assert (await db.get(task.id)).status == "failed"
    assert calls == ["ResolvedIntent"]
    assert not any(d.kind == "APPROVAL_REQUIRED" for d in await db.inbox(state="open"))
