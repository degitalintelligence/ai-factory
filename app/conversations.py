"""Durable deterministic follow-ups. Free-form text cannot infer execution approval."""

import hashlib
import json
import re

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.db import Artifact, ChatTurn, Conversation, Decision, Task, TelegramReference
from app.llm import json_completion
from app.security import secret_present
from app.staff_schemas import QuickReplyOutput
from app.store import store


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def response(summary, *, status="information", task_id=None, **extra):
    return {
        "summary": summary,
        "status": status,
        "intent_id": task_id,
        "next_action": "Gunakan tujuan baru atau tindak lanjuti task yang ditampilkan.",
        "decision_required": False,
        "evidence_refs": [f"task:{task_id}"] if task_id else [],
        "risk": "unknown",
        **extra,
    }


QUICK_REPLY_BOUNDARY = """You are LioBot. Answer only the user's trivial L0 request directly.
Do not create a plan, decision, audit, workflow, or implementation steps.
Use natural Indonesian unless the user asks otherwise.
If the user asks for one word, return exactly one word.
If the user asks for a short answer or a maximum word count, obey it strictly.
If the request actually needs repository inspection, task history, deployment state, or operator authority,
do not invent facts; reply briefly that it needs a tracked task instead.
"""


def quick_reply_profile(request, message: str, lowered: str) -> tuple[int, bool] | None:
    """Only obvious trivial L0 asks may skip the durable orchestration path."""
    if request.project or request.reply_to_intent_id or request.conversation_id:
        return None
    if re.search(r"(?:task|tujuan|intent|decision|keputusan)\s*#?\d+", message, re.I):
        return None
    if re.search(
        r"\b(review|audit|analisis|rencana|plan|implement|build|buatkan|perbaiki|fix|deploy|publish|merge|commit|push|pr|sandbox|test|tes|repo|repository|workflow|budget|status|hasil|clarif|klarif)\b",
        lowered,
        re.I,
    ):
        return None
    exact_one = bool(re.search(r"\b(?:satu|1)\s+kata\b", lowered, re.I))
    max_words = re.search(r"\bmaksimal\s+(\d+)\s+kata\b", lowered, re.I)
    brief = bool(re.search(r"\b(?:singkat|pendek|tanpa penjelasan)\b", lowered, re.I))
    simple_question = (
        len(message) <= 120
        and message.rstrip().endswith("?")
        and bool(re.match(r"\s*(apa|apakah|siapa|kapan|berapa|mana|benarkah|bisakah)\b", lowered, re.I))
    )
    if exact_one:
        return 1, True
    if max_words:
        return min(max(int(max_words[1]), 2), 40), False
    if brief:
        return 20, False
    if simple_question:
        return 25, False
    return None


async def quick_reply(request, actor: int) -> dict:
    message = request.message.strip()
    lowered = message.casefold()
    profile = quick_reply_profile(request, message, lowered)
    if not profile:
        raise ValueError("Quick reply is unavailable for this request")
    word_limit, exact_one = profile
    result = await json_completion(
        model=settings.model_for("lead"),
        role="lead",
        prompt_version="quick-reply-v1",
        system=QUICK_REPLY_BOUNDARY,
        user=(
            f"Jawab permintaan berikut secara langsung.\n"
            f"BATAS_KATA_MAKS:{word_limit}\n"
            f"SATU_KATA:{str(exact_one).lower()}\n"
            f"PERMINTAAN:{message}"
        ),
        schema=QuickReplyOutput,
        max_attempts=1,
    )
    words = re.findall(r"\S+", result.answer.strip())
    if exact_one and len(words) != 1:
        raise RuntimeError("Quick reply must be exactly one word")
    if len(words) > word_limit:
        raise RuntimeError("Quick reply exceeded the requested word limit")
    return {
        "summary": result.answer.strip(),
        "status": "completed",
        "intent_id": None,
        "next_action": "Ajukan tujuan baru bila perlu.",
        "decision_required": False,
        "evidence_refs": [],
        "risk": "low",
        "quick_reply": True,
    }


async def _thread(request, actor, chat_id):
    # A channel default is a stable thread, not a task target. Explicit HTTP callers
    # get a new thread unless they send the returned ID; project switches get a new thread.
    thread_id = request.conversation_id or (
        "tg_" + digest(f"{settings.tenant_id}:{actor}:{chat_id}:{request.project}")[:40]
        if chat_id is not None
        else "thread_" + digest(f"{settings.tenant_id}:{actor}:{request.idempotency_key}")[:40]
    )
    async with store.sessions() as s, s.begin():
        if s.bind.dialect.name == "postgresql":
            await s.execute(text("SELECT pg_advisory_xact_lock(72803105)"))
        thread = await s.get(Conversation, thread_id)
        if thread:
            if thread.tenant != settings.tenant_id or thread.owner != actor:
                raise ValueError("Conversation not found")
            if request.project and request.project != thread.project:
                raise ValueError("Project changed; start a new conversation")
        else:
            if request.conversation_id:
                raise ValueError("Conversation not found")
            if request.project and request.project not in settings.projects():
                raise ValueError("Unknown project alias")
            thread = Conversation(
                id=thread_id, tenant=settings.tenant_id, owner=actor, project=request.project
            )
            s.add(thread)
        return thread


async def _target(session, request, thread, actor):
    query = select(Task).where(Task.tenant == settings.tenant_id, Task.user_id == actor)
    if thread.project:
        query = query.where(Task.project == thread.project)
    explicit = request.reply_to_intent_id
    match = re.search(r"(?:task|tujuan|intent)\s+#?(\d+)", request.message, re.I)
    if match:
        if explicit and explicit != int(match[1]):
            raise ValueError("Conflicting follow-up targets")
        explicit = int(match[1])
    if explicit:
        task = await session.scalar(query.where(Task.id == explicit))
        if not task:
            raise ValueError("Task not found in this scope")
        return task, []
    task_ids = select(ChatTurn.task_id).where(
        ChatTurn.conversation_id == thread.id, ChatTurn.task_id.is_not(None)
    )
    tasks = list(await session.scalars(query.where(Task.id.in_(task_ids)).order_by(Task.id.desc()).limit(20)))
    # Never guess the latest task when more than one target exists in a thread.
    return (tasks[0], []) if len(tasks) == 1 else (None, [t.id for t in tasks])


async def _effective_task(session, task, actor):
    handoff = await session.scalar(
        select(Artifact.content)
        .where(
            Artifact.task_id == task.id,
            Artifact.kind == "engineering_handoff",
        )
        .order_by(Artifact.id.desc())
        .limit(1)
    )
    if not handoff:
        return task
    try:
        child_id = json.loads(handoff)["task_id"]
    except (ValueError, KeyError, TypeError):
        raise ValueError("Engineering handoff unavailable in this scope") from None
    child = await session.get(Task, child_id)
    if not child or child.user_id != actor or child.tenant != task.tenant or child.project != task.project:
        raise ValueError("Engineering handoff unavailable in this scope")
    return child


async def history(thread_id, actor):
    async with store.sessions() as s:
        thread = await s.get(Conversation, thread_id)
        if not thread or thread.tenant != settings.tenant_id or thread.owner != actor:
            raise ValueError("Conversation not found")
        turns = list(
            await s.scalars(
                select(ChatTurn)
                .where(ChatTurn.conversation_id == thread_id)
                .order_by(ChatTurn.id.desc())
                .limit(100)
            )
        )
        return {
            "conversation_id": thread_id,
            "project": thread.project,
            "turns": [
                {"message": t.message, "task_id": t.task_id, "response": json.loads(t.response_json)}
                for t in reversed(turns)
            ],
        }


async def bind_telegram_message(chat_id, message_id, task_id, actor=None):
    if not isinstance(message_id, int):
        return
    async with store.sessions() as s, s.begin():
        task = await s.get(Task, task_id)
        if (
            not task
            or task.tenant != settings.tenant_id
            or not task.user_id
            or task.chat_id != chat_id
            or (actor is not None and task.user_id != actor)
        ):
            raise ValueError("Telegram task reference unavailable")
        key = f"{settings.tenant_id}:{chat_id}:{message_id}"
        old = await s.get(TelegramReference, key)
        if old:
            if old.task_id != task_id or old.owner != task.user_id:
                raise ValueError("Telegram message already references another task")
            return
        s.add(TelegramReference(key=key, tenant=task.tenant, owner=task.user_id, task_id=task_id))


async def telegram_target(chat_id, message_id, actor):
    async with store.sessions() as s:
        row = await s.get(TelegramReference, f"{settings.tenant_id}:{chat_id}:{message_id}")
        if not row or row.owner != actor or row.tenant != settings.tenant_id:
            raise ValueError("Reply target unavailable; specify the task ID")
        return row.task_id


async def conversation_turn(request, actor, chat_id=None):
    if not request.message.strip() or secret_present(request.message):
        raise ValueError("Message is empty or contains credentials")
    key = digest(f"{settings.tenant_id}:{actor}:{request.idempotency_key}")
    fingerprint = digest(request.model_dump_json() + f":{chat_id}")
    # Check the receipt before allocating a thread. An identical HTTP retry without
    # a conversation_id returns the original thread instead of creating another one.
    async with store.sessions() as s:
        previous = await s.scalar(select(ChatTurn).where(ChatTurn.request_key == key))
        if previous:
            if previous.request_hash != fingerprint:
                raise ValueError("Idempotency key already belongs to a different request")
            return json.loads(previous.response_json)
    thread = await _thread(request, actor, chat_id)
    message = request.message.strip()
    lowered = message.casefold().rstrip("?.!")
    exact_action = bool(
        re.fullmatch(
            r"(approve|setujui|reject|tolak|ask|tanya|defer|tunda|request_changes|revisi)\s+(?:decision|keputusan)\s+#?\d+(?:\s*:\s*.*)?",
            message,
            re.I,
        )
    )
    answer = re.fullmatch(r"(?:answer|jawab)\s+(?:intent|tujuan|task)\s+#?(\d+)\s*:\s*(.+)", message, re.I)
    kind = (
        "status"
        if lowered
        in {
            "status",
            "gimana progresnya",
            "sudah sampai mana",
            "kenapa gagal",
            "why failed",
            "hasilnya",
            "lihat hasil",
        }
        or re.fullmatch(r"(?:status|hasil|kenapa gagal)\s+(?:task|tujuan|intent)\s+#?\d+", lowered)
        else None
    )
    if lowered in {"lanjut", "lanjutkan", "lanjut yang tadi", "continue"}:
        kind = "continue"
    if lowered in {"setuju", "approve", "oke", "ok", "gas", "yes", "ya"}:
        kind = "ambiguous_approval"
    revision = re.match(
        r"(?:revisi|ubah|perbaiki)\s+(?:hasil|rekomendasi|yang tadi)\s*:?\s*(.*)", message, re.I
    )
    if revision:
        kind = "revision"
    explicit_target = request.reply_to_intent_id is not None or bool(answer)
    if explicit_target and not kind and not exact_action:
        kind = "reply"
    task_id = None
    try:
        async with store.sessions() as s, s.begin():
            # Serialize same-principal requests and action receipts on PostgreSQL.
            if s.bind.dialect.name == "postgresql":
                await s.execute(text("SELECT pg_advisory_xact_lock(72803105)"))
            previous = await s.scalar(select(ChatTurn).where(ChatTurn.request_key == key))
            if previous:
                if previous.request_hash != fingerprint:
                    raise ValueError("Idempotency key already belongs to a different request")
                return json.loads(previous.response_json)
            if kind and not exact_action:
                task, candidates = await _target(s, request, thread, actor)
                if kind == "ambiguous_approval":
                    result = response(
                        "Sebutkan keputusan dan ID yang disetujui, misalnya: setujui keputusan #12. Persetujuan umum tidak mengeksekusi pekerjaan.",
                        status="needs_target",
                        decision_required=True,
                    )
                elif not task:
                    result = response(
                        "Pilih task yang dimaksud; tidak ada target tunggal dalam percakapan ini.",
                        status="needs_target",
                        candidates=candidates,
                    )
                else:
                    task_id = task.id
                    effective = await _effective_task(s, task, actor)
                    if kind == "status":
                        result = response(
                            effective.last_message or "Belum ada hasil yang tercatat.",
                            status=effective.status,
                            task_id=task.id,
                            child_task_id=effective.id if effective.id != task.id else None,
                            next_action=f"Pantau task #{effective.id}; evidence tersedia di dashboard.",
                        )
                    elif kind == "continue":
                        result = response(
                            "Task dipantau tanpa retry atau approval otomatis. "
                            + (effective.last_message or ""),
                            status=effective.status,
                            task_id=task.id,
                            next_action=f"Jawab klarifikasi atau gunakan aksi eksplisit pada task #{effective.id}.",
                        )
                    elif effective.status == "waiting_input" and kind == "reply":
                        reply = answer[2] if answer else message
                        await store._apply_resume(s, effective.id, "answer", reply, user_id=actor)
                        result = response(
                            "Jawaban diterima; rencana akan diperbarui.", status="received", task_id=task.id
                        )
                    elif effective.status == "pr_created" and kind == "revision":
                        if not revision[1].strip():
                            result = response(
                                "Sebutkan perubahan yang diminta.", status="needs_input", task_id=task.id
                            )
                        else:
                            await store._apply_resume(s, effective.id, "feedback", revision[1], user_id=actor)
                            result = response(
                                "Revisi diterima pada PR yang sama; test dan review akan diulang.",
                                status="received",
                                task_id=task.id,
                            )
                    elif (
                        kind == "revision"
                        and effective.kind == "orchestration"
                        and effective.status == "completed"
                        and revision[1].strip()
                    ):
                        from app.staff import create_intent

                        followup = request.model_copy(
                            update={
                                "project": task.project,
                                "message": f"Analisis/draft revisi hasil task #{task.id}. {revision[1]}. Gunakan hasil sebelumnya sebagai draft, bukan bukti primer; jangan menjalankan aksi eksternal.",
                            }
                        )
                        fresh = await create_intent(followup, actor, chat_id)
                        task_id = fresh.id
                        result = response(
                            "Revisi draft dibuat sebagai task terkait; hasil lama tetap disimpan.",
                            status=fresh.status,
                            task_id=fresh.id,
                            related_task_id=task.id,
                        )
                    else:
                        result = response(
                            "Tindak lanjut ini memerlukan tujuan baru yang eksplisit atau jawaban pada klarifikasi aktif. Tidak ada pekerjaan baru yang dijalankan.",
                            status="needs_input",
                            task_id=task.id,
                        )
            elif exact_action:
                # Existing decision resolver is idempotent and digest-bound. It commits
                # decision+task effects atomically; receipt recovery reuses that outcome.
                from app.chat import legacy_converse

                if thread.project:
                    number = int(re.search(r"(?:decision|keputusan)\s+#?(\d+)", message, re.I)[1])
                    card = await s.get(Decision, number)
                    if not card or card.project != thread.project:
                        raise ValueError("Decision not found in this project")
                result = await legacy_converse(request, actor, chat_id)
            elif len(message) < 5 or lowered in {"halo", "hello", "help", "bantuan"}:
                result = response(
                    "Sampaikan tujuan beserta proyeknya. Untuk follow-up, pilih task atau balas pesannya; approval perlu ID keputusan."
                )
            else:
                effective_request = request.model_copy(update={"project": thread.project})
                profile = quick_reply_profile(effective_request, message, lowered)
                if profile:
                    try:
                        result = await quick_reply(effective_request, actor)
                    except RuntimeError:
                        from app.staff import create_intent

                        task = await create_intent(effective_request, actor, chat_id)
                        task_id = task.id
                        result = response(
                            task.last_message,
                            status=task.status,
                            task_id=task.id,
                            next_action="LioBot menyiapkan konteks dan rencana.",
                        )
                else:
                    from app.staff import create_intent

                    task = await create_intent(effective_request, actor, chat_id)
                    task_id = task.id
                    result = response(
                        task.last_message,
                        status=task.status,
                        task_id=task.id,
                        next_action="LioBot menyiapkan konteks dan rencana.",
                    )
            result["conversation_id"] = thread.id
            s.add(
                ChatTurn(
                    conversation_id=thread.id,
                    request_key=key,
                    request_hash=fingerprint,
                    message=message,
                    task_id=task_id,
                    response_json=json.dumps(result, ensure_ascii=False),
                )
            )
            return result
    except IntegrityError:
        async with store.sessions() as s:
            previous = await s.scalar(select(ChatTurn).where(ChatTurn.request_key == key))
            if previous and previous.request_hash == fingerprint:
                return json.loads(previous.response_json)
        raise
