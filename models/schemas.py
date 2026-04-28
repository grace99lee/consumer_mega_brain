from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator


class SourceType(str, Enum):
    REDDIT = "reddit"
    AMAZON = "amazon"
    INSTAGRAM = "instagram"
    YOUTUBE = "youtube"
    GOOGLE_MAPS = "google_maps"
    TRUSTPILOT = "trustpilot"
    QUORA = "quora"
    WALMART = "walmart"


class SentimentLabel(str, Enum):
    POSITIVE = "positive"
    NEGATIVE = "negative"
    NEUTRAL = "neutral"
    MIXED = "mixed"


class Review(BaseModel):
    """Normalized review from any platform."""

    id: str
    source: SourceType
    author: str | None = None
    text: str
    rating: float | None = None
    date: datetime | None = None
    url: str | None = None
    product_name: str | None = None
    metadata: dict = Field(default_factory=dict)


# --- AI output models ---


def _coerce_source(v: Any) -> SourceType:
    """Accept SourceType enum values, their string equivalents, or fall back to reddit."""
    if isinstance(v, SourceType):
        return v
    try:
        return SourceType(str(v).lower())
    except ValueError:
        return SourceType.REDDIT


def _coerce_sentiment(v: Any) -> SentimentLabel:
    """Accept SentimentLabel enum values, their string equivalents, or fall back to neutral."""
    if isinstance(v, SentimentLabel):
        return v
    try:
        return SentimentLabel(str(v).lower())
    except ValueError:
        return SentimentLabel.NEUTRAL


class TaggedQuote(BaseModel):
    """An exact quote extracted by AI with attribution."""

    quote: str
    source: SourceType = SourceType.REDDIT
    author: str | None = None
    review_id: str = ""
    theme: str = ""
    sentiment: SentimentLabel = SentimentLabel.NEUTRAL

    @field_validator("source", mode="before")
    @classmethod
    def coerce_source(cls, v: Any) -> SourceType:
        return _coerce_source(v)

    @field_validator("sentiment", mode="before")
    @classmethod
    def coerce_sentiment(cls, v: Any) -> SentimentLabel:
        return _coerce_sentiment(v)


class Theme(BaseModel):
    """A theme identified across reviews."""

    name: str
    description: str
    sentiment_breakdown: dict[SentimentLabel, int] = Field(default_factory=dict)
    review_count: int = 0
    representative_quotes: list[TaggedQuote] = Field(default_factory=list)
    source_breakdown: dict[SourceType, int] = Field(default_factory=dict)

    @field_validator("sentiment_breakdown", mode="before")
    @classmethod
    def coerce_sentiment_breakdown(cls, v: Any) -> dict:
        if not isinstance(v, dict):
            return {}
        return {_coerce_sentiment(k): val for k, val in v.items()}

    @field_validator("source_breakdown", mode="before")
    @classmethod
    def coerce_source_breakdown(cls, v: Any) -> dict:
        if not isinstance(v, dict):
            return {}
        return {_coerce_source(k): val for k, val in v.items()}


class UnmetNeed(BaseModel):
    """A consumer need not being met by current products."""

    need: str
    evidence: list[TaggedQuote] = Field(default_factory=list)
    frequency: int = 0
    opportunity_score: float = 0.0


class Persona(BaseModel):
    """A user persona synthesized from review patterns."""

    name: str
    description: str
    demographics_hints: str = ""
    motivations: list[str] = Field(default_factory=list)
    pain_points: list[str] = Field(default_factory=list)
    representative_quotes: list[TaggedQuote] = Field(default_factory=list)
    estimated_prevalence: str = ""


class SentimentSummary(BaseModel):
    overall: SentimentLabel = SentimentLabel.NEUTRAL
    score: float = 0.0
    distribution: dict[SentimentLabel, int] = Field(default_factory=dict)
    by_source: dict[SourceType, float] = Field(default_factory=dict)

    @field_validator("overall", mode="before")
    @classmethod
    def coerce_overall(cls, v: Any) -> SentimentLabel:
        return _coerce_sentiment(v)

    @field_validator("distribution", mode="before")
    @classmethod
    def coerce_distribution(cls, v: Any) -> dict:
        if not isinstance(v, dict):
            return {}
        return {_coerce_sentiment(k): val for k, val in v.items()}

    @field_validator("by_source", mode="before")
    @classmethod
    def coerce_by_source(cls, v: Any) -> dict:
        if not isinstance(v, dict):
            return {}
        return {_coerce_source(k): val for k, val in v.items()}


class AnalysisResult(BaseModel):
    """Top-level output from the AI analysis pipeline."""

    query: str
    query_type: str
    total_reviews: int
    sources_used: list[SourceType] = Field(default_factory=list)
    sentiment: SentimentSummary
    themes: list[Theme] = Field(default_factory=list)
    unmet_needs: list[UnmetNeed] = Field(default_factory=list)
    personas: list[Persona] = Field(default_factory=list)
    key_quotes: list[TaggedQuote] = Field(default_factory=list)
    executive_summary: str = ""
    generated_at: datetime = Field(default_factory=datetime.now)


# --- Intermediate models for batch AI analysis ---


class BatchReviewAnalysis(BaseModel):
    """Per-review extraction from batch analysis pass."""

    review_id: str
    sentiment: SentimentLabel
    themes: list[str] = Field(default_factory=list)
    quotes: list[str] = Field(default_factory=list)
    unmet_needs: list[str] = Field(default_factory=list)


class BatchAnalysisResult(BaseModel):
    """Result of analyzing one batch of reviews."""

    reviews: list[BatchReviewAnalysis] = Field(default_factory=list)
    theme_counts: dict[str, int] = Field(default_factory=dict)
    sentiment_counts: dict[SentimentLabel, int] = Field(default_factory=dict)
    top_quotes: list[TaggedQuote] = Field(default_factory=list)
    unmet_needs: list[str] = Field(default_factory=list)

    @field_validator("theme_counts", mode="before")
    @classmethod
    def coerce_theme_counts(cls, v: Any) -> dict:
        if isinstance(v, dict):
            return v
        # Handle list-of-dicts: [{"theme": "...", "count": 5}] or [["theme", 5]]
        if isinstance(v, list):
            result: dict[str, int] = {}
            for item in v:
                if isinstance(item, dict):
                    k = item.get("theme") or item.get("name") or item.get("key") or ""
                    c = item.get("count") or item.get("value") or item.get("frequency") or 0
                    if k:
                        result[str(k)] = int(c)
                elif isinstance(item, (list, tuple)) and len(item) >= 2:
                    result[str(item[0])] = int(item[1])
            return result
        return {}

    @field_validator("sentiment_counts", mode="before")
    @classmethod
    def coerce_sentiment_counts(cls, v: Any) -> dict:
        if isinstance(v, dict):
            return {_coerce_sentiment(k): val for k, val in v.items()}
        return {}

    @field_validator("unmet_needs", mode="before")
    @classmethod
    def coerce_unmet_needs(cls, v: Any) -> list:
        if isinstance(v, list):
            # Handle list of strings or list of dicts
            result = []
            for item in v:
                if isinstance(item, str):
                    result.append(item)
                elif isinstance(item, dict):
                    text = item.get("need") or item.get("description") or item.get("text") or str(item)
                    result.append(text)
            return result
        return []
