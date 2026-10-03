from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: str
    openrouter_api_key: str
    telegram_bot_token: str
    github_token: str
    github_owner: str = "degitalintelligence"
    lab_repo: str = "telegram-lab"
    lead_model: str
    developer_model: str
    reviewer_model: str
    max_iterations: int = 3
    max_dev_steps: int = 30
    workspace_root: Path = Path("/workspaces")

settings = Settings()
