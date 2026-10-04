from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class PlanBudget(BaseModel):
    """The planner's requested budget; runtime ceilings remain operator-configured."""

    max_llm_calls: int = Field(default=30, ge=1, le=1000)
    max_tokens: int = Field(default=120_000, ge=1)
    max_cost_usd: float = Field(default=1.0, gt=0)


class LeadPlan(BaseModel):
    objective: str = Field(min_length=1)
    acceptance_criteria: list[str] = Field(min_length=1)
    assumptions: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    suggested_tests: list[str] = Field(default_factory=list)
    steps: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    approval_gates: list[str] = Field(default_factory=list)
    rollback_plan: str = ""
    budget: PlanBudget = Field(default_factory=PlanBudget)
    questions: list[str] = Field(default_factory=list)
    risk: Literal["low", "medium", "high"] = "low"
    deployment_required: bool = False
    persistence_required: bool = False
    # Criteria verifiable only once the PR exists (PR body, PR URL, published CI/deployment records).
    post_publication_criteria: list[int] = Field(default_factory=list)

    @model_validator(mode="after")
    def valid_post_publication_criteria(self):
        total = len(self.acceptance_criteria)
        if any(n < 1 or n > total for n in self.post_publication_criteria):
            raise ValueError("post_publication_criteria must reference an existing acceptance criterion")
        if len(set(self.post_publication_criteria)) != len(self.post_publication_criteria):
            raise ValueError("post_publication_criteria must not repeat a criterion index")
        return self


class DeveloperAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal[
        "list_files",
        "read_file",
        "search",
        "write_file",
        "replace_text",
        "delete_file",
        "run_command",
        "git_diff",
        "finish",
    ]
    path: str | None = None
    content: str | None = None
    old_text: str | None = None
    command: str | None = None
    note: str | None = None

    @model_validator(mode="after")
    def required_fields(self):
        if self.action in {"read_file", "write_file", "replace_text", "delete_file"} and not self.path:
            raise ValueError("path is required")
        if self.action in {"write_file", "replace_text", "search"} and self.content is None:
            raise ValueError("content is required")
        if self.action == "replace_text" and not self.old_text:
            raise ValueError("old_text is required")
        if self.action == "run_command" and not self.command:
            raise ValueError("command is required")
        return self


class CriterionEvidence(BaseModel):
    criterion: int = Field(ge=1)
    satisfied: bool
    evidence: str = Field(min_length=1)


class ReviewResult(BaseModel):
    approved: bool
    summary: str
    issues: list[str] = Field(default_factory=list)
    criteria: list[CriterionEvidence] = Field(default_factory=list)


class CommandResult(BaseModel):
    command: list[str]
    exit_code: int
    output: str = ""
    timed_out: bool = False


class TestReport(BaseModel):
    results: list[CommandResult] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)
    environment: dict[str, str] = Field(default_factory=dict)

    @property
    def passed(self):
        return (
            bool(self.results)
            and not self.issues
            and all(r.exit_code == 0 and not r.timed_out for r in self.results)
        )


class TaskKind(StrEnum):
    ENGINEERING = "engineering"
    SELF_IMPROVEMENT = "self_improvement"


# Paths that always require an explicit Dedi decision. Matched as path suffixes/segments,
# because a sensitive file rarely names its own risk (config.py holds budget and allowlist).
SENSITIVE_PATHS = (
    "app/config.py",
    "app/security.py",
    "app/gates.py",
    "app/db.py",
    "sandbox/server.py",
    "sandbox/bwrap",
    "docker-compose",
    "compose.",
    "dockerfile",
    ".github/workflows/",
    "deploy/",
    "scripts/install-sandbox",
    ".env",
)

# Free-text markers, matched against declared scope and touched areas.
SENSITIVE_TERMS = (
    "permission",
    "allowlist",
    "sandbox",
    "bwrap",
    "egress",
    "network policy",
    "secret",
    "credential",
    "api key",
    "api_key",
    "token",
    "budget",
    "hard limit",
    "model policy",
    "model_policy",
    "provider",
    "release",
    "migration",
    "audit log",
    "approval gate",
    "production",
    "rollback",
)


class SelfImprovementBrief(BaseModel):
    """Mandatory record for a self-improvement task. Absence of any field blocks the task."""

    problem: str = Field(min_length=1, max_length=4000)
    evidence: list[str] = Field(min_length=1)
    hypothesis: str = Field(min_length=1, max_length=2000)
    scope: str = Field(min_length=1, max_length=2000)
    baseline: str = Field(min_length=1, max_length=4000)
    touched_areas: list[str] = Field(default_factory=list)
    rollback_plan: str = Field(min_length=1, max_length=2000)
    blast_radius: str = Field(default="", max_length=1000)
    review_date: datetime | None = None

    @property
    def sensitive_areas(self) -> list[str]:
        # Scope and touched areas are both caller-supplied free text, so paths are matched
        # against all of it: a sensitive file named only in the scope must still be detected.
        text = (self.scope + " " + " ".join(self.touched_areas)).lower().replace("\\", "/")
        hits = {p for p in SENSITIVE_PATHS if p in text}
        hits |= {t for t in SENSITIVE_TERMS if t in text}
        return sorted(hits)

    @property
    def requires_explicit_approval(self) -> bool:
        # Self-improvement may never auto-approve itself through the normal gate path.
        return bool(self.sensitive_areas)


class Sensitivity(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"


# A role may only read memories at or below its own clearance level.
CLEARANCE = {
    "public": 0,
    "internal": 1,
    "confidential": 2,
    "restricted": 3,
}


class MemoryState(StrEnum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    RETRACTED = "retracted"


class MemoryWrite(BaseModel):
    """A memory write request. Secrets are rejected before anything is stored."""

    key: str = Field(min_length=1, max_length=200)
    value: str = Field(min_length=1, max_length=8000)
    scope: str = Field(default="", max_length=120)
    sensitivity: Sensitivity = Sensitivity.INTERNAL
    source: str = Field(min_length=1, max_length=300)
    evidence_ref: str = Field(default="", max_length=300)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    expires_at: datetime | None = None
    locked: bool = False


class MemoryView(BaseModel):
    """What an agent is allowed to see. Carries provenance so it can be judged."""

    id: int
    key: str
    value: str
    scope: str
    sensitivity: Sensitivity
    source: str
    evidence_ref: str
    confidence: float
    version: int
    state: MemoryState
    stale: bool
    label: str
    created_at: str
    expires_at: str | None


class MemoryRecall(BaseModel):
    """A retrieval slice, not a database dump."""

    items: list[MemoryView] = Field(default_factory=list)
    keys: list[str] = Field(default_factory=list)
    truncated: bool = False


class IntentRequest(BaseModel):
    """Channel-neutral natural-language intake for the v0.3 orchestration API."""

    objective: str = Field(min_length=5, max_length=20000)
    project: str = "lab"
    idempotency_key: str | None = Field(default=None, max_length=160)


class ClarificationRequest(BaseModel):
    answer: str = Field(min_length=1, max_length=10000)


class PlanApprovalRequest(BaseModel):
    plan_hash: str = Field(min_length=12, max_length=64)


class ImprovementRequest(BaseModel):
    brief: SelfImprovementBrief
    project: str = "lab"
    idempotency_key: str | None = Field(default=None, max_length=160)


class TaskRequest(BaseModel):
    requirement: str = Field(min_length=5, max_length=20000)
    project: str = "lab"
    idempotency_key: str | None = Field(default=None, max_length=160)
    kind: TaskKind = TaskKind.ENGINEERING
    brief: SelfImprovementBrief | None = None

    @model_validator(mode="after")
    def self_improvement_requires_a_brief(self):
        if self.kind == TaskKind.SELF_IMPROVEMENT and self.brief is None:
            raise ValueError("self_improvement tasks require a SelfImprovementBrief")
        if self.brief is not None and self.kind != TaskKind.SELF_IMPROVEMENT:
            raise ValueError("brief is only valid for self_improvement tasks")
        return self


class DecisionMessageType(StrEnum):
    UPDATE = "UPDATE"
    NEED_INFO = "NEED_INFO"
    DECISION_REQUIRED = "DECISION_REQUIRED"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    WARNING = "WARNING"
    BLOCKED = "BLOCKED"
    COMPLETED = "COMPLETED"
    LEARNING_PROPOSAL = "LEARNING_PROPOSAL"


class DecisionState(StrEnum):
    OPEN = "open"
    APPROVED = "approved"
    REJECTED = "rejected"
    NEEDS_INFO = "needs_info"
    DEFERRED = "deferred"
    EXPIRED = "expired"


class DecisionOutcome(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    ASK = "ask"
    DEFER = "defer"


# Natural language answers map to these exact outcomes; anything else is rejected.
DECISION_PHRASES = {
    "approve": DecisionOutcome.APPROVE,
    "approved": DecisionOutcome.APPROVE,
    "setujui": DecisionOutcome.APPROVE,
    "reject": DecisionOutcome.REJECT,
    "rejected": DecisionOutcome.REJECT,
    "tolak": DecisionOutcome.REJECT,
    "ask": DecisionOutcome.ASK,
    "ask_question": DecisionOutcome.ASK,
    "tanya": DecisionOutcome.ASK,
    "defer": DecisionOutcome.DEFER,
    "later": DecisionOutcome.DEFER,
    "nanti": DecisionOutcome.DEFER,
}

OUTCOME_STATE = {
    DecisionOutcome.APPROVE: DecisionState.APPROVED,
    DecisionOutcome.REJECT: DecisionState.REJECTED,
    DecisionOutcome.ASK: DecisionState.NEEDS_INFO,
    DecisionOutcome.DEFER: DecisionState.DEFERRED,
}


class Option(BaseModel):
    id: str = Field(min_length=1, max_length=40)
    label: str = Field(min_length=1, max_length=200)
    impact: str = Field(default="", max_length=2000)
    risk: str = Field(default="", max_length=2000)


class DecisionRequest(BaseModel):
    """A decision card. Every channel renders the same fields from the same row."""

    task_id: int | None = None
    project: str = Field(default="", max_length=40)
    kind: DecisionMessageType = DecisionMessageType.DECISION_REQUIRED
    title: str = Field(min_length=1, max_length=300)
    situation: str = Field(min_length=1, max_length=4000)
    why_now: str = Field(min_length=1, max_length=2000)
    options: list[Option] = Field(min_length=1)
    recommendation: str = Field(min_length=1, max_length=2000)
    evidence: list[str] = Field(min_length=1)
    missing_information: str = Field(default="", max_length=2000)
    rollback: str = Field(default="", max_length=2000)
    required_action: str = Field(default="", max_length=80)
    priority: Literal["low", "normal", "high", "critical"] = "normal"
    risk_level: Literal["low", "medium", "high"] = "medium"
    expires_at: datetime | None = None

    @model_validator(mode="after")
    def recommendation_must_be_an_option(self):
        if self.recommendation not in {o.label for o in self.options} and self.recommendation not in {
            o.id for o in self.options
        }:
            raise ValueError("recommendation must reference one of the offered options")
        return self
