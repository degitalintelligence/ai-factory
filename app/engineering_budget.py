"""Admit Engineering plans before their first Developer iteration."""

import re

from app.agents import developer_request, developer_turn, reviewer_request
from app.config import settings
from app.llm import completion_messages, completion_token_reserve
from app.schemas import DeveloperAction, LeadPlan, PlanBudget, ReviewResult

# Inspect source/tests, change source/tests, test, inspect diff, finish; then review.
# This is a planning baseline, not a promise that every implementation takes seven calls.
DEVELOPER_BASELINE_CALLS = 7


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
) -> tuple[LeadPlan, dict]:
    """Correct only fresh model estimates inside existing operator ceilings.

    Saved/approved plans and user budget constraints remain binding. The initial
    prompts provide an explainable baseline; future reads/diffs/retries can still
    consume more, and all runtime reservations remain enforced by the store.
    """
    estimate = plan.budget.model_dump()
    explicit = bool(re.search(r"\b(?:budget|anggaran|calls?|panggilan|tokens?|usd)\b|\$", requirement, re.I))
    adjustable = fresh and not explicit
    admitted = plan.model_copy(deep=True)
    minimum_calls = used_calls + DEVELOPER_BASELINE_CALLS + 1
    # A one-action-per-call Developer can legitimately need more than a model's
    # estimate of 20 calls. Fund one bounded iteration and gateway review attempts.
    execution_calls = used_calls + settings.max_dev_steps + 3
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
        "explicit_budget": explicit,
        "estimate": estimate,
        "admitted": admitted.budget.model_dump(),
        "effective_limits": limits,
        "used": {"calls": used_calls, "tokens": used_tokens, "reported_cost_usd": used_cost},
        "developer_baseline_calls": DEVELOPER_BASELINE_CALLS,
        "developer_reserve": developer_reserve,
        "reviewer_reserve": reviewer_reserve,
        "minimum_calls": minimum_calls,
        "execution_calls": execution_calls,
        "minimum_tokens": minimum_tokens,
        "planned_allocation_tokens": allocation,
        "issues": issues,
        "note": "Initial prompt byte reserves plus output allowance; not billed tokens or guaranteed completion. Saved/user budgets and operator ceilings are never raised; lifetime usage is retained.",
    }
    return admitted, diagnostic
