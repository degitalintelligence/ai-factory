"""Exact scoped conversational actions; ambiguous approval phrases never execute."""

import re

from app.config import settings
from app.db import Decision, Task
from app.staff import create_intent
from app.staff_schemas import ChatRequest
from app.store import store


async def legacy_converse(request: ChatRequest, actor: int, chat_id: int | None = None):
    decision = re.fullmatch(
        r"(approve|setujui|reject|tolak|ask|tanya|defer|tunda|request_changes|revisi)\s+(?:decision|keputusan)\s+#?(\d+)(?:\s*:\s*(.*))?",
        request.message.strip(),
        re.I,
    )
    if decision:
        phrase, number, reason = decision.groups()
        async with store.sessions() as s:
            row = await s.get(Decision, int(number))
            task = await s.get(Task, row.task_id) if row and row.task_id else None
            if (
                not row
                or row.tenant != settings.tenant_id
                or (row.owner != actor and (not task or task.user_id != actor))
            ):
                raise ValueError("Decision not found or not owned by you")
        phrase = {"tunda": "defer"}.get(phrase.lower(), phrase.lower())
        answer = await store.resolve_decision(row.id, phrase, user_id=actor, reason=reason or "")
        return {
            "summary": f"Keputusan #{answer.id}: {answer.state}",
            "status": answer.state,
            "next_action": "Lihat state/evidence terbaru di inbox.",
            "decision_required": False,
            "evidence_refs": [f"decision:{answer.id}"],
            "risk": answer.risk_level,
            "decision_id": answer.id,
        }
    clarification = re.fullmatch(
        r"(?:answer|jawab)\s+(?:intent|tujuan|task)\s+#?(\d+)\s*:\s*(.+)", request.message.strip(), re.I
    )
    if clarification:
        number, message = clarification.groups()
        task = await store.get(int(number))
        if not task or task.user_id != actor or task.tenant != settings.tenant_id:
            raise ValueError("Task not found or not owned by you")
        await store.resume(task.id, "answer", message, user_id=actor)
        return {
            "intent_id": task.id,
            "summary": "Jawaban diterima; rencana akan diperbarui.",
            "status": "received",
            "next_action": "LioBot melanjutkan pekerjaan.",
            "decision_required": False,
            "evidence_refs": [f"intent:{task.id}"],
            "risk": "unknown",
        }
    task = await create_intent(request, actor, chat_id)
    return {
        "intent_id": task.id,
        "summary": task.last_message,
        "status": task.status,
        "next_action": "LioBot menyiapkan konteks dan rencana; hasil tersedia di percakapan dan dashboard.",
        "decision_required": False,
        "evidence_refs": [f"intent:{task.id}"],
        "risk": "unknown",
    }


async def converse(request: ChatRequest, actor: int, chat_id: int | None = None):
    from app.conversations import conversation_turn

    return await conversation_turn(request, actor, chat_id)
