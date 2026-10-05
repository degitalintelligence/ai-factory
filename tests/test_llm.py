import json

import httpx
import pytest
from openai import AsyncOpenAI

from app import llm
from app.config import settings
from app.schemas import DeveloperAction, LeadPlan


async def test_invalid_json_retry_charges_usage_and_returns_valid_plan(db, monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        content = "bad json" if len(calls) == 1 else '{"objective":"Feature","acceptance_criteria":["Works"]}'
        return httpx.Response(
            200,
            json={
                "id": "test",
                "object": "chat.completion",
                "created": 1,
                "model": "test",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": content},
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30, "cost": 0.002},
            },
        )

    client = AsyncOpenAI(
        api_key="placeholder",
        base_url="https://openrouter.ai/api/v1",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    monkeypatch.setattr(llm, "AsyncOpenAI", lambda **kwargs: client)
    monkeypatch.setattr(settings, "openrouter_api_key", "placeholder")
    task = await db.create("Generate a plan")
    await db.claim("w")
    token = llm.run_context.set((task.id, "w"))
    try:
        result = await llm.json_completion(model="test", system="Plan", user="Feature", schema=LeadPlan)
    finally:
        llm.run_context.reset(token)
    assert result.objective == "Feature"
    task = await db.get(task.id)
    assert task.llm_calls == 2 and task.tokens == 60 and task.cost_usd == pytest.approx(0.004)


def mock_completions(monkeypatch, responses):
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        content, reason = responses[min(len(requests) - 1, len(responses) - 1)]
        choices = (
            []
            if reason == "missing"
            else [
                {
                    "index": 0,
                    "finish_reason": reason,
                    "message": {"role": "assistant", "content": content},
                }
            ]
        )
        return httpx.Response(
            200,
            json={
                "id": "test",
                "object": "chat.completion",
                "created": 1,
                "model": "test",
                "choices": choices,
                "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
            },
        )

    client = AsyncOpenAI(
        api_key="placeholder", http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    monkeypatch.setattr(llm, "AsyncOpenAI", lambda **kwargs: client)
    monkeypatch.setattr(settings, "openrouter_api_key", "placeholder")
    return requests


@pytest.mark.parametrize(
    "bad,reason,detail",
    [
        ('{"action":"read_file"}', "stop", "path is required"),
        ('{"action":"search","query":"secret-input"}', "stop", "extra_forbidden"),
        ('{"action":"shell"}', "stop", "action: literal_error"),
        ('{"action":', "stop", "invalid_json"),
        ('{"action":"list_files"}', "length", "truncated"),
        (None, "stop", "empty_content"),
        (None, "missing", "missing_choices"),
        (None, "content_filter", "refusal"),
        ('[{"action":"list_files"}]', "stop", "schema_validation"),
    ],
)
async def test_developer_retry_has_specific_feedback_and_report(db, monkeypatch, bad, reason, detail):
    requests = mock_completions(monkeypatch, [(bad, reason), ('{"action":"list_files"}', "stop")])
    task = await db.create("Test structured output")
    await db.claim("w")
    token = llm.run_context.set((task.id, "w"))
    try:
        result = await llm.json_completion(
            model="test", system="Developer", user="Inspect", schema=DeveloperAction
        )
    finally:
        llm.run_context.reset(token)
    assert result.action == "list_files"
    assert len(requests) == 2
    assert detail in requests[1]["messages"][-1]["content"]
    artifacts = await db.artifacts(task.id)
    assert len(artifacts) == 1 and artifacts[0].kind == "llm_validation"
    assert detail in artifacts[0].content
    assert "secret-input" not in artifacts[0].content
    assert any(detail in event.message for event in await db.events(task.id))


async def test_invalid_output_stops_after_three_and_does_not_leak_values(db, monkeypatch):
    requests = mock_completions(
        monkeypatch, [('{"action":"secret-value","secret-key":"secret-value"}', "stop")]
    )
    task = await db.create("Test failed output")
    await db.claim("w")
    token = llm.run_context.set((task.id, "w"))
    try:
        with pytest.raises(RuntimeError, match="DeveloperAction.*after 3 attempts") as error:
            await llm.json_completion(
                model="test", system="Developer", user="Inspect", schema=DeveloperAction
            )
    finally:
        llm.run_context.reset(token)
    assert len(requests) == 3
    artifacts = await db.artifacts(task.id)
    assert len(artifacts) == 3
    saved = str(error.value) + str([a.content for a in artifacts]) + json.dumps(requests)
    assert "secret-value" not in saved and "secret-key" not in saved
    assert (await db.get(task.id)).llm_calls == 3


@pytest.mark.parametrize(
    "content", ['  ```json\n{"action":"list_files"}\n```  ', '```\n{"action":"list_files"}\n```']
)
async def test_single_fenced_document_accepted(monkeypatch, content):
    requests = mock_completions(monkeypatch, [(content, "stop")])
    result = await llm.json_completion(
        model="test", system="Developer", user="Inspect", schema=DeveloperAction
    )
    assert result.action == "list_files" and len(requests) == 1


async def test_retry_respects_context_limit(monkeypatch):
    requests = mock_completions(monkeypatch, [("broken", "stop")])
    monkeypatch.setattr(settings, "max_prompt_chars", 1000)
    with pytest.raises(RuntimeError, match="Context budget"):
        await llm.json_completion(model="test", system="Plan", user="x" * 250, schema=LeadPlan)
    assert len(requests) <= 1


async def test_single_attempt_plan_repair_preserves_budget_and_validation_evidence(db, monkeypatch):
    requests = mock_completions(
        monkeypatch,
        [("bad JSON", "length"), ('{"objective":"valid","acceptance_criteria":["works"]}', "stop")],
    )
    task = await db.create("Test bounded plan repair")
    await db.claim("w")
    token = llm.run_context.set((task.id, "w"))
    try:
        with pytest.raises(RuntimeError, match="after 1 attempts.*truncated"):
            await llm.json_completion(
                model="test", system="Plan", user="Repair", schema=LeadPlan, max_attempts=1
            )
    finally:
        llm.run_context.reset(token)
    assert len(requests) == 1 and (await db.get(task.id)).llm_calls == 1
    artifact = next(a for a in await db.artifacts(task.id) if a.kind == "llm_validation")
    diagnostic = json.loads(artifact.content)
    assert diagnostic["finish_reason"] == "length" and diagnostic["content_chars"] == 8
    assert diagnostic["prompt_tokens"] == 10 and diagnostic["completion_tokens"] == 20
    assert diagnostic["reasoning_tokens"] is None and "bad JSON" not in artifact.content
    assert any("attempt 1/1" in e.message for e in await db.events(task.id))
