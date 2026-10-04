import logging
from datetime import datetime, timezone
from enum import StrEnum

from sqlalchemy import BigInteger, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, inspect, text
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
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


# These states reserve a repository even when no worker lease is held. A paused
# approval/input task or an open PR must not race a second branch on the same repo.
ACTIVE = {"planning", "developing", "testing", "reviewing", "publishing"}
# Repository reservation is broader than worker lease activity. Paused tasks and
# an open PR still own the repository and must not be bypassed by a new branch.
REPOSITORY_BLOCKING = ACTIVE | {"waiting_input", "awaiting_approval", "pr_created"}
# Reserved for intake: a queued task owns the repository too, otherwise repeated
# /new calls would stack unclaimed work that later claims race to execute.
REPOSITORY_RESERVED = REPOSITORY_BLOCKING | {"received"}


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


async def init_db(db_engine=None):
    """Additive startup migration. Never deletes old task, memory, or evidence rows.

    Existing databases gain new columns through ALTER TABLE ADD COLUMN with their
    declared default, so a historical memory row written before tenant isolation
    existed becomes tenant "default" rather than being dropped or left null.
    """
    target = db_engine or engine
    async with target.begin() as conn:
        if target.dialect.name == "postgresql":
            await conn.execute(text("SELECT pg_advisory_xact_lock(72803102)"))
        tables = await conn.run_sync(lambda c: inspect(c).get_table_names())
        for model in (Task, MemoryItem):
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
        await conn.execute(
            text("CREATE UNIQUE INDEX IF NOT EXISTS ux_task_idempotency ON tasks (idempotency_key)")
        )
        try:
            async with conn.begin_nested():
                await conn.execute(text("DROP INDEX IF EXISTS ux_memory_active"))
                await conn.execute(
                    text(
                        "CREATE UNIQUE INDEX IF NOT EXISTS ux_memory_active "
                        "ON memory_items (tenant, key, scope) WHERE state = 'active'"
                    )
                )
        except IntegrityError:
            # Legacy rows already contain duplicate active keys; keep startup additive
            # and rely on store-layer row locks and conflict checks until data is cleaned.
            logging.getLogger(__name__).warning(
                "ux_memory_active not created: memory_items already holds duplicate active keys"
            )
