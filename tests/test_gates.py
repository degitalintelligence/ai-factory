import pytest

from app.gates import deployment_issues, quality_issues
from app.schemas import CommandResult, CriterionEvidence, LeadPlan, ReviewResult
from app.schemas import TestReport as Report


@pytest.mark.parametrize("exit_code", [1, 2, 5, 124])
def test_model_cannot_override_failed_or_missing_tests(exit_code):
    plan = LeadPlan(objective="Feature", acceptance_criteria=["Works"])
    review = ReviewResult(
        approved=True,
        summary="Fine",
        criteria=[CriterionEvidence(criterion=1, satisfied=True, evidence="test_feature")],
    )
    report = Report(results=[CommandResult(command=["pytest"], exit_code=exit_code)])
    assert any("test gate failed" in x for x in quality_issues(plan, report, review, "diff", []))


def test_every_criterion_requires_unique_evidence():
    plan = LeadPlan(objective="Feature", acceptance_criteria=["A", "B"])
    report = Report(results=[CommandResult(command=["pytest"], exit_code=0)])
    review = ReviewResult(
        approved=True,
        summary="Fine",
        criteria=[
            CriterionEvidence(criterion=1, satisfied=True, evidence="A"),
            CriterionEvidence(criterion=1, satisfied=True, evidence="A"),
        ],
    )
    assert quality_issues(plan, report, review, "diff", [])


def test_deployment_gate_rejects_unsafe_missing_and_unpersisted_state():
    files = {
        "compose.yaml": "services:\n  bot:\n    image: example\n    privileged: true\n    volumes: [/var/run/docker.sock:/var/run/docker.sock]\n"
    }
    issues = deployment_issues(files, persistence=True)
    assert any("Dockerfile" in x for x in issues)
    assert any("Unsafe privileges" in x for x in issues)
    assert any("healthcheck" in x for x in issues)
    assert any("named volume" in x for x in issues)


def test_complete_deployment_pack_passes_static_gate():
    files = {
        "Dockerfile": "FROM python:3.12-slim\nUSER 1000\n",
        ".dockerignore": ".env\n",
        ".env.example": "TOKEN=\n",
        "docs/DEPLOYMENT.md": "Environment, health, backup, rollback",
        "compose.yaml": "services:\n  bot:\n    image: example\n    restart: unless-stopped\n    healthcheck: {test: [CMD, 'true']}\n    volumes: [data:/data]\nvolumes:\n  data:\n",
    }
    assert deployment_issues(files, persistence=True) == []
