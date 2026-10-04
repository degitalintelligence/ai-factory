"""Explicit, commit-bound publication and deployment of reviewed PRs."""

import re

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.db import Deployment, Event, SessionLocal, Task
from app.github_api import GitHubAPI
from app.store import store


class DeploymentService:
    def __init__(self, github=None, client=None, sessions=SessionLocal):
        self.github = github or GitHubAPI()
        self.client = client
        self.sessions = sessions

    async def coolify(self, method, path, **kwargs):
        if not settings.coolify_url or not settings.coolify_token:
            raise ValueError("Configure COOLIFY_URL and COOLIFY_TOKEN before deploying")

        async def send(client):
            response = await client.request(
                method,
                settings.coolify_url.rstrip("/") + "/api/v1/" + path,
                headers={"Authorization": f"Bearer {settings.coolify_token}"},
                **kwargs,
            )
            response.raise_for_status()
            return response.json()

        if self.client:
            return await send(self.client)
        async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
            return await send(client)

    async def _validated_release(self, task_id, approved_sha):
        task = await store.get(task_id)
        if not task or task.status != "pr_created" or not task.pr_url or not task.head_sha:
            raise ValueError("Release requires a reviewed, published PR task")
        if not re.fullmatch(r"[a-f0-9]{40}", approved_sha):
            raise ValueError("Provide the full 40-character merged commit SHA")
        policy = settings.projects().get(task.project)
        if not policy or policy.repo != task.repo:
            raise ValueError("Project policy does not match the task repository")
        try:
            number = int(task.pr_url.rstrip("/").split("/")[-1])
        except (AttributeError, ValueError):
            raise ValueError("Task PR URL is invalid") from None
        pr = await self.github.pull(task.repo, number)
        if (
            not pr.get("merged")
            or pr["head"]["sha"] != task.head_sha
            or pr.get("merge_commit_sha") != approved_sha
        ):
            raise ValueError("PR must be merged and both reviewed head and approved merge SHA must match")
        if await self.github.branch_sha(task.repo, task.base_branch) != approved_sha:
            raise ValueError("Approved commit is no longer the current base; re-review before release")
        reviewed = await self.github.request("GET", f"{task.repo}/git/commits/{task.head_sha}")
        merged = await self.github.request("GET", f"{task.repo}/git/commits/{approved_sha}")
        if reviewed["tree"]["sha"] != merged["tree"]["sha"]:
            raise ValueError("Merge contains changes outside the reviewed tree; run a fresh review")
        status = await self.github.request("GET", f"{task.repo}/commits/{approved_sha}/status")
        checks = await self.github.request(
            "GET", f"{task.repo}/commits/{approved_sha}/check-runs", params={"per_page": 100}
        )
        if (
            (status.get("total_count", 0) and status["state"] != "success")
            or checks.get("total_count", 0) > 100
            or any(
                c.get("conclusion") not in {"success", "skipped", "neutral"}
                for c in checks.get("check_runs", [])
            )
        ):
            raise ValueError("GitHub checks are incomplete or failing")
        return task, policy

    async def _existing_record(self, task_id):
        async with self.sessions() as s:
            return await s.scalar(select(Deployment).where(Deployment.task_id == task_id))

    async def _record_publication(self, task, approved_sha):
        """Acknowledge a merged PR for a project with no deployment target."""
        message = "Merged PR acknowledged; project has no Coolify deployment target."
        try:
            async with self.sessions() as s, s.begin():
                row = await s.get(Task, task.id, with_for_update=True)
                if not row:
                    raise ValueError("Task not found")
                existing = await s.scalar(select(Deployment).where(Deployment.task_id == task.id))
                if existing:
                    if existing.commit_sha != approved_sha or existing.status != "published":
                        raise ValueError(
                            "A different publication is already recorded; use /deployment to reconcile"
                        )
                    return existing
                record = Deployment(
                    task_id=task.id,
                    commit_sha=approved_sha,
                    status="published",
                    message=message,
                )
                row.status = "completed"
                row.last_message = f"{message} Commit: {approved_sha}"
                row.cancel_requested = False
                row.lease_owner = None
                row.lease_until = None
                s.add(record)
                s.add(
                    Event(
                        task_id=task.id,
                        kind="publication",
                        message=f"Publication acknowledged for merged commit {approved_sha}; no Coolify target.",
                    )
                )
                await s.flush()
                return record
        except IntegrityError:
            existing = await self._existing_record(task.id)
            if existing and existing.commit_sha == approved_sha and existing.status == "published":
                return existing
            raise ValueError("Publication already recorded; use /deployment to reconcile") from None

    async def publish(self, task_id, approved_sha):
        """Finalize a merged PR without invoking Coolify for a non-deploy project."""
        task = await store.get(task_id)
        if task and task.status == "completed":
            existing = await self._existing_record(task_id)
            if existing and existing.commit_sha == approved_sha and existing.status == "published":
                return existing
        task, policy = await self._validated_release(task_id, approved_sha)
        if policy.require_deployment:
            raise ValueError("Project policy requires deployment; use /deploy instead")
        return await self._record_publication(task, approved_sha)

    async def supersede(self, task_id, reason):
        """Close a stale merged-PR checkpoint so a fresh review can be queued.

        This is deliberately separate from cancellation. It only applies when the
        task PR was merged, the approved merge is no longer the branch tip, and the
        current branch still contains that merge. No publication or deployment is
        recorded by this operation.
        """
        if not reason.strip():
            raise ValueError("Supersede requires a reason")
        task = await store.get(task_id)
        if not task or task.status != "pr_created" or not task.pr_url or not task.head_sha:
            raise ValueError("Supersede requires a stale reviewed PR task")
        policy = settings.projects().get(task.project)
        if not policy or policy.repo != task.repo:
            raise ValueError("Project policy does not match the task repository")
        try:
            number = int(task.pr_url.rstrip("/").split("/")[-1])
        except (AttributeError, ValueError):
            raise ValueError("Task PR URL is invalid") from None
        pr = await self.github.pull(task.repo, number)
        if not pr.get("merged") or pr.get("head", {}).get("sha") != task.head_sha:
            raise ValueError("Supersede requires the reviewed PR to be merged unchanged")
        merged_sha = pr.get("merge_commit_sha")
        if not isinstance(merged_sha, str) or not re.fullmatch(r"[a-f0-9]{40}", merged_sha):
            raise ValueError("Merged PR has no valid full commit SHA")
        current_sha = await self.github.branch_sha(task.repo, task.base_branch)
        if not current_sha:
            raise ValueError("Current base branch cannot be resolved")
        if current_sha == merged_sha:
            raise ValueError("Approved commit is still the current base; use /publish or /deploy")
        comparison = await self.github.request("GET", f"{task.repo}/compare/{merged_sha}...{current_sha}")
        if comparison.get("merge_base_commit", {}).get("sha") != merged_sha:
            raise ValueError("Current base no longer contains the merged PR; inspect the repository")
        reviewed = await self.github.request("GET", f"{task.repo}/git/commits/{task.head_sha}")
        merged = await self.github.request("GET", f"{task.repo}/git/commits/{merged_sha}")
        if reviewed["tree"]["sha"] != merged["tree"]["sha"]:
            raise ValueError("Merge contains changes outside the reviewed tree; do not supersede")
        note = (
            f"{reason.strip()[:10000]} Current base: {current_sha}; "
            f"merged PR: {merged_sha}. No publication or deployment was recorded."
        )
        return await store.supersede(task_id, note)

    async def deploy(self, task_id, approved_sha):
        task, policy = await self._validated_release(task_id, approved_sha)
        # Backward-compatible alias: /deploy can acknowledge a merged PR when the
        # project policy explicitly says it has no deployment target.
        if not policy.require_deployment:
            return await self._record_publication(task, approved_sha)
        if not policy.coolify_uuid:
            raise ValueError("No registered Coolify application for this project")
        app = await self.coolify("GET", f"applications/{policy.coolify_uuid}")
        repo = app.get("git_repository", "").removesuffix(".git").removeprefix("https://github.com/")
        if repo != task.repo or app.get("git_branch") != task.base_branch:
            raise ValueError("Coolify application repository/branch does not match the task")
        if app.get("settings", {}).get("is_auto_deploy_enabled", True):
            raise ValueError("Disable Coolify auto-deploy for commit-bound explicit deployment")
        # Durable at-most-once submission. Ambiguous timeouts require operator reconciliation.
        async with self.sessions() as s:
            record = Deployment(task_id=task_id, commit_sha=approved_sha)
            s.add(record)
            try:
                await s.commit()
            except IntegrityError:
                await s.rollback()
                raise ValueError("Deployment already requested; use /deployment to reconcile") from None
            try:
                await self.coolify(
                    "PATCH", f"applications/{policy.coolify_uuid}", json={"git_commit_sha": approved_sha}
                )
                pinned = await self.coolify("GET", f"applications/{policy.coolify_uuid}")
                if pinned.get("git_commit_sha") != approved_sha:
                    raise ValueError("Coolify did not retain the approved commit pin")
                result = await self.coolify(
                    "POST", "deploy", json={"uuid": policy.coolify_uuid, "force": False}
                )
                deployments = result.get("deployments", [])
                if len(deployments) != 1 or not deployments[0].get("deployment_uuid"):
                    raise ValueError("Coolify response did not identify exactly one deployment")
                record.deployment_uuid = deployments[0]["deployment_uuid"]
                record.status = "queued"
                record.message = "Submitted to Coolify; deployment success is not yet verified"
            except Exception:
                record.status = "unknown"
                record.message = "Submission outcome uncertain. Inspect Coolify before any manual retry."
                await s.commit()
                raise
            await s.commit()
            await store.event(
                task_id,
                "deployment",
                f"Explicit deployment requested: {approved_sha}; {record.deployment_uuid}",
            )
            return record

    async def status(self, task_id):
        async with self.sessions() as s:
            record = await s.scalar(select(Deployment).where(Deployment.task_id == task_id))
            if not record:
                raise ValueError("No deployment or publication recorded")
            if record.deployment_uuid:
                result = await self.coolify("GET", f"deployments/{record.deployment_uuid}")
                if not result.get("commit") and result.get("status") == "finished":
                    record.status = "unverified_commit"
                    record.message = (
                        "Coolify finished but omitted the commit; verify the deployed revision manually"
                    )
                elif result.get("commit") and result["commit"] != record.commit_sha:
                    record.status = "commit_mismatch"
                    record.message = "Coolify ran a different commit; inspect deployment immediately"
                else:
                    record.status = str(result.get("status", "unknown"))[:40]
                    record.message = (
                        "Coolify-reported status; validate application health and acceptance smoke tests"
                    )
                await s.commit()
            return record
