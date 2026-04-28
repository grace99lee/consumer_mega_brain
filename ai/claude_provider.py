from __future__ import annotations

import json
import logging
import re
from typing import Type, TypeVar

import anthropic
from pydantic import BaseModel
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from ai.base import AIProvider

logger = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)

_MAX_TOKENS = 16000


def _clean_schema(schema: dict) -> dict:
    """Remove Pydantic-specific keys that confuse the tool schema validator."""
    REMOVE = {"title", "examples"}
    if isinstance(schema, dict):
        return {k: _clean_schema(v) for k, v in schema.items() if k not in REMOVE}
    if isinstance(schema, list):
        return [_clean_schema(i) for i in schema]
    return schema


def _extract_json_text(text: str) -> str:
    """Strip markdown fences and return the first JSON object found."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    text = text.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1:
        return text[start : end + 1]
    return text


class ClaudeProvider(AIProvider):
    def __init__(self, api_key: str, model: str = "claude-sonnet-4-20250514"):
        self._client = anthropic.AsyncAnthropic(api_key=api_key)
        self._model = model

    @retry(
        wait=wait_exponential(min=2, max=60),
        stop=stop_after_attempt(5),
        retry=retry_if_exception_type((anthropic.RateLimitError, anthropic.APIConnectionError)),
    )
    async def analyze(
        self,
        prompt: str,
        output_schema: Type[T],
        system_prompt: str = "",
    ) -> T:
        """
        Use Claude's tool-use API to get guaranteed valid JSON matching output_schema.
        Tool use forces the model to emit structured output — it cannot produce
        malformed JSON or add extra prose.
        """
        raw_schema = output_schema.model_json_schema()
        tool_schema = _clean_schema(raw_schema)

        tool_name = "submit_analysis"
        tools = [
            {
                "name": tool_name,
                "description": "Submit the structured analysis result.",
                "input_schema": tool_schema,
            }
        ]

        kwargs: dict = dict(
            model=self._model,
            max_tokens=_MAX_TOKENS,
            tools=tools,
            tool_choice={"type": "tool", "name": tool_name},
            messages=[{"role": "user", "content": prompt}],
        )
        if system_prompt:
            kwargs["system"] = system_prompt

        response = await self._client.messages.create(**kwargs)

        # Extract the tool input block
        for block in response.content:
            if hasattr(block, "type") and block.type == "tool_use":
                data = block.input
                return output_schema.model_validate(data)

        # Fallback: if for some reason the model returned text instead of a tool call,
        # try to parse it as JSON
        for block in response.content:
            if hasattr(block, "text"):
                extracted = _extract_json_text(block.text)
                data = json.loads(extracted)
                return output_schema.model_validate(data)

        raise ValueError("Claude returned no tool_use or text content block")

    @retry(
        wait=wait_exponential(min=2, max=60),
        stop=stop_after_attempt(5),
        retry=retry_if_exception_type((anthropic.RateLimitError, anthropic.APIConnectionError)),
    )
    async def generate_text(self, prompt: str, system_prompt: str = "") -> str:
        kwargs: dict = dict(
            model=self._model,
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )
        if system_prompt:
            kwargs["system"] = system_prompt
        response = await self._client.messages.create(**kwargs)
        return response.content[0].text
