"""Contract parity: every channel must reach the same use cases with the same validation."""

import pytest

from app.contracts import ALL_ACTIONS, perform_action, read_task
from app.store import store


async def test_contract_exposes_the_documented_action_set():
    # Telegram /start advertises exactly these commands; HTTP exposes the same set.
    assert set(ALL_ACTIONS) == {
        "cancel",
        "retry",
        "approve",
        "answer",
        "feedback",
        "deploy",
        "publish",
        "supersede",
        "deployment",
    }


async def test_unknown_action_is_rejected_identically_in_every_channel(db):
    task = await db.create("Change the return value")
    with pytest.raises(ValueError, match="Unknown action: merge"):
        await perform_action(task.id, "merge")
    # A destructive action must never be reachable through a channel adapter.
    for forbidden in ("merge", "delete", "rollback", "approve_all"):
        with pytest.raises(ValueError, match="Unknown action"):
            await perform_action(task.id, forbidden)


async def test_cancel_is_idempotent_across_repeated_channel_requests(db):
    task = await db.create("Change the return value")
    first = await perform_action(task.id, "cancel")
    second = await perform_action(task.id, "cancel")
    assert "Cancellation requested" in first and "Cancellation requested" in second
    assert (await store.get(task.id)).status == "cancelled"
    # Idempotency covers side effects too: a repeated cancel must not stack events.
    assert len([e for e in await store.events(task.id) if e.kind == "cancel"]) == 1


async def test_read_task_enforces_ownership_for_user_scoped_channels(db):
    task = await db.create("Change the return value", user_id=1)
    assert (await read_task(task.id)).id == task.id
    assert (await read_task(task.id, user_id=1)).id == task.id
    with pytest.raises(ValueError, match="not owned by you"):
        await read_task(task.id, user_id=2)
    with pytest.raises(ValueError, match="Task not found"):
        await read_task(task.id + 999)


async def test_invalid_state_produces_the_same_message_regardless_of_channel(db):
    task = await db.create("Change the return value")
    with pytest.raises(ValueError, match="Only failed/cancelled tasks can be retried"):
        await perform_action(task.id, "retry")
    with pytest.raises(ValueError, match="Task has no plan awaiting approval"):
        await perform_action(task.id, "approve", "deadbeef")
    with pytest.raises(ValueError, match="Task is not waiting for input"):
        await perform_action(task.id, "answer", "some clarification")
    with pytest.raises(ValueError, match="Feedback requires a published PR"):
        await perform_action(task.id, "feedback", "please revise")


async def test_store_reports_missing_task_consistently(db):
    with pytest.raises(ValueError, match="Task not found"):
        await read_task(4242)
