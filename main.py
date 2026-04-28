"""Consumer Insights Synthesizer — CLI entry point."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.logging import RichHandler
from rich.progress import Progress, SpinnerColumn, TextColumn

from analysis.pipeline import AnalysisPipeline
from config.settings import load_settings
from models.schemas import Review, SourceType

console = Console()

ALL_SOURCES = ["reddit", "youtube", "amazon", "trustpilot", "google_maps", "instagram", "quora", "walmart"]
ALL_EXPORTS = ["markdown", "csv", "powerpoint", "excel"]


def _slugify(text: str) -> str:
    return re.sub(r"[^\w\-]+", "_", text.lower()).strip("_")[:60]


def _get_collector(source: str, settings):
    """Lazily import and instantiate a collector."""
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
    elif source == "instagram":
        from collectors.instagram import InstagramCollector
        return InstagramCollector(settings)
    elif source == "quora":
        from collectors.quora import QuoraCollector
        return QuoraCollector(settings)
    elif source == "walmart":
        from collectors.walmart import WalmartCollector
        return WalmartCollector(settings)
    else:
        raise ValueError(f"Unknown source: {source}")


def _get_ai_provider(provider_name: str, settings):
    if provider_name == "claude":
        if not settings.has_claude():
            console.print("[red]Error: ANTHROPIC_API_KEY not set[/red]")
            sys.exit(1)
        from ai.claude_provider import ClaudeProvider
        return ClaudeProvider(api_key=settings.anthropic_api_key)
    elif provider_name == "openai":
        if not settings.has_openai():
            console.print("[red]Error: OPENAI_API_KEY not set[/red]")
            sys.exit(1)
        from ai.openai_provider import OpenAIProvider
        return OpenAIProvider(api_key=settings.openai_api_key)
    else:
        raise ValueError(f"Unknown AI provider: {provider_name}")


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
    else:
        raise ValueError(f"Unknown export format: {fmt}")


async def _run(
    query: str,
    query_type: str,
    ai_provider_name: str,
    sources: list[str],
    export_formats: list[str],
    max_reviews: int,
    output_dir: str,
    skip_collection: bool,
):
    settings = load_settings()
    slug = _slugify(query)
    out_path = Path(output_dir) / slug
    out_path.mkdir(parents=True, exist_ok=True)
    reviews_cache = out_path / "reviews.json"

    reviews: list[Review] = []

    # --- Collection ---
    if skip_collection and reviews_cache.exists():
        console.print(f"[cyan]Loading cached reviews from {reviews_cache}[/cyan]")
        raw = json.loads(reviews_cache.read_text(encoding="utf-8"))
        reviews = [Review.model_validate(r) for r in raw]
    else:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
        ) as progress:
            for source_name in sources:
                task = progress.add_task(f"Collecting from {source_name}...", total=None)
                try:
                    collector = _get_collector(source_name, settings)
                    if not collector.is_available():
                        console.print(f"[yellow]Skipping {source_name} — not configured[/yellow]")
                        progress.remove_task(task)
                        continue
                    batch = await collector.collect(query, max_results=max_reviews)
                    reviews.extend(batch)
                    progress.update(task, description=f"[green]{source_name}: {len(batch)} reviews[/green]")
                except Exception as e:
                    console.print(f"[red]Error collecting from {source_name}: {e}[/red]")
                    progress.remove_task(task)

        # Save checkpoint
        reviews_cache.write_text(
            json.dumps([r.model_dump(mode="json") for r in reviews], indent=1, default=str),
            encoding="utf-8",
        )
        console.print(f"\n[green]Collected {len(reviews)} total reviews. Saved to {reviews_cache}[/green]")

    if not reviews:
        console.print("[red]No reviews collected. Exiting.[/red]")
        return

    # --- Analysis ---
    console.print(f"\n[bold]Analyzing {len(reviews)} reviews with {ai_provider_name}...[/bold]")
    ai = _get_ai_provider(ai_provider_name, settings)
    pipeline = AnalysisPipeline(ai_provider=ai, batch_size=settings.default_batch_size)

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task("Running AI analysis...", total=None)
        result = await pipeline.run(query, query_type, reviews)
        progress.update(task, description="[green]Analysis complete[/green]")

    # Save raw analysis result
    analysis_path = out_path / "analysis.json"
    analysis_path.write_text(
        result.model_dump_json(indent=2),
        encoding="utf-8",
    )

    # --- Export ---
    console.print(f"\n[bold]Exporting results...[/bold]")
    for fmt in export_formats:
        try:
            exporter = _get_exporter(fmt)
            files = exporter.export(result, reviews, out_path)
            for f in files:
                console.print(f"  [green]Created: {f}[/green]")
        except Exception as e:
            console.print(f"  [red]Error exporting {fmt}: {e}[/red]")

    console.print(f"\n[bold green]Done! Results saved to {out_path}[/bold green]")


@click.command()
@click.argument("query")
@click.option(
    "--type", "query_type",
    type=click.Choice(["product", "brand"]),
    default="product",
    help="Whether the query is a product type or specific brand.",
)
@click.option(
    "--ai", "ai_provider",
    type=click.Choice(["claude", "openai"]),
    default=None,
    help="AI provider to use for analysis.",
)
@click.option(
    "--sources", "sources",
    multiple=True,
    type=click.Choice(ALL_SOURCES),
    default=None,
    help="Data sources to collect from. Defaults to all available.",
)
@click.option(
    "--export", "export_formats",
    multiple=True,
    type=click.Choice(ALL_EXPORTS),
    default=("markdown",),
    help="Export formats. Can be specified multiple times.",
)
@click.option("--max-reviews", default=500, help="Max reviews to collect per source.")
@click.option("--output-dir", default="output", help="Output directory.")
@click.option("--skip-collection", is_flag=True, help="Skip collection and use cached reviews.")
def main(
    query: str,
    query_type: str,
    ai_provider: str | None,
    sources: tuple[str, ...],
    export_formats: tuple[str, ...],
    max_reviews: int,
    output_dir: str,
    skip_collection: bool,
):
    """Analyze consumer insights for QUERY (a product type or brand name)."""
    # Set up logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s",
        handlers=[RichHandler(console=console, show_path=False)],
    )

    settings = load_settings()

    # Default AI provider from settings
    if ai_provider is None:
        ai_provider = settings.default_ai_provider

    # Default to all available sources
    if not sources:
        sources = tuple(settings.available_sources)

    console.print(f"\n[bold]Consumer Insights Synthesizer[/bold]")
    console.print(f"Query: [cyan]{query}[/cyan] ({query_type})")
    console.print(f"AI: [cyan]{ai_provider}[/cyan]")
    console.print(f"Sources: [cyan]{', '.join(sources)}[/cyan]")
    console.print(f"Exports: [cyan]{', '.join(export_formats)}[/cyan]")
    console.print()

    asyncio.run(_run(
        query=query,
        query_type=query_type,
        ai_provider_name=ai_provider,
        sources=list(sources),
        export_formats=list(export_formats),
        max_reviews=max_reviews,
        output_dir=output_dir,
        skip_collection=skip_collection,
    ))


if __name__ == "__main__":
    main()
