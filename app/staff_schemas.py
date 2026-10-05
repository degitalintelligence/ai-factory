"""Strict boundaries for L0/L1 orchestration. These contracts grant no external authority."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.schemas import PlanBudget


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ChatRequest(StrictModel):
    message: str = Field(min_length=5, max_length=20000)
    project: str = Field(default="", max_length=40)
    idempotency_key: str = Field(min_length=1, max_length=120)


class ResolvedIntent(StrictModel):
    execution: Literal["analysis", "engineering"] = "analysis"
    objective: str = Field(min_length=1, max_length=4000)
    desired_outcome: str = Field(min_length=1, max_length=2000)
    scope: list[str] = Field(default_factory=list, max_length=20)
    constraints: list[str] = Field(default_factory=list, max_length=20)
    urgency: Literal["normal", "urgent"] = "normal"
    risk_level: Literal["low", "medium", "high"] = "low"
    requested_authority: Literal["L0", "L1", "L2", "L3"] = "L0"
    missing_information: list[str] = Field(
        default_factory=list,
        max_length=3,
        description="Only questions without which the requested objective, target, or authority cannot be defined safely. Missing audit evidence alone is not a blocking question.",
    )
    evidence_gaps: list[str] = Field(
        default_factory=list,
        max_length=10,
        description="Unavailable proof or diagnostic data; proceed with bounded analysis and report these as unverified.",
    )
    assumptions: list[str] = Field(default_factory=list, max_length=20)


class WorkItem(StrictModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,39}$")
    skill: Literal["engineering", "product_research", "marketing", "admin_ops", "finance", "sales_cs"]
    objective: str = Field(min_length=1, max_length=2000)
    dependencies: list[str] = Field(default_factory=list, max_length=8)
    max_llm_calls: int = Field(default=3, ge=1, le=5)


class StaffPlan(StrictModel):
    objective: str = Field(min_length=1, max_length=4000)
    success_criteria: list[str] = Field(min_length=1, max_length=10)
    assumptions: list[str] = Field(default_factory=list, max_length=20)
    steps: list[WorkItem] = Field(min_length=1, max_length=6)
    dependencies: list[str] = Field(default_factory=list, max_length=20)
    risks: list[str] = Field(min_length=1, max_length=10)
    risk: Literal["low", "medium", "high"] = "low"
    approval_gates: list[str] = Field(min_length=1, max_length=10)
    rollback_plan: str = Field(min_length=1, max_length=2000)
    budget: PlanBudget = Field(default_factory=lambda: PlanBudget(max_llm_calls=24))
    deadline: datetime | None = None

    @model_validator(mode="after")
    def acyclic(self):
        ids = {item.id for item in self.steps}
        if len(ids) != len(self.steps):
            raise ValueError("Duplicate work item IDs")
        done = set()
        remaining = list(self.steps)
        while remaining:
            ready = [item for item in remaining if set(item.dependencies) <= done]
            if not ready:
                raise ValueError("Dependencies contain a cycle or unknown work item")
            for item in ready:
                done.add(item.id)
                remaining.remove(item)
        return self


class ContextItem(StrictModel):
    ref: str
    source: str
    scope: str
    owner: int | None
    created_at: str
    confidence: float = Field(ge=0, le=1)
    label: Literal["current", "stale", "unverified", "conflict"] = "current"
    content: str = Field(max_length=4000)


class Finding(StrictModel):
    title: str = Field(min_length=1, max_length=300)
    situation: str = Field(min_length=1, max_length=2000)
    priority: Literal["low", "normal", "high", "critical"]
    why_now: str = Field(min_length=1, max_length=1000)
    recommendation: str = Field(min_length=1, max_length=2000)
    alternative: str = Field(min_length=1, max_length=2000)
    risk: str = Field(min_length=1, max_length=1000)
    evidence_refs: list[str] = Field(min_length=1, max_length=10)
    confidence: float = Field(ge=0, le=1)
    decision_required: bool = True


class AuditCheck(StrictModel):
    topic: Literal["models", "workflow", "budget", "readiness", "tests"]
    verification: Literal["repository", "runtime", "ci", "unverified"]
    observation: str = Field(min_length=1, max_length=400)
    observed_values: dict[
        Annotated[str, StringConstraints(max_length=80)], Annotated[str, StringConstraints(max_length=256)]
    ] = Field(default_factory=dict, max_length=8)
    evidence_refs: list[str] = Field(default_factory=list, max_length=4)
    limitation: str = Field(default="", max_length=300)


class StaffOutput(StrictModel):
    summary: str = Field(min_length=1, max_length=4000)
    findings: list[Finding] = Field(default_factory=list, max_length=6)
    audit_checks: list[AuditCheck] = Field(default_factory=list, max_length=5)
    missing_information: list[str] = Field(default_factory=list, max_length=10)
    next_action: str = Field(min_length=1, max_length=2000)


class CompactFinding(Finding):
    """Same decision fields, bounded prose for provider generation."""

    title: str = Field(min_length=1, max_length=120)
    situation: str = Field(min_length=1, max_length=400)
    why_now: str = Field(min_length=1, max_length=180)
    recommendation: str = Field(min_length=1, max_length=350)
    alternative: str = Field(min_length=1, max_length=250)
    risk: str = Field(min_length=1, max_length=200)
    evidence_refs: list[Annotated[str, StringConstraints(min_length=1, max_length=240)]] = Field(
        min_length=1,
        max_length=4,
        description="Select 1-4 exact evidence references supporting this finding; never copy the entire context reference list.",
    )


class CompactStaffOutput(StaffOutput):
    """Generation contract; legacy persisted StaffOutput remains readable."""

    summary: str = Field(min_length=1, max_length=700)
    findings: list[CompactFinding] = Field(default_factory=list, max_length=6)
    missing_information: list[Annotated[str, StringConstraints(min_length=1, max_length=200)]] = Field(
        default_factory=list, max_length=10
    )
    next_action: str = Field(min_length=1, max_length=300)

    @model_validator(mode="after")
    def bounded_json(self):
        if len(self.model_dump_json()) > 7000:
            raise ValueError("Staff output JSON must fit 7000 characters; shorten prose, preserve evidence")
        return self


class FactualOutput(StrictModel):
    """A read-only explanation, without invented operator decisions."""

    summary: str = Field(min_length=1, max_length=2000)
    evidence_refs: list[str] = Field(min_length=1, max_length=4)
    confidence: float = Field(ge=0, le=1)
    missing_information: list[str] = Field(default_factory=list, max_length=3)

    @property
    def findings(self):
        return []


class OutputEvaluation(StrictModel):
    approved: bool = Field(
        description="Whether the submitted answer satisfies its objective and evidence constraints."
    )
    issues: list[str] = Field(
        default_factory=list,
        max_length=10,
        description="Defects in the submitted answer requiring correction, not the problems discovered by an audit. Each issue identifies the defective claim, reason and needed correction. Empty when approved.",
    )
    summary: str = Field(
        min_length=1,
        max_length=2000,
        description="Short verdict about answer quality, not a new audit or rewritten answer.",
    )


class DecisionAction(StrictModel):
    answer: str = Field(min_length=1, max_length=200)
    reason: str = Field(default="", max_length=2000)
    delegate_to: int | None = Field(default=None, ge=1)


class DeploymentReconciliation(StrictModel):
    approved_sha: str = Field(pattern=r"^[a-f0-9]{40}$")
    deployment_uuid: str = Field(default="", pattern=r"^[A-Za-z0-9_-]{0,200}$")
    no_submission_confirmed: bool = False
    evidence: str = Field(min_length=10, max_length=2000)

    @model_validator(mode="after")
    def exactly_one_resolution(self):
        if bool(self.deployment_uuid) == self.no_submission_confirmed:
            raise ValueError("Attach a verified deployment UUID OR confirm no submission")
        return self


class RollbackConfirmation(StrictModel):
    deployment_uuid: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,100}$")
    revision: str = Field(pattern=r"^[a-f0-9]{40}$")
    evidence: list[str] = Field(min_length=1, max_length=10)
    compatibility_checked: bool
