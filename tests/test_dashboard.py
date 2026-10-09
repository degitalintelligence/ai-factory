"""Dashboard semantics and scoped thread lookup; no live actions or model calls."""

import shutil
import subprocess
from pathlib import Path

import httpx
import pytest

from app.chat import converse
from app.config import settings
from app.db import ChatTurn, Conversation
from app.main import app
from app.staff_schemas import ChatRequest


@pytest.mark.parametrize(
    "project,message,resolved",
    [
        ("lab", "Review product requirements", "lab"),
        ("", "Audit repository ai-factory. Jangan melakukan perubahan.", "self"),
    ],
)
async def test_overview_returns_owned_thread_without_foreign_receipts(
    db, monkeypatch, project, message, resolved
):
    monkeypatch.setattr(settings, "api_token", "operator-test-token")
    monkeypatch.setattr(settings, "api_operator_user_id", 7)
    result = await converse(ChatRequest(message=message, project=project, idempotency_key="dash-1"), 7)
    async with db.sessions() as session, session.begin():
        session.add(Conversation(id="foreign", owner=8, tenant=settings.tenant_id, project="lab"))
        await session.flush()
        session.add(
            ChatTurn(
                conversation_id="foreign",
                request_key="foreign",
                request_hash="foreign",
                message="Foreign receipt",
                task_id=result["intent_id"],
                response_json="{}",
            )
        )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://factory") as client:
        assert (await client.get("/v1/overview")).status_code == 401
        headers = {"Authorization": "Bearer operator-test-token"}
        overview = (await client.get("/v1/overview", headers=headers)).json()
        task = overview["tasks"][0]
        assert task["conversation_id"] == result["conversation_id"]
        assert task["conversation_project"] == project
        assert task["project"] == resolved
        history = await client.get(f"/v1/conversations/{task['conversation_id']}", headers=headers)
        assert history.status_code == 200
        assert history.json()["turns"][0]["message"] == message
        followup = await client.post(
            "/v1/chat",
            headers=headers,
            json={
                "message": "status",
                "project": task["conversation_project"],
                "conversation_id": task["conversation_id"],
                "reply_to_intent_id": task["id"],
                "idempotency_key": "dash-followup",
            },
        )
        assert followup.status_code == 202
        assert followup.json()["intent_id"] == task["id"]
        assert (await client.get("/v1/conversations/foreign", headers=headers)).status_code == 404


@pytest.mark.skipif(not shutil.which("node"), reason="Node required for browser presentation logic")
def test_dashboard_explains_recorded_state_and_rejects_unsafe_links():
    source = r"""
const assert = require('node:assert/strict');
const {taskState,attention,taskMatches,safeLink,artifactMap,guidance} = require('./app/console/app.js');
const task={id:77,status:'completed',effective_status:'failed',project:'self',requirement:'Audit model',summary:'Recorded budget stop'};
assert.equal(taskState(task),'failed');
assert.equal(attention(task),true);
assert.equal(taskMatches(task,'model','attention'),true);
assert.equal(taskMatches(task,'model','done'),false);
assert.equal(guidance(task).cause,'Recorded budget stop');
assert.match(guidance(task).next,/retry harus dipilih secara eksplisit/);
assert.equal(guidance(task,{staff_failure:{message:'Exact recorded failure'}}).cause,'Exact recorded failure');
assert.equal(safeLink('javascript:alert(1)'),null);
assert.equal(safeLink('data:text/html,test'),null);
assert.equal(safeLink('https://github.com/owner/repo/pull/1'),'https://github.com/owner/repo/pull/1');
assert.equal(artifactMap([{kind:'failure',content:'{"message":"Stopped"}'}]).failure.message,'Stopped');
assert.match(guidance({status:'deployment_unknown'}).next,/Rekonsiliasi/);
assert.match(guidance({status:'completed'}).next,/tidak otomatis/);
"""
    subprocess.run(["node", "-e", source], cwd=Path(__file__).parents[1], check=True)
