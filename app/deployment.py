"""Explicit, commit-bound deployment of an existing registered Coolify application."""

import re

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.db import Deployment, SessionLocal
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

    async def deploy(self, task_id, approved_sha):
        task = await store.get(task_id)
        if not task or task.status != "pr_created" or not task.pr_url or not task.head_sha:
            raise ValueError("Deployment requires a reviewed, published V0.2 task")
        if not re.fullmatch(r"[a-f0-9]{40}", approved_sha):
            raise ValueError("Provide the full 40-character merged commit SHA")
        policy = settings.projects().get(task.project)
        if not policy or policy.repo != task.repo or not policy.coolify_uuid:
            raise ValueError("No registered Coolify application for this project")
        number = int(task.pr_url.rstrip("/").split("/")[-1])
        pr = await self.github.pull(task.repo, number)
        if (
            not pr.get("merged")
            or pr["head"]["sha"] != task.head_sha
            or pr.get("merge_commit_sha") != approved_sha
        ):
            raise ValueError("PR must be merged and both reviewed head and approved merge SHA must match")
        if await self.github.branch_sha(task.repo, task.base_branch) != approved_sha:
            raise ValueError("Approved commit is no longer the current base; re-review before deployment")
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
                raise ValueError("No deployment requested")
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
