from datetime import datetime, timezone
from enum import StrEnum

from sqlalchemy import BigInteger, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, inspect, text
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
    FAILED = "failed"
    CANCELLED = "cancelled"


ACTIVE = {"planning", "developing", "testing", "reviewing", "publishing"}


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


engine = create_async_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def init_db(db_engine=None):
    """Additive V0.1 migration. Never deletes old task rows or volumes."""
    target = db_engine or engine
    async with target.begin() as conn:
        if target.dialect.name == "postgresql":
            await conn.execute(text("SELECT pg_advisory_xact_lock(72803102)"))
        tables = await conn.run_sync(lambda c: inspect(c).get_table_names())
        if "tasks" in tables:
            columns = await conn.run_sync(lambda c: {x["name"] for x in inspect(c).get_columns("tasks")})
            for column in Task.__table__.columns:
                if column.name in columns:
                    continue
                sql_type = column.type.compile(dialect=target.dialect)
                default = f" DEFAULT {column.server_default.arg}" if column.server_default else ""
                if column.name in {"project", "repo", "base_branch", "policy_json", "feedback_json"}:
                    default = " DEFAULT '" + str(column.server_default.arg).replace("'", "''") + "'"
                await conn.execute(text(f'ALTER TABLE tasks ADD COLUMN "{column.name}" {sql_type}{default}'))
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(
            text("CREATE UNIQUE INDEX IF NOT EXISTS ux_task_idempotency ON tasks (idempotency_key)")
        )
