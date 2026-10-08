"""Real PostgreSQL queue/migration gate, run in CI (optional locally)."""

import asyncio
import os
from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base, init_db, utcnow
from app.store import Store, plan_hash


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


@pytest.mark.skipif(
    not os.getenv("TEST_POSTGRES_URL"), reason="TEST_POSTGRES_URL required for real PostgreSQL integration"
)
async def test_postgres_concurrent_approval_requests_produce_one_card(monkeypatch):
    """Five simultaneous approval requests for one task: exactly one open card.

    The task row lock serializes the check, and the partial unique index backs it
    up; a loser that still hits the index recovers by re-reading the winner.
    """
    engine = create_async_engine(os.environ["TEST_POSTGRES_URL"])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await init_db(engine)
    store = Store(async_sessionmaker(engine, expire_on_commit=False))
    task = await store.create("Approval race feature")
    await store.update(task.id, status="awaiting_approval", plan_json='{"steps": ["ship it"]}')
    fresh = await store.get(task.id)

    cards = await asyncio.gather(
        *(
            store.ensure_task_approval_decision(fresh, "High-risk plan", plan_hash('{"steps": ["ship it"]}'))
            for _ in range(5)
        ),
        return_exceptions=True,
    )
    failed = [c for c in cards if isinstance(c, BaseException)]
    assert not failed, failed
    assert {card.id for card in cards} == {cards[0].id}
    assert len(await store.inbox(state="open")) == 1
    await engine.dispose()


@pytest.mark.skipif(
    not os.getenv("TEST_POSTGRES_URL"), reason="TEST_POSTGRES_URL required for real PostgreSQL integration"
)
async def test_postgres_tenant_budget_reservations_across_workers(monkeypatch):
    from sqlalchemy import select

    from app.config import settings
    from app.db import DailyBudget, Task
    from app.store import BudgetExceeded

    engine = create_async_engine(os.environ["TEST_POSTGRES_URL"])
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
    await init_db(engine)
    store = Store(async_sessionmaker(engine, expire_on_commit=False))
    monkeypatch.setattr(settings, "global_max_llm_calls_per_day", 2)
    async with store.sessions() as session, session.begin():
        for index in range(8):
            session.add(
                Task(
                    requirement="Concurrent bounded analysis",
                    repo=f"liobot/task-{index}",
                    branch=f"liobot/{index}",
                    kind="orchestration",
                )
            )
    claimed = await asyncio.gather(*(store.claim(f"budget-worker-{index}") for index in range(8)))
    assert all(claimed)
    outcomes = await asyncio.gather(
        *(store.reserve_call(task.id, task.lease_owner, token_reserve=1) for task in claimed),
        return_exceptions=True,
    )
    assert sum(not isinstance(result, BaseException) for result in outcomes) == 2
    assert sum(isinstance(result, BudgetExceeded) for result in outcomes) == 6
    async with store.sessions() as session:
        assert (await session.scalar(select(DailyBudget))).calls == 2
    await engine.dispose()


@pytest.mark.skipif(
    not os.getenv("TEST_POSTGRES_URL"), reason="TEST_POSTGRES_URL required for real PostgreSQL integration"
)
async def test_postgres_v04_duplicate_intake_and_atomic_clarification(monkeypatch):
    from sqlalchemy import func, select

    from app.chat import converse
    from app.config import settings
    from app.db import ChatTurn, Conversation
    from app.staff_schemas import ChatRequest
    from app.store import store

    engine = create_async_engine(os.environ["TEST_POSTGRES_URL"])
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
    await init_db(engine)
    await init_db(engine)
    monkeypatch.setattr(store, "sessions", async_sessionmaker(engine, expire_on_commit=False))
    monkeypatch.setattr(settings, "projects_json", '{"lab":{"repo":"owner/repo"}}')
    request = ChatRequest(message="Review product requirements", project="lab", idempotency_key="pg-v04")
    try:
        responses = await asyncio.gather(*(converse(request, 7) for _ in range(5)))
        assert all(response == responses[0] for response in responses)
        async with store.sessions() as session:
            assert await session.scalar(select(func.count()).select_from(ChatTurn)) == 1
            assert await session.scalar(select(func.count()).select_from(Conversation)) == 1
        task_id = responses[0]["intent_id"]
        claimed = await store.claim("v04-worker")
        assert claimed.id == task_id
        cards = await asyncio.gather(
            *(store.wait_for_clarification(task_id, "v04-worker", "Which audience?") for _ in range(5))
        )
        assert len({card.id for card in cards}) == 1
        assert len(await store.inbox(state="open")) == 1
        await store.update(task_id, "v04-worker", lease_owner=None, lease_until=None)
        answers = await asyncio.gather(
            *(store.answer_clarification(cards[0].id, "Startup founders", 7) for _ in range(5))
        )
        assert all(answer.status == "queued" for answer in answers)
        fresh = await store.get(task_id)
        assert fresh.requirement.count("Startup founders") == 1
        assert fresh.approved_plan_hash is None
        assert not await store.inbox(state="open")
    finally:
        await engine.dispose()
