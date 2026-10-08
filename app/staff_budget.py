"""Rendered workflow estimates are distinct from per-call reservations and billed usage."""

import json
import math

from app.config import settings
from app.llm import completion_messages, completion_token_reserve, prompt_digest
from app.staff_schemas import OutputEvaluation


def context_payload(context, *, compact=False):
    values = (
        [{"ref": i.ref, "source": i.source, "content": i.content, "label": i.label} for i in context]
        if compact
        else [i.model_dump() for i in context]
    )
    return json.dumps(values, ensure_ascii=False, separators=(",", ":") if compact else None)


def skill_output_prompt(rules, skill, objective, context_json, dependencies):
    return f"{rules}\nSKILL:{skill}; objective:{objective}\nCONTEXT:{context_json}\nDEPENDENCIES:{json.dumps(dependencies)}"


def skill_review_prompt(rules, objective, context_json, output_json):
    return f"{rules}\nIndependently check claims against the supplied facts, goal and constraints. Reject unsupported inference, unsafe authority or missing required output.\nOBJECTIVE:{objective}\nCONTEXT:{context_json}\nOUTPUT:{output_json}"


def final_output_prompt(rules, objective, context_json, outputs):
    return f"{rules}\nProduce the final answer to the objective. If asked for top three, return at most three prioritized findings. Do not invent findings if data is insufficient.\nOBJECTIVE:{objective}\nCONTEXT:{context_json}\nSKILL OUTPUTS:{json.dumps(outputs)}"


def final_review_prompt(rules, plan, context_json, output_json):
    return f"{rules}\nCheck final result against success criteria; reject any unsupported claim.\nPLAN:{plan.model_dump_json()}\nCONTEXT:{context_json}\nRESULT:{output_json}"


def select_context(context, objective, required_refs=(), *, complete=False):
    """Keep complete scoped audits; other skills get relevant authorized slices."""
    if complete:
        return list(context)
    words = {w.casefold().strip(".,!?;") for w in objective.split() if len(w) > 3}
    required = set(required_refs)
    ranked = sorted(
        context,
        key=lambda item: (
            item.ref not in required,
            -sum(w in (item.scope + " " + item.content).casefold() for w in words),
            item.label != "current",
        ),
    )
    selected = []
    size = 0
    for item in ranked:
        cost = len(item.model_dump_json())
        if item.ref in required or size + cost <= min(12000, settings.max_prompt_chars // 3):
            selected.append(item)
            size += cost
    return selected


def workflow_envelope(
    plan,
    requirement,
    context,
    rules,
    schema,
    renderer,
    *,
    completed=(),
    final_calls=2,
    full_context=False,
    compact_context=False,
):
    """Size unseen outputs with explicit assumptions; actual reservations remain authoritative."""
    calls = []
    output_bound = "x" * 7000
    full_json = context_payload(context, compact=compact_context)
    for step in plan.steps:
        if step.id in completed:
            continue
        selected = select_context(context, step.objective, complete=full_context)
        # Dependency refs are not known yet. For preflight use the complete authorized
        # slice for planning; execution can narrow it without adding new refs.
        context_json = full_json if step.dependencies else context_payload(selected, compact=compact_context)
        objective = requirement if full_context else step.objective
        dependencies = {key: output_bound for key in step.dependencies}
        calls += [
            (
                step.id + ":output",
                schema,
                skill_output_prompt(rules, step.skill, objective, context_json, dependencies),
            ),
            (
                step.id + ":review",
                OutputEvaluation,
                skill_review_prompt(rules, objective, context_json, output_bound),
            ),
        ]
    if final_calls:
        calls += [
            (
                "final:output",
                schema,
                final_output_prompt(rules, requirement, full_json, {s.id: output_bound for s in plan.steps}),
            ),
            ("final:review", OutputEvaluation, final_review_prompt(rules, plan, full_json, output_bound)),
        ]
    entries = []
    for name, contract, user in calls:
        contract, system, user = renderer(contract, user, degraded=True)
        messages = completion_messages(system, user, contract)
        input_bytes = sum(len(m["content"].encode()) for m in messages)
        entries.append(
            {
                "call": name,
                "schema": contract.__name__,
                "prompt_sha256": prompt_digest(messages),
                "planned_input_bytes": input_bytes,
                "planned_token_reservation": completion_token_reserve(messages),
                "estimated_tokens": math.ceil(input_bytes / 4) + settings.max_output_tokens,
            }
        )
    retry_slots = sum(max(0, step.max_llm_calls - 2) for step in plan.steps if step.id not in completed)
    final_retry_slots = (
        min(4, max(0, plan.budget.max_llm_calls - len(entries) - retry_slots)) if final_calls else 0
    )
    maximum = max((entry["planned_token_reservation"] for entry in entries), default=0)
    return {
        "method": "Rendered schema-bearing messages; 7000-char output/dependency placeholders. ASCII placeholders are sizing assumptions, not Unicode byte bounds. Byte/4 + configured output tokens is a planning heuristic, not a tokenizer or bill; per-call rendered reservations and actual usage remain binding.",
        "calls": entries,
        "mandatory_calls": len(entries),
        "retry_call_slots": retry_slots,
        "final_retry_call_slots": final_retry_slots,
        "estimated_tokens": sum(e["estimated_tokens"] for e in entries),
        "planned_mandatory_daily_reservations": sum(e["planned_token_reservation"] for e in entries),
        "planned_retry_daily_reservations": (retry_slots + final_retry_slots) * maximum,
        "max_single_reservation": maximum,
        "cost_estimate": None,
        "cost_note": "No provider price assumed; existing reported-cost/call/token limits remain binding.",
    }
