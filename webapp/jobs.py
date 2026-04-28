"""In-memory job store for async analysis jobs."""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import uuid
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import AsyncIterator

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# Make sure project root is on sys.path when run via uvicorn
_root = Path(__file__).parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))


class JobStatus(str, Enum):
    PENDING = "pending"
    COLLECTING = "collecting"
    ANALYZING = "analyzing"
    EXPORTING = "exporting"
    DONE = "done"
    ERROR = "error"


class ProgressEvent(BaseModel):
    type: str  # "log" | "status" | "done" | "error"
    message: str
    status: JobStatus | None = None
    data: dict | None = None


class JobState(BaseModel):
    job_id: str
    query: str
    query_type: str
    sources: list[str]
    export_formats: list[str]
    max_reviews: int
    ai_provider: str
    status: JobStatus = JobStatus.PENDING
    created_at: datetime = Field(default_factory=datetime.now)
    finished_at: datetime | None = None
    error: str | None = None
    output_dir: str = ""
    result_json: str | None = None
    review_count: int = 0
    events: list[dict] = Field(default_factory=list)


# Global job store
_jobs: dict[str, JobState] = {}
_queues: dict[str, asyncio.Queue] = {}


def create_job(
    query: str,
    query_type: str,
    sources: list[str],
    export_formats: list[str],
    max_reviews: int,
    ai_provider: str,
) -> JobState:
    job_id = str(uuid.uuid4())
    job = JobState(
        job_id=job_id,
        query=query,
        query_type=query_type,
        sources=sources,
        export_formats=export_formats,
        max_reviews=max_reviews,
        ai_provider=ai_provider,
    )
    _jobs[job_id] = job
    _queues[job_id] = asyncio.Queue()
    return job


def get_job(job_id: str) -> JobState | None:
    return _jobs.get(job_id)


def list_jobs() -> list[JobState]:
    return sorted(_jobs.values(), key=lambda j: j.created_at, reverse=True)


async def emit(job_id: str, event: ProgressEvent):
    """Push an event to the job's SSE queue and append to history."""
    job = _jobs.get(job_id)
    if job:
        job.events.append(event.model_dump())
    q = _queues.get(job_id)
    if q:
        await q.put(event)


async def sse_stream(job_id: str) -> AsyncIterator[str]:
    """Yield SSE-formatted strings for the given job."""
    q = _queues.get(job_id)
    if not q:
        yield f"data: {json.dumps({'type': 'error', 'message': 'Job not found'})}\n\n"
        return

    job = _jobs.get(job_id)

    # Replay history for late-connecting clients
    if job:
        for ev in job.events:
            yield f"data: {json.dumps(ev)}\n\n"
        if job.status in (JobStatus.DONE, JobStatus.ERROR):
            return

    # Stream live events
    while True:
        try:
            event: ProgressEvent = await asyncio.wait_for(q.get(), timeout=30.0)
            yield f"data: {event.model_dump_json()}\n\n"
            if event.type in ("done", "error"):
                break
        except asyncio.TimeoutError:
            yield f"data: {json.dumps({'type': 'ping'})}\n\n"


async def run_job(job_id: str):
    """Execute the full collection + analysis + export pipeline for a job."""
    import re
    from analysis.pipeline import AnalysisPipeline
    from config.settings import load_settings
    from models.schemas import Review

    job = _jobs.get(job_id)
    if not job:
        return

    settings = load_settings()

    def _slugify(text: str) -> str:
        return re.sub(r"[^\w\-]+", "_", text.lower()).strip("_")[:60]

    slug = _slugify(job.query)
    out_path = Path(settings.output_dir) / slug
    out_path.mkdir(parents=True, exist_ok=True)
    job.output_dir = str(out_path)

    async def log(msg: str, status: JobStatus | None = None):
        if status:
            job.status = status
        ev = ProgressEvent(type="log", message=msg, status=job.status)
        await emit(job_id, ev)
        logger.info("[job %s] %s", job_id[:8], msg)

    try:
        # --- Collection ---
        job.status = JobStatus.COLLECTING
        await log("Starting data collection...", JobStatus.COLLECTING)

        reviews: list[Review] = []

        for source_name in job.sources:
            await log(f"Collecting from {source_name}...")
            try:
                collector = _get_collector(source_name, settings)
                if not collector.is_available():
                    await log(f"  ⚠ {source_name} — not configured, skipping")
                    continue
                batch = await collector.collect(job.query, max_results=job.max_reviews)
                reviews.extend(batch)
                await log(f"  ✓ {source_name}: {len(batch)} items collected")
            except Exception as e:
                await log(f"  ✗ {source_name} error: {e}")

        job.review_count = len(reviews)
        await log(f"Collection complete: {len(reviews)} total items")

        if not reviews:
            raise ValueError("No reviews collected from any source. Check your API keys and network.")

        # Cache reviews
        reviews_cache = out_path / "reviews.json"
        reviews_cache.write_text(
            json.dumps([r.model_dump(mode="json") for r in reviews], indent=1, default=str),
            encoding="utf-8",
        )

        # --- Analysis ---
        await log("Starting AI analysis...", JobStatus.ANALYZING)

        ai = _get_ai_provider(job.ai_provider, settings)
        if ai is None:
            raise ValueError(f"AI provider '{job.ai_provider}' not configured")

        pipeline = AnalysisPipeline(ai_provider=ai, batch_size=settings.default_batch_size)

        n_batches = max(1, len(reviews) // settings.default_batch_size)
        await log(f"Analyzing {len(reviews)} items in ~{n_batches} batches...")

        result = await pipeline.run(job.query, job.query_type, reviews)
        await log("AI analysis complete")

        # Save analysis JSON
        analysis_path = out_path / "analysis.json"
        job.result_json = result.model_dump_json(indent=2)
        analysis_path.write_text(job.result_json, encoding="utf-8")

        # --- Export ---
        await log("Exporting results...", JobStatus.EXPORTING)
        for fmt in job.export_formats:
            try:
                exporter = _get_exporter(fmt)
                files = exporter.export(result, reviews, out_path)
                for f in files:
                    await log(f"  ✓ Exported: {f.name}")
            except Exception as e:
                await log(f"  ✗ Export error ({fmt}): {e}")

        job.status = JobStatus.DONE
        job.finished_at = datetime.now()

        done_event = ProgressEvent(
            type="done",
            message=f"Analysis complete! {len(reviews)} items analyzed.",
            status=JobStatus.DONE,
            data={"output_dir": str(out_path), "review_count": len(reviews)},
        )
        await emit(job_id, done_event)

    except Exception as e:
        job.status = JobStatus.ERROR
        job.error = str(e)
        job.finished_at = datetime.now()
        err_event = ProgressEvent(
            type="error",
            message=f"Job failed: {e}",
            status=JobStatus.ERROR,
        )
        await emit(job_id, err_event)
        logger.exception("Job %s failed", job_id)


def _get_collector(source: str, settings):
    if source == "reddit":
        from collectors.reddit import RedditCollector
        return RedditCollector(settings)
    elif source == "youtube":
        from collectors.youtube import YouTubeCollector
        return YouTubeCollector(settings)
    elif source == "amazon":
        from collectors.amazon import AmazonCollector
        return AmazonCollector(settings)
    elif source == "trustpilot":
        from collectors.trustpilot import TrustpilotCollector
        return TrustpilotCollector(settings)
    elif source == "google_maps":
        from collectors.google_maps import GoogleMapsCollector
        return GoogleMapsCollector(settings)
    elif source == "quora":
        from collectors.quora import QuoraCollector
        return QuoraCollector(settings)
    raise ValueError(f"Unknown source: {source}")


def _get_ai_provider(provider_name: str, settings):
    if provider_name == "claude":
        if not settings.has_claude():
            return None
        from ai.claude_provider import ClaudeProvider
        return ClaudeProvider(api_key=settings.anthropic_api_key)
    elif provider_name == "openai":
        if not settings.has_openai():
            return None
        from ai.openai_provider import OpenAIProvider
        return OpenAIProvider(api_key=settings.openai_api_key)
    return None


def _get_exporter(fmt: str):
    if fmt == "markdown":
        from exporters.markdown import MarkdownExporter
        return MarkdownExporter()
    elif fmt == "csv":
        from exporters.csv_export import CSVExporter
        return CSVExporter()
    elif fmt == "powerpoint":
        from exporters.powerpoint import PowerPointExporter
        return PowerPointExporter()
    elif fmt == "excel":
        from exporters.excel import ExcelExporter
        return ExcelExporter()
    raise ValueError(f"Unknown format: {fmt}")
