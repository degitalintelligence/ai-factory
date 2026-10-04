from urllib.parse import quote

import httpx

from app.config import settings


class GitHubAPI:
    def __init__(self, client=None):
        self.client = client
        self.headers = {
            "Authorization": f"Bearer {settings.github_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    async def request(self, method, path, **kwargs):
        async def send(client):
            response = await client.request(
                method, "https://api.github.com/repos/" + path, headers=self.headers, **kwargs
            )
            response.raise_for_status()
            return response.json()

        if self.client:
            return await send(self.client)
        async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
            return await send(client)

    async def branch_sha(self, repo, branch):
        try:
            result = await self.request("GET", f"{repo}/git/ref/heads/{quote(branch, safe='/')}")
            return result["object"]["sha"]
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            raise

    async def pull(self, repo, number):
        return await self.request("GET", f"{repo}/pulls/{number}")

    async def find_pr(self, repo, branch):
        prs = await self.request(
            "GET",
            f"{repo}/pulls",
            params={"state": "all", "head": f"{repo.split('/')[0]}:{branch}", "per_page": 100},
        )
        return next((p for p in prs if p["head"]["ref"] == branch), None)

    async def create_pr(self, *, repo_full_name, branch, title, body, base="main"):
        existing = await self.find_pr(repo_full_name, branch)
        if existing:
            if existing["state"] != "open":
                raise RuntimeError("Task PR was closed/merged; create a new task instead")
            await self.request(
                "PATCH", f"{repo_full_name}/pulls/{existing['number']}", json={"body": body, "title": title}
            )
            return existing["html_url"]
        try:
            result = await self.request(
                "POST",
                f"{repo_full_name}/pulls",
                json={"title": title, "head": branch, "base": base, "body": body, "draft": False},
            )
            return result["html_url"]
        except (httpx.TimeoutException, httpx.HTTPStatusError):
            # A lost response may still mean creation succeeded; reconcile before retrying.
            existing = await self.find_pr(repo_full_name, branch)
            if existing and existing["state"] == "open":
                return existing["html_url"]
            raise
