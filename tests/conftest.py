import os
import subprocess

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("WORKER_ENABLED", "false")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "")

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.db import init_db
from app.store import store
from app.workspace import Workspace


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    await init_db(engine)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(store, "sessions", sessions)
    monkeypatch.setattr(
        settings,
        "projects_json",
        '{"lab":{"repo":"owner/repo"},"other":{"repo":"owner/other"},"self":{"repo":"owner/self"}}',
    )
    # Self-improvement intake is gated on the registered self-target alias, so tests
    # exercise it with "self" instead of the default lab harness.
    monkeypatch.setattr(settings, "self_project", "self")
    yield store
    await engine.dispose()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "workspace_root", tmp_path)
    path = tmp_path / "task-1"
    path.mkdir()

    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=path, text=True, capture_output=True, check=True
        ).stdout.strip()

    git("init", "-b", "main")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "Test")
    # The engine runs git with GIT_CONFIG_GLOBAL=/dev/null, so it never inherits an
    # operator's core.autocrlf. The fixture must match that, otherwise a Windows host
    # commits LF and re-adds CRLF, which looks like an implementation diff.
    git("config", "core.autocrlf", "false")
    git("config", "core.safecrlf", "false")
    (path / "app.py").write_text("def value(): return 1\n")
    (path / "README.md").write_text("# Demo\n")
    (path / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n*.db\n")
    git("add", ".")
    git("commit", "-m", "Initial")
    sha = git("rev-parse", "HEAD")
    git("checkout", "-b", "ai-factory/task-1")
    workspace = Workspace(1, "owner/repo", "ai-factory/task-1", base_sha=sha)
    return workspace, git
