import json
from typing import TypeVar
from openai import AsyncOpenAI
from pydantic import BaseModel
from app.config import settings

T = TypeVar("T", bound=BaseModel)
client = AsyncOpenAI(api_key=settings.openrouter_api_key, base_url="https://openrouter.ai/api/v1")

async def json_completion(*, model: str, system: str, user: str, schema: type[T]) -> T:
    response = await client.chat.completions.create(
        model=model,
        temperature=0,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        response_format={"type": "json_object"},
    )
    content = response.choices[0].message.content or "{}"
    return schema.model_validate(json.loads(content))
