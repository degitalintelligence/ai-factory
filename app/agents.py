import json
from app.config import settings
from app.llm import json_completion
from app.schemas import DeveloperAction, LeadPlan, ReviewResult
from app.workspace import Workspace

async def lead_plan(requirement: str) -> LeadPlan:
    system = """You are the AI Software Factory Lead.
Turn a human feature request into a small, implementable engineering task.
Do not invent unrelated scope. Return JSON only matching:
{
  "objective": "...",
  "acceptance_criteria": ["..."],
  "constraints": ["..."],
  "suggested_tests": ["..."],
  "risk": "low|medium|high"
}"""
    return await json_completion(model=settings.lead_model, system=system, user=requirement, schema=LeadPlan)

async def developer_loop(*, workspace: Workspace, requirement: str, plan: LeadPlan, reviewer_feedback: list[str] | None = None) -> None:
    history: list[str] = []
    feedback = reviewer_feedback or []
    system = """You are an autonomous software developer working in a restricted repository workspace.
You may choose exactly one action per response and MUST return JSON only:
{"action":"list_files|read_file|write_file|delete_file|run_command|git_diff|finish",
 "path":null,
 "content":null,
 "command":null,
 "note":null}
Rules:
- Inspect existing code before editing.
- Make the smallest coherent implementation.
- Never access secrets, network, parent directories, or .git internals.
- Commands are restricted to pytest and Python compile checks.
- Before finish, inspect git_diff and run relevant tests.
- Tests must not leave runtime/generated artifacts in the repository.
- Runtime data such as SQLite databases, logs, caches, .env files, and compiled files must not be committed.
- Use temporary/in-memory test storage and update .gitignore when runtime files are expected.
- If reviewer feedback asks to remove an artifact, use delete_file.
- write_file content must contain the COMPLETE replacement file.
- Do not use markdown fences around JSON."""
    context = (
        f"REQUIREMENT:\n{requirement}\n\n"
        f"PLAN:\n{plan.model_dump_json(indent=2)}\n\n"
        f"REVIEWER FEEDBACK:\n{json.dumps(feedback)}"
    )
    for _ in range(settings.max_dev_steps):
        user = context + "\n\nTOOL HISTORY:\n" + "\n\n".join(history[-12:])
        action = await json_completion(
            model=settings.developer_model,
            system=system,
            user=user,
            schema=DeveloperAction,
        )
        try:
            if action.action == "list_files":
                result = workspace.list_files()
            elif action.action == "read_file":
                result = workspace.read_file(action.path or "")
            elif action.action == "write_file":
                result = workspace.write_file(action.path or "", action.content or "")
            elif action.action == "delete_file":
                result = workspace.delete_file(action.path or "")
            elif action.action == "run_command":
                result = workspace.run_command(action.command or "")
            elif action.action == "git_diff":
                result = workspace.diff()
            elif action.action == "finish":
                return
            else:
                result = "Unsupported action"
        except Exception as exc:
            result = f"ERROR: {type(exc).__name__}: {exc}"
        history.append(f"ACTION: {action.model_dump_json()}\nRESULT:\n{result}")
    raise RuntimeError("Developer exceeded MAX_DEV_STEPS.")

async def review_change(
    *,
    requirement: str,
    plan: LeadPlan,
    diff: str,
    test_output: str,
    hygiene_issues: list[str],
) -> ReviewResult:
    system = """You are an independent code reviewer.
Be skeptical. Judge only against the requirement, plan, diff, test evidence, and repository hygiene evidence.
Reject incomplete, unsafe, obviously broken, untested, or repository-dirty work.
Generated/runtime artifacts such as SQLite databases, logs, caches, .env files, compiled files, or test leftovers must not be committed.
Tests that leave new repository artifacts are a rejection condition.
Return JSON only:
{"approved": true|false, "summary":"...", "issues":["..."]}"""
    user = (
        f"REQUIREMENT:\n{requirement}\n\n"
        f"PLAN:\n{plan.model_dump_json(indent=2)}\n\n"
        f"DIFF:\n{diff}\n\n"
        f"TEST OUTPUT:\n{test_output}\n\n"
        f"REPOSITORY HYGIENE ISSUES:\n{json.dumps(hygiene_issues, indent=2)}"
    )
    return await json_completion(
        model=settings.reviewer_model,
        system=system,
        user=user,
        schema=ReviewResult,
    )
