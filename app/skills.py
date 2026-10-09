"""Declarative capability registry for LioBot's orchestration layer.

A skill is a permissioned capability contract, not an unbounded prompt. The
registry is intentionally small: Engineering is executable in M1, while the
other domains are draft-only until they have a connector and evaluator.
"""

from pydantic import BaseModel, ConfigDict, Field


class SkillDefinition(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    version: str = Field(min_length=1, max_length=32)
    domain: str = Field(min_length=1, max_length=80)
    purpose: str = Field(min_length=1, max_length=1000)
    inputs: list[str] = Field(min_length=1)
    outputs: list[str] = Field(min_length=1)
    allowed_tools: list[str] = Field(default_factory=list)
    data_scope: str = Field(min_length=1, max_length=300)
    risk_level: str = Field(pattern=r"^(low|medium|high)$")
    approval_policy: str = Field(pattern=r"^(none|policy|always)$")
    evaluator: str = Field(min_length=1, max_length=500)
    rollback: str = Field(min_length=1, max_length=500)
    owner: str = Field(min_length=1, max_length=120)
    model_policy: str = "operator-configured audited model or alias; no silent fallback"
    budget_policy: str = "subtask, task and daily operator ceilings; retry never resets usage"
    input_schema: str = "ContextItem[] and WorkItem"
    output_schema: str = "StaffOutput or bounded L0 FactualOutput; engineering also uses DeveloperAction/TestReport/ReviewResult"
    execution_state: str = Field(pattern=r"^(verified|draft_only|disabled)$")


class SkillRegistry:
    def __init__(self, skills: list[SkillDefinition]):
        self._skills = {skill.id: skill for skill in skills}

    def ids(self) -> tuple[str, ...]:
        return tuple(self._skills)

    def get(self, skill_id: str) -> SkillDefinition:
        try:
            return self._skills[skill_id]
        except KeyError as exc:
            raise ValueError(f"Unknown skill: {skill_id}") from exc

    def all(self) -> list[SkillDefinition]:
        return list(self._skills.values())

    def prompt_view(self) -> list[dict]:
        """Return only stable contract fields that are safe for a model prompt."""
        return [
            {
                "id": skill.id,
                "version": skill.version,
                "domain": skill.domain,
                "purpose": skill.purpose,
                "inputs": skill.inputs,
                "outputs": skill.outputs,
                "allowed_tools": skill.allowed_tools,
                "risk_level": skill.risk_level,
                "approval_policy": skill.approval_policy,
                "execution_state": skill.execution_state,
            }
            for skill in self._skills.values()
        ]


SKILLS = SkillRegistry(
    [
        SkillDefinition(
            id="engineering",
            version="1.0.0",
            domain="Engineering",
            purpose="Repository-aware implementation, testing, review, release preparation, and deployment evidence.",
            inputs=["objective", "repository_context", "plan", "feedback"],
            outputs=["diff", "tests", "review_evidence", "release_candidate"],
            allowed_tools=["repository.read", "repository.write", "sandbox.test", "github.pr"],
            data_scope="registered project workspace and policy snapshot",
            risk_level="medium",
            approval_policy="policy",
            evaluator="deterministic gates plus independent review",
            rollback="revert the reviewed commit or close the unmerged PR",
            owner="LioBot Core",
            execution_state="verified",
        ),
        SkillDefinition(
            id="product_research",
            version="0.1.0",
            domain="Product/Research",
            purpose="Clarify requirements, compare options, prioritize outcomes, and design experiments.",
            inputs=["objective", "business_context", "evidence"],
            outputs=["requirements", "options", "priorities", "experiment_plan"],
            allowed_tools=["memory.read", "approved.research.read"],
            data_scope="approved project and strategy context",
            risk_level="low",
            approval_policy="none",
            evaluator="criteria coverage and evidence references",
            rollback="discard the draft recommendation",
            owner="LioBot Core",
            execution_state="draft_only",
        ),
        SkillDefinition(
            id="marketing",
            version="0.1.0",
            domain="Marketing",
            purpose="Prepare campaign, content, funnel, and competitor-monitoring recommendations.",
            inputs=["objective", "brand_context", "market_evidence"],
            outputs=["campaign_draft", "content_plan", "funnel_hypothesis", "measurement_plan"],
            allowed_tools=["memory.read", "approved.research.read"],
            data_scope="approved brand and market context",
            risk_level="medium",
            approval_policy="always",
            evaluator="hypothesis, audience, KPI, and experiment evidence",
            rollback="withdraw the draft before external publication",
            owner="LioBot Core",
            execution_state="draft_only",
        ),
        SkillDefinition(
            id="admin_ops",
            version="0.1.0",
            domain="Admin/Ops",
            purpose="Turn operating goals into SOPs, checklists, schedules, vendor follow-ups, and escalations.",
            inputs=["objective", "operational_context", "constraints"],
            outputs=["sop_draft", "checklist", "schedule", "follow_up_queue"],
            allowed_tools=["memory.read"],
            data_scope="approved operational context",
            risk_level="medium",
            approval_policy="policy",
            evaluator="checklist completeness and owner/deadline coverage",
            rollback="disable the generated schedule or checklist",
            owner="LioBot Core",
            execution_state="draft_only",
        ),
        SkillDefinition(
            id="finance",
            version="0.1.0",
            domain="Finance",
            purpose="Prepare cashflow, reporting, invoice, and anomaly-analysis drafts without moving money.",
            inputs=["objective", "financial_context", "evidence"],
            outputs=["report", "cashflow_draft", "anomaly_list", "decision_options"],
            allowed_tools=["memory.read", "approved.data.read"],
            data_scope="approved financial data only",
            risk_level="high",
            approval_policy="always",
            evaluator="reconciliation, source references, and variance evidence",
            rollback="discard the draft; no transaction is executed",
            owner="LioBot Core",
            execution_state="draft_only",
        ),
        SkillDefinition(
            id="sales_cs",
            version="0.1.0",
            domain="Sales/Customer Success",
            purpose="Prepare lead qualification, follow-up, response, and escalation recommendations.",
            inputs=["objective", "customer_context", "conversation_evidence"],
            outputs=["lead_queue", "reply_draft", "follow_up_plan", "escalation"],
            allowed_tools=["memory.read", "approved.conversation.read"],
            data_scope="approved customer and conversation scope",
            risk_level="high",
            approval_policy="always",
            evaluator="response policy, evidence, and escalation coverage",
            rollback="discard draft; external message remains unsent",
            owner="LioBot Core",
            execution_state="draft_only",
        ),
    ]
)


def select_skills(objective: str) -> list[str]:
    """Choose a conservative proposal set; execution still obeys each skill policy."""
    from app.audit_scope import readonly_repository_audit

    if readonly_repository_audit(objective):
        return ["engineering", "product_research"]
    text = objective.lower()
    selected: list[str] = []
    keyword_groups = (
        (
            "engineering",
            ("code", "coding", "repository", "repo", "test", "deploy", "api", "bug", "implement"),
        ),
        ("product_research", ("requirement", "product", "research", "prioritize", "experiment", "strategy")),
        ("marketing", ("marketing", "campaign", "content", "funnel", "competitor", "ads", "seo")),
        ("admin_ops", ("admin", "ops", "sop", "schedule", "vendor", "checklist", "operational")),
        ("finance", ("finance", "cashflow", "invoice", "budget", "reconcile", "anomaly")),
        ("sales_cs", ("sales", "lead", "customer", "support", "follow-up", "cs", "client")),
    )
    for skill_id, keywords in keyword_groups:
        if any(keyword in text for keyword in keywords):
            selected.append(skill_id)
    return selected or ["product_research"]
