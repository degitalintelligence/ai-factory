import asyncio
import hashlib
import json
import logging
import re

from app.agents import developer_loop, lead_plan, review_change
from app.config import Project, settings
from app.gates import deployment_issues, post_publication_issues, quality_issues
from app.github_api import GitHubAPI
from app.llm import run_context
from app.schemas import LeadPlan
from app.security import redact
from app.store import TaskStopped, plan_hash, store
from app.workspace import Workspace

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


async def repository_context(workspace, task):
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
        baseline_paths = list(dict.fromkeys((*BASELINE_DOCUMENTS, *referenced)))[:40]
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
            if t.repo == task.repo and t.id != task.id and t.status == "pr_created"
        ][:3]
        chunks += [
            f"Previous completed task #{t.id}: {t.requirement[:1000]}\n{t.last_message[:2000]}"
            for t in previous
        ]
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
            "context_sha256": hashlib.sha256(context.encode()).hexdigest(),
        },
        sort_keys=True,
    )
    return context, baseline


async def run_task(task_id, notify=None, owner=None):
    task = await store.get(task_id)
    if not task or not owner:
        raise TaskStopped("Tasks must be claimed by a durable worker")
    token = run_context.set((task_id, owner))

    async def check():
        return await store.check(task_id, owner)

    async def transition(status, message, **kwargs):
        await check()
        await store.update(task_id, owner, status=status, last_message=message, **kwargs)
        await store.event(task_id, status, message)
        if notify:
            try:
                await notify(f"Task #{task_id} [{task.project} → {task.repo}]\n{redact(message)}")
            except Exception:
                logger.warning("Notification unavailable for task %s", task_id)

    try:
        current = settings.projects().get(task.project)
        policy = Project.model_validate_json(task.policy_json)
        if current is None or current.model_dump() != policy.model_dump():
            raise RuntimeError("Project policy changed or access revoked; create a fresh task")
        api = GitHubAPI()
        if task.pr_url and not task.head_sha:
            pr = await api.find_pr(task.repo, task.branch)
            if not pr or pr["state"] != "open":
                raise RuntimeError("Feedback needs an open task PR; start a new task after merge")
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
                "Deployment files are statically checked when required. A live deployment is a separate explicit action."
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
            note = (
                "\nPost-publication verification: all evidence sections present."
                if not published
                else "\nPost-publication verification needs operator attention:\n- " + "\n- ".join(published)
            )
            await transition("pr_created", f"Passed gates. PR: {url}\n{summary}{note}", pr_url=url)

        # The commit and approval record are durable BEFORE a push or PR API request.
        if task.head_sha and task.review_digest:
            await transition("publishing", "Reconciling previously approved publication")
            await publish(task.head_sha, task.review_digest, "Publication recovered without duplicate PR")
            return
        context, baseline = await repository_context(workspace, task)
        await store.artifact(task_id, "baseline", baseline)
        if task.plan_json:
            plan = LeadPlan.model_validate_json(task.plan_json)
        else:
            await transition("planning", "Lead is analysing requirements and repository context")
            plan = await lead_plan(task.requirement, context)
            plan.deployment_required = plan.deployment_required or policy.require_deployment
            task.plan_json = plan.model_dump_json()
            await store.update(task_id, owner, plan_json=task.plan_json)
            await store.artifact(task_id, "plan", task.plan_json)
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
        feedback = json.loads(task.feedback_json)
        for iteration in range(task.iteration + 1, settings.max_iterations + 1):
            await transition(
                "developing",
                f"Developer iteration {iteration}/{settings.max_iterations}",
                iteration=iteration,
            )

            async def trace(step, record):
                await check()
                await store.event(task_id, "tool", f"Iteration {iteration}, step {step}: {record}")

            await developer_loop(
                workspace=workspace,
                requirement=task.requirement,
                plan=plan,
                reviewer_feedback=feedback,
                checkpoint=check,
                trace=trace,
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
            feedback = list(
                dict.fromkeys(issues + review.issues + ([] if review.approved else [review.summary]))
            )
            await store.update(task_id, owner, feedback_json=json.dumps(feedback))
            await store.event(task_id, "rejected", "\n".join(feedback))
        raise RuntimeError(
            "Review iteration limit reached; use /report and /logs, then /retry or create a smaller task"
        )
    except TaskStopped:
        raise
    except Exception as exc:
        message = redact(f"{type(exc).__name__}: {exc}")[:4000]
        await transition("failed", message)
    finally:
        run_context.reset(token)
