"""Outcome metrics distinguish execution, handoff and pending work, not model confidence."""

import json
from collections import Counter


def task_outcomes(tasks, artifacts):
    by_id = {t.id: t for t in tasks}
    handoffs = {}
    accepted = set()
    for item in artifacts:
        if item.kind == "engineering_handoff":
            try:
                handoffs[item.task_id] = json.loads(item.content)["task_id"]
            except (ValueError, KeyError, TypeError):
                handoffs[item.task_id] = None
        elif item.kind == "improvement_outcome":
            accepted.add(item.task_id)
    counts = Counter()
    outcomes = {}
    for task in tasks:
        effective = task
        if task.id in handoffs:
            counts["handoffs"] += 1
            child = by_id.get(handoffs[task.id])
            if (
                child is None
                or child.tenant != task.tenant
                or child.user_id != task.user_id
                or child.project != task.project
            ):
                outcomes[task.id] = "handoff_unverified"
                counts["handoff_unverified"] += 1
                continue
            effective = child
            outcomes[task.id] = f"handoff:{child.id}:{child.status}"
            # Count the actual engineering task once, not its parent acknowledgement.
            continue
        if effective.status in {"completed", "reviewed", "deployed"}:
            category = "successful"
        elif effective.status in {"failed", "deployment_failed"}:
            category = "failed"
        elif effective.status in {"cancelled", "superseded"}:
            category = "stopped"
        elif effective.status == "pr_created":
            category = "reviewed_prs"
        elif effective.status in {
            "waiting_input",
            "awaiting_approval",
            "deployment_unknown",
            "deployment_pending",
        }:
            category = "blocked"
        else:
            category = "active"
        counts[category] += 1
        outcomes[task.id] = category
    denominator = counts["successful"] + counts["failed"]
    return {
        "outcomes": dict(counts),
        "task_outcomes": outcomes,
        "terminal_evaluated_tasks": denominator,
        "success_rate": counts["successful"] / denominator if denominator else None,
        "success_rate_definition": "successful / (successful + failed); excludes handoffs, active, blocked, cancelled, superseded and open PRs",
        "measured_improvements": len(accepted),
        "business_acceptance": "Not inferred from CI, task completion, handoff or PR publication.",
    }
