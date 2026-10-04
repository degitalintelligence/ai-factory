import asyncio
import json
import math
from contextvars import ContextVar
from typing import TypeVar

from openai import APIConnectionError, APIStatusError, AsyncOpenAI
from pydantic import BaseModel, ValidationError

from app.config import settings
from app.store import store

T = TypeVar("T", bound=BaseModel)
run_context: ContextVar[tuple[int, str] | None] = ContextVar("run_context", default=None)


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


async def json_completion(*, model: str, system: str, user: str, schema: type[T]) -> T:
    if not settings.openrouter_api_key or not model:
        raise RuntimeError("OpenRouter API key/model is not configured")
    if len(system) + len(user) > settings.max_prompt_chars:
        raise RuntimeError("Context budget exceeded; split the task")
    context = run_context.get()
    messages = [
        {
            "role": "system",
            "content": system
            + "\nReturn one JSON object matching this schema:\n"
            + json.dumps(schema.model_json_schema()),
        },
        {"role": "user", "content": user},
    ]
    async with AsyncOpenAI(
        api_key=settings.openrouter_api_key,
        base_url="https://openrouter.ai/api/v1",
        timeout=settings.llm_timeout_seconds,
        max_retries=0,
    ) as client:
        for attempt in range(3):
            if sum(len(m["content"]) for m in messages) > settings.max_prompt_chars:
                raise RuntimeError("Context budget exceeded during structured-output retry")
            if context:
                await store.reserve_call(
                    *context,
                    token_reserve=sum(len(m["content"].encode()) for m in messages)
                    + settings.max_output_tokens
                    + 100,
                )
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
                return schema.model_validate(json.loads(content))
            except (ValidationError, ValueError) as exc:
                detail = validation_detail(exc, schema)
                diagnostic = {
                    "schema": schema.__name__,
                    "model": model,
                    "attempt": attempt + 1,
                    "max_output_tokens": settings.max_output_tokens,
                    "detail": detail,
                }
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
    raise RuntimeError("LLM completion failed")
