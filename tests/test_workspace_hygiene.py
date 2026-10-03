import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://user:pass@localhost/test")
os.environ.setdefault("OPENROUTER_API_KEY", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:test")
os.environ.setdefault("GITHUB_TOKEN", "test")
os.environ.setdefault("LEAD_MODEL", "test")
os.environ.setdefault("DEVELOPER_MODEL", "test")
os.environ.setdefault("REVIEWER_MODEL", "test")

from app.workspace import Workspace, is_suspicious_artifact


def test_suspicious_runtime_artifacts_are_detected():
    assert is_suspicious_artifact("todos.db")
    assert is_suspicious_artifact(".env")
    assert is_suspicious_artifact("logs/app.log")
    assert is_suspicious_artifact("__pycache__/bot.cpython-312.pyc")
    assert not is_suspicious_artifact("bot.py")
    assert not is_suspicious_artifact("tests/test_bot.py")


def test_hygiene_gate_detects_artifact_created_by_tests():
    workspace = Workspace(1, "owner/repo", "ai-factory/task-1")
    before = " M bot.py\n?? tests/test_bot.py\n"
    after = before + "?? todos.db\n"
    issues = workspace.hygiene_issues(before, after)
    assert any("todos.db" in issue for issue in issues)


def test_hygiene_gate_allows_clean_test_run():
    workspace = Workspace(1, "owner/repo", "ai-factory/task-1")
    before = " M bot.py\n?? tests/test_bot.py\n"
    assert workspace.hygiene_issues(before, before) == []
