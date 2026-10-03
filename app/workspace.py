import shlex
import shutil
import subprocess
from pathlib import Path
from app.config import settings

ALLOWED_COMMAND_PREFIXES = (
    ("pytest",),
    ("python", "-m", "pytest"),
    ("python", "-m", "compileall"),
    ("python", "-m", "py_compile"),
)

SUSPICIOUS_ARTIFACT_NAMES = {
    ".env",
    ".coverage",
    ".DS_Store",
}
SUSPICIOUS_ARTIFACT_SUFFIXES = {
    ".db",
    ".sqlite",
    ".sqlite3",
    ".log",
    ".pid",
    ".pyc",
}
SUSPICIOUS_ARTIFACT_DIRS = {
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
}

class WorkspaceError(RuntimeError):
    pass

def _status_path(line: str) -> str:
    path = line[3:] if len(line) >= 4 else ""
    if " -> " in path:
        path = path.split(" -> ", 1)[1]
    return path.strip()

def is_suspicious_artifact(path: str) -> bool:
    p = Path(path)
    if p.name in SUSPICIOUS_ARTIFACT_NAMES:
        return True
    if p.suffix.lower() in SUSPICIOUS_ARTIFACT_SUFFIXES:
        return True
    return any(part in SUSPICIOUS_ARTIFACT_DIRS for part in p.parts)

class Workspace:
    def __init__(self, task_id: int, repo_full_name: str, branch: str):
        self.task_id = task_id
        self.repo_full_name = repo_full_name
        self.branch = branch
        self.path = settings.workspace_root / f"task-{task_id}"

    def _run(self, args: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
        result = subprocess.run(
            args,
            cwd=self.path if self.path.exists() else None,
            text=True,
            capture_output=True,
            timeout=180,
        )
        if check and result.returncode != 0:
            raise WorkspaceError(result.stderr or result.stdout)
        return result

    def prepare(self) -> None:
        if self.path.exists():
            shutil.rmtree(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        clone_url = f"https://x-access-token:{settings.github_token}@github.com/{self.repo_full_name}.git"
        subprocess.run(
            ["git", "clone", clone_url, str(self.path)],
            check=True,
            text=True,
            capture_output=True,
            timeout=180,
        )
        self._run(["git", "config", "user.email", "ai-factory@local"])
        self._run(["git", "config", "user.name", "AI Factory"])
        self._run(["git", "checkout", "-b", self.branch])

    def list_files(self) -> str:
        files: list[str] = []
        for p in self.path.rglob("*"):
            if ".git" in p.parts or not p.is_file():
                continue
            files.append(str(p.relative_to(self.path)))
        return "\n".join(sorted(files))[:20000]

    def read_file(self, path: str) -> str:
        target = self._safe_path(path)
        if not target.exists() or not target.is_file():
            raise WorkspaceError(f"File not found: {path}")
        return target.read_text(encoding="utf-8")[:40000]

    def write_file(self, path: str, content: str) -> str:
        target = self._safe_path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"Wrote {path}"

    def delete_file(self, path: str) -> str:
        target = self._safe_path(path)
        if not target.exists():
            return f"File already absent: {path}"
        if not target.is_file():
            raise WorkspaceError(f"Not a file: {path}")
        target.unlink()
        return f"Deleted {path}"

    def run_command(self, command: str) -> str:
        if any(token in command for token in (";", "|", "&&", "||", ">", "<", "$(")):
            raise WorkspaceError("Shell operators are not allowed.")
        args = shlex.split(command)
        if not args:
            raise WorkspaceError("Empty command.")
        allowed = any(tuple(args[: len(prefix)]) == prefix for prefix in ALLOWED_COMMAND_PREFIXES)
        if not allowed:
            raise WorkspaceError(f"Command not allowed: {command}")
        result = self._run(args, check=False)
        return f"exit={result.returncode}\nSTDOUT:\n{result.stdout[-12000:]}\nSTDERR:\n{result.stderr[-12000:]}"

    def diff(self) -> str:
        return self._run(["git", "diff"], check=False).stdout[:50000]

    def status_porcelain(self) -> str:
        return self._run(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            check=False,
        ).stdout

    def hygiene_issues(self, before_tests: str, after_tests: str) -> list[str]:
        before_paths = {
            _status_path(line)
            for line in before_tests.splitlines()
            if line.strip()
        }
        after_lines = [line for line in after_tests.splitlines() if line.strip()]
        after_paths = {_status_path(line) for line in after_lines}

        issues: list[str] = []

        for path in sorted(after_paths - before_paths):
            issues.append(
                f"Tests created or dirtied repository artifact: {path}. "
                "Tests must not leave new working-tree artifacts."
            )

        for path in sorted(after_paths):
            if is_suspicious_artifact(path):
                issues.append(
                    f"Suspicious runtime/generated artifact present in git status: {path}. "
                    "Use temp/in-memory storage or ignore runtime data instead of committing it."
                )

        return list(dict.fromkeys(issues))

    def default_tests(self) -> str:
        outputs = []
        for cmd in (["python", "-m", "compileall", "."], ["pytest", "-q"]):
            result = self._run(cmd, check=False)
            outputs.append(
                f"$ {' '.join(cmd)}\nexit={result.returncode}\n"
                f"{result.stdout[-8000:]}\n{result.stderr[-8000:]}"
            )
        return "\n\n".join(outputs)

    def assert_safe_to_commit(self) -> None:
        status = self.status_porcelain()
        suspicious = sorted(
            {
                _status_path(line)
                for line in status.splitlines()
                if line.strip() and is_suspicious_artifact(_status_path(line))
            }
        )
        if suspicious:
            raise WorkspaceError(
                "Refusing to commit suspicious runtime/generated artifacts: "
                + ", ".join(suspicious)
            )

    def commit_and_push(self, message: str) -> None:
        self.assert_safe_to_commit()
        self._run(["git", "add", "-A"])
        status = self._run(["git", "status", "--porcelain"], check=False).stdout.strip()
        if not status:
            raise WorkspaceError("No changes to commit.")
        self._run(["git", "commit", "-m", message])
        self._run(["git", "push", "-u", "origin", self.branch])

    def _safe_path(self, path: str) -> Path:
        candidate = (self.path / path).resolve()
        root = self.path.resolve()
        if candidate != root and root not in candidate.parents:
            raise WorkspaceError("Path escapes workspace.")
        return candidate
