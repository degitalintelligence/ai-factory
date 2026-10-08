"""Preflight accounts for rendered schemas, dependencies, review and bounded retry slots."""

import json

from app import staff
from app.config import settings
from app.llm import completion_messages, completion_token_reserve
from app.staff_budget import select_context, skill_output_prompt, workflow_envelope
from app.staff_schemas import ChatRequest, ContextItem, ResolvedIntent, StaffOutput, StaffPlan


def item(ref="memory:1:v1", content="Relevant product strategy", label="current"):
    return ContextItem(
        ref=ref,
        source="memory",
        scope="lab",
        owner=7,
        created_at="2026-10-09",
        confidence=0.5 if label != "current" else 1,
        label=label,
        content=content,
    )


def plan(tokens=120000):
    return StaffPlan(
        objective="Review product strategy",
        success_criteria=["Grounded recommendation"],
        steps=[
            {
                "id": "research",
                "skill": "product_research",
                "objective": "Review product strategy",
                "max_llm_calls": 3,
            },
            {
                "id": "summarize",
                "skill": "engineering",
                "objective": "Summarize evidence",
                "dependencies": ["research"],
                "max_llm_calls": 3,
            },
        ],
        risks=["Limited evidence"],
        approval_gates=["Draft only"],
        rollback_plan="Discard draft",
        budget={"max_tokens": tokens, "max_llm_calls": 24},
    )


def test_envelope_includes_review_dependency_outputs_finalization_and_retries():
    data = workflow_envelope(
        plan(), "Review strategy", [item()], "Cite exact refs", StaffOutput, staff.render_staff_prompt
    )
    assert data["mandatory_calls"] == 6 and data["retry_call_slots"] == 2
    assert data["cost_estimate"] is None
    assert data["planned_mandatory_daily_reservations"] > data["estimated_tokens"]
    assert (
        next(c for c in data["calls"] if c["call"] == "summarize:output")["planned_input_bytes"]
        > next(c for c in data["calls"] if c["call"] == "research:output")["planned_input_bytes"]
    )


def test_estimate_uses_actual_schema_renderer_and_reservation_helper():
    context = [item()]
    data = workflow_envelope(
        plan(), "Review strategy", context, "Rules", StaffOutput, staff.render_staff_prompt
    )
    user = skill_output_prompt(
        "Rules",
        "product_research",
        plan().steps[0].objective,
        json.dumps([i.model_dump() for i in context], ensure_ascii=False),
        {},
    )
    schema, system, rendered = staff.render_staff_prompt(StaffOutput, user, degraded=True)
    assert data["calls"][0]["planned_token_reservation"] == completion_token_reserve(
        completion_messages(system, rendered, schema)
    )


def test_completed_checkpoint_removes_only_its_own_calls():
    data = workflow_envelope(
        plan(),
        "Review strategy",
        [item()],
        "Rules",
        StaffOutput,
        staff.render_staff_prompt,
        completed={"research"},
        final_calls=0,
    )
    assert data["mandatory_calls"] == 2 and data["retry_call_slots"] == 1
    assert all(c["call"].startswith("summarize:") for c in data["calls"])


def test_context_slicing_preserves_required_refs_and_scoped_audit():
    context = [item(ref=f"memory:{i}:v1", content="x" * 3900) for i in range(8)]
    selected = select_context(context, "Unrelated objective", [context[-1].ref])
    assert context[-1] in selected and len(selected) < len(context)
    assert select_context(context, "Audit", complete=True) == context


async def test_insufficient_tokens_stop_before_skill_calls_or_approval(db, monkeypatch):
    task = await staff.create_intent(
        ChatRequest(message="Review product strategy", project="lab", idempotency_key="token-preflight"), 7
    )
    await db.claim("worker")
    calls = []

    async def provider(*, schema, **kwargs):
        calls.append(schema.__name__)
        if schema is ResolvedIntent:
            return ResolvedIntent(objective="Review strategy", desired_outcome="Grounded recommendation")
        if schema is StaffPlan:
            return plan(tokens=1000)
        raise AssertionError("No specialist should run after failed preflight")

    monkeypatch.setattr(staff, "complete", provider)
    await staff.run_staff_task(task.id, "worker")
    assert calls == ["ResolvedIntent", "StaffPlan"]
    assert (await db.get(task.id)).status == "failed"
    assert not any(d.kind == "APPROVAL_REQUIRED" for d in await db.inbox())
    artifacts = await db.artifacts(task.id)
    envelope = json.loads(next(a.content for a in artifacts if a.kind == "workflow_budget_envelope"))
    assert envelope["limits"]["max_total_tokens"] == 1000


async def test_daily_preflight_cannot_widen_global_budget(db, monkeypatch):
    monkeypatch.setattr(settings, "global_max_tokens_per_day", 1000)
    task = await staff.create_intent(
        ChatRequest(message="Review product strategy", project="lab", idempotency_key="daily-preflight"), 7
    )
    await db.claim("worker")

    async def provider(*, schema, **kwargs):
        return (
            ResolvedIntent(objective="Review strategy", desired_outcome="Grounded recommendation")
            if schema is ResolvedIntent
            else plan()
        )

    monkeypatch.setattr(staff, "complete", provider)
    await staff.run_staff_task(task.id, "worker")
    assert (await db.get(task.id)).status == "failed"
    assert settings.global_max_tokens_per_day == 1000


async def test_real_prompt_renderer_matches_gateway_call(db, monkeypatch):
    captured = {}

    async def gateway(**kwargs):
        captured.update(kwargs)
        return ResolvedIntent(objective="Review", desired_outcome="Useful answer")

    monkeypatch.setattr(staff, "json_completion", gateway)
    await staff.complete(schema=ResolvedIntent, role="lead", user="Review product")
    schema, system, user = staff.render_staff_prompt(ResolvedIntent, "Review product")
    assert captured["schema"] is schema and captured["system"] == system and captured["user"] == user
