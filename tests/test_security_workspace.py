import shutil

import pytest

from app.config import Project, Settings, settings
from app.schemas import TestReport as Report
from app.security import redact, secret_present, validate_path
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
    try:
        (workspace.path / "link").symlink_to(external)
    except OSError as exc:
        # Creating a symlink on Windows needs an elevated privilege or developer mode.
        # Skipping here keeps the Linux sandbox job as the authority for this check.
        pytest.skip(f"symlink creation is not permitted on this host: {exc}")
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


def test_changed_test_files_are_detected_from_diff(repo):
    workspace, git = repo
    workspace.write_file("bot.py", "def handler():\n    return 1\n")
    workspace.write_file("tests/test_bot.py", "def test_handler():\n    assert True\n")
    workspace.write_file("docs/guide.md", "hi\n")
    assert workspace.changed_test_files(workspace.diff()) == ["tests/test_bot.py"]
    assert workspace.standalone_tests([]) is None


def test_changed_test_files_detects_jest_and_node_conventions(repo):
    workspace, git = repo
    workspace.write_file("src/a.test.js", "test('a', () => {});\n")
    workspace.write_file("src/b.spec.ts", "it('b', () => {});\n")
    workspace.write_file("src/c_test.js", "console.log(1);\n")
    workspace.write_file("__tests__/d.js", "console.log(1);\n")
    workspace.write_file("test-e.js", "console.log(1);\n")
    workspace.write_file("src/helper.js", "console.log(1);\n")
    assert sorted(workspace.changed_test_files(workspace.diff())) == sorted(
        ["src/a.test.js", "src/b.spec.ts", "src/c_test.js", "__tests__/d.js", "test-e.js"]
    )


def test_default_tests_does_not_pass_runner_specific_flags_to_node(repo, monkeypatch):
    workspace, _ = repo
    workspace.policy = Project(repo="owner/repo", profile="node")
    calls = []
    monkeypatch.setattr(workspace, "run_commands", lambda commands: calls.append(commands) or Report())
    workspace.default_tests()
    assert calls[0][0] == ["npm", "test"]


@pytest.mark.skipif(shutil.which("npm") is None, reason="npm is not installed")
def test_node_standalone_tests_passes_only_changed_paths_to_the_runner(repo, monkeypatch):
    """The runner owns its own flags; the engine only forwards the changed test paths."""
    workspace, _ = repo
    workspace.policy = Project(repo="owner/repo", profile="node")
    calls = []
    monkeypatch.setattr(workspace, "run_commands", lambda commands: calls.append(commands) or Report())
    workspace.write_file("src/a.test.js", "test('a', () => {});\n")
    workspace.write_file("src/b.test.js", "test('b', () => {});\n")
    paths = workspace.changed_test_files(workspace.diff())
    assert sorted(paths) == ["src/a.test.js", "src/b.test.js"]
    workspace.standalone_tests(paths)
    assert calls == [[["npm", "test", "--", *sorted(paths)]]]


def test_command_never_runs_on_control_host(repo, monkeypatch):
    workspace, git = repo
    with pytest.raises(WorkspaceError):
        workspace.run_command("sh -c 'cat /etc/passwd'")
    calls = []
    from app.schemas import TestReport

    monkeypatch.setattr(workspace, "run_commands", lambda commands: calls.append(commands) or TestReport())
    workspace.run_command("python -m pytest -q")
    assert calls == [[["python", "-m", "pytest", "-q"]]]


def test_url_embedded_credentials_are_redacted():
    leaked = "postgres://operator:s3cret-password@db.internal:5432/prod"
    assert "s3cret-password" not in redact(leaked)
    assert secret_present("redis://cache:hunter2@cache.internal/0")
    # Ordinary URLs without credentials stay untouched.
    assert redact("docs at https://example.com/guide") == "docs at https://example.com/guide"


def test_replace_text_edits_real_bytes_and_stays_fail_closed(repo, monkeypatch):
    """replace_text must operate on the real bytes; a redacted read must never be written back."""
    workspace, _ = repo
    monkeypatch.setattr(settings, "github_token", "my-private-github-credential")
    target = workspace.path / "config.py"
    target.write_text('TOKEN = "my-private-github-credential"\nANSWER = 1\n', encoding="utf-8")
    # Model-facing reads stay redacted.
    assert "my-private-github-credential" not in workspace.read_file("config.py")
    # Editing a clean line must not silently write a redacted copy of the secret.
    with pytest.raises(WorkspaceError, match="credentials"):
        workspace.replace_text("config.py", "ANSWER = 1", "ANSWER = 2")
    assert "my-private-github-credential" in target.read_text()
    assert "ANSWER = 1" in target.read_text()
    # Secret-free files still round-trip on the real bytes.
    (workspace.path / "plain.py").write_text("value = 1\n", encoding="utf-8")
    workspace.replace_text("plain.py", "value = 1", "value = 2")
    assert (workspace.path / "plain.py").read_text() == "value = 2\n"


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


@pytest.mark.parametrize("repo", ["../other", "owner/..", ".", "..", "./repo", "owner/.", "owner/../escape"])
def test_project_repo_cannot_traverse_out_of_the_registry(repo):
    """'.' and '..' satisfy the owner/name character class but name no repository."""
    with pytest.raises(ValueError, match="repo"):
        Project(repo=repo)


def test_project_repo_accepts_an_ordinary_owner_and_name():
    assert Project(repo="degitalintelligence/telegram-lab").repo == "degitalintelligence/telegram-lab"


@pytest.mark.parametrize(
    "leaked",
    [
        "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE",
        "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcdefghij",
        "password = hunter2hunter2",
        "glpat-ABCDEFGHIJKLMNOPQRST",
        "xoxb-1234567890-abcdefghijkl",
        "AIzaSyA1234567890abcdefghijklmnopqrstuv",
    ],
)
def test_common_credential_shapes_are_redacted(leaked):
    assert "[REDACTED]" in redact(leaked), leaked
    assert secret_present(leaked)


def test_ordinary_prose_is_not_mangled_by_redaction():
    """Redaction must not destroy normal engineering text."""
    for line in (
        "Set the token: the model returns one per request",
        "password: ask the operator to rotate it",
        "See https://example.com/guide for the API reference",
        "The secret sauce is idempotency",
    ):
        assert redact(line) == line
