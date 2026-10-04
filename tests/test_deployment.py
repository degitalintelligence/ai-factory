import pytest

from app.config import settings
from app.deployment import DeploymentService


@pytest.fixture
async def deployment_task(db, monkeypatch):
    monkeypatch.setattr(settings, "projects_json", '{"lab":{"repo":"owner/repo","coolify_uuid":"app123"}}')
    task = await db.create("Prepare deployable app")
    await db.update(
        task.id, status="pr_created", pr_url="https://github.com/owner/repo/pull/1", head_sha="a" * 40
    )
    return await db.get(task.id)


class FakeGitHub:
    merged = True
    tree_equal = True
    checks_pass = True

    async def pull(self, repo, number):
        return {"merged": self.merged, "head": {"sha": "a" * 40}, "merge_commit_sha": "b" * 40}

    async def branch_sha(self, repo, branch):
        return "b" * 40

    async def request(self, method, path, **kwargs):
        if "/git/commits/" in path:
            return {"tree": {"sha": "tree" if self.tree_equal or path.endswith("a" * 40) else "different"}}
        if path.endswith("/status"):
            return {"total_count": 1, "state": "success" if self.checks_pass else "pending"}
        return {"total_count": 1, "check_runs": [{"conclusion": "success"}]}


class FakeService(DeploymentService):
    def __init__(self, sessions):
        super().__init__(github=FakeGitHub(), sessions=sessions)
        self.calls = []
        self.pin = ""
        self.ambiguous = False

    async def coolify(self, method, path, **kwargs):
        self.calls.append((method, path))
        if method == "PATCH":
            self.pin = kwargs["json"]["git_commit_sha"]
            return {}
        if path.startswith("applications/"):
            return {
                "git_repository": "https://github.com/owner/repo",
                "git_branch": "main",
                "git_commit_sha": self.pin,
                "settings": {"is_auto_deploy_enabled": False},
            }
        if path.startswith("deployments/"):
            return {"commit": "b" * 40, "status": "finished"}
        if self.ambiguous:
            raise TimeoutError("Response lost")
        return {"deployments": [{"deployment_uuid": "deploy123"}]}


async def test_deploy_is_exact_commit_bound_and_at_most_once(db, deployment_task):
    service = FakeService(db.sessions)
    with pytest.raises(ValueError, match="40-character"):
        await service.deploy(deployment_task.id, "short-sha")
    record = await service.deploy(deployment_task.id, "b" * 40)
    assert service.pin == "b" * 40 and record.status == "queued"
    with pytest.raises(ValueError, match="already requested"):
        await service.deploy(deployment_task.id, "b" * 40)
    assert service.calls.count(("POST", "deploy")) == 1
    assert (await service.status(deployment_task.id)).status == "finished"


@pytest.mark.parametrize(
    "field,match",
    [
        ("merged", "must be merged"),
        ("tree_equal", "outside the reviewed tree"),
        ("checks_pass", "incomplete or failing"),
    ],
)
async def test_unreviewed_or_failed_release_is_blocked(db, deployment_task, field, match):
    service = FakeService(db.sessions)
    setattr(service.github, field, False)
    with pytest.raises(ValueError, match=match):
        await service.deploy(deployment_task.id, "b" * 40)
    assert ("POST", "deploy") not in service.calls


async def test_ambiguous_deployment_is_not_retried_automatically(db, deployment_task):
    service = FakeService(db.sessions)
    service.ambiguous = True
    with pytest.raises(TimeoutError):
        await service.deploy(deployment_task.id, "b" * 40)
    record = await service.status(deployment_task.id)
    assert record.status == "unknown"
    with pytest.raises(ValueError, match="already requested"):
        await service.deploy(deployment_task.id, "b" * 40)
    assert service.calls.count(("POST", "deploy")) == 1
