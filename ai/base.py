from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TypeVar, Type

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class AIProvider(ABC):
    @abstractmethod
    async def analyze(
        self,
        prompt: str,
        output_schema: Type[T],
        system_prompt: str = "",
    ) -> T:
        """Send prompt to the AI model and return a structured response matching output_schema."""
        ...

    @abstractmethod
    async def generate_text(self, prompt: str, system_prompt: str = "") -> str:
        """Send prompt and return free-form text response."""
        ...
