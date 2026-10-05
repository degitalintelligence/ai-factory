import io
import json
import logging
from functools import wraps

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from app.audit_scope import repository_audit, target_project
from app.config import settings
from app.contracts import (
    create_self_improvement,
    create_task,
    decision_inbox,
    perform_action,
    read_task,
    resolve_decision,
)
from app.schemas import SelfImprovementBrief
from app.security import redact
from app.store import plan_hash, store

logger = logging.getLogger(__name__)


async def reply(update, text):
    for start in range(0, len(text), 3500):
        await update.effective_message.reply_text(text[start : start + 3500])


def protected(handler):
    @wraps(handler)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.effective_user or not update.effective_message or not update.effective_chat:
            return
        if (
            update.effective_user.id not in settings.allowed_users()
            or update.effective_chat.type != "private"
        ):
            await reply(
                update,
                f"Access denied. Use a private chat and register your numeric Telegram user ID: {update.effective_user.id}",
            )
            return
        try:
            await handler(update, context)
        except ValueError as exc:
            await reply(update, redact(str(exc)))
        except Exception:
            logger.exception("Telegram command failed")
            await reply(update, "Command failed. No success is assumed; inspect /status or server logs.")

    return wrapped


async def owned_task(update, context):
    if not context.args or not context.args[0].isdigit():
        raise ValueError("Provide a numeric task ID")
    return await read_task(int(context.args[0]), update.effective_user.id)


@protected
async def start_handler(update, context):
    await reply(
        update,
        "LioBot by AI Factory — operator interface\n\n/new <requirement>\n/new <project> | <requirement>\n/projects — registered repositories\n/tasks — queue\n/status <id>\n/plan <id>\n/logs <id>\n/report <id>\n/cancel <id>\n/retry <id>\n/answer <id> <clarification>\n/approve <id> <plan-hash>\n/feedback <id> <revision>\n/deploy <id> <full-merged-sha>\n/publish <id> <full-merged-sha> — acknowledge a merged non-deploy project\n/supersede <id> <reason> — close a stale merged PR before fresh review\n/deployment <id>\n/inbox [state] [project] — decision inbox\n/decide <id> <approve|reject|ask|defer|request_changes|delegate>\n/improve problem:...; evidence:...; hypothesis:...; scope:...; baseline:...; rollback:...\n\nCode → isolated tests → independent review → PR. Merges are human-controlled.\nThis Telegram bot is a channel adapter; decisions are stored in the core inbox, not in chat.",
    )


@protected
async def projects_handler(update, context):
    await reply(
        update,
        "\n".join(
            f"{name} → {p.repo} [{p.base_branch}, {p.profile}]" for name, p in settings.projects().items()
        ),
    )


@protected
async def new_handler(update, context):
    raw = " ".join(context.args).strip()
    if "|" in raw:
        project, requirement = (part.strip() for part in raw.split("|", 1))
    else:
        first, _, remainder = raw.partition(" ")
        if first in settings.projects() or first == "self":
            project, requirement = first, remainder.strip()
        else:
            project, requirement = "", raw
    project = target_project(requirement, project, settings.projects())
    if not project:
        project = "lab"
    task = await create_task(
        requirement,
        project,
        update.effective_chat.id,
        update.effective_user.id,
        f"telegram:{update.update_id}",
    )
    await reply(
        update,
        f"Queued task #{task.id}\nProject: {task.project}\nRepository: {task.repo}\nBranch: {task.branch}",
    )


@protected
async def tasks_handler(update, context):
    tasks = await store.list(user_id=update.effective_user.id)
    await reply(
        update,
        "\n".join(f"#{t.id} {t.project} · {t.status} · {t.requirement[:65]}" for t in tasks)
        or "No tasks yet",
    )


async def task_diagnostics(task):
    artifacts = {a.kind: a.content for a in await store.artifacts(task.id)}
    skills = json.loads(artifacts.get("resolved_skills", "{}"))
    if not skills and task.kind != "orchestration":
        skills = {
            "selected": ["engineering"],
            "workflow": "lead/audit" if repository_audit(task.requirement) else "engineering",
        }
    context = json.loads(artifacts.get("context", "[]"))
    refs = [item["ref"] for item in context]
    if not refs and "baseline" in artifacts:
        baseline = json.loads(artifacts["baseline"])
        refs = [
            f"repo:{task.project}:{task.base_sha}:{item['path']}" for item in baseline.get("inspected", [])
        ]
    failure = json.loads(artifacts.get("staff_failure", artifacts.get("failure", "{}")))
    return (
        f"Base SHA: {task.base_sha or 'unresolved'}\n"
        f"Resolved skills: {', '.join(skills.get('steps', skills.get('selected', []))) or 'pending'}\n"
        f"Workflow: {skills.get('workflow', 'pending')}\n"
        f"Context references: {', '.join(refs) or 'none'}\n"
        f"Failure stage: {failure.get('stage', 'not recorded' if task.status == 'failed' else 'none')}"
    )


@protected
async def status_handler(update, context):
    t = await owned_task(update, context)
    diagnostics = await task_diagnostics(t)
    cost = f"${t.cost_usd:.4f}" + (" (partial; provider omitted some costs)" if t.cost_incomplete else "")
    await reply(
        update,
        f"Task #{t.id} [{t.project}]\nRepository: {t.repo}\n{diagnostics}\nStatus: {t.status}\nIteration: {t.iteration}/{settings.max_iterations}\nLLM calls: {t.llm_calls}; tokens: {t.tokens}; reported cost: {cost}\nBranch: {t.branch}\nPR: {t.pr_url or '-'}\nLast: {t.last_message or '-'}",
    )


@protected
async def plan_handler(update, context):
    t = await owned_task(update, context)
    if not t.plan_json:
        await reply(update, "Plan not available yet")
        return
    await reply(
        update,
        json.dumps(json.loads(t.plan_json), indent=2, ensure_ascii=False)
        + f"\nPlan hash: {plan_hash(t.plan_json)[:12]}",
    )


@protected
async def logs_handler(update, context):
    t = await owned_task(update, context)
    entries = await store.events(t.id, 12)
    await reply(
        update,
        f"Repository: {t.repo}\n{await task_diagnostics(t)}\n\n"
        + "\n\n".join(f"{e.created_at.isoformat()} {e.kind}\n{e.message[:600]}" for e in reversed(entries))
        or "No events",
    )


@protected
async def report_handler(update, context):
    t = await owned_task(update, context)
    artifacts = await store.artifacts(t.id)
    report = f"# Task {t.id}\n\nRepository: {t.repo}\n{await task_diagnostics(t)}\nStatus: {t.status}\n\n{t.requirement}\n"
    report += "\n".join(
        f"\n## {a.kind} — {a.created_at.isoformat()}\n\n```\n{a.content}\n```" for a in artifacts
    )
    document = io.BytesIO(redact(report).encode())
    document.name = f"factory-task-{t.id}.md"
    await update.effective_message.reply_document(document=document)


@protected
async def action_handler(update, context):
    t = await owned_task(update, context)
    action = update.effective_message.text.split()[0].split("@")[0].removeprefix("/")
    confirmation = await perform_action(
        t.id, action, " ".join(context.args[1:]), user_id=update.effective_user.id
    )
    await reply(update, confirmation)


def render_decision(decision):
    """Render one decision card. Any channel shows the same fields from the same row."""
    options = ", ".join(f"{o.get('id')}={o.get('label')}" for o in json.loads(decision.options_json or "[]"))
    lines = [
        f"Decision #{decision.id} [{decision.kind}] — {decision.state.upper()}",
        f"Title: {decision.title}",
        f"Task: #{decision.task_id}" if decision.task_id else "Task: -",
        f"Priority: {decision.priority}; risk: {decision.risk_level}",
        f"Situation: {decision.situation}",
        f"Why now: {decision.why_now}",
        f"Options: {options or '-'}",
        f"Recommendation: {decision.recommendation}",
        f"Evidence: {'; '.join(json.loads(decision.evidence_json or '[]')) or '-'}",
    ]
    if decision.missing_information:
        lines.append(f"Missing information: {decision.missing_information}")
    if decision.rollback:
        lines.append(f"Rollback: {decision.rollback}")
    if decision.expires_at:
        lines.append(f"Expires: {decision.expires_at.isoformat()}")
    if decision.decided_by:
        lines.append(f"Decided by {decision.decided_by} at {decision.decided_at.isoformat()}")
    if decision.state == "open":
        lines.append(f"Answer with /decide {decision.id} <approve|reject|ask|defer|request_changes|delegate>")
    return redact("\n".join(lines))


@protected
async def inbox_handler(update, context):
    decisions = await decision_inbox(
        state=context.args[0] if context.args else "open",
        project=context.args[1] if len(context.args) > 1 else None,
        owner=update.effective_user.id,
    )
    await reply(
        update,
        "\n\n".join(render_decision(d) for d in decisions) or "No decisions pending",
    )


@protected
async def decide_handler(update, context):
    if len(context.args) < 2 or not context.args[0].isdigit():
        raise ValueError("Usage: /decide <decision-id> <approve|reject|ask|defer|request_changes|delegate>")
    delegate_to = None
    reason = " ".join(context.args[2:])
    if context.args[1] == "delegate":
        if len(context.args) < 4 or not context.args[2].isdigit():
            raise ValueError("Usage: /decide <id> delegate <operator-id> <reason>")
        delegate_to = int(context.args[2])
        reason = " ".join(context.args[3:])
    decision = await resolve_decision(
        int(context.args[0]),
        context.args[1],
        user_id=update.effective_user.id,
        reason=reason,
        delegate_to=delegate_to,
    )
    await reply(update, f"Decision #{decision.id} → {decision.state}\n\n{render_decision(decision)}")


@protected
async def improve_handler(update, context):
    raw = " ".join(context.args)
    fields = {}
    for token in raw.split(";"):
        key, _, value = token.partition(":")
        if value.strip():
            fields[key.strip().lower()] = value.strip()
    missing = [
        k
        for k in ("problem", "evidence", "hypothesis", "scope", "baseline", "rollback", "project")
        if k not in fields
    ]
    if missing:
        raise ValueError(
            "Missing fields: "
            + ", ".join(missing)
            + "\nFormat: /improve problem:...; evidence:...; hypothesis:...; scope:...; "
            "baseline:...; rollback:...; project:<registered-self-target-alias>"
        )
    brief = SelfImprovementBrief(
        problem=fields["problem"],
        evidence=[e.strip() for e in fields["evidence"].split(",") if e.strip()],
        hypothesis=fields["hypothesis"],
        scope=fields["scope"],
        baseline=fields["baseline"],
        rollback_plan=fields["rollback"],
        touched_areas=[a.strip() for a in fields.get("areas", "").split(",") if a.strip()],
        blast_radius=fields.get("blast", ""),
    )
    task, needs_approval, areas = await create_self_improvement(
        brief,
        fields["project"],
        update.effective_chat.id,
        update.effective_user.id,
        f"telegram:{update.update_id}",
    )
    # Self-improvement is always gated, so the reply always names the approval stop.
    note = "\n\nThis task stops at /approve before any code runs."
    if areas:
        note += f"\nSensitive areas detected: {', '.join(areas)}."
    await reply(
        update,
        f"Queued self-improvement #{task.id}\nBranch: {task.branch}\nProblem: {brief.problem}{note}",
    )


@protected
async def chat_handler(update, context):
    from app.chat import converse
    from app.staff_schemas import ChatRequest

    result = await converse(
        ChatRequest(message=update.effective_message.text, idempotency_key=f"telegram:{update.update_id}"),
        update.effective_user.id,
        update.effective_chat.id,
    )
    summary = result.get("summary", str(result))
    if result.get("intent_id") is not None:
        summary = f"LioBot — tujuan #{result['intent_id']} [{result['status']}]\n{summary}"
    await reply(update, summary)


def build_telegram_app():
    app = Application.builder().token(settings.telegram_bot_token).build()
    for command, handler in {
        "start": start_handler,
        "help": start_handler,
        "projects": projects_handler,
        "new": new_handler,
        "tasks": tasks_handler,
        "status": status_handler,
        "plan": plan_handler,
        "logs": logs_handler,
        "report": report_handler,
        "inbox": inbox_handler,
        "decide": decide_handler,
        "improve": improve_handler,
    }.items():
        app.add_handler(CommandHandler(command, handler))
    for command in (
        "cancel",
        "retry",
        "answer",
        "approve",
        "feedback",
        "deploy",
        "publish",
        "supersede",
        "deployment",
    ):
        app.add_handler(CommandHandler(command, action_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat_handler))
    return app
