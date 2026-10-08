"""Admit Engineering plans before their first Developer iteration."""

import re

from app.agents import developer_request, developer_turn, reviewer_request
from app.config import settings
from app.llm import completion_messages, completion_token_reserve
from app.schemas import DeveloperAction, LeadPlan, PlanBudget, ReviewResult

# Inspect source/tests, change source/tests, test, inspect diff, finish; then review.
# This is a planning baseline, not a promise that every implementation takes seven calls.
DEVELOPER_BASELINE_CALLS = 7
REVIEW_CALLS_PER_ITERATION = 1
SCHEMA_RETRY_MARGIN_CALLS = 2

# A budget constraint is explicit only when an amount is bound to budget vocabulary:
# "Budget maksimal 30000 token", "At most 20 calls", "$0.50", "USD 500", "50000 token".
# Prose that merely mentions budget/calls/tokens/USD without any amount (regression:
# tasks #81/#82, "Repeated budget failure in 17 tasks") must stay adjustable so the
# admission can re-fund fresh plans inside operator ceilings.
_EXPLICIT_BUDGET_RE = re.compile(
    r"\b(?:budget|anggaran)\b\s*(?:batas|maksimal|max|limit|total)?\s*[:=]?\s*\d[\d.,]*"
    r"|(?:batas|maksimal|max|limit|total|at\s+most|up\s+to|no\s+more\s+than|<=|<)\s*[:=]?\s*"
    r"[\d.,_ ]*?\d[\d.,]*\s*(?:k\b)?\s*(?:tokens?|calls?|panggilan|usd)\b"
    r"|\$\s*\d[\d.,]*"
    r"|\busd\b\s*[\d.,]*\d"
    r"|\b\d[\d.,]*\s*(?:k\b)?\s*usd\b"
    r"|\b\d\d[\d.,]*\s*(?:k\b)?\s*(?:tokens?|calls?|panggilan)\b",
    re.I,
)


def engineering_iteration_step_limit(
    *, current_calls: int, max_calls: int, iteration: int, max_iterations: int
) -> int:
    """Allocate Developer actions without consuming later repair/review capacity."""
    future_iterations = max(0, max_iterations - iteration)
    future_reserve = future_iterations * (DEVELOPER_BASELINE_CALLS + REVIEW_CALLS_PER_ITERATION)
    current_review = REVIEW_CALLS_PER_ITERATION
    return max(
        0,
        min(settings.max_dev_steps, max_calls - current_calls - current_review - future_reserve),
    )


def engineering_budget_admission(
    plan: LeadPlan,
    *,
    requirement: str,
    file_index: str,
    context: str,
    used_calls: int,
    used_tokens: int,
    used_cost: float,
    fresh: bool,
    reviewer_feedback: list[str],
    readmit: bool = False,
) -> tuple[LeadPlan, dict]:
    """Correct fresh model estimates and re-fund retry/recovery plans inside operator ceilings.

    Explicit user budget constraints remain binding. Readmission (readmit=True on an
    unapproved saved plan, as on retry or recovery) re-funds the remaining calls/tokens
    for the lifetime usage already spent, never above operator ceilings; an approved
    plan keeps its exact binding. The initial prompts provide an explainable
    baseline; future reads/diffs/retries can still consume more, and all runtime
    reservations remain enforced by the store. Lifetime usage is never reset.
    """
    estimate = plan.budget.model_dump()
    explicit = bool(_EXPLICIT_BUDGET_RE.search(requirement))
    adjustable = (fresh or readmit) and not explicit
    admitted = plan.model_copy(deep=True)
    minimum_calls = used_calls + DEVELOPER_BASELINE_CALLS + 1
    # Fund one complete authoring iteration plus a bounded repair/review baseline
    # for every remaining iteration. Otherwise MAX_DEV_STEPS can consume the
    # lifetime envelope before deterministic test feedback can be repaired.
    execution_calls = (
        used_calls
        + settings.max_dev_steps
        + REVIEW_CALLS_PER_ITERATION
        + (settings.max_iterations - 1) * (DEVELOPER_BASELINE_CALLS + REVIEW_CALLS_PER_ITERATION)
        + SCHEMA_RETRY_MARGIN_CALLS
    )
    if adjustable:
        admitted.budget.max_llm_calls = min(
            settings.max_llm_calls, max(estimate["max_llm_calls"], minimum_calls, execution_calls)
        )

    # Re-render once after allocation: the budget itself appears in both prompts.
    for _ in range(2):
        system, user = developer_request(
            requirement=requirement,
            plan=admitted,
            file_index=file_index,
            reviewer_feedback=reviewer_feedback,
        )
        developer_reserve = completion_token_reserve(
            completion_messages(system, developer_turn(user), DeveloperAction)
        )
        review_system, review_user = reviewer_request(
            requirement=requirement,
            plan=admitted,
            diff="",
            test_output="",
            hygiene_issues=[],
            context=context[:22000],
            standalone_output="No new or changed test files",
        )
        reviewer_reserve = completion_token_reserve(
            completion_messages(review_system, review_user, ReviewResult)
        )
        minimum_tokens = used_tokens + DEVELOPER_BASELINE_CALLS * developer_reserve + reviewer_reserve
        planned_calls = max(0, admitted.budget.max_llm_calls - used_calls - 1)
        allocation = used_tokens + planned_calls * developer_reserve + reviewer_reserve
        if adjustable:
            # Round upward for prompt-length changes; never increase operator policy.
            admitted.budget.max_tokens = min(
                settings.max_total_tokens,
                max(estimate["max_tokens"], PlanBudget().max_tokens, ((allocation + 999) // 1000) * 1000),
            )

    limits = {
        "max_llm_calls": min(settings.max_llm_calls, admitted.budget.max_llm_calls),
        "max_total_tokens": min(settings.max_total_tokens, admitted.budget.max_tokens),
        "max_cost_usd": min(settings.max_cost_usd, admitted.budget.max_cost_usd),
    }
    issues = []
    if limits["max_llm_calls"] < minimum_calls:
        issues.append(f"calls: baseline={minimum_calls}, limit={limits['max_llm_calls']}")
    if limits["max_total_tokens"] < minimum_tokens:
        issues.append(
            f"tokens: used={used_tokens}, developer_reserve={developer_reserve}, "
            f"reviewer_reserve={reviewer_reserve}, baseline={minimum_tokens}, "
            f"limit={limits['max_total_tokens']}"
        )
    if used_cost >= limits["max_cost_usd"]:
        issues.append(f"reported_cost: used={used_cost}, limit={limits['max_cost_usd']}")
    diagnostic = {
        "workflow": "engineering",
        "fresh": fresh,
        "readmit": readmit,
        "explicit_budget": explicit,
        "estimate": estimate,
        "admitted": admitted.budget.model_dump(),
        "effective_limits": limits,
        "used": {"calls": used_calls, "tokens": used_tokens, "reported_cost_usd": used_cost},
        "developer_baseline_calls": DEVELOPER_BASELINE_CALLS,
        "repair_iterations_funded": settings.max_iterations - 1,
        "review_calls_per_iteration": REVIEW_CALLS_PER_ITERATION,
        "schema_retry_margin_calls": SCHEMA_RETRY_MARGIN_CALLS,
        "developer_reserve": developer_reserve,
        "reviewer_reserve": reviewer_reserve,
        "minimum_calls": minimum_calls,
        "execution_calls": execution_calls,
        "minimum_tokens": minimum_tokens,
        "planned_allocation_tokens": allocation,
        "issues": issues,
        "note": "Initial prompt byte reserves plus output allowance; not billed tokens or guaranteed completion. Operator ceilings are never raised; lifetime usage is retained. Readmission on retry/recovery re-funds the remaining calls/tokens within those ceilings unless the requirement sets an explicit budget or the plan was already approved.",
    }
    return admitted, diagnostic
