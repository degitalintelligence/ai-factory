from typing import Literal
from pydantic import BaseModel, Field

class LeadPlan(BaseModel):
    objective: str
    acceptance_criteria: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    suggested_tests: list[str] = Field(default_factory=list)
    risk: Literal["low", "medium", "high"] = "low"

class DeveloperAction(BaseModel):
    action: Literal["list_files", "read_file", "write_file", "run_command", "git_diff", "finish"]
    path: str | None = None
    content: str | None = None
    command: str | None = None
    note: str | None = None

class ReviewResult(BaseModel):
    approved: bool
    summary: str
    issues: list[str] = Field(default_factory=list)
