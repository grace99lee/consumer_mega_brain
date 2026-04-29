"""FastAPI webapp for Consumer Insights Synthesizer."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# Ensure project root is importable
_root = Path(__file__).parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from webapp.jobs import (
    create_job, get_job, list_jobs, run_job, sse_stream, JobStatus
)

app = FastAPI(title="Consumer Insights Synthesizer", version="0.1.0")

# Mount static files
_static = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(_static)), name="static")

ALL_SOURCES = ["reddit", "youtube", "amazon", "trustpilot", "quora"]
ALL_EXPORTS = ["markdown", "csv", "powerpoint", "excel"]


# --- Request / Response models ---

class AnalyzeRequest(BaseModel):
    query: str
    query_type: str = "product"  # "product" | "brand"
    ai_provider: str = "claude"  # "claude" | "openai"
    sources: list[str] = ALL_SOURCES
    export_formats: list[str] = ["markdown", "excel"]
    max_reviews: int = 200


class JobSummary(BaseModel):
    job_id: str
    query: str
    query_type: str
    status: str
    review_count: int
    created_at: str
    finished_at: str | None
    error: str | None


# --- Routes ---

@app.get("/", response_class=HTMLResponse)
async def root():
    index = Path(__file__).parent / "static" / "index.html"
    return HTMLResponse(index.read_text(encoding="utf-8"))


@app.post("/api/analyze")
async def start_analysis(req: AnalyzeRequest, background_tasks: BackgroundTasks):
    if not req.query.strip():
        raise HTTPException(status_code=400, detail="Query cannot be empty")

    valid_sources = [s for s in req.sources if s in ALL_SOURCES]
    if not valid_sources:
        raise HTTPException(status_code=400, detail="No valid sources selected")

    valid_exports = [e for e in req.export_formats if e in ALL_EXPORTS]
    if not valid_exports:
        valid_exports = ["markdown"]

    job = create_job(
        query=req.query.strip(),
        query_type=req.query_type,
        sources=valid_sources,
        export_formats=valid_exports,
        max_reviews=min(max(10, req.max_reviews), 1000),
        ai_provider=req.ai_provider,
    )

    background_tasks.add_task(run_job, job.job_id)

    return {"job_id": job.job_id, "status": job.status}


@app.get("/api/jobs")
async def get_jobs():
    return [
        JobSummary(
            job_id=j.job_id,
            query=j.query,
            query_type=j.query_type,
            status=j.status.value,
            review_count=j.review_count,
            created_at=j.created_at.isoformat(),
            finished_at=j.finished_at.isoformat() if j.finished_at else None,
            error=j.error,
        )
        for j in list_jobs()
    ]


@app.get("/api/jobs/{job_id}")
async def get_job_detail(job_id: str):
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return {
        "job_id": job.job_id,
        "query": job.query,
        "query_type": job.query_type,
        "status": job.status.value,
        "sources": job.sources,
        "export_formats": job.export_formats,
        "ai_provider": job.ai_provider,
        "review_count": job.review_count,
        "created_at": job.created_at.isoformat(),
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "error": job.error,
        "output_dir": job.output_dir,
        "result": json.loads(job.result_json) if job.result_json else None,
    }


@app.get("/api/jobs/{job_id}/stream")
async def stream_job(job_id: str):
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    async def event_generator():
        async for chunk in sse_stream(job_id):
            yield chunk

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/jobs/{job_id}/export/{format}")
async def download_export(job_id: str, format: str):
    import io
    import zipfile
    from webapp.jobs import _get_exporter

    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status != JobStatus.DONE:
        raise HTTPException(status_code=400, detail="Job not complete")

    out_path = Path(job.output_dir)

    format_files = {
        "markdown": "report.md",
        "excel": "report.xlsx",
        "powerpoint": "report.pptx",
        "csv": None,  # multiple files — zip them
    }

    if format not in format_files:
        raise HTTPException(status_code=400, detail=f"Unknown format: {format}")

    def _regenerate_export(fmt: str) -> None:
        """Regenerate an export file from persisted analysis.json + reviews.json."""
        from models.schemas import AnalysisResult, Review

        analysis_path = out_path / "analysis.json"
        if not analysis_path.exists():
            raise FileNotFoundError("analysis.json not found — cannot regenerate export")

        result_json = analysis_path.read_text(encoding="utf-8")
        result = AnalysisResult.model_validate_json(result_json)

        reviews: list[Review] = []
        reviews_path = out_path / "reviews.json"
        if reviews_path.exists():
            reviews_data = json.loads(reviews_path.read_text(encoding="utf-8"))
            reviews = [Review.model_validate(r) for r in reviews_data]

        exporter = _get_exporter(fmt)
        exporter.export(result, reviews, out_path)

    filename = format_files[format]
    if filename:
        file_path = out_path / filename
        if not file_path.exists():
            # Try to regenerate from persisted analysis data
            try:
                _regenerate_export(format)
            except Exception as exc:
                raise HTTPException(
                    status_code=404,
                    detail=f"Export file not found and could not be regenerated: {exc}",
                )
        if not file_path.exists():
            raise HTTPException(status_code=404, detail=f"Export file not found: {filename}")
        return FileResponse(
            str(file_path),
            filename=f"{job.query[:40]}_{format}.{filename.split('.')[-1]}",
        )
    else:
        # CSV: zip all .csv files, regenerating if needed
        csv_files = list(out_path.glob("*.csv"))
        if not csv_files:
            try:
                _regenerate_export("csv")
                csv_files = list(out_path.glob("*.csv"))
            except Exception as exc:
                raise HTTPException(
                    status_code=404,
                    detail=f"No CSV files found and could not be regenerated: {exc}",
                )
        if not csv_files:
            raise HTTPException(status_code=404, detail="No CSV files found")

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in csv_files:
                zf.write(f, f.name)
        buf.seek(0)
        return StreamingResponse(
            buf,
            media_type="application/zip",
            headers={"Content-Disposition": f"attachment; filename={job.query[:40]}_csv.zip"},
        )
