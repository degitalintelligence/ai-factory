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
    async def complete(*, schema, role, user, max_attempts=3):
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
    assert not any(a.kind == "staff_draft" for a in await db.artifacts(task.id))
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

    async def complete(*, schema, role, user, max_attempts=3):
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

    async def crash(*, schema, role, user, max_attempts=3):
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

    async def finance(*, schema, role, user, max_attempts=3):
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

    async def clarify(*, schema, role, user, max_attempts=3):
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

    async def incomplete(*, schema, role, user, max_attempts=3):
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
    monkeypatch.setattr(settings, "max_llm_calls", 10)
    task, source = await setup_goal(db)
    real = provider(db, f"task:{source.id}", [])
    plans = []

    async def oversized(*, schema, role, user, max_attempts=3):
        result = await real(schema=schema, role=role, user=user)
        if schema is StaffPlan:
            plans.append(user)
            if len(plans) == 2:
                assert max_attempts == 1
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
    monkeypatch.setattr(settings, "max_llm_calls", 10)
    task, source = await setup_goal(db)
    real = provider(db, f"task:{source.id}", [])
    planning_calls = []

    async def oversized(*, schema, role, user, max_attempts=3):
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
    assert "Plan cannot fit:" in (await db.get(task.id)).last_message
    assert f"/report {task.id}" in (await db.get(task.id)).last_message
    assert not any(d.kind == "APPROVAL_REQUIRED" for d in await db.inbox(state="open"))


async def test_named_chat_task_diagnostics_are_in_context_without_scope_leak(db):
    source = await staff.create_intent(
        ChatRequest(message="Audit stopped previously", idempotency_key="previous"), 7
    )
    await db.update(source.id, status="failed")
    async with db.sessions() as session, session.begin():
        session.add(
            Artifact(task_id=source.id, kind="staff_failure", content="7 calls used + 4 required; limit 10")
        )
    private = await staff.create_intent(
        ChatRequest(message="Private chat evidence", idempotency_key="private"), 8
    )
    goal = await staff.create_intent(
        ChatRequest(message=f"Analisis task #{source.id} saja", idempotency_key="diagnose"), 7
    )
    context = await staff.assemble_context(goal)
    assert any(
        item.ref == f"task:{source.id}:evidence" and "7 calls used + 4 required" in item.content
        for item in context
    )
    assert not any(item.ref.startswith(f"task:{private.id}") for item in context)
    # A project-specific goal must not widen to unscoped cross-project chat history.
    scoped = await staff.create_intent(
        ChatRequest(message=f"Analisis task #{source.id} saja", project="self", idempotency_key="scoped"), 7
    )
    assert not any(item.ref.startswith(f"task:{source.id}") for item in await staff.assemble_context(scoped))


@pytest.mark.parametrize(
    "operator_limit, explicit_budget, expected",
    [(10, False, "completed"), (5, False, "failed"), (10, True, "failed")],
)
async def test_single_step_model_budget_accounts_for_intake_and_honors_caps(
    db, monkeypatch, operator_limit, explicit_budget, expected
):
    monkeypatch.setattr(settings, "max_llm_calls", operator_limit)
    source = await db.create("Recorded task evidence", user_id=7)
    await db.update(source.id, status="failed")
    message = f"Analisis task #{source.id} saja. Jelaskan satu penyebab berhenti."
    if explicit_budget:
        message += " Budget maksimal 5 calls."
    goal = await staff.create_intent(ChatRequest(message=message, idempotency_key="single"), 7)
    await db.claim("worker")
    real = provider(db, f"task:{source.id}", [])

    async def underestimated(*, schema, role, user, max_attempts=3):
        result = await real(schema=schema, role=role, user=user)
        if schema is StaffPlan:
            data = result.model_dump()
            data["steps"] = [data["steps"][0]]
            data["budget"]["max_llm_calls"] = 5
            return StaffPlan.model_validate(data)
        return result

    monkeypatch.setattr(staff, "complete", underestimated)
    await staff.run_staff_task(goal.id, "worker")
    saved = await db.get(goal.id)
    assert saved.status == expected
    artifacts = await db.artifacts(goal.id)
    if expected == "completed":
        assert saved.llm_calls == 6 and db.budget_envelope(saved)["max_llm_calls"] == 8
        assert any(a.kind == "plan_budget_accounting" for a in artifacts)
    else:
        assert saved.llm_calls == 2 and db.budget_envelope(saved)["max_llm_calls"] == 5
        assert not any(a.kind == "plan_budget_accounting" for a in artifacts)


@pytest.mark.parametrize("valid_retry", [True, False])
async def test_real_gateway_review_retry_is_funded_and_invalid_review_never_completes(
    db, monkeypatch, valid_retry
):
    import json

    from openai import AsyncOpenAI

    from app import llm

    source = await db.create("Recorded failure evidence", user_id=7)
    await db.update(source.id, status="failed")
    goal = await staff.create_intent(
        ChatRequest(
            message=f"Analisis task #{source.id} saja. Jelaskan satu penyebab berhenti.",
            idempotency_key="review-retry",
        ),
        7,
    )
    await db.claim("worker")
    data = plan().model_dump()
    data["steps"] = [data["steps"][0]]
    data["steps"][0]["max_llm_calls"] = 2
    data["budget"]["max_llm_calls"] = 7
    approved = OutputEvaluation(approved=True, summary="Supported by recorded evidence").model_dump_json()
    replies = [
        ResolvedIntent(
            objective="Explain one task failure", desired_outcome="One supported cause"
        ).model_dump_json(),
        json.dumps(data),
        output(f"task:{source.id}").model_dump_json(),
        '{"approved":true,"summary":"missing closing brace"',
        approved if valid_retry else '{"approved":',
        output(f"task:{source.id}").model_dump_json(),
        approved,
    ]
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "test",
                "object": "chat.completion",
                "created": 1,
                "model": "test",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": replies[len(requests) - 1]},
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30, "cost": 0},
            },
        )

    monkeypatch.setattr(
        llm,
        "AsyncOpenAI",
        lambda **kwargs: AsyncOpenAI(
            api_key="placeholder", http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        ),
    )
    monkeypatch.setattr(settings, "openrouter_api_key", "placeholder")
    await staff.run_staff_task(goal.id, "worker")
    saved = await db.get(goal.id)
    artifacts = await db.artifacts(goal.id)
    async with db.sessions() as session:
        step = await session.scalar(select(Subtask).where(Subtask.task_id == goal.id))
    assert step.calls == 3
    if valid_retry:
        assert saved.status == "completed" and saved.llm_calls == 7 and len(requests) == 7
        assert step.status == "completed" and any(a.kind == "staff_result" for a in artifacts)
    else:
        assert saved.status == "failed" and saved.llm_calls == 5 and len(requests) == 5
        assert step.status != "completed" and not any(a.kind == "staff_result" for a in artifacts)
        failure = next(a.content for a in artifacts if a.kind == "staff_failure")
        assert "invalid structured output after 2 attempts" in failure
        assert "Subtask lifetime model-call budget exhausted" not in failure
        draft = next(a.content for a in artifacts if a.kind == "staff_draft")
        assert json.loads(draft)["output"] == output(f"task:{source.id}").model_dump()
        assert not any(a.kind == "staff_draft_evaluation" for a in artifacts)


def factual_message(task_id):
    return f"Analisis task #{task_id} saja. Jelaskan satu penyebab berhenti berdasarkan evidence yang tersedia. Maksimal 150 kata. Jangan melakukan perubahan."


@pytest.mark.parametrize("rejected", [False, True])
async def test_factual_contract_focus_review_and_recovery(db, monkeypatch, rejected):
    import json

    from app.staff_schemas import FactualOutput

    source = await db.create("Recorded task", user_id=7)
    await db.update(source.id, status="failed", last_message="Budget exhausted")
    await db.artifact(source.id, "staff_failure", '{"detail":"7 calls used + 4 required; limit 10"}')
    other = await db.create("UNRELATED PRIVATE CONTEXT", user_id=8)
    await db.update(other.id, status="failed")
    goal = await staff.create_intent(
        ChatRequest(message=factual_message(source.id), idempotency_key="factual"), 7
    )
    await db.claim("worker")
    monkeypatch.setattr(settings, "max_llm_calls", 10)
    monkeypatch.setattr(settings, "github_token", "placeholder")
    monkeypatch.setattr(
        staff.GitHubAPI, "branch_sha", AsyncMock(side_effect=AssertionError("No repository reads"))
    )
    calls = []
    answer = FactualOutput(
        summary="Task berhenti karena rencana membutuhkan 11 panggilan: 7 telah digunakan dan 4 masih diperlukan, melebihi batas 10. Ini adalah kondisi berhenti yang tercatat; akar masalah belum terbukti.",
        evidence_refs=[f"task:{source.id}:evidence"],
        confidence=1,
    )

    async def complete(*, schema, role, user, max_attempts=3):
        calls.append(schema.__name__)
        assert "UNRELATED PRIVATE CONTEXT" not in user
        await db.reserve_call(*run_context.get(), token_reserve=1, subtask=subtask_context.get())
        if schema is ResolvedIntent:
            return ResolvedIntent(objective="Explain one cause", desired_outcome="Short factual answer")
        if schema is FactualOutput:
            assert "Tanpa prioritas" in user
            return answer
        assert schema is OutputEvaluation
        assert "bukan nama field atau enum schema" in user
        return OutputEvaluation(
            approved=not rejected,
            issues=["Klaim tidak didukung"] if rejected else [],
            summary="Evidence reviewed",
        )

    monkeypatch.setattr(staff, "complete", complete)
    await staff.run_staff_task(goal.id, "worker")
    saved = await db.get(goal.id)
    artifacts = {a.kind: a.content for a in await db.artifacts(goal.id)}
    context = json.loads(artifacts["context"])
    assert {i["ref"] for i in context} == {f"task:{source.id}", f"task:{source.id}:evidence"}
    assert "factual_draft" in artifacts and "factual_draft_evaluation" in artifacts
    assert saved.llm_calls == 3
    assert "StaffPlan" not in calls
    async with db.sessions() as session:
        cards = list(await session.scalars(select(Decision).where(Decision.task_id == goal.id)))
    if rejected:
        assert saved.status == "failed" and "staff_result" not in artifacts
        assert "Klaim tidak didukung" in saved.last_message
        assert all(c.category != "recommendation" for c in cards)
    else:
        assert saved.status == "completed" and not cards
        result = FactualOutput.model_validate_json(artifacts["staff_result"])
        assert len(staff.render_result(goal.id, result).split()) <= 150
        assert "prioritas" not in staff.render_result(goal.id, result)
        assert "next_action" not in json.loads(artifacts["staff_result"])
        await staff.run_staff_task(goal.id, "worker")
        assert len(calls) == 3


@pytest.mark.parametrize("scope,owner", [("", 8), ("self", 7)])
async def test_factual_named_task_cannot_widen_access(db, monkeypatch, scope, owner):
    source = await staff.create_intent(
        ChatRequest(message="Private unscoped source", idempotency_key="private"), owner
    )
    await db.update(source.id, status="failed")
    goal = await staff.create_intent(
        ChatRequest(message=factual_message(source.id), project=scope, idempotency_key="blocked"), 7
    )
    await db.claim("worker")
    monkeypatch.setattr(
        staff, "complete", AsyncMock(side_effect=AssertionError("No model call without evidence"))
    )
    await staff.run_staff_task(goal.id, "worker")
    saved = await db.get(goal.id)
    assert saved.status == "failed" and saved.llm_calls == 0
    assert "scope akses" in saved.last_message


def test_factual_validation_and_conservative_routing():
    from app.staff_schemas import ContextItem, FactualOutput

    context = [
        ContextItem(
            ref="task:24",
            source="tasks",
            scope="",
            owner=7,
            created_at="now",
            confidence=1,
            content="Recorded failure",
        )
    ]
    assert staff.factual_request(factual_message(24)) == (24, 150)
    assert staff.factual_request(factual_message(24) + " Deploy sekarang.") is None
    assert staff.factual_request(factual_message(24).replace("150", "1000")) is None
    answer = FactualOutput(summary="Bukti tersedia.", evidence_refs=["task:99"], confidence=1)
    assert "unauthorized" in staff.validate_factual(answer, context, 150)[0]
    answer = FactualOutput(summary="kata " * 150, evidence_refs=["task:24"], confidence=1)
    assert "word limit" in staff.validate_factual(answer, context, 150)[0]
    with pytest.raises(ValidationError):
        FactualOutput(summary="Bukti tersedia.", evidence_refs=["task:24"], confidence=1, priority="high")


async def test_factual_gateway_retries_invalid_json_without_planner(db, monkeypatch):
    import json

    from openai import AsyncOpenAI

    from app import llm
    from app.staff_schemas import FactualOutput

    source = await db.create("Recorded failed task", user_id=7)
    await db.update(source.id, status="failed")
    await db.artifact(source.id, "staff_failure", '{"detail":"7 used + 4 required; limit 10"}')
    goal = await staff.create_intent(
        ChatRequest(message=factual_message(source.id), idempotency_key="gateway-factual"), 7
    )
    await db.claim("worker")
    answer = FactualOutput(
        summary="Rencana memerlukan 11 panggilan, melebihi batas 10: 7 terpakai dan 4 masih diperlukan.",
        evidence_refs=[f"task:{source.id}:evidence"],
        confidence=1,
    ).model_dump_json()
    approved = OutputEvaluation(approved=True, summary="Bukti dan batas kata sesuai").model_dump_json()
    replies = [
        ResolvedIntent(
            objective="Jelaskan satu penyebab", desired_outcome="Jawaban ringkas"
        ).model_dump_json(),
        answer,
        '{"approved":',
        approved,
        answer,
        approved,
    ]
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "test",
                "object": "chat.completion",
                "created": 1,
                "model": "test",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": replies[len(requests) - 1]},
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30, "cost": 0},
            },
        )

    monkeypatch.setattr(
        llm,
        "AsyncOpenAI",
        lambda **kwargs: AsyncOpenAI(
            api_key="placeholder", http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        ),
    )
    monkeypatch.setattr(settings, "openrouter_api_key", "placeholder")
    await staff.run_staff_task(goal.id, "worker")
    saved = await db.get(goal.id)
    assert saved.status == "completed" and saved.llm_calls == 4
    async with db.sessions() as session:
        step = await session.scalar(select(Subtask).where(Subtask.task_id == goal.id))
        assert step.calls == 3
    assert all("StaffPlan" not in r["messages"][0]["content"] for r in requests)


async def test_factual_operator_ceiling_is_not_raised(db, monkeypatch):
    source = await db.create("Failed evidence", user_id=7)
    await db.update(source.id, status="failed")
    goal = await staff.create_intent(
        ChatRequest(message=factual_message(source.id), idempotency_key="low-cap"), 7
    )
    await db.claim("worker")
    monkeypatch.setattr(settings, "max_llm_calls", 2)
    calls = []

    async def complete(**kwargs):
        calls.append(kwargs["schema"])
        await db.reserve_call(*run_context.get(), token_reserve=1)
        return ResolvedIntent(objective="Analisis terbatas", desired_outcome="Satu penyebab")

    monkeypatch.setattr(staff, "complete", complete)
    await staff.run_staff_task(goal.id, "worker")
    saved = await db.get(goal.id)
    assert saved.status == "failed" and saved.llm_calls == 1
    assert calls == [ResolvedIntent]
    assert StaffPlan.model_validate_json(saved.plan_json).budget.max_llm_calls == 2
    assert "Plan cannot fit" in saved.last_message


@pytest.mark.parametrize("tampered_review", [False, True])
async def test_factual_reviewed_checkpoint_survives_restart_without_rewrite(db, monkeypatch, tampered_review):
    from app.staff_schemas import FactualOutput
    from app.store import TaskStopped

    source = await db.create("Recorded failure", user_id=7)
    await db.update(source.id, status="failed")
    goal = await staff.create_intent(
        ChatRequest(message=factual_message(source.id), idempotency_key="restart-exact"), 7
    )
    await db.claim("worker")
    answer = FactualOutput(
        summary="Rencana memerlukan 11 panggilan dan melampaui batas 10.",
        evidence_refs=[f"task:{source.id}"],
        confidence=1,
    )
    calls = []

    async def complete(*, schema, **kwargs):
        calls.append(schema)
        await db.reserve_call(*run_context.get(), token_reserve=1, subtask=subtask_context.get())
        if schema is ResolvedIntent:
            return ResolvedIntent(objective="Satu penyebab", desired_outcome="Penjelasan ringkas")
        if schema is FactualOutput:
            return answer
        return OutputEvaluation(approved=True, summary="Bukti sesuai")

    monkeypatch.setattr(staff, "complete", complete)
    original_update = db.update

    async def interrupt_update(task_id, *args, **kwargs):
        if kwargs.get("status") == "reviewing":
            raise TaskStopped("Simulated interruption after independent review")
        return await original_update(task_id, *args, **kwargs)

    monkeypatch.setattr(db, "update", interrupt_update)
    with pytest.raises(TaskStopped):
        await staff.run_staff_task(goal.id, "worker")
    assert calls == [ResolvedIntent, FactualOutput, OutputEvaluation]
    if tampered_review:
        async with db.sessions() as session, session.begin():
            row = await session.scalar(select(Subtask).where(Subtask.task_id == goal.id))
            row.evaluation_json = OutputEvaluation(
                approved=False, issues=["Unsupported claim"], summary="Rejected"
            ).model_dump_json()
    monkeypatch.setattr(db, "update", original_update)
    await staff.run_staff_task(goal.id, "worker")
    assert len(calls) == 3
    artifacts = {a.kind: a.content for a in await db.artifacts(goal.id)}
    if tampered_review:
        assert (await db.get(goal.id)).status == "failed" and "staff_result" not in artifacts
    else:
        assert (await db.get(goal.id)).status == "completed"
        assert FactualOutput.model_validate_json(artifacts["staff_result"]) == answer
        assert artifacts["staff_result"] == artifacts["factual_draft"]


async def test_saved_factual_v1_plan_is_not_converted(db, monkeypatch):
    from app.staff_schemas import FactualOutput

    source = await db.create("Saved evidence", user_id=7)
    await db.update(source.id, status="failed")
    goal = await staff.create_intent(
        ChatRequest(message=factual_message(source.id), idempotency_key="old-v1"), 7
    )
    old_plan = plan()
    old_plan.steps = [old_plan.steps[0]]
    await db.update(goal.id, plan_json=old_plan.model_dump_json())
    await db.artifact(goal.id, "output_contract", "factual-v1")
    await db.claim("worker")
    calls = []

    async def complete(*, schema, **kwargs):
        calls.append(schema)
        await db.reserve_call(*run_context.get(), token_reserve=1, subtask=subtask_context.get())
        if schema is ResolvedIntent:
            return ResolvedIntent(objective="Satu penyebab", desired_outcome="Penjelasan ringkas")
        if schema is FactualOutput:
            return FactualOutput(
                summary="Tugas gagal berdasarkan bukti yang tersedia.",
                evidence_refs=[f"task:{source.id}"],
                confidence=1,
            )
        return OutputEvaluation(approved=True, summary="Bukti sesuai")

    monkeypatch.setattr(staff, "complete", complete)
    await staff.run_staff_task(goal.id, "worker")
    saved = await db.get(goal.id)
    assert saved.status == "completed" and saved.plan_json == old_plan.model_dump_json()
    assert calls == [ResolvedIntent, FactualOutput, OutputEvaluation, FactualOutput, OutputEvaluation]
    assert next(a.content for a in await db.artifacts(goal.id) if a.kind == "output_contract") == "factual-v1"


def test_compact_staff_contract_preserves_decision_fields_and_legacy_reader():
    from app.staff_schemas import CompactStaffOutput

    data = output("task:24").model_dump()
    compact = CompactStaffOutput.model_validate(data)
    assert compact.model_dump() == data
    assert len(compact.findings) == 3 and all(f.decision_required for f in compact.findings)
    assert set(CompactStaffOutput.model_fields) == set(StaffOutput.model_fields)
    data["summary"] = "x" * 701
    assert StaffOutput.model_validate(data).summary == data["summary"]
    with pytest.raises(ValidationError):
        CompactStaffOutput.model_validate(data)
    data = output("task:24").model_dump()
    finding = data["findings"][0]
    for field, limit in {
        "title": 120,
        "situation": 400,
        "why_now": 180,
        "recommendation": 350,
        "alternative": 250,
        "risk": 200,
    }.items():
        finding[field] = "x" * limit
    data["findings"] = [finding.copy() for _ in range(6)]
    with pytest.raises(ValidationError, match="7000 characters"):
        CompactStaffOutput.model_validate(data)


@pytest.mark.parametrize("recover", [True, False])
async def test_staff_audit_gateway_compact_output_and_truncation(db, monkeypatch, recover):
    import json

    from openai import AsyncOpenAI

    from app import llm

    goal, source = await setup_goal(db)
    good = output(f"task:{source.id}").model_dump_json()
    approved = OutputEvaluation(approved=True, summary="Claims supported").model_dump_json()
    replies = [
        ResolvedIntent(objective="Audit", desired_outcome="Three decisions").model_dump_json(),
        plan().model_dump_json(),
        "",
        good if recover else "x" * 19153,
        approved,
        good,
        approved,
        good,
        approved,
    ]
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        index = len(requests) - 1
        return httpx.Response(
            200,
            json={
                "id": "test",
                "object": "chat.completion",
                "created": 1,
                "model": "test",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "length" if index == 2 or (index == 3 and not recover) else "stop",
                        "message": {"role": "assistant", "content": replies[index]},
                    }
                ],
                "usage": {
                    "prompt_tokens": 7300,
                    "completion_tokens": 8192 if index == 2 or (index == 3 and not recover) else 100,
                    "total_tokens": 15492 if index == 2 or (index == 3 and not recover) else 7400,
                    "cost": 0,
                },
            },
        )

    monkeypatch.setattr(
        llm,
        "AsyncOpenAI",
        lambda **kwargs: AsyncOpenAI(
            api_key="placeholder", http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        ),
    )
    monkeypatch.setattr(settings, "openrouter_api_key", "placeholder")
    monkeypatch.setattr(settings, "max_total_tokens", 600000)
    await staff.run_staff_task(goal.id, "worker")
    saved = await db.get(goal.id)
    artifacts = {a.kind: a.content for a in await db.artifacts(goal.id)}
    assert requests[2]["max_tokens"] == settings.max_output_tokens
    assert "CompactStaffOutput" in requests[2]["messages"][0]["content"]
    assert "hard limit 7000" in requests[2]["messages"][1]["content"]
    assert "invalid_output" in artifacts or "llm_validation" in artifacts
    if recover:
        reviewer_prompt = requests[4]["messages"][1]["content"]
        assert "Audit findings are not review issues" in reviewer_prompt
        assert "exact claim" in reviewer_prompt
        assert "Do not rewrite the answer" in reviewer_prompt
    if recover:
        assert saved.status == "completed" and saved.llm_calls == 9
        result = StaffOutput.model_validate_json(artifacts["staff_result"])
        assert len(result.findings) == 3
        async with db.sessions() as session:
            cards = list(await session.scalars(select(Decision).where(Decision.task_id == goal.id)))
        assert len(cards) == 3
    else:
        assert saved.status == "failed" and saved.llm_calls == 4
        assert "staff_result" not in artifacts
        assert "invalid structured output after 2 attempts" in saved.last_message
        assert "truncated" in saved.last_message
        assert "x" * 100 not in artifacts["staff_failure"]


@pytest.mark.parametrize("final", [False, True])
@pytest.mark.parametrize("approved", [False, True])
async def test_rejected_staff_review_preserves_draft_and_never_publishes(db, monkeypatch, final, approved):
    import json

    task, source = await setup_goal(db)
    base = provider(db, f"task:{source.id}", [])
    reviews = 0

    async def complete(**kwargs):
        nonlocal reviews
        result = await base(**kwargs)
        if kwargs["schema"] is OutputEvaluation:
            reviews += 1
            if reviews == (3 if final else 1):
                return OutputEvaluation(
                    approved=approved,
                    issues=["Klaim memperluas bukti dua audit ke semua eksekusi; batasi cakupan."],
                    summary="Perlu koreksi cakupan.",
                )
        return result

    monkeypatch.setattr(staff, "complete", complete)
    await staff.run_staff_task(task.id, "worker")
    assert (await db.get(task.id)).status == "failed"
    artifacts = {a.kind: a.content for a in await db.artifacts(task.id)}
    if final:
        draft = StaffOutput.model_validate_json(artifacts["staff_final_draft"])
        evaluation = json.loads(artifacts["staff_final_draft_evaluation"])
    else:
        saved = json.loads(artifacts["staff_draft"])
        assert saved["step_id"] == "audit" and saved["plan_hash"]
        draft = StaffOutput.model_validate(saved["output"])
        evaluation = json.loads(artifacts["staff_draft_evaluation"])
        assert evaluation["step_id"] == "audit"
    assert draft == output(f"task:{source.id}")
    assert evaluation["evaluation"]["approved"] is approved
    assert evaluation["evaluation"]["issues"] and evaluation["local_issues"] == []
    assert "staff_result" not in artifacts
    async with db.sessions() as session:
        cards = list(await session.scalars(select(Decision).where(Decision.task_id == task.id)))
    assert all("Reliability issue" not in card.title for card in cards)


async def test_credential_draft_is_not_retained(db, monkeypatch):
    task, source = await setup_goal(db)
    base = provider(db, f"task:{source.id}", [])

    async def complete(**kwargs):
        result = await base(**kwargs)
        if kwargs["schema"] is StaffOutput:
            result.summary = "sk-" + "syntheticcredential" * 2
        return result

    monkeypatch.setattr(staff, "complete", complete)
    await staff.run_staff_task(task.id, "worker")
    assert (await db.get(task.id)).status == "failed"
    artifacts = await db.artifacts(task.id)
    assert not any(a.kind in {"staff_draft", "staff_final_draft", "staff_result"} for a in artifacts)
    assert not any("syntheticcredential" in a.content for a in artifacts)
