import re

import yaml

from app.schemas import LeadPlan, ReviewResult, TestReport


def deployment_issues(files, persistence=False):
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
            declared = set(data.get("volumes", {}))
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
    if dockerfile and not re.search(r"(?im)^USER\s+(?!root\b|0\b)\S+", dockerfile):
        issues.append("Application Dockerfile must declare a non-root USER")
    return issues


def quality_issues(plan: LeadPlan, report: TestReport, review: ReviewResult, diff: str, hygiene: list[str]):
    issues = list(hygiene)
    if not diff.strip():
        issues.append("No implementation diff")
    if not report.passed:
        issues.append("Mandatory test gate failed (including missing tests, timeout, or test artifacts)")
        issues.extend(report.issues)
    if review.approved and review.issues:
        issues.append("Reviewer cannot approve while reporting unresolved issues")
    by_id = {x.criterion: x for x in review.criteria}
    if len(review.criteria) != len(plan.acceptance_criteria) or set(by_id) != set(
        range(1, len(plan.acceptance_criteria) + 1)
    ):
        issues.append("Reviewer must provide evidence for every acceptance criterion exactly once")
    for n in range(1, len(plan.acceptance_criteria) + 1):
        if n in by_id and not by_id[n].satisfied:
            issues.append(f"Acceptance criterion {n} not satisfied")
    return list(dict.fromkeys(issues))
