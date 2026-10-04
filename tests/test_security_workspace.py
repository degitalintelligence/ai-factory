import pytest

from app.config import Settings, settings
from app.security import redact, validate_path
from app.workspace import WorkspaceError


@pytest.mark.parametrize(
    "path",
    ["../secret", "/etc/passwd", ".git/config", "a/../../b", ".env", "a/.env.production", ".npmrc", "x.pem"],
)
def test_paths_are_restricted(path):
    with pytest.raises(ValueError):
        validate_path(path)


def test_example_env_is_allowed():
    assert str(validate_path(".env.example")) == ".env.example"


def test_all_new_files_are_in_review_diff(repo):
    workspace, git = repo
    workspace.write_file("new_feature.py", "answer = 42\n")
    workspace.write_file("tests/test_feature.py", "def test_it(): assert True\n")
    diff = workspace.diff()
    assert "new_feature.py" in diff and "tests/test_feature.py" in diff and "+answer = 42" in diff


def test_symlink_escape_and_git_write_blocked(repo, tmp_path):
    workspace, git = repo
    external = tmp_path / "secret"
    external.write_text("never expose")
    (workspace.path / "link").symlink_to(external)
    with pytest.raises(WorkspaceError, match="Symlinks"):
        workspace.read_file("link")
    with pytest.raises(ValueError):
        workspace.write_file(".git/config", "malicious")


def test_tracked_db_and_secrets_block_snapshot(repo, monkeypatch):
    workspace, git = repo
    (workspace.path / "data.db").write_text("runtime")
    git("add", "-f", "data.db")
    with pytest.raises(WorkspaceError, match="runtime artifact"):
        workspace.snapshot()
    workspace.delete_file("data.db")
    monkeypatch.setattr(settings, "github_token", "my-private-github-credential")
    with pytest.raises(WorkspaceError, match="credentials"):
        workspace.write_file("secret.py", 'TOKEN = "my-private-github-credential"')
    assert "my-private-github-credential" not in redact("failed: my-private-github-credential")


def test_post_review_changes_cannot_be_committed(repo):
    workspace, git = repo
    workspace.write_file("feature.py", "one = 1\n")
    digest = workspace.digest()
    workspace.write_file("feature.py", "one = 2\n")
    with pytest.raises(WorkspaceError, match="changed after review"):
        workspace.commit("test", digest)


def test_command_never_runs_on_control_host(repo, monkeypatch):
    workspace, git = repo
    with pytest.raises(WorkspaceError):
        workspace.run_command("sh -c 'cat /etc/passwd'")
    calls = []
    from app.schemas import TestReport

    monkeypatch.setattr(workspace, "run_commands", lambda commands: calls.append(commands) or TestReport())
    workspace.run_command("python -m pytest -q")
    assert calls == [[["python", "-m", "pytest", "-q"]]]


def test_postgres_password_is_url_encoded():
    config = Settings(_env_file=None, database_host="postgres", database_password="p@ss:/#word")
    from sqlalchemy.engine import make_url

    assert make_url(config.database_url).password == "p@ss:/#word"


def test_runtime_rejects_open_telegram_access():
    config = Settings(
        _env_file=None, worker_enabled=False, telegram_bot_token="x", telegram_allowed_user_ids=""
    )
    with pytest.raises(ValueError, match="TELEGRAM_ALLOWED_USER_IDS"):
        config.validate_runtime()
