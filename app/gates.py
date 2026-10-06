import re

import yaml

from app.schemas import LeadPlan, ReviewResult, TestReport

# Sections the PR body must actually carry for post-publication criteria to be verifiable by a human.
REQUIRED_PR_SECTIONS = (
    "## Requirement",
    "## Plan",
    "## Independent review",
    "## Test evidence",
    "Reviewed source digest:",
    "Commit:",
)


def post_publication_issues(pr, *, digest, sha, deferred):
    """Verify the published PR against evidence only obtainable after publication."""
    issues = []
    if not pr:
        return ["Published PR could not be read back from GitHub"]
    body = pr.get("body") or ""
    for section in REQUIRED_PR_SECTIONS:
        if section not in body:
            issues.append(f"Published PR body is missing required section: {section}")
    if digest not in body:
        issues.append("Published PR body does not record the reviewed source digest")
    if sha not in body:
        issues.append("Published PR body does not record the approved commit")
    head = (pr.get("head") or {}).get("sha")
    if head and head != sha:
        issues.append("Published PR head does not match the approved commit")
    if pr.get("state") != "open":
        issues.append(f"Published PR is not open (state={pr.get('state')})")
    for n in sorted(deferred):
        if f"#{n}" not in body:
            issues.append(f"Published PR body does not state post-publication criterion #{n}")
    return issues


def deployment_issues(files, persistence=False):
    """Static deployment-file gate only: structure and policy, never runtime verification.

    A clean result proves the declared deployment deliverables exist and the Compose
    file follows the required shape. It proves nothing about a real image build, boot,
    or health check; deployment evidence comes only from an explicit /deploy, the
    /deployment reconciliation, and the project's own post-deployment smoke tests.
    """
    issues = []
    required = ["Dockerfile", ".dockerignore", ".env.example", "docs/DEPLOYMENT.md"]
    issues.extend(f"Deployment deliverable missing: {p}" for p in required if p not in files)
    compose_path = next(
        (
            p
            for p in ("docker-compose.yaml", "docker-compose.yml", "compose.yaml", "compose.yml")
            if p in files
        ),
        None,
    )
    if not compose_path:
        return issues + ["Deployment requires a Compose file"]
    try:
        data = yaml.safe_load(files[compose_path])
        services = data.get("services", {})
        if not isinstance(services, dict) or not services:
            raise ValueError("services must be a nonempty mapping")
        for name, service in services.items():
            if not isinstance(service, dict):
                raise ValueError("service must be a mapping")
            if (
                service.get("privileged")
                or service.get("network_mode") == "host"
                or service.get("pid") == "host"
                or service.get("cap_add")
            ):
                issues.append(f"Unsafe privileges in service {name}")
            if not service.get("healthcheck") or service["healthcheck"].get("disable"):
                issues.append(f"Service {name} needs an enabled healthcheck")
            if not service.get("restart"):
                issues.append(f"Service {name} needs a restart policy")
            for volume in service.get("volumes", []):
                source = volume.get("source", "") if isinstance(volume, dict) else str(volume).split(":")[0]
                if "docker.sock" in str(volume) or source.startswith(("/", "~", "..")):
                    issues.append(f"Unsafe host mount in service {name}")
        if persistence:
            declared = set(data.get("volumes") or {})
            mounts = [
                v.get("source", "") if isinstance(v, dict) else str(v).split(":")[0]
                for s in services.values()
                for v in s.get("volumes", [])
            ]
            if not any(x in declared for x in mounts):
                issues.append("Persistent application data requires a declared and mounted named volume")
        doc = files.get("docs/DEPLOYMENT.md", "").lower()
        for term in ("backup", "rollback", "health", "environment"):
            if term not in doc:
                issues.append(f"Deployment runbook must cover {term}")
    except (ValueError, TypeError, AttributeError, yaml.YAMLError):
        issues.append("Compose file failed structural validation")
    dockerfile = files.get("Dockerfile", "")
    users = re.findall(r"(?im)^USER\s+(\S+)", dockerfile)
    if dockerfile and (not users or _root_account(users[-1])):
        issues.append("Application Dockerfile must declare a non-root USER in its final stage")
    return issues


def _root_account(user: str) -> bool:
    """A USER directive may carry a group suffix; only the account decides the gate."""
    return user.split(":", 1)[0].lower() in {"root", "0"}


def removed_python_tests(diff: str) -> list[str]:
    """Return existing Python test functions deleted without an equivalent addition."""
    removed = set()
    added = set()
    pattern = re.compile(r"^(?:async\s+)?def\s+(test_[A-Za-z0-9_]+)\s*\(")
    for line in diff.splitlines():
        if line.startswith(("---", "+++")) or len(line) < 2:
            continue
        match = pattern.match(line[1:])
        if not match:
            continue
        if line[0] == "-":
            removed.add(match.group(1))
        elif line[0] == "+":
            added.add(match.group(1))
    return sorted(removed - added)


def quality_issues(
    plan: LeadPlan,
    report: TestReport,
    review: ReviewResult,
    diff: str,
    hygiene: list[str],
    standalone: TestReport | None = None,
    allow_no_diff: bool = False,
):
    issues = list(hygiene)
    if not diff.strip() and not allow_no_diff:
        issues.append("No implementation diff")
    if not report.passed:
        issues.append("Mandatory test gate failed (including missing tests, timeout, or test artifacts)")
        issues.extend(report.issues)
    if standalone is not None and not standalone.passed:
        issues.append("New or changed tests must pass when run standalone")
        issues.extend(standalone.issues)
    removed_tests = removed_python_tests(diff)
    if removed_tests:
        issues.append(
            "Existing Python tests were removed without equivalent definitions: " + ", ".join(removed_tests)
        )
    if review.approved and review.issues:
        issues.append("Reviewer cannot approve while reporting unresolved issues")
    # Criteria that depend on the published PR are verified after publication, not before it.
    required = set(range(1, len(plan.acceptance_criteria) + 1)) - set(plan.post_publication_criteria)
    by_id = {}
    duplicates = set()
    for x in review.criteria:
        if x.criterion in by_id:
            duplicates.add(x.criterion)
        by_id[x.criterion] = x
    # Deferred criteria may be omitted entirely; required criteria must each appear exactly once.
    if not required <= set(by_id) or duplicates & required:
        issues.append("Reviewer must provide evidence for every acceptance criterion exactly once")
    for n in sorted(required):
        if n in by_id and not by_id[n].satisfied:
            issues.append(f"Acceptance criterion {n} not satisfied")
    return list(dict.fromkeys(issues))
