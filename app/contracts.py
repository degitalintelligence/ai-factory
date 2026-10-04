"""Channel-independent use cases.

Every channel adapter (Telegram, HTTP API, future dashboard) must call these
functions instead of reimplementing state transitions, so the same action has the
same validation, evidence and error message in every channel.
"""

import json

from app.config import settings
from app.deployment import DeploymentService
from app.schemas import CLEARANCE, DecisionRequest, TaskKind
from app.store import store

TASK_ACTIONS = ("cancel", "retry", "approve", "answer", "feedback")
DEPLOY_ACTIONS = ("deploy", "deployment")
PUBLISH_ACTIONS = ("publish",)
SUPERSEDE_ACTIONS = ("supersede",)
ALL_ACTIONS = (*TASK_ACTIONS, *DEPLOY_ACTIONS, *PUBLISH_ACTIONS, *SUPERSEDE_ACTIONS)


def _reject_unknown(task_id, action):
    raise ValueError(f"Unknown action: {action}")


async def perform_action(task_id, action, message=""):
    """Apply one operator action to a task.

    Returns a short operator-facing confirmation. Raises ValueError when the
    action is unknown or not permitted in the task's current state.
    """
    if action in DEPLOY_ACTIONS:
        service = DeploymentService()
        if action == "deploy":
            record = await service.deploy(task_id, message)
            return f"Deployment {record.status}: {record.deployment_uuid}\n{record.message}"
        record = await service.status(task_id)
        return f"Deployment: {record.status}\nCommit: {record.commit_sha}\n{record.message}"
    if action in PUBLISH_ACTIONS:
        record = await DeploymentService().publish(task_id, message)
        return f"Publication {record.status}\nCommit: {record.commit_sha}\n{record.message}"
    if action in SUPERSEDE_ACTIONS:
        task = await DeploymentService().supersede(task_id, message)
        return f"Task #{task.id} superseded\n{task.last_message}"
    if action not in TASK_ACTIONS:
        _reject_unknown(task_id, action)
    if action == "cancel":
        await store.cancel(task_id)
        return f"Cancellation requested for #{task_id}; inspect status for completion"
    await store.resume(task_id, action, message)
    return f"Task #{task_id} requeued ({action})"


async def create_task(requirement, project, chat_id, user_id, idempotency_key=None, kind=None, brief=None):
    """Create a task after alias and policy validation.

    Raises ValueError for an unknown project alias so every channel reports it identically.
    """
    return await store.create(
        requirement,
        project,
        chat_id,
        user_id,
        idempotency_key,
        kind=kind or TaskKind.ENGINEERING,
        brief=brief,
    )


async def create_self_improvement(brief, project, chat_id=None, user_id=None, idempotency_key=None):
    """Create a gated self-improvement task.

    A brief that touches policy, credentials, budget, deployment, or gate logic is
    reported as requiring explicit approval so the caller can raise a decision card.
    """
    task = await store.create(
        f"Self-improvement: {brief.problem}",
        project,
        chat_id,
        user_id,
        idempotency_key,
        kind=TaskKind.SELF_IMPROVEMENT,
        brief=brief,
    )
    return task, brief.requires_explicit_approval, brief.sensitive_areas


async def read_task(task_id, user_id=None):
    """Fetch a task, optionally enforcing per-user ownership for user-scoped channels."""
    task = await store.get(task_id)
    if not task:
        raise ValueError("Task not found")
    if user_id is not None and task.user_id != user_id:
        raise ValueError("Task not found or not owned by you")
    return task


async def raise_decision(card):
    """Record a decision card so every channel reads the same row."""
    return await store.create_decision(card)


async def decision_inbox(state=None, project=None, limit=50):
    """List pending decisions. Telegram and any dashboard read this same list."""
    await store.expire_stale_decisions()
    return await store.inbox(state=state, project=project, limit=limit)


async def resolve_decision(decision_id, phrase, user_id=None):
    """Answer a decision card. Unknown answers never mutate state."""
    return await store.resolve_decision(decision_id, phrase, user_id=user_id)


async def context_slice(keys, role, scope=None, limit=25):
    """Render a memory slice for a prompt. Provenance travels with every item."""
    items, truncated = await store.recall(keys, role=role, scope=scope, limit=limit)
    lines = [
        f"- {i.key} = {i.value} [{i.label}; source={i.source}; confidence={i.confidence:g}]" for i in items
    ]
    if not lines:
        return ""
    note = "\n(truncated)" if truncated else ""
    return "CONTEXT SLICE (permission-filtered, verify before relying on it):\n" + "\n".join(lines) + note


async def remember(item, owner=None, task_id=None):
    """Store a memory through the contract so every channel applies the same rules."""
    return await store.remember(item, owner=owner, task_id=task_id)


async def propose_clearance(changes, rationale, task_id=None, priority="high", risk_level="high"):
    """Ask Dedi to change who may read which memory.

    Clearance is configuration that governs access, so this only ever *proposes*. Nothing
    is applied here: the value lands in ROLE_CLEARANCE_JSON through a deployment, which is
    itself human-controlled. Recording the proposal is what keeps this auditable and stops
    an agent from widening its own access silently.
    """
    current = settings.role_clearance()
    unknown = sorted({r for r in changes if r not in current})
    if unknown:
        raise ValueError("Unknown agent role: " + ", ".join(unknown))
    valid = {"public", "internal", "confidential", "restricted"}
    bad = sorted({v for v in changes.values() if v not in valid})
    if bad:
        raise ValueError("Invalid sensitivity: " + ", ".join(bad))
    unchanged = {r: (current[r], v) for r, v in changes.items() if current[r] == v}
    if unchanged:
        raise ValueError(
            "No change proposed for " + ", ".join(f"{r} (already {old})" for r, (old, _) in unchanged.items())
        )
    proposed = {**current, **changes}
    # Widening any role's reach is the risky direction, so that is what the card leans against.
    widening = any(CLEARANCE[v] > CLEARANCE[current[r]] for r, v in changes.items())
    why_now = "A role is blocked from memory it needs for its current work."
    return await raise_decision(
        DecisionRequest(
            title="Change role clearance for the memory plane",
            situation=(
                f"Current clearance: {json.dumps(current, sort_keys=True)}. "
                f"Proposed: {json.dumps(proposed, sort_keys=True)}."
            ),
            why_now=why_now,
            options=[
                {
                    "id": "apply",
                    "label": f"Approve the proposed clearance: {json.dumps(changes, sort_keys=True)}",
                    "impact": "Agents see memory up to the new level on the next deployment",
                    "risk": "Widening access exposes memory the role could not previously read",
                },
                {
                    "id": "decline",
                    "label": "Keep the current clearance unchanged",
                    "impact": "No role gains or loses access",
                    "risk": "A role may stay unable to read memory it needs for its work",
                },
            ],
            recommendation="decline" if widening else "apply",
            evidence=[f"{rationale}", f"task #{task_id}" if task_id else "raised from the channel adapter"],
            missing_information="Whether each listed role genuinely needs the wider access.",
            rollback="Restore the previous ROLE_CLEARANCE_JSON and redeploy; no memory is deleted.",
            required_action="approve to apply, reject to keep the current clearance",
            priority=priority,
            risk_level=risk_level,
            project="",
            task_id=task_id,
        )
    )
