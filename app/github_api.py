import httpx
from app.config import settings

class GitHubAPI:
    def __init__(self) -> None:
        self.headers = {
            "Authorization": f"Bearer {settings.github_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    async def create_pr(self, *, repo_full_name: str, branch: str, title: str, body: str) -> str:
        url = f"https://api.github.com/repos/{repo_full_name}/pulls"
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                url,
                headers=self.headers,
                json={"title": title, "head": branch, "base": "main", "body": body},
            )
            response.raise_for_status()
            return response.json()["html_url"]
