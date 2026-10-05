import io
import json
import logging
from functools import wraps

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

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
        "LioBot by AI Factory — operator interface\n\n/new <requirement>\n/new <project> | <requirement>\n/projects — registered repositories\n/tasks — queue\n/status <id>\n/plan <id>\n/logs <id>\n/report <id>\n/cancel <id>\n/retry <id>\n/answer <id> <clarification>\n/approve <id> <plan-hash>\n/feedback <id> <revision>\n/deploy <id> <full-merged-sha>\n/publish <id> <full-merged-sha> — acknowledge a merged non-deploy project\n/supersede <id> <reason> — close a stale merged PR before fresh review\n/deployment <id>\n/inbox [state] [project] — decision inbox\n/decide <id> <approve|reject|ask|defer>\n/improve problem:...; evidence:...; hypothesis:...; scope:...; baseline:...; rollback:...\n\nCode → isolated tests → independent review → PR. Merges are human-controlled.\nThis Telegram bot is a channel adapter; decisions are stored in the core inbox, not in chat.",
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
    project, requirement = (part.strip() for part in raw.split("|", 1)) if "|" in raw else ("lab", raw)
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


@protected
async def status_handler(update, context):
    t = await owned_task(update, context)
    cost = f"${t.cost_usd:.4f}" + (" (partial; provider omitted some costs)" if t.cost_incomplete else "")
    await reply(
        update,
        f"Task #{t.id} [{t.project}]\nRepository: {t.repo}\nStatus: {t.status}\nIteration: {t.iteration}/{settings.max_iterations}\nLLM calls: {t.llm_calls}; tokens: {t.tokens}; reported cost: {cost}\nBranch: {t.branch}\nPR: {t.pr_url or '-'}\nLast: {t.last_message or '-'}",
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
        "\n\n".join(f"{e.created_at.isoformat()} {e.kind}\n{e.message[:600]}" for e in reversed(entries))
        or "No events",
    )


@protected
async def report_handler(update, context):
    t = await owned_task(update, context)
    artifacts = await store.artifacts(t.id)
    report = f"# Task {t.id}\n\nRepository: {t.repo}\nStatus: {t.status}\n\n{t.requirement}\n"
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
        lines.append(f"Answer with /decide {decision.id} <approve|reject|ask|defer>")
    return redact("\n".join(lines))


@protected
async def inbox_handler(update, context):
    decisions = await decision_inbox(
        state=context.args[0] if context.args else "open",
        project=context.args[1] if len(context.args) > 1 else None,
    )
    await reply(
        update,
        "\n\n".join(render_decision(d) for d in decisions) or "No decisions pending",
    )


@protected
async def decide_handler(update, context):
    if len(context.args) < 2 or not context.args[0].isdigit():
        raise ValueError("Usage: /decide <decision-id> <approve|reject|ask|defer>")
    decision = await resolve_decision(int(context.args[0]), context.args[1], user_id=update.effective_user.id)
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
    return app
