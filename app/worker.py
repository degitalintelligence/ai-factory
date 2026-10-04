import asyncio
import contextlib
import logging
import uuid

from app.config import settings
from app.db import utcnow
from app.orchestrator import run_task
from app.store import TaskStopped, store

logger = logging.getLogger(__name__)


class WorkerPool:
    def __init__(self, notify=None):
        self.notify = notify
        self.runners = []
        self.stopping = asyncio.Event()
        self.last_tick = None
        self.last_error = None

    def start(self):
        self.runners = [
            asyncio.create_task(self.loop(f"{uuid.uuid4()}-{n}")) for n in range(settings.worker_concurrency)
        ]

    async def stop(self):
        self.stopping.set()
        for runner in self.runners:
            runner.cancel()
        await asyncio.gather(*self.runners, return_exceptions=True)

    async def supervise(self, task, owner):
        async def notify(message):
            if self.notify and task.chat_id:
                await self.notify(task.chat_id, message)

        job = asyncio.create_task(run_task(task.id, notify, owner))
        started = asyncio.get_running_loop().time()
        try:
            while not job.done():
                done, _ = await asyncio.wait({job}, timeout=min(5, settings.lease_seconds / 3))
                if done:
                    break
                self.last_tick = utcnow()
                latest = await store.get(task.id)
                if not latest or latest.cancel_requested:
                    raise TaskStopped("Cancellation requested")
                if asyncio.get_running_loop().time() - started > settings.task_timeout_seconds:
                    raise TimeoutError("Task wall-clock budget exceeded")
                await store.heartbeat(task.id, owner)
            await job
        except TaskStopped:
            job.cancel()
            with contextlib.suppress(TaskStopped):
                await store.update(task.id, owner, status="cancelled", last_message="Cancelled by operator")
        except TimeoutError:
            job.cancel()
            await store.update(
                task.id, owner, status="failed", last_message="Task wall-clock budget exceeded"
            )
        finally:
            if not job.done():
                job.cancel()
            await asyncio.gather(job, return_exceptions=True)
            # On shutdown leave an expired lease: next worker recovers the checkpoint.
            with contextlib.suppress(TaskStopped):
                await store.update(task.id, owner, lease_owner=None, lease_until=utcnow())

    async def loop(self, owner):
        while not self.stopping.is_set():
            self.last_tick = utcnow()
            try:
                task = await store.claim(owner)
                self.last_error = None
                if task:
                    await self.supervise(task, owner)
                    continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = type(exc).__name__
                logger.error("Worker cycle failed: %s", type(exc).__name__)
            try:
                await asyncio.wait_for(self.stopping.wait(), timeout=2)
            except TimeoutError:
                pass
