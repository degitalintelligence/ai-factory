import httpx
import pytest
from openai import AsyncOpenAI

from app import llm
from app.config import settings
from app.schemas import LeadPlan


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
