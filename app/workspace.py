import ast
import hashlib
import json
import os
import re
import shlex
import subprocess
import tempfile
from pathlib import Path

import httpx

from app.config import Project, settings
from app.schemas import TestReport
from app.security import redact, secret_present, validate_path

SUSPICIOUS_ARTIFACT_SUFFIXES = {".db", ".sqlite", ".sqlite3", ".log", ".pid", ".pyc"}
SUSPICIOUS_ARTIFACT_DIRS = {
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "node_modules",
    ".venv",
}


def is_suspicious_artifact(path):
    p = Path(path)
    return (
        p.name in {".env", ".coverage", ".DS_Store"}
        or (p.name.startswith(".env.") and p.name != ".env.example")
        or p.suffix.lower() in SUSPICIOUS_ARTIFACT_SUFFIXES
        or any(x in SUSPICIOUS_ARTIFACT_DIRS for x in p.parts)
    )


class WorkspaceError(RuntimeError):
    pass


def top_level_definition_counts(content):
    """Count module-level Python definitions without rejecting transient syntax."""
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return {}
    counts = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
                (isinstance(decorator, ast.Name) and decorator.id == "overload")
                or (isinstance(decorator, ast.Attribute) and decorator.attr == "overload")
                for decorator in node.decorator_list
            ):
                continue
            counts[node.name] = counts.get(node.name, 0) + 1
    return counts


def credential_scan_content(path, content):
    """Ignore only explicit local/CI fixture lines in the repository's own CI workflow."""
    if path != ".github/workflows/ci.yml":
        return content
    safe_markers = ("ci-only", "factory_ci")
    return "\n".join(
        line for line in content.splitlines() if not any(marker in line for marker in safe_markers)
    )


class Workspace:
    def __init__(self, task_id, repo_full_name, branch, policy=None, base_sha=None):
        self.task_id = task_id
        self.repo_full_name = repo_full_name
        self.branch = branch
        self.policy = policy or Project(repo=repo_full_name)
        self.path = settings.workspace_root / f"task-{task_id}"
        self.base_sha = base_sha

    def _run(self, args, *, check=True, auth=False):
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": "/tmp",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_TERMINAL_PROMPT": "0",
            "LANG": "C.UTF-8",
        }
        if args[0] == "git":
            args = [
                "git",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "protocol.file.allow=never",
                "-c",
                "core.quotePath=false",
                *args[1:],
            ]
        with tempfile.TemporaryDirectory(prefix="factory-auth-") as d:
            if auth:
                helper = Path(d) / "askpass"
                helper.write_text(
                    '#!/bin/sh\ncase "$1" in *Username*) printf "%s" "x-access-token" ;; *) printf "%s" "$FACTORY_GIT_TOKEN" ;; esac\n'
                )
                helper.chmod(0o700)
                env.update(GIT_ASKPASS=str(helper), FACTORY_GIT_TOKEN=settings.github_token)
            result = subprocess.run(
                args,
                cwd=self.path if self.path.exists() else self.path.parent,
                env=env,
                text=True,
                capture_output=True,
                timeout=min(90, settings.lease_seconds // 2),
            )
        if check and result.returncode != 0:
            raise WorkspaceError(redact(result.stderr or result.stdout)[-4000:])
        return result

    def prepare(self, *, existing_branch=False):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            if not (self.path / ".git").is_dir():
                raise WorkspaceError("Workspace exists without a valid checkout; manual inspection required")
            remote = self._run(["git", "remote", "get-url", "origin"]).stdout.strip()
            if remote != f"https://github.com/{self.repo_full_name}.git":
                raise WorkspaceError("Workspace remote does not match registered project")
            current = self._run(["git", "branch", "--show-current"]).stdout.strip()
            if current != self.branch:
                raise WorkspaceError("Workspace branch mismatch")
        else:
            self._run(
                [
                    "git",
                    "clone",
                    "--no-recurse-submodules",
                    "--branch",
                    self.policy.base_branch,
                    f"https://github.com/{self.repo_full_name}.git",
                    str(self.path),
                ],
                auth=True,
            )
            self.base_sha = self.base_sha or self._run(["git", "rev-parse", "HEAD"]).stdout.strip()
            remote = self._run(
                ["git", "show-ref", "--verify", f"refs/remotes/origin/{self.branch}"], check=False
            )
            if remote.returncode == 0:
                self._run(["git", "checkout", "-b", self.branch, f"origin/{self.branch}"])
            elif existing_branch:
                raise WorkspaceError("Expected feedback branch is missing")
            else:
                self._run(["git", "checkout", "-b", self.branch])
        self._run(["git", "config", "user.email", "ai-factory@local"])
        self._run(["git", "config", "user.name", "AI Factory"])
        if not self.base_sha:
            self.base_sha = self._run(
                ["git", "merge-base", "HEAD", f"origin/{self.policy.base_branch}"]
            ).stdout.strip()
        return self.base_sha

    def _safe_path(self, path):
        validate_path(path)
        candidate = self.path / path
        if candidate.is_symlink() or any(x.is_symlink() for x in candidate.parents if x != self.path.parent):
            raise WorkspaceError("Symlinks are inaccessible")
        resolved = candidate.resolve()
        if self.path.resolve() not in resolved.parents:
            raise WorkspaceError("Path escapes workspace")
        return candidate

    def paths(self):
        result = self._run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"])
        return sorted(set(p for p in result.stdout.split("\0") if p))

    def list_files(self):
        return "\n".join(self.paths())[:30000]

    def read_file(self, path):
        target = self._safe_path(path)
        if not target.is_file():
            raise WorkspaceError("File not found")
        if target.stat().st_size > 100000:
            raise WorkspaceError("File too large; use search")
        return redact(target.read_text(encoding="utf-8"))

    def _read_raw(self, path):
        """Read without redaction so edits operate on real bytes; writes stay fail-closed."""
        target = self._safe_path(path)
        if not target.is_file():
            raise WorkspaceError("File not found")
        if target.stat().st_size > 100000:
            raise WorkspaceError("File too large; use search")
        return target.read_text(encoding="utf-8")

    def write_file(self, path, content):
        target = self._safe_path(path)
        if len(content.encode()) > 200000 or secret_present(content):
            raise WorkspaceError("File too large or contains credentials")
        if target.suffix == ".py":
            current_counts = {}
            if target.is_file():
                current_counts = top_level_definition_counts(target.read_text(encoding="utf-8"))
            candidate_counts = top_level_definition_counts(content)
            introduced = {
                name
                for name, count in candidate_counts.items()
                if count > 1 and count > max(1, current_counts.get(name, 0))
            }
            if introduced:
                raise WorkspaceError(
                    "Python mutation would introduce duplicate top-level definitions: "
                    + ", ".join(sorted(introduced))
                    + "; read the current file and preserve every existing definition exactly once"
                )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"Wrote {path}"

    def replace_text(self, path, old_text, content):
        current = self._read_raw(path)
        matches = current.count(old_text)
        if matches != 1:
            raise WorkspaceError(
                f"old_text must match exactly once (matches={matches}); "
                "read the current file and use a smaller unique exact anchor"
            )
        return self.write_file(path, current.replace(old_text, content, 1))

    def delete_file(self, path):
        self._safe_path(path).unlink(missing_ok=True)
        return f"Deleted {path}"

    def search(self, text):
        matches = []
        for path in self.paths():
            try:
                for n, line in enumerate(self.read_file(path).splitlines(), 1):
                    if text.lower() in line.lower():
                        matches.append(f"{path}:{n}: {line[:300]}")
                        if len(matches) >= 100:
                            return "\n".join(matches)
            except (ValueError, UnicodeError, WorkspaceError):
                continue
        return "\n".join(matches) or "No matches"

    def snapshot(self):
        files = {}
        size = 0
        for path in self.paths():
            target = self._safe_path(path)
            if not target.exists():
                continue
            if is_suspicious_artifact(path):
                raise WorkspaceError(f"Remove generated/runtime artifact: {path}")
            if not target.is_file() or target.stat().st_size > 200000:
                raise WorkspaceError(f"Unsupported or oversized file: {path}")
            try:
                content = target.read_text(encoding="utf-8")
            except UnicodeError as exc:
                raise WorkspaceError(f"Binary file requires human handling: {path}") from exc
            if secret_present(credential_scan_content(path, content)):
                raise WorkspaceError(f"Credential detected in {path}; remove and rotate it")
            size += len(content.encode())
            if size > 4_000_000 or len(files) >= 2500:
                raise WorkspaceError("Repository exceeds V0.2 source snapshot limit")
            files[path] = content
        return files

    def digest(self):
        return hashlib.sha256(json.dumps(self.snapshot(), sort_keys=True).encode()).hexdigest()

    def diff(self):
        self.assert_safe_to_commit()
        self._run(["git", "add", "-A"])
        result = self._run(
            ["git", "diff", "--cached", "--no-ext-diff", "--no-renames", self.base_sha or "HEAD"]
        ).stdout
        if len(result) > 110000:
            raise WorkspaceError("Diff exceeds review context limit; split the task")
        return redact(result)

    def assert_safe_to_commit(self):
        self.snapshot()

    def run_commands(self, commands):
        payload = {
            "files": self.snapshot(),
            "commands": commands,
            "timeout": settings.command_timeout_seconds,
            "profile": self.policy.profile,
            "install_dependencies": self.policy.install_dependencies,
        }
        with httpx.Client(
            timeout=settings.command_timeout_seconds * (len(commands) + 2) + 30, trust_env=False
        ) as client:
            response = client.post(
                settings.sandbox_url.rstrip("/") + "/run",
                json=payload,
                headers={"Authorization": f"Bearer {settings.sandbox_token}"},
            )
            response.raise_for_status()
            return TestReport.model_validate(response.json())

    def run_command(self, command):
        args = shlex.split(command)
        allowed = (
            ("python", "-m", "pytest"),
            ("pytest",),
            ("python", "-m", "compileall"),
            ("python", "-m", "py_compile"),
            ("npm", "test"),
            ("npm", "run", "build"),
            ("node", "--test"),
        )
        if not any(tuple(args[: len(p)]) == p for p in allowed):
            raise WorkspaceError(
                "Use pytest, Python compile checks, npm test/build, or node --test. "
                "Shell exports and environment-prefixed commands are unsupported; "
                "configure test-only environment with pytest monkeypatch.setenv in the test fixture. "
                "Never use live credentials."
            )
        return self.run_commands([args]).model_dump_json()

    def default_tests(self):
        commands = (
            [
                ["python", "-m", "compileall", "-q", "."],
                ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider"],
            ]
            if self.policy.profile == "python"
            else [["npm", "test", "--", "--runInBand"], ["npm", "run", "build", "--if-present"]]
        )
        if self.policy.profile == "node":
            # Test runner options differ; project test script owns runner-specific flags.
            commands[0] = ["npm", "test"]
        return self.run_commands(commands)

    def changed_test_files(self, diff):
        """Test files added or modified by the diff, used to prove new tests run standalone."""
        changed = []
        for line in diff.splitlines():
            if not line.startswith("+++ b/"):
                continue
            path = line[6:].strip()
            if path == "/dev/null" or path in changed:
                continue
            p = Path(path)
            name = p.name
            # Jest and node:test default globs: *.test.*, *_test.*, test-*, or anything under __tests__/.
            if (
                name.startswith("test_")
                or name.startswith("test-")
                or name.endswith(("_test.py", "_test.js", "_test.ts"))
                or name.endswith((".test.js", ".test.ts", ".test.mjs", ".test.cjs", ".spec.js", ".spec.ts"))
                or "__tests__" in p.parts
            ):
                changed.append(path)
        return changed

    def standalone_tests(self, paths):
        """Run only the changed test files so new behaviour is proven in isolation from the suite."""
        if not paths:
            return None
        if self.policy.profile == "python":
            return self.run_commands([["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", *paths]])
        return self.run_commands([["npm", "test", "--", *paths]])

    def commit(self, message, expected_digest):
        if self.digest() != expected_digest:
            raise WorkspaceError("Source changed after review")
        self._run(["git", "add", "-A"])
        if self._run(["git", "diff", "--cached", "--quiet"], check=False).returncode:
            self._run(["git", "commit", "-m", message])
        sha = self._run(["git", "rev-parse", "HEAD"]).stdout.strip()
        if sha == self.base_sha:
            raise WorkspaceError("No changes to publish")
        return sha

    def push(self, sha):
        if not re.fullmatch(r"[a-f0-9]{40}", sha):
            raise WorkspaceError("Invalid commit")
        self._run(["git", "push", "origin", f"{sha}:refs/heads/{self.branch}"], auth=True)
