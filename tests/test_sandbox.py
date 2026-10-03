from unittest.mock import patch

import httpx
import pytest
from pydantic import ValidationError

from app.schemas import CommandResult
from sandbox import server


@pytest.mark.parametrize(
    "path", ["../outside", ".git/config", ".env", "/etc/passwd", "node_modules/exploit.js"]
)
def test_sandbox_rejects_unsafe_upload_paths(path):
    with pytest.raises(ValidationError):
        server.RunRequest(files={path: "x"}, commands=[["pytest"]])


def test_sandbox_argv_has_no_secrets_and_network_isolated(tmp_path):
    args = server.bwrap_args(tmp_path, ["python", "-m", "pytest"])
    assert "--unshare-all" in args and "--clearenv" in args and "--share-net" not in args
    assert "/app" not in args and "/workspaces" not in args
    assert "--cap-drop" in args


def test_tests_cannot_mutate_source_without_detection(monkeypatch):
    def execute(root, command, timeout, network=False):
        (root / "source.py").write_text("modified")
        (root / "todo.db").write_text("runtime")
        return CommandResult(command=command, exit_code=0)

    monkeypatch.setattr(server, "execute", execute)
    report = server.run(server.RunRequest(files={"source.py": "original"}, commands=[["pytest"]]))
    assert not report.passed
    assert "source.py" in report.issues[0] and "todo.db" in report.issues[0]


def test_dependency_network_requires_operator_opt_in(monkeypatch):
    monkeypatch.delenv("SANDBOX_INSTALL_DEPS", raising=False)
    report = server.run(
        server.RunRequest(
            files={"requirements.txt": "example"}, commands=[["pytest"]], install_dependencies=True
        )
    )
    assert not report.passed and "disabled" in report.issues[0]


def test_only_dependency_stage_uses_network(monkeypatch):
    calls = []

    def execute(root, command, timeout, network=False):
        calls.append((command, network))
        return CommandResult(command=command, exit_code=0)

    monkeypatch.setenv("SANDBOX_INSTALL_DEPS", "true")
    monkeypatch.setattr(server, "execute", execute)
    report = server.run(
        server.RunRequest(
            files={"requirements.txt": "pytest==8.4.2"}, commands=[["pytest"]], install_dependencies=True
        )
    )
    assert report.passed and [c[1] for c in calls] == [True, False]
    assert "--only-binary=:all:" in calls[0][0]


async def test_sandbox_fails_closed_and_is_authenticated(monkeypatch):
    monkeypatch.setenv("SANDBOX_TOKEN", "runner-test-token")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(server.app), base_url="http://runner"
    ) as client:
        body = {"files": {"test.py": "x=1"}, "commands": [["pytest"]]}
        assert (await client.post("/run", json=body)).status_code == 401
        with patch.object(server, "bwrap_args", side_effect=RuntimeError("No namespace support")):
            response = await client.post(
                "/run", json=body, headers={"Authorization": "Bearer runner-test-token"}
            )
            assert response.status_code == 503
