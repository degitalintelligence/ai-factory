"""Run against the real sandbox container; no LLM/provider credentials required."""

import json
import os
import urllib.error
import urllib.request

url = os.environ.get("SANDBOX_SMOKE_URL", "http://localhost:8090")
token = os.environ["SANDBOX_TOKEN"]
source = """import os
import socket
import pytest

def test_isolation():
    assert not os.path.exists('/app')
    assert not os.path.exists('/workspaces')
    assert 'SANDBOX_TOKEN' not in os.environ
    assert 'GITHUB_TOKEN' not in os.environ
    with pytest.raises(OSError):
        socket.create_connection(('1.1.1.1', 443), timeout=1)

def test_writes_use_tmp_path(tmp_path):
    path = tmp_path / 'persistent-test.db'
    path.write_text('data')
    assert path.read_text() == 'data'
"""


def run(files):
    request = urllib.request.Request(
        url + "/run",
        data=json.dumps(
            {
                "files": files,
                "commands": [["python", "-m", "pytest", "-q", "-p", "no:cacheprovider"]],
                "timeout": 30,
            }
        ).encode(),
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=45) as response:
        return json.load(response)


with urllib.request.urlopen(url + "/health", timeout=15) as response:
    assert response.status == 200
passed = run({"test_isolation.py": source})
assert not passed["issues"] and passed["results"][0]["exit_code"] == 0, passed
failed = run({"test_failure.py": "def test_fails(): assert False\n"})
assert failed["results"][0]["exit_code"] != 0, failed
artifacts = run(
    {
        "test_artifact.py": "from pathlib import Path\ndef test_artifact(): Path('oops.db').write_text('runtime')\n"
    }
)
assert artifacts["issues"], artifacts
print("Real sandbox isolation, test failure, and artifact smoke checks passed")
