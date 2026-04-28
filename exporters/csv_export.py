from __future__ import annotations

import csv
from pathlib import Path

from exporters.base import BaseExporter
from models.schemas import AnalysisResult, Review


class CSVExporter(BaseExporter):
    def export(
        self,
        result: AnalysisResult,
        reviews: list[Review],
        output_dir: Path,
    ) -> list[Path]:
        output_dir.mkdir(parents=True, exist_ok=True)
        created: list[Path] = []

        # reviews.csv
        reviews_path = output_dir / "reviews.csv"
        with open(reviews_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["id", "source", "author", "text", "rating", "date", "url", "product_name"])
            for r in reviews:
                writer.writerow([
                    r.id, r.source.value, r.author, r.text, r.rating,
                    r.date.isoformat() if r.date else "", r.url, r.product_name,
                ])
        created.append(reviews_path)

        # themes.csv
        themes_path = output_dir / "themes.csv"
        with open(themes_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["theme", "description", "review_count", "positive", "negative", "neutral", "mixed"])
            for t in result.themes:
                writer.writerow([
                    t.name, t.description, t.review_count,
                    t.sentiment_breakdown.get("positive", 0),
                    t.sentiment_breakdown.get("negative", 0),
                    t.sentiment_breakdown.get("neutral", 0),
                    t.sentiment_breakdown.get("mixed", 0),
                ])
        created.append(themes_path)

        # quotes.csv
        quotes_path = output_dir / "quotes.csv"
        with open(quotes_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["quote", "source", "author", "review_id", "theme", "sentiment"])
            for q in result.key_quotes:
                writer.writerow([
                    q.quote, q.source.value, q.author, q.review_id, q.theme, q.sentiment.value,
                ])
        created.append(quotes_path)

        # unmet_needs.csv
        needs_path = output_dir / "unmet_needs.csv"
        with open(needs_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["need", "frequency", "opportunity_score"])
            for n in result.unmet_needs:
                writer.writerow([n.need, n.frequency, n.opportunity_score])
        created.append(needs_path)

        # personas.csv
        personas_path = output_dir / "personas.csv"
        with open(personas_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["name", "description", "demographics", "motivations", "pain_points", "prevalence"])
            for p in result.personas:
                writer.writerow([
                    p.name, p.description, p.demographics_hints,
                    "; ".join(p.motivations), "; ".join(p.pain_points),
                    p.estimated_prevalence,
                ])
        created.append(personas_path)

        return created
