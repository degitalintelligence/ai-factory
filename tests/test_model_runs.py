import json

import httpx
import pytest
from openai import AsyncOpenAI

from app import llm
from app.config import settings
from app.schemas import DeveloperAction, LeadPlan


def mock_completions(monkeypatch, responses):
    """Mirror tests/test_llm.py so a model_run can be produced without a provider."""
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        content, reason = responses[min(len(requests) - 1, len(responses) - 1)]
        choices = [
            {"index": 0, "finish_reason": reason, "message": {"role": "assistant", "content": content}}
        ]
        return httpx.Response(
            200,
            json={
                "id": "test",
                "object": "chat.completion",
                "created": 1,
                "model": "test",
                "choices": choices,
                "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30, "cost": 0.002},
            },
        )

    client = AsyncOpenAI(
        api_key="test", http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    monkeypatch.setattr(llm, "AsyncOpenAI", lambda **kwargs: client)
    monkeypatch.setattr(settings, "openrouter_api_key", "placeholder")
    return requests


async def claimed(db, requirement="Instrument model runs"):
    task = await db.create(requirement)
    await db.claim("w")
    return task


async def test_every_attempt_is_recorded_with_role_alias_model_and_prompt_evidence(db, monkeypatch):
    mock_completions(
        monkeypatch,
        [("bad json", "stop"), ('{"objective":"Feature","acceptance_criteria":["Works"]}', "stop")],
    )
    monkeypatch.setattr(settings, "model_aliases_json", '{"bunny-alpha":"stealth/space-bunny-alpha"}')
    monkeypatch.setattr(settings, "lead_model", "bunny-alpha")
    task = await claimed(db)
    token = llm.run_context.set((task.id, "w"))
    try:
        await llm.json_completion(
            model=settings.model_for("lead"),
            system="Plan",
            user="Feature",
            schema=LeadPlan,
            role="lead",
            prompt_version="lead-v1",
        )
    finally:
        llm.run_context.reset(token)

    runs = await db.model_runs(task.id)
    assert [run.attempt for run in runs] == [1, 2]
    assert [run.outcome for run in runs] == ["invalid_output", "ok"]
    for run in runs:
        assert run.role == "lead"
        # The configured alias and the resolved provider model are both recorded, so a
        # later provider change is explainable without reading application code.
        assert run.model_alias == "bunny-alpha"
        assert run.model == "stealth/space-bunny-alpha"
        assert run.prompt_version == "lead-v1"
        assert run.schema_name == "LeadPlan"
        assert len(run.prompt_sha256) == 64
        assert run.latency_ms >= 0
    # The retry changes the rendered prompt, so the two hashes must differ.
    assert runs[0].prompt_sha256 != runs[1].prompt_sha256
    assert runs[0].tokens == 30 and runs[0].cost_reported is True
    assert runs[1].outcome == "ok"


async def test_provider_failure_is_recorded_without_response_body_or_credentials(db, monkeypatch):
    def handler(request):
        return httpx.Response(401, json={"error": {"message": "invalid api key sk-live-SECRETVALUE"}})

    client = AsyncOpenAI(
        api_key="test", http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    monkeypatch.setattr(llm, "AsyncOpenAI", lambda **kwargs: client)
    monkeypatch.setattr(settings, "openrouter_api_key", "placeholder")
    task = await claimed(db, "Record a provider failure")
    token = llm.run_context.set((task.id, "w"))
    try:
        with pytest.raises(RuntimeError, match="LLM provider request failed"):
            await llm.json_completion(
                model="test",
                system="Developer",
                user="Inspect",
                schema=DeveloperAction,
                role="developer",
                prompt_version="developer-v1",
            )
    finally:
        llm.run_context.reset(token)

    runs = await db.model_runs(task.id)
    assert len(runs) == 1
    assert runs[0].outcome == "provider_error"
    assert "401" in runs[0].detail
    assert "SECRETVALUE" not in runs[0].detail
    # A non-retryable status must not consume the remaining attempts.
    assert runs[0].tokens == 0 and runs[0].cost_reported is False


async def test_validation_failure_detail_is_sanitized_and_charged(db, monkeypatch):
    mock_completions(monkeypatch, [('{"action":"read_file"}', "stop"), ('{"action":"list_files"}', "stop")])
    task = await claimed(db, "Record a validation failure")
    token = llm.run_context.set((task.id, "w"))
    try:
        await llm.json_completion(
            model="test",
            system="Developer",
            user="Inspect",
            schema=DeveloperAction,
            role="developer",
            prompt_version="developer-v1",
        )
    finally:
        llm.run_context.reset(token)

    failed = (await db.model_runs(task.id))[0]
    assert failed.outcome == "invalid_output"
    assert failed.detail == "schema_validation: path is required"
    assert failed.tokens == 30


async def test_unreported_cost_is_marked_incomplete_on_the_call_record(db, monkeypatch):
    def handler(request):
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
                        "message": {"role": "assistant", "content": '{"action":"list_files"}'},
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
            },
        )

    client = AsyncOpenAI(
        api_key="test", http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    monkeypatch.setattr(llm, "AsyncOpenAI", lambda **kwargs: client)
    monkeypatch.setattr(settings, "openrouter_api_key", "placeholder")
    task = await claimed(db, "Record an unreported cost")
    token = llm.run_context.set((task.id, "w"))
    try:
        await llm.json_completion(model="test", system="Developer", user="Inspect", schema=DeveloperAction)
    finally:
        llm.run_context.reset(token)

    run = (await db.model_runs(task.id))[0]
    assert run.outcome == "ok"
    # Provider-reported cost may be partial; the record says so instead of claiming zero.
    assert run.cost_reported is False and run.cost_usd is None
    stored = await db.get(task.id)
    assert stored.cost_incomplete is True


async def test_calls_without_a_task_context_persist_nothing(db, monkeypatch):
    mock_completions(monkeypatch, [('{"action":"list_files"}', "stop")])
    await llm.json_completion(model="test", system="Developer", user="Inspect", schema=DeveloperAction)
    task = await claimed(db, "Unrelated task")
    assert await db.model_runs(task.id) == []


async def test_model_runs_are_append_only_evidence_for_a_task(db):
    task = await claimed(db, "Append-only evidence")
    await db.record_model_run(task.id, role="lead", model_alias="bunny-alpha", model="m", attempt=1)
    await db.record_model_run(task.id, role="reviewer", model_alias="bunny-alpha", model="m", attempt=1)
    runs = await db.model_runs(task.id)
    assert [(run.role, run.attempt) for run in runs] == [("lead", 1), ("reviewer", 1)]
    assert all(run.task_id == task.id for run in runs)


def test_configured_model_rejects_an_unknown_role():
    with pytest.raises(ValueError, match="Unknown model role"):
        settings.configured_model("supervisor")
