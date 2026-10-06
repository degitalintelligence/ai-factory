import pytest

from app.gates import deployment_issues, post_publication_issues, quality_issues, removed_python_tests
from app.schemas import CommandResult, CriterionEvidence, LeadPlan, ReviewResult
from app.schemas import TestReport as Report

DIGEST = "d" * 64
SHA = "a" * 40


def pr_body(**overrides):
    body = (
        "## Requirement\nShip it\n\n## Plan\n```json\n{}\n```\n\n"
        "## Independent review\n```json\n{}\n```\n\n## Test evidence\n```json\n{}\n```\n\n"
        f"Reviewed source digest: `{DIGEST}`\n\nCommit: `{SHA}`\n"
    )
    body += overrides.pop("extra", "")
    return {"state": "open", "body": body, "head": {"sha": SHA}, **overrides}


def test_post_publication_gate_passes_for_complete_evidence():
    assert post_publication_issues(pr_body(), digest=DIGEST, sha=SHA, deferred=[2]) == [
        "Published PR body does not state post-publication criterion #2"
    ]


def test_post_publication_gate_passes_when_deferred_criterion_is_stated():
    pr = pr_body(extra="\nPost-publication criteria verified by this PR: #2\n")
    assert post_publication_issues(pr, digest=DIGEST, sha=SHA, deferred=[2]) == []


def test_post_publication_gate_detects_missing_sections():
    issues = post_publication_issues(
        {"state": "open", "body": "just a title", "head": {"sha": SHA}}, digest=DIGEST, sha=SHA, deferred=[]
    )
    assert any("## Requirement" in x for x in issues)
    assert any("does not record the reviewed source digest" in x for x in issues)


def test_post_publication_gate_detects_tampered_digest_or_commit():
    issues = post_publication_issues(pr_body(), digest="e" * 64, sha=SHA, deferred=[])
    assert any("does not record the reviewed source digest" in x for x in issues)
    issues = post_publication_issues(pr_body(), digest=DIGEST, sha="b" * 40, deferred=[])
    assert any("does not record the approved commit" in x for x in issues)


def test_post_publication_gate_detects_head_mismatch_and_closed_pr():
    issues = post_publication_issues(pr_body(head={"sha": "c" * 40}), digest=DIGEST, sha=SHA, deferred=[])
    assert any("head does not match" in x for x in issues)
    issues = post_publication_issues(pr_body(state="closed"), digest=DIGEST, sha=SHA, deferred=[])
    assert any("not open" in x for x in issues)


def test_post_publication_gate_reports_unreadable_pr():
    assert post_publication_issues(None, digest=DIGEST, sha=SHA, deferred=[]) == [
        "Published PR could not be read back from GitHub"
    ]


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


@pytest.mark.parametrize(
    ("dockerfile", "final_stage_is_safe"),
    [
        ("FROM python:3.12-slim\nUSER 1000\n", True),
        ("FROM python:3.12-slim\nUSER root\nUSER 1000\n", True),
        ("FROM python:3.12-slim\nUSER 1000\nUSER root\n", False),
        ("FROM python:3.12-slim\nUSER 0:0\n", False),
        ("FROM python:3.12-slim\nUSER root\n", False),
        ("FROM python:3.12-slim\n", False),
        ("FROM python:3.12-slim\n# USER root\nUSER 1001\n", True),
    ],
)
def test_dockerfile_user_gate_judges_the_final_stage_directive(dockerfile, final_stage_is_safe):
    """Only the final USER directive decides the gate; earlier stages must not satisfy it."""
    files = {
        "Dockerfile": dockerfile,
        ".dockerignore": ".env\n",
        ".env.example": "TOKEN=\n",
        "docs/DEPLOYMENT.md": "Environment, health, backup, rollback",
        "compose.yaml": (
            "services:\n  bot:\n    image: example\n    restart: unless-stopped\n"
            "    healthcheck: {test: [CMD, 'true']}\n    volumes: [data:/data]\nvolumes:\n  data:\n"
        ),
    }
    issues = deployment_issues(files, persistence=True)
    assert (issues == []) is final_stage_is_safe
    if not final_stage_is_safe:
        assert any("Dockerfile" in x for x in issues)


def test_new_tests_must_pass_standalone():
    plan = LeadPlan(objective="Feature", acceptance_criteria=["Works"])
    review = ReviewResult(
        approved=True,
        summary="Fine",
        criteria=[CriterionEvidence(criterion=1, satisfied=True, evidence="test_feature")],
    )
    passed = Report(results=[CommandResult(command=["pytest"], exit_code=0)])
    failed = Report(results=[CommandResult(command=["pytest"], exit_code=1)])
    assert any("standalone" in x for x in quality_issues(plan, passed, review, "diff", [], failed))
    assert quality_issues(plan, passed, review, "diff", [], passed) == []


def test_existing_python_tests_cannot_be_silently_replaced():
    diff = """diff --git a/tests/test_todo.py b/tests/test_todo.py
--- a/tests/test_todo.py
+++ b/tests/test_todo.py
-def test_todo_adds_description():
-    pass
-def test_todo_data_persists_after_store_restart(tmp_path):
-    pass
+def test_todo_count_empty():
+    pass
"""
    assert removed_python_tests(diff) == [
        "test_todo_adds_description",
        "test_todo_data_persists_after_store_restart",
    ]
    plan = LeadPlan(objective="Feature", acceptance_criteria=["Works"])
    report = Report(results=[CommandResult(command=["pytest"], exit_code=0)])
    review = ReviewResult(
        approved=True,
        summary="Approved",
        criteria=[CriterionEvidence(criterion=1, satisfied=True, evidence="new test")],
    )
    issues = quality_issues(plan, report, review, diff, [])
    assert any("Existing Python tests were removed" in issue for issue in issues)


def test_moved_python_test_definition_is_not_treated_as_deleted():
    diff = """-def test_existing_behavior():
+def test_existing_behavior():
"""
    assert removed_python_tests(diff) == []


def test_post_publication_criterion_is_not_blocking_before_pr_exists():
    plan = LeadPlan(
        objective="LioBot",
        acceptance_criteria=["Bot replies", "PR body documents behavior"],
        post_publication_criteria=[2],
    )
    review = ReviewResult(
        approved=True,
        summary="Code is ready; PR body cannot exist yet",
        criteria=[
            CriterionEvidence(criterion=1, satisfied=True, evidence="tests/test_bot.py"),
            CriterionEvidence(criterion=2, satisfied=False, evidence="Pending publication"),
        ],
    )
    report = Report(results=[CommandResult(command=["pytest"], exit_code=0)])
    assert quality_issues(plan, report, review, "diff", []) == []


def test_pre_publication_criterion_still_blocks_when_unsatisfied():
    plan = LeadPlan(objective="LioBot", acceptance_criteria=["A", "B"], post_publication_criteria=[2])
    review = ReviewResult(
        approved=True,
        summary="Incomplete",
        criteria=[
            CriterionEvidence(criterion=1, satisfied=False, evidence="Missing"),
            CriterionEvidence(criterion=2, satisfied=False, evidence="Pending"),
        ],
    )
    report = Report(results=[CommandResult(command=["pytest"], exit_code=0)])
    assert any("criterion 1 not satisfied" in x for x in quality_issues(plan, report, review, "diff", []))


def test_missing_evidence_for_required_criterion_still_blocks():
    plan = LeadPlan(objective="LioBot", acceptance_criteria=["A", "B"], post_publication_criteria=[2])
    review = ReviewResult(
        approved=True,
        summary="No evidence for A",
        criteria=[CriterionEvidence(criterion=2, satisfied=False, evidence="Pending")],
    )
    report = Report(results=[CommandResult(command=["pytest"], exit_code=0)])
    assert any("exactly once" in x for x in quality_issues(plan, report, review, "diff", []))


def test_post_publication_criterion_may_be_omitted_entirely():
    plan = LeadPlan(
        objective="LioBot",
        acceptance_criteria=["Bot replies", "PR body documents behavior"],
        post_publication_criteria=[2],
    )
    review = ReviewResult(
        approved=True,
        summary="Only the pre-publication criterion is judged",
        criteria=[CriterionEvidence(criterion=1, satisfied=True, evidence="tests/test_bot.py")],
    )
    report = Report(results=[CommandResult(command=["pytest"], exit_code=0)])
    assert quality_issues(plan, report, review, "diff", []) == []


def test_duplicate_evidence_for_required_criterion_blocks():
    plan = LeadPlan(objective="X", acceptance_criteria=["A"])
    review = ReviewResult(
        approved=True,
        summary="Duplicate",
        criteria=[
            CriterionEvidence(criterion=1, satisfied=True, evidence="a"),
            CriterionEvidence(criterion=1, satisfied=True, evidence="b"),
        ],
    )
    report = Report(results=[CommandResult(command=["pytest"], exit_code=0)])
    assert any("exactly once" in x for x in quality_issues(plan, report, review, "diff", []))


def test_plan_rejects_out_of_range_post_publication_criterion():
    with pytest.raises(ValueError):
        LeadPlan(objective="X", acceptance_criteria=["A"], post_publication_criteria=[2])
