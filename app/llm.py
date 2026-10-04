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
                if not response.choices or response.choices[0].finish_reason == "length":
                    raise ValueError("Response truncated; return a smaller action")
                content = response.choices[0].message.content or ""
                if content.startswith("```json") and content.rstrip().endswith("```"):
                    content = content[7:].rstrip()[:-3]
                return schema.model_validate(json.loads(content))
            except (ValidationError, ValueError) as exc:
                if attempt == 2:
                    raise RuntimeError("Model returned invalid structured output after 3 attempts") from exc
                messages.append(
                    {
                        "role": "user",
                        "content": "Previous answer failed schema validation. Return valid JSON with all required fields; no commentary.",
                    }
                )
    raise RuntimeError("LLM completion failed")
