from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from models.schemas import AnalysisResult, Review


class BaseExporter(ABC):
    @abstractmethod
    def export(
        self,
        result: AnalysisResult,
        reviews: list[Review],
        output_dir: Path,
    ) -> list[Path]:
        """Export analysis results. Returns list of files created."""
        ...
