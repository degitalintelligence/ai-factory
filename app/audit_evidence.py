"""Pinned, bounded repository audit sources and deterministic claim coverage.

Repository source, live control-process observations and GitHub CI are separate
kinds of evidence. Reading source never executes target code or proves readiness.
"""

import ast
import hashlib
import json
import re

import httpx

from app.audit_scope import SELF_REPOSITORY
from app.config import settings
from app.db import DailyBudget, utcnow
from app.security import redact
from app.staff_schemas import ContextItem, StaffOutput
from app.store import store

TOPICS = {
    "models": r"\bmodels?\b|konfigurasi model",
    "workflow": r"evidence-audit|\bworkflow\b|orchestration|orkestrasi",
    "budget": r"\bbudget\b|anggaran",
    "readiness": r"readiness|/ready|kesiapan",
    "tests": r"\btests?\b|pytest|pengujian",
}


def requested_checks(requirement: str) -> list[str]:
    return [key for key, pattern in TOPICS.items() if re.search(pattern, requirement, re.I)] or ["workflow"]


def audit_item(task, name, source, data) -> ContextItem:
    content = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    if not isinstance(data, str) and len(content) > 3900:
        content = json.dumps(
            {
                "status": "unavailable",
                "scope": "Observation exceeded bounded metadata limit; no verification claimed",
            }
        )
    return ContextItem(
        ref=f"audit:{task.project}:{task.base_sha}:{name}",
        source=source,
        scope=task.project,
        owner=task.user_id,
        created_at=utcnow().isoformat(),
        confidence=1,
        content=redact(content)[:4000],
    )


def source_excerpt(path: str, text: str, topics: list[str]) -> str:
    """Line-numbered source slices, not the first N characters of a large module."""
    lines = text.splitlines()
    spans = []
    symbols = {
        "app/config.py": {"configured_model", "model_for", "model_aliases"},
        "app/main.py": {"ready"},
        "app/audit_scope.py": {"readonly_repository_audit", "repository_audit"},
        "app/staff.py": {"evidence_audit_request"},
    }.get(path, set())
    if path.endswith(".py"):
        try:
            tree = ast.parse(text)
            spans += [
                (node.lineno, node.end_lineno)
                for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in symbols
            ]
        except SyntaxError:
            pass
    anchors = {
        "app/config.py": ("lead_model:", "developer_model:", "reviewer_model:", "global_max_"),
        "app/staff.py": (
            "evidence_audit =",
            '"id": "audit_evidence"',
            '"id": "audit_decisions"',
            'contract = "evidence-audit-v1"',
            "selected =",
            "if exact_factual or evidence_audit:",
        ),
        "app/store.py": ('key = f"{task.tenant}:', "daily.calls >=", "daily.reserved_tokens +="),
        "app/llm.py": ("model_alias=alias", "model=settings.model_for", "record_model_run", "model=model"),
    }.get(path, ())
    spans += [
        (max(1, i - 2), min(len(lines), i + 9))
        for i, line in enumerate(lines, 1)
        if any(anchor in line for anchor in anchors)
    ]
    if not spans:
        spans = [(1, min(len(lines), 65))]
    selected = sorted({i for start, end in spans for i in range(start, end + 1)})
    prefix = (
        f"Repository source excerpt; not execution evidence. path={path}; "
        f"full_source_sha256={hashlib.sha256(text.encode()).hexdigest()}; "
        "omitted lines are not inspected here.\n"
    )
    rendered = prefix
    for i in selected:
        line = f"L{i}: {lines[i - 1]}\n"
        if len(rendered) + len(line) > 3900:
            rendered += "[excerpt bounded; remaining selected lines omitted]"
            break
        rendered += line
    return rendered


def source_paths(task) -> list[str]:
    checks = requested_checks(task.requirement)
    if task.repo != SELF_REPOSITORY:
        # Other products cannot inherit factory-specific source paths or runtime state.
        return ["pyproject.toml", "package.json", ".github/workflows/ci.yml"] if "tests" in checks else []
    paths = []
    for topic in checks:
        paths += {
            "models": ["app/config.py", "app/llm.py"],
            "workflow": ["app/staff.py", "app/audit_scope.py"],
            "budget": ["app/store.py", "app/config.py"],
            "readiness": ["app/main.py"],
            "tests": [".github/workflows/ci.yml", "tests/test_audit_orchestration.py"],
        }[topic]
    return list(dict.fromkeys(paths))[:9]


async def readiness_observation() -> dict:
    # Same process/dependencies as the public /ready handler; no target code execution.
    from app.main import ready

    try:
        result = await ready()
        return {
            "status": result["status"],
            "endpoint": "/ready",
            "method": "in-process handler",
            "worker_checks_enabled": settings.worker_enabled,
            "telegram_checks_enabled": bool(settings.telegram_bot_token),
            "scope": "current factory control process only; no ARM/business acceptance proof",
        }
    except Exception as exc:
        return {
            "status": "unavailable",
            "endpoint": "/ready",
            "error_category": type(exc).__name__,
            "scope": "one failed readiness observation; not a root-cause diagnosis",
        }


async def audit_observations(task, github) -> list[ContextItem]:
    checks = requested_checks(task.requirement)
    items = [
        audit_item(
            task,
            "scope",
            "repository_audit_scope",
            {
                "required_checks": checks,
                "repository": task.repo,
                "base_sha": task.base_sha,
                "rule": "Answer each requested check; unavailable proof is an explicit unverified check. "
                "Documentation is design evidence only; source is implementation evidence; CI is not live runtime.",
            },
        )
    ]
    if task.repo == SELF_REPOSITORY:
        if "models" in checks:
            items.append(
                audit_item(
                    task,
                    "configured-models",
                    "factory_runtime_configuration",
                    {
                        "observed_at": utcnow().isoformat(),
                        "roles": {
                            role: {
                                "alias": settings.configured_model(role),
                                "provider_model": settings.model_for(role),
                            }
                            for role in ("lead", "developer", "reviewer")
                        },
                        "scope": "effective configuration of current control process, not proof of actual calls "
                        "by all three roles or independently attested deployed commit",
                    },
                )
            )
        if "budget" in checks:
            key = f"{task.tenant}:{utcnow().date().isoformat()}"
            async with store.sessions() as session:
                daily = await session.get(DailyBudget, key)
                current = await session.get(type(task), task.id)
            items.append(
                audit_item(
                    task,
                    "budget",
                    "factory_runtime_budget",
                    {
                        "observed_at": utcnow().isoformat(),
                        "period": "UTC calendar day",
                        "tenant": task.tenant,
                        "daily_limits": {
                            "calls": settings.global_max_llm_calls_per_day,
                            "reserved_tokens": settings.global_max_tokens_per_day,
                            "reported_cost_usd": settings.global_max_cost_usd_per_day,
                        },
                        "daily_usage": {
                            "calls": daily.calls if daily else 0,
                            "reserved_tokens": daily.reserved_tokens if daily else 0,
                            "reported_cost_usd": daily.cost_usd if daily else 0,
                        },
                        "current_task": {
                            "id": task.id,
                            "calls": current.llm_calls,
                            "tokens": current.tokens,
                            "reported_cost_usd": current.cost_usd,
                            "cost_incomplete": current.cost_incomplete,
                        },
                        "scope": "snapshot before this audit's model calls; reserved tokens are not billed tokens",
                    },
                )
            )
        if "readiness" in checks:
            items.append(
                audit_item(task, "readiness", "factory_runtime_readiness", await readiness_observation())
            )
    if "tests" in checks and settings.github_token:
        try:
            # Filter again locally: GitHub might ignore unsupported query parameters.
            runs = await github.request(
                "GET", f"{task.repo}/actions/runs", params={"head_sha": task.base_sha, "per_page": 10}
            )
            matched = [run for run in runs.get("workflow_runs", []) if run.get("head_sha") == task.base_sha][
                :1
            ]
            evidence = []
            for run in matched:
                jobs = await github.request(
                    "GET", f"{task.repo}/actions/runs/{run['id']}/jobs", params={"per_page": 20}
                )
                evidence.append(
                    {
                        "run_id": run["id"],
                        "head_sha": run["head_sha"],
                        "url": run.get("html_url"),
                        "status": run.get("status"),
                        "conclusion": run.get("conclusion"),
                        "jobs": [
                            {
                                "name": j["name"],
                                "status": j.get("status"),
                                "conclusion": j.get("conclusion"),
                                "steps": [
                                    {"name": s["name"][:150], "conclusion": s.get("conclusion")}
                                    for s in j.get("steps", [])[:12]
                                    if re.search(r"pytest|test|ruff|isolation|compose", s["name"], re.I)
                                ],
                            }
                            for j in jobs.get("jobs", [])[:3]
                        ],
                    }
                )
            items.append(
                audit_item(
                    task,
                    "ci",
                    "repository_ci",
                    {
                        "base_sha": task.base_sha,
                        "runs": evidence,
                        "scope": "CI metadata for this exact repository commit; not tests executed in production. "
                        "No matching run is unavailable proof, not a failed suite.",
                    },
                )
            )
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            items.append(
                audit_item(
                    task,
                    "ci",
                    "repository_ci",
                    {"base_sha": task.base_sha, "runs": [], "status": "unavailable"},
                )
            )
    return items


def audit_validation(output: StaffOutput, context: list[ContextItem]) -> list[str]:
    scopes = [item for item in context if item.source == "repository_audit_scope"]
    if not scopes:
        return []  # Historic/generic staff outputs keep their existing contract.
    required = json.loads(scopes[0].content)["required_checks"]
    checks = output.audit_checks
    issues = []
    covered = [check.topic for check in checks]
    if len(covered) != len(set(covered)):
        issues.append("Repository audit contains duplicate check topics")
    if set(covered) != set(required):
        issues.append(f"Repository audit scope coverage: required {required}; returned {covered}")
    refs = {item.ref: item for item in context}
    for check in checks:
        sources = [refs[ref] for ref in check.evidence_refs if ref in refs]
        if len(sources) != len(check.evidence_refs):
            issues.append(f"Audit check {check.topic} cites unknown or unauthorized evidence")
        expected = {
            "models": "factory_runtime_configuration",
            "budget": "factory_runtime_budget",
            "readiness": "factory_runtime_readiness",
            "tests": "repository_ci",
        }
        if check.verification == "runtime" and not any(
            item.source == expected.get(check.topic) for item in sources
        ):
            issues.append(f"Audit check {check.topic}: runtime verification requires direct runtime evidence")
        if check.verification == "repository" and not any(
            item.source == "registered_repository_source" for item in sources
        ):
            issues.append(
                f"Audit check {check.topic}: repository verification requires source evidence, not documentation"
            )
        if check.verification == "runtime" and any(
            item.source == expected.get(check.topic)
            and json.loads(item.content).get("status") == "unavailable"
            for item in sources
        ):
            issues.append(f"Audit check {check.topic}: unavailable observation is not runtime verification")
        if check.verification == "repository":
            relevant = {
                "models": ("app/config.py", "app/llm.py"),
                "workflow": ("app/staff.py", "app/audit_scope.py"),
                "budget": ("app/store.py", "app/config.py"),
                "readiness": ("app/main.py",),
                "tests": (".github/workflows/ci.yml", "pyproject.toml", "package.json"),
            }
            if not any(
                item.source == "registered_repository_source"
                and (
                    item.ref.endswith(relevant[check.topic])
                    or check.topic == "tests"
                    and ":tests/" in item.ref
                )
                for item in sources
            ):
                issues.append(f"Audit check {check.topic}: source reference does not cover this check")
        if check.verification == "runtime" and check.topic == "readiness":
            if not any(
                item.source == "factory_runtime_readiness"
                and json.loads(item.content).get("status") == "ready"
                for item in sources
            ):
                issues.append("Readiness cannot be verified from an unavailable probe")
        if check.verification == "ci":
            successful_test = any(
                item.source == "repository_ci"
                and any(
                    step.get("conclusion") == "success"
                    and re.search(r"pytest|(?:^|\s)test(?:\s|$)", step.get("name", ""), re.I)
                    for run in json.loads(item.content).get("runs", [])
                    for job in run.get("jobs", [])
                    if job.get("conclusion") == "success"
                    and run.get("head_sha") == json.loads(scopes[0].content)["base_sha"]
                    for step in job.get("steps", [])
                )
                for item in sources
            )
            if check.topic != "tests" or not successful_test:
                issues.append(
                    f"Audit check {check.topic}: CI verification requires a successful test step at the locked SHA"
                )
    # Known Task #47 leakage is audit instruction text, not an evidence gap.
    for gap in output.missing_information:
        if re.search(
            r"narasi tersembunyi|hidden narrative|penanganan ValueError|warning.*exhaustion", gap, re.I
        ):
            issues.append("Repository audit leaks internal review instructions into missing_information")
    for finding in output.findings:
        cited = [refs[ref] for ref in finding.evidence_refs if ref in refs]
        if cited and all(item.source == "registered_repository_excerpt" for item in cited):
            claim = finding.title + " " + finding.situation
            if re.search(
                r"\bandal\b|\breliable\b|keamanan.*ketat|security.*strict|\bterbukti\b", claim, re.I
            ):
                issues.append(
                    "Documentation-only finding asserts operational reliability/security; qualify design and request execution evidence"
                )
    if re.search(
        r"(?:mengonfirmasi|membuktikan|terbukti|confirms).*(?:andal|reliable|keamanan.*ketat|strict security)",
        output.summary,
        re.I,
    ):
        issues.append("Repository audit summary overstates operational reliability/security")
    for check in checks:
        if check.verification == "unverified" and not check.limitation.strip():
            issues.append(f"Unverified audit check {check.topic} requires an explicit evidence limitation")
        if (
            check.topic == "tests"
            and check.verification != "ci"
            and re.search(r"(?<!tidak )(?<!belum )\blulus\b|\bpassed\b", check.observation, re.I)
        ):
            issues.append("Test pass claim requires direct CI test execution evidence")
    return list(dict.fromkeys(issues))
