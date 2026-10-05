import asyncio
import hashlib
import json
import math
import time
from contextvars import ContextVar
from typing import TypeVar

from openai import APIConnectionError, APIStatusError, AsyncOpenAI
from pydantic import BaseModel, ValidationError

from app.config import settings
from app.store import store

T = TypeVar("T", bound=BaseModel)
run_context: ContextVar[tuple[int, str] | None] = ContextVar("run_context", default=None)
subtask_context: ContextVar[tuple[int, int] | None] = ContextVar("subtask_context", default=None)


def prompt_digest(messages) -> str:
    """Identify the exact rendered prompt without storing any of its content.

    The retry instruction changes the rendered prompt on every attempt, so hashing the
    real messages is what makes this hash usable as per-attempt evidence.
    """
    return hashlib.sha256("\x1e".join(f"{m['role']}:{m['content']}" for m in messages).encode()).hexdigest()


def validation_detail(exc: ValueError, schema: type[BaseModel]) -> str:
    """Describe errors without copying model output, unknown keys, or input values."""
    if isinstance(exc, ValidationError):
        details = []
        for error in exc.errors(include_input=False, include_url=False)[:8]:
            field = error["loc"][0] if error["loc"] else "object"
            field = field if field in schema.model_fields else "object"
            required_error = str(error.get("ctx", {}).get("error", ""))
            if required_error in {
                "path is required",
                "content is required",
                "old_text is required",
                "command is required",
            }:
                details.append(required_error)
                continue
            details.append(f"{field}: {error['type']}")
        return "schema_validation: " + "; ".join(details)
    if isinstance(exc, json.JSONDecodeError):
        return f"invalid_json: line {exc.lineno}, column {exc.colno}"
    return str(exc)  # Only locally constructed response-state errors reach here.


async def json_completion(
    *,
    model: str,
    system: str,
    user: str,
    schema: type[T],
    role: str = "",
    prompt_version: str = "",
) -> T:
    """One structured call, audited per attempt.

    Every attempt appends an immutable model_run row so the lifetime task counters can
    be decomposed afterwards: which role ran, which configured alias resolved to which
    provider model, which prompt version and hash were sent, how long the call took and
    how it ended. Only the hash of the prompt is kept; prompt and completion text are
    never persisted.
    """
    if not settings.openrouter_api_key or not model:
        raise RuntimeError("OpenRouter API key/model is not configured")
    if len(system) + len(user) > settings.max_prompt_chars:
        raise RuntimeError("Context budget exceeded; split the task")
    context = run_context.get()
    alias = settings.configured_model(role) if role else ""
    messages = [
        {
            "role": "system",
            "content": system
            + "\nReturn one JSON object matching this schema:\n"
            + json.dumps(schema.model_json_schema()),
        },
        {"role": "user", "content": user},
    ]

    async def audit(attempt, outcome, detail="", tokens=0, cost=None, latency_ms=0, digest=""):
        if not context:
            return
        await store.record_model_run(
            context[0],
            role=role,
            model_alias=alias,
            model=model,
            prompt_version=prompt_version,
            prompt_sha256=digest,
            schema_name=schema.__name__,
            attempt=attempt,
            outcome=outcome,
            detail=detail,
            tokens=tokens,
            cost_usd=cost,
            latency_ms=latency_ms,
        )

    async with AsyncOpenAI(
        api_key=settings.openrouter_api_key,
        base_url="https://openrouter.ai/api/v1",
        timeout=settings.llm_timeout_seconds,
        max_retries=0,
    ) as client:
        for attempt in range(3):
            if sum(len(m["content"]) for m in messages) > settings.max_prompt_chars:
                await audit(
                    attempt + 1,
                    "prompt_budget_exceeded",
                    "Context budget exceeded during structured-output retry",
                    digest=prompt_digest(messages),
                )
                raise RuntimeError("Context budget exceeded during structured-output retry")
            if context:
                await store.reserve_call(
                    *context,
                    subtask=subtask_context.get(),
                    token_reserve=sum(len(m["content"].encode()) for m in messages)
                    + settings.max_output_tokens
                    + 100,
                )
            digest = prompt_digest(messages)
            started = time.monotonic()
            try:
                response = await client.chat.completions.create(
                    model=model,
                    temperature=0,
                    messages=messages,
                    max_tokens=settings.max_output_tokens,
                    response_format={"type": "json_object"},
                    extra_body={"usage": {"include": True}},
                )
            except (APIConnectionError, APIStatusError) as exc:
                latency_ms = int((time.monotonic() - started) * 1000)
                status = getattr(exc, "status_code", None)
                await audit(
                    attempt + 1,
                    "provider_error",
                    f"{type(exc).__name__}: {status}" if status else type(exc).__name__,
                    latency_ms=latency_ms,
                    digest=digest,
                )
                if attempt == 2 or (
                    isinstance(exc, APIStatusError) and exc.status_code not in {408, 429, 500, 502, 503, 504}
                ):
                    raise RuntimeError(
                        "LLM provider request failed; check provider/model configuration"
                    ) from exc
                await asyncio.sleep(2**attempt)
                continue
            usage = response.usage
            usage_data = usage.model_dump() if usage else {}
            cost = usage_data.get("cost")
            if cost is not None and (
                not isinstance(cost, (int, float)) or not math.isfinite(cost) or cost < 0
            ):
                cost = None
            tokens = (
                usage.total_tokens
                if usage and usage.total_tokens
                else (len(system) + len(user)) // 3 + settings.max_output_tokens
            )
            latency_ms = int((time.monotonic() - started) * 1000)
            if context:
                await store.record_usage(*context, tokens, cost)
            try:
                if not response.choices:
                    raise ValueError("missing_choices: provider returned no completion")
                choice = response.choices[0]
                if choice.finish_reason == "length":
                    raise ValueError("truncated: output token limit reached; return a smaller action")
                if choice.message.refusal or choice.finish_reason == "content_filter":
                    raise ValueError("refusal: provider declined this response; do not bypass its policy")
                content = (choice.message.content or "").strip()
                if not content:
                    raise ValueError("empty_content: provider returned no JSON text")
                # Accept a single fenced JSON document, never extract JSON from prose.
                lines = content.splitlines()
                if len(lines) >= 3 and lines[0] in {"```json", "```"} and lines[-1] == "```":
                    content = "\n".join(lines[1:-1])
                validated = schema.model_validate(json.loads(content))
            except (ValidationError, ValueError) as exc:
                detail = validation_detail(exc, schema)
                diagnostic = {
                    "schema": schema.__name__,
                    "model": model,
                    "attempt": attempt + 1,
                    "max_output_tokens": settings.max_output_tokens,
                    "detail": detail,
                }
                await audit(
                    attempt + 1,
                    "invalid_output",
                    detail,
                    tokens=tokens,
                    cost=cost,
                    latency_ms=latency_ms,
                    digest=digest,
                )
                if context:
                    await store.artifact(context[0], "llm_validation", json.dumps(diagnostic))
                    await store.event(
                        context[0], "llm_validation", f"{schema.__name__} attempt {attempt + 1}/3: {detail}"
                    )
                if attempt == 2:
                    raise RuntimeError(
                        f"{schema.__name__}: invalid structured output after 3 attempts ({detail}); see /report"
                    ) from exc
                messages.append(
                    {
                        "role": "user",
                        "content": f"Previous answer was rejected: {detail}. "
                        "Regenerate one complete JSON object matching the supplied schema and action rules. "
                        "Use exact field names at the top level; no wrappers, arrays, extra fields or commentary. "
                        "Include required fields with correct types. Keep the response small. "
                        "No action from the rejected response was executed.",
                    }
                )
                continue
            await audit(
                attempt + 1,
                "ok",
                tokens=tokens,
                cost=cost,
                latency_ms=latency_ms,
                digest=digest,
            )
            return validated
    raise RuntimeError("LLM completion failed")
