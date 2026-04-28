from __future__ import annotations

import json
import logging
from typing import Type, TypeVar

from openai import AsyncOpenAI, RateLimitError, APIConnectionError
from pydantic import BaseModel
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from ai.base import AIProvider

logger = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)


class OpenAIProvider(AIProvider):
    def __init__(self, api_key: str, model: str = "gpt-4o"):
        self._client = AsyncOpenAI(api_key=api_key)
        self._model = model

    @retry(
        wait=wait_exponential(min=2, max=60),
        stop=stop_after_attempt(5),
        retry=retry_if_exception_type((RateLimitError, APIConnectionError)),
    )
    async def analyze(
        self,
        prompt: str,
        output_schema: Type[T],
        system_prompt: str = "",
    ) -> T:
        schema = output_schema.model_json_schema()

        json_instruction = (
            f"\n\nRespond with ONLY valid JSON matching this schema:\n"
            f"```json\n{json.dumps(schema, indent=2)}\n```\n"
            f"No markdown, no explanation — just the JSON object."
        )

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt + json_instruction})

        response = await self._client.chat.completions.create(
            model=self._model,
            messages=messages,
            max_tokens=16000,
            response_format={"type": "json_object"},
        )

        text = response.choices[0].message.content.strip()
        data = json.loads(text)
        return output_schema.model_validate(data)

    @retry(
        wait=wait_exponential(min=2, max=60),
        stop=stop_after_attempt(5),
        retry=retry_if_exception_type((RateLimitError, APIConnectionError)),
    )
    async def generate_text(self, prompt: str, system_prompt: str = "") -> str:
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        response = await self._client.chat.completions.create(
            model=self._model,
            messages=messages,
            max_tokens=4096,
        )
        return response.choices[0].message.content
