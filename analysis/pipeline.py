from __future__ import annotations

import json
import logging
from datetime import datetime

from ai.base import AIProvider
from ai.prompts import BATCH_ANALYSIS_PROMPT, SYNTHESIS_PROMPT, SYSTEM_PROMPT
from models.schemas import (
    AnalysisResult,
    BatchAnalysisResult,
    Review,
    SourceType,
)

logger = logging.getLogger(__name__)


class AnalysisPipeline:
    def __init__(self, ai_provider: AIProvider, batch_size: int = 50):
        self.ai = ai_provider
        self.batch_size = batch_size

    async def run(
        self, query: str, query_type: str, reviews: list[Review]
    ) -> AnalysisResult:
        if not reviews:
            raise ValueError("No reviews to analyze")

        sources_used = list({r.source for r in reviews})
        batches = self._chunk(reviews, self.batch_size)

        logger.info(
            "Analyzing %d reviews in %d batches (batch_size=%d)",
            len(reviews),
            len(batches),
            self.batch_size,
        )

        # Pass 1: Batch analysis
        batch_results: list[BatchAnalysisResult] = []
        for i, batch in enumerate(batches, 1):
            logger.info("Processing batch %d/%d (%d reviews)", i, len(batches), len(batch))
            result = await self._analyze_batch(query, query_type, batch)
            batch_results.append(result)

        # Pass 2: Synthesis
        logger.info("Running synthesis across %d batch results", len(batch_results))
        final = await self._synthesize(query, query_type, reviews, batch_results, sources_used)
        return final

    async def _analyze_batch(
        self, query: str, query_type: str, reviews: list[Review]
    ) -> BatchAnalysisResult:
        reviews_data = [
            {
                "review_id": r.id,
                "source": r.source.value,
                "author": r.author,
                "text": r.text[:2000],  # Truncate very long reviews
                "rating": r.rating,
                "date": r.date.isoformat() if r.date else None,
            }
            for r in reviews
        ]

        prompt = BATCH_ANALYSIS_PROMPT.format(
            n_reviews=len(reviews),
            query=query,
            query_type=query_type,
            reviews_json=json.dumps(reviews_data, indent=1),
        )

        return await self.ai.analyze(
            prompt=prompt,
            output_schema=BatchAnalysisResult,
            system_prompt=SYSTEM_PROMPT,
        )

    async def _synthesize(
        self,
        query: str,
        query_type: str,
        reviews: list[Review],
        batch_results: list[BatchAnalysisResult],
        sources_used: list[SourceType],
    ) -> AnalysisResult:
        source_list = ", ".join(s.value for s in sources_used)

        # Exclude per-review breakdowns — synthesis only needs the aggregated data.
        # This keeps the input token count manageable for large review sets.
        batch_data = [
            {
                "theme_counts": br.theme_counts,
                "sentiment_counts": {k.value: v for k, v in br.sentiment_counts.items()},
                "top_quotes": [q.model_dump() for q in br.top_quotes[:10]],
                "unmet_needs": br.unmet_needs,
            }
            for br in batch_results
        ]

        prompt = SYNTHESIS_PROMPT.format(
            total_reviews=len(reviews),
            query=query,
            n_sources=len(sources_used),
            source_list=source_list,
            batch_results_json=json.dumps(batch_data, indent=1, default=str),
        )

        result = await self.ai.analyze(
            prompt=prompt,
            output_schema=AnalysisResult,
            system_prompt=SYSTEM_PROMPT,
        )

        # Ensure top-level fields are set correctly
        result.query = query
        result.query_type = query_type
        result.total_reviews = len(reviews)
        result.sources_used = sources_used
        result.generated_at = datetime.now()

        return result

    @staticmethod
    def _chunk(items: list, size: int) -> list[list]:
        return [items[i : i + size] for i in range(0, len(items), size)]
