import hashlib
import logging
from datetime import datetime, timezone
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    insert,
    inspect,
    select,
    text,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncAttrs, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.config import settings


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(AsyncAttrs, DeclarativeBase):
    pass


class TaskStatus(StrEnum):
    RECEIVED = "received"
    PLANNING = "planning"
    WAITING_INPUT = "waiting_input"
    AWAITING_APPROVAL = "awaiting_approval"
    DEVELOPING = "developing"
    TESTING = "testing"
    REVIEWING = "reviewing"
    PUBLISHING = "publishing"
    PR_CREATED = "pr_created"
    DEPLOYMENT_PENDING = "deployment_pending"
    DEPLOYING = "deploying"
    DEPLOYED = "deployed"
    DEPLOYMENT_UNKNOWN = "deployment_unknown"
    DEPLOYMENT_FAILED = "deployment_failed"
    COMPLETED = "completed"
    SUPERSEDED = "superseded"
    REVIEWED = "reviewed"
    FAILED = "failed"
    CANCELLED = "cancelled"


# These states reserve a repository even when no worker lease is held. A paused
# approval/input task or an open PR must not race a second branch on the same repo.
ACTIVE = {"planning", "developing", "testing", "reviewing", "publishing"}
# Repository reservation is broader than worker lease activity. Paused tasks, an open
# PR, and an in-flight deployment still own the repository and must not be bypassed.
REPOSITORY_BLOCKING = ACTIVE | {
    "waiting_input",
    "awaiting_approval",
    "pr_created",
    "deployment_pending",
    "deploying",
    "deployment_unknown",
}
# Reserved for intake: a queued task owns the repository too, otherwise repeated
# /new calls would stack unclaimed work that later claims race to execute.
REPOSITORY_RESERVED = REPOSITORY_BLOCKING | {"received"}
# Deployed and deployment_failed are durable terminal outcomes. deployment_unknown is
# deliberately not terminal and never auto-retries: only an explicit /deployment
# reconciliation can move it to a final state.


class Task(Base):
    __tablename__ = "tasks"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    requirement: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="received", index=True)
    branch: Mapped[str | None] = mapped_column(String(200))
    pr_url: Mapped[str | None] = mapped_column(Text)
    last_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    project: Mapped[str] = mapped_column(String(40), default="lab", server_default="lab")
    repo: Mapped[str] = mapped_column(String(200), default="", server_default="")
    base_branch: Mapped[str] = mapped_column(String(200), default="main", server_default="main")
    policy_json: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    chat_id: Mapped[int | None] = mapped_column(BigInteger)
    user_id: Mapped[int | None] = mapped_column(BigInteger)
    idempotency_key: Mapped[str | None] = mapped_column(String(160), unique=True)
    plan_json: Mapped[str | None] = mapped_column(Text)
    feedback_json: Mapped[str] = mapped_column(Text, default="[]", server_default="[]")
    approved_plan_hash: Mapped[str | None] = mapped_column(String(64))
    base_sha: Mapped[str | None] = mapped_column(String(64))
    head_sha: Mapped[str | None] = mapped_column(String(64))
    review_digest: Mapped[str | None] = mapped_column(String(64))
    iteration: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    recoveries: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    llm_calls: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    cost_usd: Mapped[float] = mapped_column(Float, default=0, server_default="0")
    cost_incomplete: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    lease_owner: Mapped[str | None] = mapped_column(String(100))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    kind: Mapped[str] = mapped_column(
        String(32), default="engineering", server_default="engineering", index=True
    )


class Event(Base):
    __tablename__ = "task_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id"), index=True)
    kind: Mapped[str] = mapped_column(String(60))
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Artifact(Base):
    __tablename__ = "task_artifacts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id"), index=True)
    kind: Mapped[str] = mapped_column(String(60))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ModelRun(Base):
    """One immutable record of one structured model call.

    The budget counters on a task are aggregates that survive retries but cannot be
    decomposed afterwards. This row is the per-call evidence they cannot reconstruct: which
    role ran, which configured alias and resolved provider model were actually used, which
    prompt version was sent, how long the call took, and how it ended. Rows are only ever
    inserted, never updated.
    """

    __tablename__ = "model_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id"), index=True)
    role: Mapped[str] = mapped_column(String(40), default="", server_default="", index=True)
    # The configured alias and the resolved provider model are both kept: the alias is what
    # the operator configured, the resolved model is what actually ran.
    model_alias: Mapped[str] = mapped_column(String(120), default="", server_default="")
    model: Mapped[str] = mapped_column(String(200), default="", server_default="")
    prompt_version: Mapped[str] = mapped_column(String(40), default="", server_default="")
    prompt_sha256: Mapped[str] = mapped_column(String(64), default="", server_default="")
    schema_name: Mapped[str] = mapped_column(String(60), default="", server_default="")
    attempt: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    outcome: Mapped[str] = mapped_column(String(40), default="ok", server_default="ok", index=True)
    detail: Mapped[str] = mapped_column(Text, default="", server_default="")
    tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    cost_usd: Mapped[float | None] = mapped_column(Float)
    cost_reported: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    latency_ms: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class SchemaMigration(Base):
    """Ledger of the named additive startup migrations that already ran.

    The statements themselves are still derived from the models at startup, so this ledger
    is not what makes them additive. It records which named step ran and a checksum of the
    SQL it ran, so an edited migration shows up as drift instead of being silently skipped
    by IF NOT EXISTS.
    """

    __tablename__ = "schema_migrations"
    version: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), default="", server_default="")
    checksum: Mapped[str] = mapped_column(String(64), default="", server_default="")
    applied_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Deployment(Base):
    __tablename__ = "deployments"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id"), unique=True)
    commit_sha: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(40), default="submitting")
    deployment_uuid: Mapped[str | None] = mapped_column(String(200))
    message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Decision(Base):
    """One durable decision request. Shared source of truth for every channel adapter."""

    __tablename__ = "decisions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id"), index=True)
    project: Mapped[str] = mapped_column(String(40), default="", server_default="")
    kind: Mapped[str] = mapped_column(
        String(40), default="DECISION_REQUIRED", server_default="DECISION_REQUIRED"
    )
    title: Mapped[str] = mapped_column(String(300), default="")
    situation: Mapped[str] = mapped_column(Text, default="")
    why_now: Mapped[str] = mapped_column(Text, default="")
    options_json: Mapped[str] = mapped_column(Text, default="[]", server_default="[]")
    impact_json: Mapped[str] = mapped_column(Text, default="[]", server_default="[]")
    risk_json: Mapped[str] = mapped_column(Text, default="[]", server_default="[]")
    recommendation: Mapped[str] = mapped_column(Text, default="")
    evidence_json: Mapped[str] = mapped_column(Text, default="[]", server_default="[]")
    missing_information: Mapped[str] = mapped_column(Text, default="")
    rollback: Mapped[str] = mapped_column(Text, default="")
    required_action: Mapped[str] = mapped_column(String(80), default="")
    priority: Mapped[str] = mapped_column(String(20), default="normal", server_default="normal")
    risk_level: Mapped[str] = mapped_column(String(20), default="medium", server_default="medium")
    state: Mapped[str] = mapped_column(String(20), default="open", server_default="open", index=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    decided_by: Mapped[int | None] = mapped_column(BigInteger)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime)
    decision_note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class MemoryItem(Base):
    """One memory entry with provenance, freshness, permission, and a correction path.

    Memories are never deleted. A correction supersedes the previous version and a
    retraction closes it, so the history of what was believed stays auditable.
    """

    __tablename__ = "memory_items"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(200), index=True)
    value: Mapped[str] = mapped_column(Text)
    # tenant is the hard isolation boundary; owner narrows further within one tenant.
    # A memory written by one tenant must never be recalled by another, so every read
    # filters on tenant and an absent tenant fails closed rather than matching everything.
    tenant: Mapped[str] = mapped_column(String(120), default="default", server_default="default", index=True)
    scope: Mapped[str] = mapped_column(String(120), default="", server_default="", index=True)
    sensitivity: Mapped[str] = mapped_column(String(20), default="internal", server_default="internal")
    source: Mapped[str] = mapped_column(String(300), default="", server_default="")
    evidence_ref: Mapped[str] = mapped_column(String(300), default="", server_default="")
    owner: Mapped[int | None] = mapped_column(BigInteger)
    confidence: Mapped[float] = mapped_column(Float, default=1.0, server_default="1.0")
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    state: Mapped[str] = mapped_column(String(20), default="active", server_default="active", index=True)
    supersedes: Mapped[int | None] = mapped_column(ForeignKey("memory_items.id"))
    locked: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


engine = create_async_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


def _default_clause(server_default):
    """Render the DEFAULT clause for an additive ALTER TABLE ADD COLUMN.

    Plain string defaults are single-quoted so PostgreSQL treats them as
    literals instead of identifiers; TextClause defaults stay raw SQL.
    """
    if server_default is None:
        return ""
    arg = server_default.arg
    if isinstance(arg, str):
        return " DEFAULT '" + arg.replace("'", "''") + "'"
    return f" DEFAULT {arg}"


# Named, ordered, additive startup steps. The order is the apply order, and a step is only
# skipped when the ledger already holds its version with the same SQL checksum, so editing
# a statement is reported as drift instead of being silently ignored.
MIGRATIONS = (
    (
        "0001_task_idempotency_key",
        "Unique idempotency key so a repeated intake cannot create a second task.",
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_task_idempotency ON tasks (idempotency_key)",
    ),
    (
        "0002_memory_active_key",
        "One active memory value per (tenant, key, scope).",
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_memory_active "
        "ON memory_items (tenant, key, scope) WHERE state = 'active'",
    ),
    (
        "0003_single_open_approval",
        "At most one open approval card per task, so a retry cannot stack duplicate decisions.",
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_decision_open_approval "
        "ON decisions (task_id) WHERE state = 'open' AND kind = 'APPROVAL_REQUIRED'",
    ),
)


async def _apply_migrations(conn):
    """Apply each named step once, in order, and record it in the ledger.

    A step whose checksum changed under an existing version is reported and not re-applied:
    re-running edited DDL automatically is exactly the kind of surprise an operator must
    approve, and every statement here is additive and safe to run by hand.
    """
    logger = logging.getLogger(__name__)
    ledger = SchemaMigration.__table__
    for version, name, statement in MIGRATIONS:
        checksum = hashlib.sha256(statement.encode()).hexdigest()
        applied = (await conn.execute(select(ledger.c.checksum).where(ledger.c.version == version))).first()
        if applied is not None:
            if applied[0] != checksum:
                logger.warning(
                    "Migration %s was already applied with a different statement; "
                    "review it manually before changing the schema",
                    version,
                )
            continue
        try:
            async with conn.begin_nested():
                await conn.execute(text(statement))
                await conn.execute(insert(ledger).values(version=version, name=name, checksum=checksum))
        except IntegrityError:
            # Legacy data already violates the invariant. Startup stays additive and the
            # store-layer lock/duplicate check stays authoritative until the data is fixed.
            logger.warning(
                "Migration %s could not be applied: existing rows violate it; "
                "the store layer still prevents new duplicates",
                version,
            )


async def init_db(db_engine=None):
    """Additive startup migration. Never deletes old task, memory, or evidence rows.

    Existing databases gain new columns through ALTER TABLE ADD COLUMN with their
    declared default, so a historical memory row written before tenant isolation
    existed becomes tenant "default" rather than being dropped or left null. Named
    index migrations then run once each and are recorded in schema_migrations.
    """
    target = db_engine or engine
    async with target.begin() as conn:
        if target.dialect.name == "postgresql":
            await conn.execute(text("SELECT pg_advisory_xact_lock(72803102)"))
        tables = await conn.run_sync(lambda c: inspect(c).get_table_names())
        for model in (Task, MemoryItem, ModelRun):
            if model.__tablename__ not in tables:
                continue
            name = model.__tablename__
            columns = await conn.run_sync(lambda c, n=name: {x["name"] for x in inspect(c).get_columns(n)})
            for column in model.__table__.columns:
                if column.name in columns:
                    continue
                sql_type = column.type.compile(dialect=target.dialect)
                default = _default_clause(column.server_default)
                await conn.execute(text(f'ALTER TABLE {name} ADD COLUMN "{column.name}" {sql_type}{default}'))
        await conn.run_sync(Base.metadata.create_all)
        await _apply_migrations(conn)
