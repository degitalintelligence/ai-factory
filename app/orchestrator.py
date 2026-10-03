import asyncio
from datetime import datetime
from sqlalchemy import select
from app.agents import developer_loop, lead_plan, review_change
from app.config import settings
from app.db import SessionLocal, Task, TaskStatus
from app.github_api import GitHubAPI
from app.workspace import Workspace

async def create_task(requirement: str) -> Task:
    async with SessionLocal() as session:
        task = Task(requirement=requirement, status=TaskStatus.RECEIVED.value)
        session.add(task)
        await session.commit()
        await session.refresh(task)
        return task

async def get_task(task_id: int) -> Task | None:
    async with SessionLocal() as session:
        result = await session.execute(select(Task).where(Task.id == task_id))
        return result.scalar_one_or_none()

async def _update_task(task_id: int, **values) -> None:
    async with SessionLocal() as session:
        task = await session.get(Task, task_id)
        if not task:
            return
        for key, value in values.items():
            setattr(task, key, value)
        task.updated_at = datetime.utcnow()
        await session.commit()

async def run_task(task_id: int, notify) -> None:
    task = await get_task(task_id)
    if not task:
        return
    repo_full_name = f"{settings.github_owner}/{settings.lab_repo}"
    branch = f"ai-factory/task-{task_id}"
    workspace = Workspace(task_id, repo_full_name, branch)
    try:
        await _update_task(task_id, status=TaskStatus.PLANNING.value, branch=branch)
        await notify(f"Task #{task_id}: Lead is planning…")
        plan = await lead_plan(task.requirement)

        if plan.risk == "high":
            message = "Lead marked this task HIGH RISK. V0.1 will not execute high-risk tasks automatically."
            await _update_task(task_id, status=TaskStatus.FAILED.value, last_message=message)
            await notify(f"Task #{task_id} stopped: {message}")
            return

        await asyncio.to_thread(workspace.prepare)
        feedback: list[str] = []

        for iteration in range(1, settings.max_iterations + 1):
            await _update_task(task_id, status=TaskStatus.DEVELOPING.value)
            await notify(f"Task #{task_id}: Developer iteration {iteration}/{settings.max_iterations}…")
            await developer_loop(
                workspace=workspace,
                requirement=task.requirement,
                plan=plan,
                reviewer_feedback=feedback,
            )

            status_before_tests = await asyncio.to_thread(workspace.status_porcelain)
            tests = await asyncio.to_thread(workspace.default_tests)
            status_after_tests = await asyncio.to_thread(workspace.status_porcelain)
            hygiene_issues = await asyncio.to_thread(
                workspace.hygiene_issues,
                status_before_tests,
                status_after_tests,
            )
            diff = await asyncio.to_thread(workspace.diff)

            await _update_task(task_id, status=TaskStatus.REVIEWING.value)
            await notify(f"Task #{task_id}: Reviewer checking diff + tests + repo hygiene…")
            review = await review_change(
                requirement=task.requirement,
                plan=plan,
                diff=diff,
                test_output=tests,
                hygiene_issues=hygiene_issues,
            )

            # Deterministic policy gate: the LLM reviewer cannot approve past
            # known hygiene violations.
            if hygiene_issues:
                review.approved = False
                review.issues = list(dict.fromkeys(hygiene_issues + review.issues))
                review.summary = (
                    "Rejected by deterministic repository hygiene gate. "
                    + review.summary
                )

            if review.approved:
                await asyncio.to_thread(workspace.commit_and_push, f"AI Factory task #{task_id}")
                pr_url = await GitHubAPI().create_pr(
                    repo_full_name=repo_full_name,
                    branch=branch,
                    title=f"AI Factory task #{task_id}",
                    body=(
                        f"Requirement:\n\n{task.requirement}\n\n"
                        f"Lead plan:\n\n{plan.model_dump_json(indent=2)}\n\n"
                        f"Reviewer:\n\n{review.summary}\n\n"
                        "Repository hygiene gate: PASS"
                    ),
                )
                await _update_task(
                    task_id,
                    status=TaskStatus.PR_CREATED.value,
                    pr_url=pr_url,
                    last_message=review.summary,
                )
                await notify(f"✅ Task #{task_id} passed review + hygiene gate. PR created:\n{pr_url}")
                return

            feedback = review.issues or [review.summary]
            await notify(
                f"Task #{task_id}: Reviewer rejected iteration {iteration}. "
                f"Returning {len(feedback)} issue(s) to Developer."
            )

        message = "Max review iterations reached without approval."
        await _update_task(task_id, status=TaskStatus.FAILED.value, last_message=message)
        await notify(f"❌ Task #{task_id} failed: {message}")
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        await _update_task(task_id, status=TaskStatus.FAILED.value, last_message=message)
        await notify(f"❌ Task #{task_id} crashed:\n{message}")
