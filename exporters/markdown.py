from __future__ import annotations

from pathlib import Path

from exporters.base import BaseExporter
from models.schemas import AnalysisResult, Review, SentimentLabel


class MarkdownExporter(BaseExporter):
    def export(
        self,
        result: AnalysisResult,
        reviews: list[Review],
        output_dir: Path,
    ) -> list[Path]:
        output_dir.mkdir(parents=True, exist_ok=True)
        report_path = output_dir / "report.md"

        lines: list[str] = []
        _a = lines.append

        # Header
        _a(f"# Consumer Insights Report: {result.query}")
        _a(f"")
        _a(f"**Type**: {result.query_type} | **Reviews analyzed**: {result.total_reviews} | "
           f"**Sources**: {', '.join(s.value for s in result.sources_used)}")
        _a(f"**Generated**: {result.generated_at:%Y-%m-%d %H:%M}")
        _a("")

        # Executive Summary
        _a("## Executive Summary")
        _a("")
        _a(result.executive_summary)
        _a("")

        # Sentiment
        _a("## Overall Sentiment")
        _a("")
        s = result.sentiment
        _a(f"**Overall**: {s.overall.value} (score: {s.score:+.2f})")
        _a("")
        if s.distribution:
            _a("| Sentiment | Count |")
            _a("|-----------|-------|")
            for label, count in s.distribution.items():
                _a(f"| {label.value if isinstance(label, SentimentLabel) else label} | {count} |")
            _a("")
        if s.by_source:
            _a("**By source**:")
            for source, score in s.by_source.items():
                _a(f"- {source.value if hasattr(source, 'value') else source}: {score:+.2f}")
            _a("")

        # Themes
        _a("## Key Themes")
        _a("")
        for i, theme in enumerate(result.themes, 1):
            _a(f"### {i}. {theme.name} ({theme.review_count} mentions)")
            _a("")
            _a(theme.description)
            _a("")
            if theme.representative_quotes:
                for q in theme.representative_quotes[:3]:
                    _a(f"> \"{q.quote}\" — *{q.author or 'Anonymous'}, {q.source.value}*")
                    _a("")

        # Unmet Needs
        _a("## Unmet Needs")
        _a("")
        if result.unmet_needs:
            _a("| Need | Frequency | Opportunity Score |")
            _a("|------|-----------|-------------------|")
            for need in result.unmet_needs:
                _a(f"| {need.need} | {need.frequency} | {need.opportunity_score:.2f} |")
            _a("")

            for need in result.unmet_needs:
                _a(f"**{need.need}** (opportunity: {need.opportunity_score:.2f})")
                _a("")
                for q in need.evidence[:2]:
                    _a(f"> \"{q.quote}\" — *{q.author or 'Anonymous'}, {q.source.value}*")
                    _a("")

        # Personas
        _a("## Consumer Personas")
        _a("")
        for persona in result.personas:
            _a(f"### {persona.name}")
            _a("")
            _a(persona.description)
            _a("")
            if persona.demographics_hints:
                _a(f"**Demographics**: {persona.demographics_hints}")
            _a(f"**Estimated prevalence**: {persona.estimated_prevalence}")
            _a("")
            if persona.motivations:
                _a("**Motivations**:")
                for m in persona.motivations:
                    _a(f"- {m}")
                _a("")
            if persona.pain_points:
                _a("**Pain points**:")
                for p in persona.pain_points:
                    _a(f"- {p}")
                _a("")
            if persona.representative_quotes:
                for q in persona.representative_quotes[:2]:
                    _a(f"> \"{q.quote}\" — *{q.author or 'Anonymous'}, {q.source.value}*")
                    _a("")

        # Key Quotes
        _a("## Key Quotes")
        _a("")
        for q in result.key_quotes:
            _a(f"> \"{q.quote}\"")
            _a(f"> — *{q.author or 'Anonymous'}* ({q.source.value}) | Theme: {q.theme} | Sentiment: {q.sentiment.value}")
            _a("")

        report_path.write_text("\n".join(lines), encoding="utf-8")
        return [report_path]
