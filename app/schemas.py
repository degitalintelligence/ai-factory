from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class LeadPlan(BaseModel):
    objective: str = Field(min_length=1)
    acceptance_criteria: list[str] = Field(min_length=1)
    constraints: list[str] = Field(default_factory=list)
    suggested_tests: list[str] = Field(default_factory=list)
    steps: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)
    risk: Literal["low", "medium", "high"] = "low"
    deployment_required: bool = False
    persistence_required: bool = False


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

    @property
    def passed(self):
        return (
            bool(self.results)
            and not self.issues
            and all(r.exit_code == 0 and not r.timed_out for r in self.results)
        )


class TaskRequest(BaseModel):
    requirement: str = Field(min_length=5, max_length=20000)
    project: str = "lab"
    idempotency_key: str | None = Field(default=None, max_length=160)
