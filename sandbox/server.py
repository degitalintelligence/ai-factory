"""Credential-free execution service. No Docker socket, host checkout, or control DB."""

import hashlib
import hmac
import os
import shutil
import signal
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field, model_validator

from app.schemas import CommandResult, TestReport
from app.security import validate_path

MAX_BYTES = 4_000_000
LIMIT = threading.BoundedSemaphore(1)
PREFIXES = (
    ("python", "-m", "pytest"),
    ("pytest",),
    ("python", "-m", "compileall"),
    ("python", "-m", "py_compile"),
    ("npm", "test"),
    ("npm", "run", "build"),
    ("node", "--test"),
)


class RunRequest(BaseModel):
    files: dict[str, str]
    commands: list[list[str]] = Field(min_length=1, max_length=5)
    timeout: int = Field(default=180, ge=1, le=600)
    profile: Literal["python", "node"] = "python"
    install_dependencies: bool = False

    @model_validator(mode="after")
    def bounded_input(self):
        if len(self.files) > 2500 or sum(len(v.encode()) for v in self.files.values()) > MAX_BYTES:
            raise ValueError("Snapshot too large")
        for path in self.files:
            p = validate_path(path)
            if p.parts[0] in {".factory-deps", "node_modules"}:
                raise ValueError("Reserved dependency directory")
        for args in self.commands:
            if (
                not any(tuple(args[: len(p)]) == p for p in PREFIXES)
                or len(args) > 100
                or any(len(x) > 1000 for x in args)
            ):
                raise ValueError("Unsupported command")
        return self


def bwrap_args(root: Path, command: list[str], network=False):
    if not shutil.which("bwrap") or not shutil.which("prlimit"):
        raise RuntimeError("Sandbox unavailable: bubblewrap/prlimit missing")
    args = [
        "prlimit",
        "--cpu=180",
        "--nproc=128",
        "--nofile=256",
        "--fsize=16777216",
        "--",
        "bwrap",
        "--unshare-all",
        "--die-with-parent",
        "--new-session",
        "--cap-drop",
        "ALL",
        "--clearenv",
        "--ro-bind",
        "/usr",
        "/usr",
    ]
    for directory in ("/lib", "/lib64", "/bin", "/sbin"):
        if Path(directory).exists():
            args += ["--ro-bind", directory, directory]
    args += [
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",
        "--dir",
        "/etc",
        "--bind",
        str(root),
        "/workspace",
        "--chdir",
        "/workspace",
        "--setenv",
        "PATH",
        "/usr/local/bin:/usr/bin:/bin",
        "--setenv",
        "HOME",
        "/tmp",
        "--setenv",
        "LANG",
        "C.UTF-8",
        "--setenv",
        "CI",
        "true",
        "--setenv",
        "PYTHONPYCACHEPREFIX",
        "/tmp/pycache",
        "--setenv",
        "PYTHONPATH",
        "/workspace/.factory-deps:/workspace",
        "--setenv",
        "PYTEST_ADDOPTS",
        "-p no:cacheprovider",
        "--setenv",
        "PIP_DISABLE_PIP_VERSION_CHECK",
        "1",
    ]
    if network:
        args += ["--share-net"]
        for file in ("/etc/resolv.conf", "/etc/hosts", "/etc/ssl/certs"):
            if Path(file).exists():
                args += ["--ro-bind", file, file]
    return args + ["--", *command]


def execute(root, command, timeout, network=False):
    with tempfile.TemporaryFile() as log:
        proc = subprocess.Popen(
            bwrap_args(root, command, network),
            stdout=log,
            stderr=subprocess.STDOUT,
            env={"PATH": "/usr/local/bin:/usr/bin:/bin"},
            start_new_session=True,
        )
        timed_out = False
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
        finally:
            # Kill the entire namespace/process group, including orphan child processes.
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
        length = log.tell()
        log.seek(max(0, length - 24000))
        output = log.read(24000).decode("utf-8", errors="replace")
    if length > 24000:
        output = "[Output tail; log exceeded 24KB]\n" + output
    return CommandResult(
        command=command, exit_code=124 if timed_out else proc.returncode, output=output, timed_out=timed_out
    )


def fingerprint(root):
    result = {}
    for path in root.rglob("*"):
        rel = path.relative_to(root)
        if rel.parts[0] in {"node_modules", ".factory-deps"}:
            continue
        if path.is_symlink():
            result[str(rel)] = "symlink:" + os.readlink(path)
        elif path.is_file():
            result[str(rel)] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def run(request: RunRequest):
    if not LIMIT.acquire(blocking=False):
        raise HTTPException(429, "Sandbox busy")
    try:
        with tempfile.TemporaryDirectory(prefix="factory-run-") as directory:
            root = Path(directory)
            for path, content in request.files.items():
                target = root / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content)
            baseline = fingerprint(root)
            report = TestReport()
            if request.install_dependencies:
                if os.getenv("SANDBOX_INSTALL_DEPS", "false").lower() != "true":
                    report.issues.append("Dependency network access is disabled by sandbox operator")
                    return report
                if request.profile == "python":
                    deps = [
                        name for name in ("requirements.txt", "requirements-dev.txt") if name in request.files
                    ]
                    commands = (
                        [
                            [
                                "python",
                                "-m",
                                "pip",
                                "install",
                                "--only-binary=:all:",
                                "--target",
                                ".factory-deps",
                                *[item for name in deps for item in ("-r", name)],
                            ]
                        ]
                        if deps
                        else []
                    )
                else:
                    commands = [["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"]]
                for command in commands:
                    result = execute(root, command, request.timeout, network=True)
                    report.results.append(result)
                    if result.exit_code != 0:
                        return report
            for command in request.commands:
                result = execute(root, command, request.timeout)
                report.results.append(result)
                if result.exit_code != 0:
                    break
            after = fingerprint(root)
            changed = sorted(p for p in set(baseline) | set(after) if baseline.get(p) != after.get(p))
            # Build artifacts are allowed only in standard output directories for Node.
            changed = [
                p
                for p in changed
                if not (
                    request.profile == "node"
                    and Path(p).parts[0] in {"dist", "build", ".next", "coverage"}
                    and p not in baseline
                )
            ]
            if changed:
                report.issues.append(
                    "Commands modified source or left test artifacts: " + ", ".join(changed[:30])
                )
            return report
    finally:
        LIMIT.release()


def authorize(authorization: str = Header(default="")):
    token = os.getenv("SANDBOX_TOKEN", "")
    if not token or not hmac.compare_digest(authorization, f"Bearer {token}"):
        raise HTTPException(401, "Unauthorized")


app = FastAPI(title="AI Factory Sandbox", docs_url=None, redoc_url=None)


@app.post("/run", dependencies=[Depends(authorize)])
def run_endpoint(request: RunRequest):
    try:
        return run(request)
    except (OSError, RuntimeError) as exc:
        raise HTTPException(503, "Sandbox unavailable; check namespace support") from exc


@app.get("/health")
def health():
    with tempfile.TemporaryDirectory() as directory:
        result = execute(
            Path(directory),
            [
                "python",
                "-c",
                "import os; assert not os.path.exists('/app'); assert 'SANDBOX_TOKEN' not in os.environ; print('isolated')",
            ],
            10,
        )
    if result.exit_code != 0:
        raise HTTPException(503, "Linux user namespaces unavailable; see docs/OPERATIONS.md")
    return {"status": "ok", "isolation": "bubblewrap", "network": "disabled for tests"}
