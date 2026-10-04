import json
import re
from pathlib import Path
from urllib.parse import quote

from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Project(BaseModel):
    repo: str
    base_branch: str = "main"
    profile: str = "python"
    require_deployment: bool = False
    install_dependencies: bool = False
    coolify_uuid: str = ""

    @model_validator(mode="after")
    def validate_project(self):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.repo):
            raise ValueError("repo must be owner/name")
        # The owner/name character class also matches "." and "..", which name a parent
        # directory rather than a repository.
        if any(part in {".", ".."} for part in self.repo.split("/")):
            raise ValueError("repo owner and name must not be '.' or '..'")
        if self.profile not in {"python", "node"}:
            raise ValueError("profile must be python or node")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./-]*", self.base_branch) or ".." in self.base_branch:
            raise ValueError("invalid base branch")
        return self


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: str = "sqlite+aiosqlite:///./factory.db"
    database_host: str = ""
    database_password: str = ""
    openrouter_api_key: str = ""
    telegram_bot_token: str = ""
    github_token: str = ""
    github_owner: str = "degitalintelligence"
    lab_repo: str = "telegram-lab"
    # Roles accept either a concrete provider model or an audited alias.
    model_aliases_json: str = '{"bunny-alpha":"stealth/space-bunny-alpha"}'
    lead_model: str = "bunny-alpha"
    developer_model: str = "bunny-alpha"
    reviewer_model: str = "bunny-alpha"
    projects_json: str = ""
    telegram_allowed_user_ids: str = ""
    api_token: str = ""
    # HTTP is a shared operator credential, so the server—not the request body—
    # supplies the audit principal for approvals.
    api_operator_user_id: int | None = Field(default=None, ge=1)

    @field_validator("api_operator_user_id", mode="before")
    @classmethod
    def blank_principal_means_unset(cls, value):
        """An empty forwarded variable (Coolify/Compose) is "not configured", not a parse error.

        Fail-closed semantics stay with validate_runtime(): an empty principal only
        blocks startup when API_TOKEN is actually set.
        """
        if isinstance(value, str) and not value.strip():
            return None
        return value

    max_iterations: int = Field(default=4, ge=1, le=10)
    max_dev_steps: int = Field(default=36, ge=1, le=200)
    # A separate stall ceiling prevents repetitive agent actions from consuming the whole task budget.
    max_developer_stall_steps: int = Field(default=8, ge=2, le=30)
    # Developer prompts are narrower than the global LLM prompt budget.
    max_developer_context_chars: int = Field(default=100_000, ge=20_000, le=180_000)
    # Self-improvement lead context is intentionally compact and target-focused.
    self_task_context_chars: int = Field(default=45_000, ge=10_000, le=120_000)
    max_llm_calls: int = Field(default=150, ge=1, le=1000)
    max_total_tokens: int = Field(default=600_000, ge=1)
    max_cost_usd: float = Field(default=5.0, gt=0)
    max_output_tokens: int = Field(default=8192, ge=256, le=32768)
    max_prompt_chars: int = Field(default=180_000, ge=1000)
    llm_timeout_seconds: int = Field(default=120, ge=5, le=600)
    task_timeout_seconds: int = Field(default=3600, ge=30)
    worker_concurrency: int = Field(default=1, ge=1, le=4)
    worker_enabled: bool = True
    lease_seconds: int = Field(default=90, ge=30)
    max_recoveries: int = Field(default=2, ge=0, le=10)
    workspace_root: Path = Path("/workspaces")
    sandbox_url: str = "http://sandbox:8090"
    sandbox_token: str = ""
    command_timeout_seconds: int = Field(default=180, ge=5, le=600)
    coolify_url: str = ""
    coolify_token: str = ""
    role_clearance_json: str = ""

    @model_validator(mode="after")
    def database_credentials(self):
        if self.database_host:
            if not self.database_password:
                raise ValueError("DATABASE_PASSWORD is required with DATABASE_HOST")
            self.database_url = f"postgresql+asyncpg://ai_factory:{quote(self.database_password, safe='')}@{self.database_host}:5432/ai_factory"
        return self

    def model_aliases(self) -> dict[str, str]:
        """Resolve model aliases from operator configuration; unknown aliases fail closed."""
        try:
            raw = self.model_aliases_json.strip() or '{"bunny-alpha":"stealth/space-bunny-alpha"}'
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("MODEL_ALIASES_JSON must be valid JSON") from exc
        if (
            not isinstance(data, dict)
            or not data
            or any(
                not isinstance(k, str) or not k or not isinstance(v, str) or not v for k, v in data.items()
            )
        ):
            raise ValueError("MODEL_ALIASES_JSON must map non-empty aliases to model IDs")
        return data

    def configured_model(self, role: str) -> str:
        """The operator-configured model name for a role, before alias resolution.

        Kept separate from model_for() so a call can be audited with both the alias the
        operator configured and the provider model that actually ran.
        """
        if role not in {"lead", "developer", "reviewer"}:
            raise ValueError("Unknown model role")
        return getattr(self, f"{role}_model")

    def model_for(self, role: str) -> str:
        configured = self.configured_model(role)
        return self.model_aliases().get(configured, configured)

    def projects(self) -> dict[str, Project]:
        data = (
            json.loads(self.projects_json)
            if self.projects_json
            else {"lab": {"repo": f"{self.github_owner}/{self.lab_repo}"}}
        )
        if (
            not isinstance(data, dict)
            or not data
            or not all(re.fullmatch(r"[a-z0-9_-]{1,40}", k) for k in data)
        ):
            raise ValueError("PROJECTS_JSON must map project aliases to policies")
        return {k: Project.model_validate(v) for k, v in data.items()}

    def role_clearance(self) -> dict[str, str]:
        """Which sensitivity each agent role may read.

        Configuration, not code: the AI Lead may propose a change, but it is applied
        only after Dedi's approval, so it travels through the same decision path as any
        other policy change. Unknown values fail closed rather than widening access.
        """
        defaults = {"lead": "confidential", "developer": "internal", "reviewer": "internal"}
        data = json.loads(self.role_clearance_json) if self.role_clearance_json else defaults
        valid = {"public", "internal", "confidential", "restricted"}
        if not isinstance(data, dict) or not data:
            raise ValueError("ROLE_CLEARANCE_JSON must map agent roles to a sensitivity level")
        if any(not isinstance(v, str) or v not in valid for v in data.values()):
            raise ValueError(f"ROLE_CLEARANCE_JSON values must be one of {sorted(valid)}")
        return data

    def allowed_users(self) -> set[int]:
        return {int(x.strip()) for x in self.telegram_allowed_user_ids.split(",") if x.strip()}

    def validate_runtime(self):
        self.projects()
        self.role_clearance()
        if self.telegram_bot_token and not self.allowed_users():
            raise ValueError("TELEGRAM_ALLOWED_USER_IDS is required; bot access fails closed")
        if self.api_token and self.api_operator_user_id is None:
            # An approval or deployment decided through HTTP would otherwise be recorded
            # with no principal, making the audit trail unattributable.
            raise ValueError("API_OPERATOR_USER_ID is required when API_TOKEN is set")
        if self.worker_enabled:
            missing = [
                name
                for name in (
                    "github_token",
                    "openrouter_api_key",
                    "lead_model",
                    "developer_model",
                    "reviewer_model",
                    "sandbox_token",
                )
                if not getattr(self, name)
            ]
            if missing:
                raise ValueError("Missing worker settings: " + ", ".join(missing))


settings = Settings()
