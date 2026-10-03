import asyncio
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from app.config import settings
from app.orchestrator import create_task, get_task, run_task

async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "AI Factory V0.1 ready.\n\n"
        "Use:\n/new <requirement>\n/status <task_id>"
    )

async def new_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    requirement = " ".join(context.args).strip()
    if not requirement:
        await update.message.reply_text("Usage: /new <requirement>")
        return
    task = await create_task(requirement)
    chat_id = update.effective_chat.id
    async def notify(message: str) -> None:
        await context.bot.send_message(chat_id=chat_id, text=message)
    await update.message.reply_text(f"Created task #{task.id}.")
    asyncio.create_task(run_task(task.id, notify))

async def status_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /status <task_id>")
        return
    task = await get_task(int(context.args[0]))
    if not task:
        await update.message.reply_text("Task not found.")
        return
    message = (
        f"Task #{task.id}\n"
        f"Status: {task.status}\n"
        f"Branch: {task.branch or '-'}\n"
        f"PR: {task.pr_url or '-'}\n"
        f"Last: {task.last_message or '-'}"
    )
    await update.message.reply_text(message)

def build_telegram_app() -> Application:
    app = Application.builder().token(settings.telegram_bot_token).build()
    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(CommandHandler("new", new_handler))
    app.add_handler(CommandHandler("status", status_handler))
    return app
