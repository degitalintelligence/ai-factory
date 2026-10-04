"""Ordered migration ledger: applied once, verifiable, and honest about drift.

Startup DDL is still derived from the models, so the ledger is not what makes it
additive. It is what makes it *auditable*: an operator can tell which named step ran, on
which SQL, and an edited statement is reported instead of being silently skipped by
IF NOT EXISTS.
"""

import hashlib
import logging

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.db import MIGRATIONS, init_db


@pytest.fixture
async def ledger_engine(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'ledger.db'}")
    await init_db(engine)
    yield engine
    await engine.dispose()


async def ledger_rows(engine):
    async with engine.connect() as conn:
        return list(
            await conn.execute(text("SELECT version, name, checksum FROM schema_migrations ORDER BY version"))
        )


async def test_every_declared_step_is_recorded_once_with_its_sql_checksum(ledger_engine):
    rows = await ledger_rows(ledger_engine)

    assert [row[0] for row in rows] == [version for version, _, _ in MIGRATIONS]
    for row, (version, name, statement) in zip(rows, MIGRATIONS, strict=True):
        assert row[1] == name
        assert row[2] == hashlib.sha256(statement.encode()).hexdigest()


async def test_a_second_startup_does_not_reapply_or_duplicate_the_ledger(ledger_engine):
    before = await ledger_rows(ledger_engine)

    await init_db(ledger_engine)
    await init_db(ledger_engine)

    assert await ledger_rows(ledger_engine) == before


async def test_an_edited_statement_is_reported_as_drift_and_left_alone(ledger_engine, caplog):
    version, _, _ = MIGRATIONS[0]
    tampered = "0" * 64
    async with ledger_engine.begin() as conn:
        await conn.execute(
            text("UPDATE schema_migrations SET checksum = :c WHERE version = :v"),
            {"c": tampered, "v": version},
        )

    with caplog.at_level(logging.WARNING, logger="app.db"):
        await init_db(ledger_engine)

    # Reported, not silently ignored and not automatically re-run behind the operator's back.
    assert any(version in record.getMessage() for record in caplog.records)
    stored = {row[0]: row[2] for row in await ledger_rows(ledger_engine)}
    assert stored[version] == tampered


async def test_the_declared_steps_are_unique_and_ordered():
    versions = [version for version, _, _ in MIGRATIONS]

    assert versions == sorted(versions)
    assert len(set(versions)) == len(versions)
    assert all(statement.strip() for _, _, statement in MIGRATIONS)


async def test_a_database_without_the_ledger_gains_it_without_losing_rows(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'v01.db'}")
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "CREATE TABLE tasks (id INTEGER PRIMARY KEY, requirement TEXT, status VARCHAR(32), "
                "branch VARCHAR(200), pr_url TEXT, last_message TEXT, created_at DATETIME, updated_at DATETIME)"
            )
        )
        await conn.execute(
            text("INSERT INTO tasks (id, requirement, status) VALUES (1, 'historical row', 'pr_created')")
        )

    await init_db(engine)

    assert [row[0] for row in await ledger_rows(engine)] == [v for v, _, _ in MIGRATIONS]
    async with engine.connect() as conn:
        assert (await conn.execute(text("SELECT requirement FROM tasks WHERE id = 1"))).one()[
            0
        ] == "historical row"
    await engine.dispose()
