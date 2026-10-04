"""WorkerPool supervision: cancellation, wall-clock timeout, heartbeat, and shutdown.

These are the guarantees the worker makes to the store while a task runs. None of them
is exercised by the engine tests, which call `run_task` directly, so a regression here
would only appear as a stuck or silently duplicated task in production.
"""

import asyncio

import pytest

from app.config import settings
from app.db import utcnow
from app.store import store
from app.worker import WorkerPool


@pytest.fixture(autouse=True)
def fast_poll(monkeypatch):
    """Shorten the supervisor poll so the loop is exercised without a real lease wait.

    `supervise` polls every `min(5, lease_seconds / 3)` seconds, so a short lease is what
    makes cancellation, timeout, and heartbeat observable in a test.
    """
    monkeypatch.setattr(settings, "lease_seconds", 1.2)


async def test_a_running_task_is_heartbeated_so_its_lease_never_expires(db, monkeypatch):
    beats = []
    original = store.heartbeat

    async def record(task_id, owner):
        beats.append(task_id)
        await original(task_id, owner)

    async def slow_run(task_id, notify=None, owner=None):
        await asyncio.sleep(1.0)

    monkeypatch.setattr(store, "heartbeat", record)
    monkeypatch.setattr("app.worker.run_task", slow_run)

    task = await db.create("Long running feature")
    claimed = await db.claim("w")
    pool = WorkerPool()
    await pool.supervise(claimed, "w")

    assert beats and set(beats) == {task.id}
    assert len(beats) >= 2  # the lease is renewed repeatedly, not only at the start


async def test_cancellation_stops_the_job_and_marks_the_task_cancelled(db, monkeypatch):
    running = asyncio.Event()
    interrupted = asyncio.Event()

    async def blocking_run(task_id, notify=None, owner=None):
        running.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            interrupted.set()
            raise

    monkeypatch.setattr("app.worker.run_task", blocking_run)

    task = await db.create("Cancel me midway")
    claimed = await db.claim("w")
    pool = WorkerPool()
    job = asyncio.create_task(pool.supervise(claimed, "w"))
    await asyncio.wait_for(running.wait(), timeout=5)
    await db.cancel(task.id)
    await asyncio.wait_for(job, timeout=10)

    assert interrupted.is_set()
    after = await db.get(task.id)
    assert after.status == "cancelled"
    assert after.lease_owner is None


async def test_a_task_that_outlives_its_wall_clock_budget_fails(db, monkeypatch):
    monkeypatch.setattr(settings, "task_timeout_seconds", 0)
    interrupted = asyncio.Event()

    async def blocking_run(task_id, notify=None, owner=None):
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            interrupted.set()
            raise

    monkeypatch.setattr("app.worker.run_task", blocking_run)

    task = await db.create("Runaway task")
    claimed = await db.claim("w")
    await WorkerPool().supervise(claimed, "w")

    after = await db.get(task.id)
    assert after.status == "failed"
    assert "wall-clock" in after.last_message
    assert interrupted.is_set()
    # The lease is released so the task is not stranded by an expired worker.
    assert after.lease_owner is None


async def test_a_completed_task_releases_its_lease(db, monkeypatch):
    async def finishing_run(task_id, notify=None, owner=None):
        await store.update(task_id, owner, status="reviewed")

    monkeypatch.setattr("app.worker.run_task", finishing_run)

    task = await db.create("Review only task")
    claimed = await db.claim("w")
    pool = WorkerPool()
    await pool.supervise(claimed, "w")

    after = await db.get(task.id)
    assert after.status == "reviewed"
    assert after.lease_owner is None
    assert after.lease_until <= utcnow()
    assert pool.last_error is None


async def test_progress_is_forwarded_to_the_channel_notifier(db, monkeypatch):
    seen = []

    async def notify(chat_id, message):
        seen.append((chat_id, message))

    async def talking_run(task_id, notify=None, owner=None):
        await notify("Planning finished")

    monkeypatch.setattr("app.worker.run_task", talking_run)

    await db.create("Announce progress", chat_id=4242)
    claimed = await db.claim("w")
    await WorkerPool(notify=notify).supervise(claimed, "w")

    assert seen == [(4242, "Planning finished")]


async def test_a_task_without_a_chat_never_reaches_the_notifier(db, monkeypatch):
    seen = []

    async def notify(chat_id, message):
        seen.append((chat_id, message))

    async def talking_run(task_id, notify=None, owner=None):
        await notify("Planning finished")

    monkeypatch.setattr("app.worker.run_task", talking_run)

    await db.create("No channel for this one")
    claimed = await db.claim("w")
    await WorkerPool(notify=notify).supervise(claimed, "w")

    assert seen == []


async def test_the_loop_executes_queued_work_and_then_stops(db, monkeypatch):
    executed = []

    async def quick_run(task_id, notify=None, owner=None):
        executed.append(task_id)
        await store.update(task_id, owner, status="completed")

    monkeypatch.setattr("app.worker.run_task", quick_run)

    task = await db.create("Queued work")

    pool = WorkerPool()
    runner = asyncio.create_task(pool.loop("worker-1"))
    for _ in range(200):
        await asyncio.sleep(0.02)
        if executed:
            break
    pool.stopping.set()
    await asyncio.wait_for(runner, timeout=10)

    assert executed == [task.id]
    assert (await db.get(task.id)).status == "completed"


async def test_a_failing_cycle_is_recorded_and_the_loop_keeps_claiming(db, monkeypatch):
    attempts = []
    observed = []
    pool = WorkerPool()

    async def flaky_claim(owner):
        attempts.append(owner)
        if len(attempts) == 1:
            raise RuntimeError("transient store failure")
        observed.append(pool.last_error)
        pool.stopping.set()
        return None

    monkeypatch.setattr(store, "claim", flaky_claim)
    await asyncio.wait_for(pool.loop("worker-1"), timeout=10)

    # The failure is visible to /ready instead of only reaching the log.
    assert observed == ["RuntimeError"]


async def test_stopping_during_a_run_leaves_a_recoverable_checkpoint(db, monkeypatch):
    monkeypatch.setattr(settings, "worker_concurrency", 2)
    running = asyncio.Event()

    async def blocking_run(task_id, notify=None, owner=None):
        running.set()
        await asyncio.sleep(30)

    monkeypatch.setattr("app.worker.run_task", blocking_run)

    await db.create("Interrupted by shutdown")
    pool = WorkerPool()
    pool.start()
    await asyncio.wait_for(running.wait(), timeout=5)
    await pool.stop()

    assert pool.stopping.is_set()
    assert all(runner.done() for runner in pool.runners)
    # The lease is dropped, so the next worker recovers the task instead of waiting it out.
    after = (await db.list())[0]
    assert after.lease_owner is None
    assert after.lease_until <= utcnow()
