from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


@dataclass
class Settings:
    # Reddit
    reddit_client_id: str = ""
    reddit_client_secret: str = ""
    reddit_user_agent: str = "ConsumerInsights/1.0"

    # YouTube
    youtube_api_key: str = ""

    # AI providers
    anthropic_api_key: str = ""
    openai_api_key: str = ""

    # Scraping
    proxy_url: str = ""

    # Defaults
    default_ai_provider: str = "claude"
    default_max_reviews: int = 500
    default_batch_size: int = 50
    output_dir: str = "output"

    def has_reddit(self) -> bool:
        return bool(self.reddit_client_id and self.reddit_client_secret)

    def has_youtube(self) -> bool:
        return bool(self.youtube_api_key)

    def has_claude(self) -> bool:
        return bool(self.anthropic_api_key)

    def has_openai(self) -> bool:
        return bool(self.openai_api_key)

    @property
    def available_sources(self) -> list[str]:
        sources = []
        if self.has_reddit():
            sources.append("reddit")
        if self.has_youtube():
            sources.append("youtube")
        # Playwright-based collectors don't need API keys
        sources.extend(["amazon", "trustpilot", "google_maps", "instagram"])
        return sources


def load_settings(env_path: str | Path | None = None) -> Settings:
    """Load settings from .env file and environment variables."""
    if env_path:
        load_dotenv(env_path)
    else:
        load_dotenv()

    return Settings(
        reddit_client_id=os.getenv("REDDIT_CLIENT_ID", ""),
        reddit_client_secret=os.getenv("REDDIT_CLIENT_SECRET", ""),
        reddit_user_agent=os.getenv("REDDIT_USER_AGENT", "ConsumerInsights/1.0"),
        youtube_api_key=os.getenv("YOUTUBE_API_KEY", ""),
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
        openai_api_key=os.getenv("OPENAI_API_KEY", ""),
        proxy_url=os.getenv("PROXY_URL", ""),
        default_ai_provider=os.getenv("DEFAULT_AI_PROVIDER", "claude"),
        default_max_reviews=int(os.getenv("DEFAULT_MAX_REVIEWS", "500")),
        default_batch_size=int(os.getenv("DEFAULT_BATCH_SIZE", "50")),
        output_dir=os.getenv("OUTPUT_DIR", "output"),
    )
