"""Real PostgreSQL queue/migration gate, run in CI (optional locally)."""

import asyncio
import os
from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base, init_db, utcnow
from app.store import Store


@pytest.mark.skipif(
    not os.getenv("TEST_POSTGRES_URL"), reason="TEST_POSTGRES_URL required for real PostgreSQL integration"
)
async def test_postgres_concurrent_claim_recovery_and_unique_keys(monkeypatch):
    # Dedicated disposable CI database only; never point TEST_POSTGRES_URL at production.
    engine = create_async_engine(os.environ["TEST_POSTGRES_URL"])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await init_db(engine)
    await init_db(engine)
    store = Store(async_sessionmaker(engine, expire_on_commit=False))
    # Concurrent intake on one repository: the advisory lock must let exactly one through.
    results = await asyncio.gather(
        *(store.create(f"Queue feature {i}") for i in range(8)), return_exceptions=True
    )
    created = [r for r in results if not isinstance(r, BaseException)]
    rejected = [r for r in results if isinstance(r, ValueError)]
    assert len(created) == 1 and len(rejected) == 7
    claims = await asyncio.gather(*(store.claim(f"worker-{i}") for i in range(8)))
    active = [t for t in claims if t]
    assert len(active) == 1  # one active task per repository across workers
    task = active[0]
    await store.update(task.id, task.lease_owner, lease_until=utcnow() - timedelta(seconds=1))
    recovered = await store.claim("recovery")
    assert recovered.id == task.id and recovered.recoveries == 1
    await engine.dispose()
