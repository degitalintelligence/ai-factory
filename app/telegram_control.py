import io
import json
import logging
from functools import wraps

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from app.config import settings
from app.deployment import DeploymentService
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
            logger.error("Telegram command failed", exc_info=False)
            await reply(update, "Command failed. No success is assumed; inspect /status or server logs.")

    return wrapped


async def owned_task(update, context):
    if not context.args or not context.args[0].isdigit():
        raise ValueError("Provide a numeric task ID")
    task = await store.get(int(context.args[0]))
    if not task or task.user_id != update.effective_user.id:
        raise ValueError("Task not found or not owned by you")
    return task


@protected
async def start_handler(update, context):
    await reply(
        update,
        "AI Factory V0.2\n\n/new <requirement>\n/new <project> | <requirement>\n/projects — registered repositories\n/tasks — queue\n/status <id>\n/plan <id>\n/logs <id>\n/report <id>\n/cancel <id>\n/retry <id>\n/answer <id> <clarification>\n/approve <id> <plan-hash>\n/feedback <id> <revision>\n/deploy <id> <full-merged-sha>\n/deployment <id>\n\nCode → isolated tests → independent review → PR. Merges are human-controlled.",
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
    task = await store.create(
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
    message = " ".join(context.args[1:])
    if action == "cancel":
        await store.cancel(t.id)
        await reply(update, f"Cancellation requested for #{t.id}; inspect /status for completion")
    elif action == "deploy":
        record = await DeploymentService().deploy(t.id, message)
        await reply(update, f"Deployment {record.status}: {record.deployment_uuid}\n{record.message}")
    elif action == "deployment":
        record = await DeploymentService().status(t.id)
        await reply(update, f"Deployment: {record.status}\nCommit: {record.commit_sha}\n{record.message}")
    else:
        await store.resume(t.id, action, message)
        await reply(update, f"Task #{t.id} requeued ({action})")


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
    }.items():
        app.add_handler(CommandHandler(command, handler))
    for command in ("cancel", "retry", "answer", "approve", "feedback", "deploy", "deployment"):
        app.add_handler(CommandHandler(command, action_handler))
    return app
