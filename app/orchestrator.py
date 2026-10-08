import asyncio
import hashlib
import json
import logging
import re

from app.agents import (
    DeveloperStalled,
    developer_loop,
    lead_plan,
    normalize_lead_plan,
    review_change,
)
from app.audit_scope import referenced_tasks, repository_audit, target_project, validate_sha
from app.config import Project, settings
from app.contracts import context_slice
from app.engineering_budget import engineering_budget_admission, engineering_iteration_step_limit
from app.gates import deployment_issues, post_publication_issues, quality_issues
from app.github_api import GitHubAPI
from app.llm import run_context
from app.schemas import LeadPlan, SelfImprovementBrief
from app.security import redact
from app.store import BudgetExceeded, TaskStopped, plan_hash, store
from app.workspace import Workspace, is_suspicious_artifact

_TEST_ARTIFACT_PREFIX = "Commands modified source or left test artifacts:"


def engineering_repair_feedback(issues: list[str], review_issues: list[str]) -> list[str]:
    """Turn deterministic failures into non-contradictory Developer instructions.

    Removing a flagged tracked runtime artifact is mandated controller policy, so
    review issues that advise restoring/ignoring the artifact (Task 56) or merely
    describe its mandated deletion as a problem (Task 63) carry no actionable
    repair and must not contradict the deterministic guidance below.
    """
    artifact_paths = []
    for issue in issues:
        if issue.startswith(_TEST_ARTIFACT_PREFIX):
            artifact_paths.extend(
                path.strip() for path in issue.removeprefix(_TEST_ARTIFACT_PREFIX).split(",") if path.strip()
            )

    # "delete"/"remove" cover deleted/deletion/removal inflections; "cleanup" covers
    # "corrective cleanup" style phrasing observed in Task 63 review issues.
    non_actionable = ("restore", "ignore", "delete", "remove", "cleanup")
    filtered_review = []
    for issue in review_issues:
        lowered = issue.casefold()
        names_artifact = any(path.casefold() in lowered for path in artifact_paths)
        if names_artifact and any(word in lowered for word in non_actionable):
            continue
        filtered_review.append(issue)

    guidance = []
    if artifact_paths:
        guidance.append(
            "Deterministic repair required: keep tracked/generated runtime artifacts out of the source "
            "snapshot; do not restore or ignore them. Inspect every existing test, including smoke and "
            "application-registration tests, for default storage creation. Route those calls through "
            "tmp_path, an in-memory store, or the repository's test storage configuration before rerunning "
            f"the suite. A zero test exit code with sandbox issues is still a failure. Artifacts: "
            f"{', '.join(artifact_paths)}"
        )
    return list(dict.fromkeys(guidance + issues + filtered_review))


def initial_source_hygiene_feedback(file_index: str, feedback: list[str]) -> list[str]:
    """Make tracked runtime artifacts binding before the first Developer action."""
    artifact_paths = [path for path in file_index.splitlines() if is_suspicious_artifact(path)]
    if not artifact_paths:
        return feedback
    return engineering_repair_feedback(
        [f"{_TEST_ARTIFACT_PREFIX} {', '.join(artifact_paths)}"],
        feedback,
    )


logger = logging.getLogger(__name__)

# Documents that describe how the repository must be changed, captured verbatim for replayable provenance.
BASELINE_DOCUMENTS = (
    "README.md",
    "AGENTS.md",
    "requirements.txt",
    "pyproject.toml",
    "package.json",
    "docs/ARCHITECTURE.md",
    "bot.py",
    "pytest.ini",
    "tests/test_smoke.py",
)


# Self-improvement tasks must start from the requested files and policy, not the whole product tree.
SELF_BASELINE_DOCUMENTS = ("README.md", "AGENTS.md")


def requirement_related_paths(file_index: str, requirement: str, *, limit: int = 8) -> list[str]:
    """Select small, named source/test files whose paths match requirement terms.

    The standard baseline is intentionally small, but feature-specific regression
    tests are often more useful than a generic smoke test. Path matching is kept
    deterministic so the recorded baseline remains replayable.
    """
    terms = {token for token in re.findall(r"[a-z0-9]+", requirement.casefold()) if len(token) >= 4}
    ranked = []
    for position, path in enumerate(file_index.splitlines()):
        if is_suspicious_artifact(path):
            continue
        path_terms = set(re.findall(r"[a-z0-9]+", path.casefold()))
        overlap = terms & path_terms
        if not overlap:
            continue
        test_bonus = 2 if path.startswith("tests/") or "/test" in path else 0
        ranked.append((-(len(overlap) + test_bonus), position, path))
    return [path for _score, _position, path in sorted(ranked)[:limit]]


async def repository_context(workspace, task):
    if repository_audit(task.requirement):
        validate_sha(task.base_sha)
    files = workspace.list_files()
    is_self = str(getattr(task, "kind", "")) == "self_improvement"
    context_limit = settings.self_task_context_chars if is_self else 120000
    # The inventory is useful for navigation, but a very large tree must not crowd out
    # the actual requirement and target files in a self-improvement prompt.
    inventory_limit = 16000 if is_self else len(files)
    file_index = files[:inventory_limit]
    chunks = ["PROJECT POLICY: " + task.policy_json, "FILES:\n" + file_index]
    if is_self:
        chunks.insert(
            1,
            "CONTEXT MODE: compact self-improvement baseline; inspect additional files with tools only when needed.",
        )
    inspected = []
    referenced = re.findall(
        r"(?<!https://)(?<!http://)(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\.(?:py|js|ts|json|toml|ini|md|yaml|yml)",
        task.requirement,
    )
    if is_self:
        baseline_paths = list(dict.fromkeys((*referenced, *SELF_BASELINE_DOCUMENTS)))[:12]
        per_file_limit = 4000
    else:
        related = requirement_related_paths(files, task.requirement)
        baseline_paths = list(dict.fromkeys((*referenced, *related, *BASELINE_DOCUMENTS)))[:40]
        per_file_limit = 10000
    for path in baseline_paths:
        try:
            raw_content = workspace.read_file(path)
            content = raw_content[:per_file_limit]
        except (ValueError, OSError, RuntimeError):
            continue
        inspected.append(
            {
                "path": path,
                "sha256": hashlib.sha256(content.encode()).hexdigest(),
                "content": content,
                "truncated": len(raw_content) > per_file_limit,
            }
        )
        chunks.append(f"{path}:\n{content}")
    if not is_self:
        previous = [
            t
            for t in await store.list(30)
            if t.repo == task.repo
            and t.id != task.id
            and t.status == "pr_created"
            and t.user_id == task.user_id
            and getattr(t, "tenant", "default") == getattr(task, "tenant", "default")
            and (not repository_audit(task.requirement) or t.id in referenced_tasks(task.requirement))
        ][:3]
        chunks += [
            f"Previous completed task #{t.id}: {t.requirement[:1000]}\n{t.last_message[:2000]}"
            for t in previous
        ]
    # Memory is part of the assembled context, not a separate lookup the caller must
    # remember. It is permission-filtered (tenant + owner), carries its own provenance,
    # and is labelled as untrusted data in the prompt so it cannot act as an instruction.
    slice_text = await context_slice(
        None,
        role="lead",
        scope=task.project,
        limit=15,
        owner=task.user_id,
        tenant=getattr(task, "tenant", settings.tenant_id),
        exact_scope=repository_audit(task.requirement),
        char_budget=min(4000, context_limit // 10),
    )
    if slice_text:
        chunks.append(slice_text)
    # Leave headroom for the system prompt, JSON schema and requirement within max_prompt_chars.
    context = "\n\n".join(chunks)[:context_limit]
    baseline = json.dumps(
        {
            "base_sha": task.base_sha,
            "base_branch": task.base_branch,
            "policy": json.loads(task.policy_json),
            "context_mode": "self_improvement_compact" if is_self else "standard",
            "context_char_limit": context_limit,
            "file_count": len(files.splitlines()),
            "file_inventory": files,
            "inspected": inspected,
            "memory_slice_included": bool(slice_text),
            "memory_slice_sha256": (hashlib.sha256(slice_text.encode()).hexdigest() if slice_text else ""),
            "context_sha256": hashlib.sha256(context.encode()).hexdigest(),
        },
        sort_keys=True,
    )
    return context, baseline


async def brief_scope_paths(task) -> frozenset[str]:
    """Deterministic developer mutation allowlist from the stored self-improvement brief.

    An empty frozenset keeps legacy behaviour for briefs that name no scope_paths;
    a nonempty allowlist is enforced by the developer loop scope guard.
    """
    if str(getattr(task, "kind", "")) != "self_improvement":
        return frozenset()
    for row in await store.artifacts(task.id):
        if row.kind == "self_improvement_brief":
            brief = SelfImprovementBrief.model_validate_json(row.content)
            return frozenset(brief.scope_paths)
    return frozenset()


async def run_task(task_id, notify=None, owner=None):
    task = await store.get(task_id)
    if not task or not owner:
        raise TaskStopped("Tasks must be claimed by a durable worker")
    if task.kind == "orchestration":
        from app.staff import run_staff_task

        return await run_staff_task(task_id, owner, notify)
    token = run_context.set((task_id, owner))

    async def check():
        return await store.check(task_id, owner)

    async def transition(status, message, **kwargs):
        await check()
        nonlocal stage
        if status != "failed":
            stage = status
        await store.update(task_id, owner, status=status, last_message=message, **kwargs)
        await store.event(task_id, status, message)
        if notify:
            try:
                await notify(f"Task #{task_id} [{task.project} → {task.repo}]\n{redact(message)}")
            except Exception:
                logger.warning("Notification unavailable for task %s", task_id)

    stage = "target_resolution"
    try:
        target_project(task.requirement, task.project, settings.projects())
        current = settings.projects().get(task.project)
        policy = Project.model_validate_json(task.policy_json)
        if current is None or current.model_dump() != policy.model_dump():
            raise RuntimeError("Project policy changed or access revoked; create a fresh task")
        api = GitHubAPI()
        if task.pr_url and not task.head_sha:
            pr = await api.find_pr(task.repo, task.branch)
            if not pr or pr["state"] != "open":
                raise RuntimeError("Feedback needs an open task PR; start a new task after merge")
        stage = "baseline_lock"
        workspace = Workspace(task.id, task.repo, task.branch, policy, task.base_sha)
        base_sha = await asyncio.to_thread(workspace.prepare, existing_branch=bool(task.pr_url))
        await store.update(task_id, owner, base_sha=base_sha)
        task.base_sha = base_sha

        async def publish(sha, digest, summary):
            await check()
            if await asyncio.to_thread(workspace.digest) != digest:
                raise RuntimeError("Workspace changed since approved review; cannot publish")
            if await api.branch_sha(task.repo, task.base_branch) != task.base_sha:
                raise RuntimeError("Base branch changed; start a fresh task against the updated base")
            remote = await api.branch_sha(task.repo, task.branch)
            if remote != sha:
                await check()
                await asyncio.to_thread(workspace.push, sha)
            if await api.branch_sha(task.repo, task.branch) != sha:
                raise RuntimeError("Remote branch differs from approved commit")
            await check()
            artifacts = await store.artifacts(task_id)
            review = next((a.content for a in reversed(artifacts) if a.kind == "review"), "")
            tests = next((a.content for a in reversed(artifacts) if a.kind == "tests"), "")
            body = (
                f"## Requirement\n{task.requirement}\n\n## Plan\n```json\n{task.plan_json}\n```\n\n"
                f"## Independent review\n```json\n{review}\n```\n\n## Test evidence\n```json\n{tests}\n```\n\n"
                f"Reviewed source digest: `{digest}`\n\nCommit: `{sha}`\n\n"
                "Deterministic tests, acceptance mapping, source integrity and hygiene gates passed. "
                "Deployment files, when required, were only statically checked for structure and "
                "policy; nothing was built, booted, or verified against a live deployment. "
                "A live deployment is a separate explicit operator action, reconciled with /deployment."
            )
            if task.plan_json:
                # Read the durable plan; publication reconciliation runs before plan is parsed below.
                deferred = LeadPlan.model_validate_json(task.plan_json).post_publication_criteria
            else:
                deferred = []
            if deferred:
                body += (
                    "\n\nPost-publication criteria verified by this PR: "
                    + ", ".join(f"#{n}" for n in sorted(deferred))
                    + ". Each is satisfied by this PR body, the linked commit, and the published checks."
                )
            if len(body) > 60000:
                body = body[:56000] + "\n\nFull evidence retained in task artifacts (/report)."
            url = await api.create_pr(
                repo_full_name=task.repo,
                branch=task.branch,
                base=task.base_branch,
                title=f"AI Factory #{task_id}: {task.requirement.splitlines()[0][:90]}",
                body=redact(body),
            )
            published = post_publication_issues(
                await api.pull(task.repo, int(url.rsplit("/", 1)[-1])),
                digest=digest,
                sha=sha,
                deferred=deferred,
            )
            await store.artifact(
                task_id, "post_publication", json.dumps({"pr_url": url, "issues": published})
            )
            if published:
                # Fail closed. The PR already exists, so its URL must stay durable for
                # reconciliation, but the task must not claim success while the published PR
                # does not match the reviewed evidence. A retry re-reads the same PR and
                # re-runs this verification instead of creating another one.
                await store.update(task_id, owner, pr_url=url)
                raise RuntimeError(
                    "Post-publication verification failed; the PR is published but does not "
                    "match the reviewed evidence. Inspect /report, correct it, then /retry:\n- "
                    + "\n- ".join(published)
                )
            await transition(
                "pr_created",
                f"Passed gates. PR: {url}\n{summary}\n"
                "Post-publication verification: all evidence sections present.",
                pr_url=url,
            )

        # The commit and approval record are durable BEFORE a push or PR API request.
        if task.head_sha and task.review_digest:
            await transition("publishing", "Reconciling previously approved publication")
            await publish(task.head_sha, task.review_digest, "Publication recovered without duplicate PR")
            return
        if repository_audit(task.requirement):
            validate_sha(task.base_sha)
            requested = re.findall(r"\b[0-9a-f]{40}\b", task.requirement)
            if requested and set(requested) != {task.base_sha}:
                raise ValueError(
                    "Requested audit commit differs from locked workspace baseline; create a fresh audit"
                )
            await store.artifact(
                task_id,
                "audit_target",
                json.dumps({"repository": task.repo, "project": task.project, "base_sha": task.base_sha}),
            )
        await store.artifact(
            task_id,
            "resolved_skills",
            json.dumps(
                {
                    "selected": ["engineering"],
                    "workflow": "lead/audit" if repository_audit(task.requirement) else "engineering",
                }
            ),
        )
        stage = "context_gathering"
        context, baseline = await repository_context(workspace, task)
        await store.artifact(task_id, "baseline", baseline)
        fresh_plan = not bool(task.plan_json)
        readmit = False
        if task.plan_json:
            raw_plan = task.plan_json
            plan = normalize_lead_plan(LeadPlan.model_validate_json(raw_plan), task.requirement)
            task.plan_json = plan.model_dump_json()
            if task.plan_json != raw_plan:
                await store.update(task_id, owner, plan_json=task.plan_json)
                await store.artifact(task_id, "plan", task.plan_json)
        else:
            stage = "lead_planning"
            await transition("planning", "Lead is analysing requirements and repository context")
            plan = await lead_plan(task.requirement, context)
            plan.deployment_required = plan.deployment_required or policy.require_deployment
            plan = normalize_lead_plan(plan, task.requirement)
            if not plan.questions and not plan.review_only:
                stage = "plan_budget_admission"
                current = await store.get(task_id)
                plan, accounting = engineering_budget_admission(
                    plan,
                    requirement=task.requirement,
                    file_index=workspace.list_files(),
                    context=context,
                    used_calls=current.llm_calls,
                    used_tokens=current.tokens,
                    used_cost=current.cost_usd,
                    fresh=True,
                    reviewer_feedback=json.loads(task.feedback_json),
                )
                await store.artifact(task_id, "plan_budget_accounting", json.dumps(accounting))
            task.plan_json = plan.model_dump_json()
            await store.update(task_id, owner, plan_json=task.plan_json)
            await store.artifact(task_id, "plan", task.plan_json)
        if not plan.questions and not plan.review_only:
            stage = "plan_budget_admission"
            if not fresh_plan:
                current = await store.get(task_id)
                saved_plan_json = task.plan_json
                # Re-fund an unapproved saved plan (retry or recovery) for the lifetime
                # usage already spent. An approved plan keeps its exact binding: the
                # approval hash must never move silently.
                readmit = current.approved_plan_hash is None
                plan, accounting = engineering_budget_admission(
                    plan,
                    requirement=task.requirement,
                    file_index=workspace.list_files(),
                    context=context,
                    used_calls=current.llm_calls,
                    used_tokens=current.tokens,
                    used_cost=current.cost_usd,
                    fresh=False,
                    readmit=readmit,
                    reviewer_feedback=json.loads(task.feedback_json),
                )
                task.plan_json = plan.model_dump_json()
                if task.plan_json != saved_plan_json:
                    # Persisting the re-funded plan is what makes the new allocation
                    # effective for budget_envelope and the step allocator.
                    await store.update(task_id, owner, plan_json=task.plan_json)
                    await store.artifact(task_id, "plan", task.plan_json)
                await store.artifact(task_id, "plan_budget_accounting", json.dumps(accounting))
            if accounting["issues"]:
                raise BudgetExceeded(
                    "Engineering plan cannot fund baseline implementation and independent review ("
                    + "; ".join(accounting["issues"])
                    + "). Inspect /plan, /report and /logs; create a new adequately funded task "
                    "or obtain an approved policy change. Saved budgets and lifetime usage are not reset."
                )
        if plan.questions:
            await transition(
                "waiting_input",
                "Clarification needed: " + " | ".join(plan.questions) + f"\nUse /answer {task_id} <answer>",
            )
            return
        needs_approval = plan.risk == "high" or task.kind == "self_improvement"
        if needs_approval and task.approved_plan_hash != plan_hash(task.plan_json):
            reason = "Self-improvement" if task.kind == "self_improvement" else "High-risk plan"
            await store.ensure_task_approval_decision(task, reason, plan_hash(task.plan_json))
            await transition(
                "awaiting_approval",
                f"{reason} ready for review. Decision Inbox has the approval card. Approve the exact plan with /decide <decision-id> approve or /approve {task_id} {plan_hash(task.plan_json)[:12]}",
            )
            return
        if plan.review_only:
            await transition("testing", "Running mandatory tests for review-only task")
            diff = await asyncio.to_thread(workspace.diff)
            digest = await asyncio.to_thread(workspace.digest)
            files = await asyncio.to_thread(workspace.snapshot)
            report = await asyncio.to_thread(workspace.default_tests)
            new_tests = await asyncio.to_thread(workspace.changed_test_files, diff)
            standalone = await asyncio.to_thread(workspace.standalone_tests, new_tests)
            issues = []
            if await asyncio.to_thread(workspace.digest) != digest:
                issues.append("Source changed during tests")
            if diff.strip():
                issues.append("Review-only task produced an implementation diff")
            if plan.deployment_required:
                issues += deployment_issues(files, plan.persistence_required)
            await store.artifact(task_id, "tests", report.model_dump_json())
            await store.artifact(
                task_id,
                "test_environment",
                json.dumps(
                    report.environment
                    or {
                        "runner": "isolated sandbox",
                        "test_network": "disabled by sandbox policy",
                        "credentials": "not mounted",
                        "telegram_polling": "not started",
                    },
                    sort_keys=True,
                ),
            )
            await store.artifact(
                task_id,
                "standalone_tests",
                json.dumps(
                    {"test_files": new_tests, "report": standalone.model_dump() if standalone else None}
                ),
            )
            await store.artifact(task_id, "diff", diff)
            await store.artifact(
                task_id,
                "review_only",
                json.dumps({"no_mutation": not bool(diff.strip()), "pr_created": False}, sort_keys=True),
            )
            await transition("reviewing", "Independent review: requirements, tests, security and deployment")
            review = await review_change(
                requirement=task.requirement,
                plan=plan,
                diff=diff,
                test_output=report.model_dump_json(),
                standalone_output=(
                    json.dumps({"test_files": new_tests, "report": standalone.model_dump()})
                    if standalone
                    else "No new or changed test files"
                ),
                hygiene_issues=issues,
                context=context[:22000],
            )
            issues = quality_issues(
                plan,
                report,
                review,
                diff,
                issues,
                standalone,
                allow_no_diff=True,
            )
            await store.artifact(task_id, "review", review.model_dump_json())
            await store.artifact(
                task_id,
                "gates",
                json.dumps(
                    {"passed": review.approved and not issues, "issues": issues, "source_digest": digest}
                ),
            )
            if not review.approved or issues:
                feedback = list(
                    dict.fromkeys(issues + review.issues + ([] if review.approved else [review.summary]))
                )
                await store.update(task_id, owner, feedback_json=json.dumps(feedback))
                await store.event(task_id, "rejected", "\n".join(feedback))
                details = "\n- ".join(feedback) or "Independent review did not approve the unchanged baseline"
                raise RuntimeError(
                    "Review-only task found a correction or failed an evidence gate; "
                    "create a bounded implementation task:\n- " + details
                )
            await transition(
                "reviewed",
                f"Review complete; no correction required. {review.summary}",
                review_digest=digest,
            )
            return

        feedback = json.loads(task.feedback_json)
        initial_feedback = initial_source_hygiene_feedback(workspace.list_files(), feedback)
        if initial_feedback != feedback:
            feedback = initial_feedback
            await store.update(task_id, owner, feedback_json=json.dumps(feedback))
        scope_paths = await brief_scope_paths(task)
        for iteration in range(task.iteration + 1, settings.max_iterations + 1):
            await transition(
                "developing",
                f"Developer iteration {iteration}/{settings.max_iterations}",
                iteration=iteration,
            )

            async def trace(step, record):
                await check()
                await store.event(task_id, "tool", f"Iteration {iteration}, step {step}: {record}")
                await store.artifact(
                    task_id,
                    "developer_trace",
                    json.dumps({"iteration": iteration, "step": step, "record": record}),
                    owner=owner,
                )

            current = await store.get(task_id)
            envelope = store.budget_envelope(current)
            step_limit = engineering_iteration_step_limit(
                current_calls=current.llm_calls,
                max_calls=envelope["max_llm_calls"],
                iteration=iteration,
                max_iterations=settings.max_iterations,
            )
            await store.artifact(
                task_id,
                "developer_allocation",
                json.dumps(
                    {
                        "iteration": iteration,
                        "step_limit": step_limit,
                        "used_calls": current.llm_calls,
                        "max_llm_calls": envelope["max_llm_calls"],
                        "future_iterations": settings.max_iterations - iteration,
                    }
                ),
            )
            # A resumed workspace (retry after failure or a repair iteration) may
            # carry incomplete or duplicated edits from the previous attempt; the
            # model must verify actual file content instead of assuming baseline.
            resume_note = (
                "WORKSPACE RESUME WARNING: this checkout may already contain uncommitted changes "
                "from a previous attempt. Inspect actual file content with list_files/read_file "
                "before editing; earlier edits may be incomplete, duplicated or corrupted. Do not "
                "assume baseline content."
                if (iteration > 1 or readmit)
                else ""
            )
            try:
                await developer_loop(
                    workspace=workspace,
                    requirement=task.requirement,
                    plan=plan,
                    reviewer_feedback=feedback,
                    checkpoint=check,
                    trace=trace,
                    step_limit=step_limit,
                    resume_note=resume_note,
                    scope_paths=scope_paths,
                )
            except (DeveloperStalled, BudgetExceeded) as exc:
                budget_reserved = isinstance(exc, BudgetExceeded) and (
                    "reserving independent review" in str(exc)
                )
                if isinstance(exc, BudgetExceeded) and not budget_reserved:
                    raise
                recoverable = not budget_reserved and iteration < settings.max_iterations
                stall_feedback = [
                    redact(str(exc)),
                    (
                        "Recovery iteration required: inspect the current mutated source again. Do not "
                        "repeat the stale replace_text anchor. Use a small exact anchor from the current "
                        "file, or a whole-file write only after reading that exact path. Preserve all "
                        "existing tests."
                        if recoverable
                        else (
                            "Developer call budget reached the independent-review reserve; a safe nonempty "
                            "partial diff may proceed only through mandatory tests and that reserved review."
                            if budget_reserved
                            else "Final authoring iteration stopped; a safe nonempty partial diff may proceed "
                            "only through mandatory tests and independent review."
                        )
                    ),
                ]
                feedback = list(dict.fromkeys(feedback + stall_feedback))
                await store.update(task_id, owner, feedback_json=json.dumps(feedback))
                await store.artifact(
                    task_id,
                    "developer_stall",
                    json.dumps(
                        {
                            "iteration": iteration,
                            "recoverable": recoverable,
                            "reason": "budget_reserved" if budget_reserved else "stalled",
                            "feedback": stall_feedback,
                        },
                        sort_keys=True,
                    ),
                )
                await store.event(
                    task_id,
                    (
                        "developer_recovery"
                        if recoverable
                        else "developer_budget_reserved"
                        if budget_reserved
                        else "developer_stall"
                    ),
                    "\n".join(stall_feedback),
                )
                if recoverable:
                    continue
                try:
                    final_stall_diff = await asyncio.to_thread(workspace.diff)
                except (ValueError, RuntimeError, OSError) as diff_exc:
                    raise RuntimeError(
                        "Developer authoring stopped and the partial source "
                        f"cannot enter mandatory checks: {redact(str(diff_exc))}"
                    ) from exc
                if not final_stall_diff.strip():
                    raise RuntimeError("Developer authoring stopped without an evaluable diff") from exc
                await store.event(
                    task_id,
                    "developer_handoff",
                    "Developer authoring stopped with a safe nonempty diff; running mandatory tests "
                    "and the reserved independent review without another Developer call.",
                )
            await check()
            try:
                diff = await asyncio.to_thread(workspace.diff)
                digest = await asyncio.to_thread(workspace.digest)
                files = await asyncio.to_thread(workspace.snapshot)
            except (ValueError, RuntimeError, OSError) as exc:
                feedback = [redact(str(exc))]
                await store.update(task_id, owner, feedback_json=json.dumps(feedback))
                continue
            await transition("testing", "Running mandatory tests in isolated sandbox")
            report = await asyncio.to_thread(workspace.default_tests)
            new_tests = await asyncio.to_thread(workspace.changed_test_files, diff)
            standalone = await asyncio.to_thread(workspace.standalone_tests, new_tests)
            issues = []
            if await asyncio.to_thread(workspace.digest) != digest:
                issues.append("Source changed during tests")
            if plan.deployment_required:
                issues += deployment_issues(files, plan.persistence_required)
            await store.artifact(task_id, "tests", report.model_dump_json())
            await store.artifact(
                task_id,
                "test_environment",
                json.dumps(
                    report.environment
                    or {
                        "runner": "isolated sandbox",
                        "test_network": "disabled by sandbox policy",
                        "credentials": "not mounted",
                        "telegram_polling": "not started",
                    },
                    sort_keys=True,
                ),
            )
            await store.artifact(
                task_id,
                "standalone_tests",
                json.dumps(
                    {"test_files": new_tests, "report": standalone.model_dump() if standalone else None}
                ),
            )
            await store.artifact(task_id, "diff", diff)
            await transition("reviewing", "Independent review: requirements, tests, security and deployment")
            review = await review_change(
                requirement=task.requirement,
                plan=plan,
                diff=diff,
                test_output=report.model_dump_json(),
                standalone_output=(
                    json.dumps({"test_files": new_tests, "report": standalone.model_dump()})
                    if standalone
                    else "No new or changed test files"
                ),
                hygiene_issues=issues,
                context=context[:22000],
            )
            issues = quality_issues(plan, report, review, diff, issues, standalone)
            await store.artifact(task_id, "review", review.model_dump_json())
            await store.artifact(
                task_id,
                "gates",
                json.dumps(
                    {"passed": review.approved and not issues, "issues": issues, "source_digest": digest}
                ),
            )
            if review.approved and not issues:
                await check()
                sha = await asyncio.to_thread(workspace.commit, f"AI Factory task #{task_id}", digest)
                await transition(
                    "publishing",
                    "Review passed; publishing approved commit",
                    head_sha=sha,
                    review_digest=digest,
                )
                await publish(sha, digest, review.summary)
                return
            feedback = engineering_repair_feedback(
                issues,
                review.issues + ([] if review.approved else [review.summary]),
            )
            await store.update(task_id, owner, feedback_json=json.dumps(feedback))
            await store.event(task_id, "rejected", "\n".join(feedback))
        raise RuntimeError(
            "Review iteration limit reached; use /report and /logs, then /retry or create a smaller task"
        )
    except TaskStopped:
        raise
    except Exception as exc:
        await store.artifact(
            task_id,
            "failure",
            json.dumps({"stage": stage, "category": type(exc).__name__, "detail": redact(str(exc))[:2000]}),
        )
        message = redact(f"{type(exc).__name__}: {exc}")[:4000]
        await transition("failed", message)
    finally:
        run_context.reset(token)
