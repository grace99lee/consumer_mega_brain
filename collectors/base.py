from __future__ import annotations

from abc import ABC, abstractmethod

from config.settings import Settings
from models.schemas import Review, SourceType


class BaseCollector(ABC):
    source_type: SourceType

    def __init__(self, settings: Settings):
        self.settings = settings

    @abstractmethod
    async def collect(self, query: str, max_results: int = 200) -> list[Review]:
        """Fetch reviews matching query. Handles pagination internally."""
        ...

    @abstractmethod
    def is_available(self) -> bool:
        """Return True if required API keys / dependencies are configured."""
        ...
