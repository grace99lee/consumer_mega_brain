from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timezone

import httpx

from collectors.base import BaseCollector
from config.settings import Settings
from models.schemas import Review, SourceType

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "en-US,en;q=0.9",
}
_BASE = "https://www.reddit.com"


class RedditCollector(BaseCollector):
    source_type = SourceType.REDDIT

    def is_available(self) -> bool:
        return True  # Uses public JSON API — no credentials needed

    async def collect(self, query: str, max_results: int = 200) -> list[Review]:
        reviews: list[Review] = []
        seen_ids: set[str] = set()

        async with httpx.AsyncClient(headers=_HEADERS, timeout=20, follow_redirects=True) as client:
            # Search posts
            posts = await self._search_posts(client, query, min(max_results, 100))

            for post in posts:
                data = post.get("data", {})
                post_id = data.get("id", "")

                # Include the post body if substantive
                selftext = data.get("selftext", "").strip()
                if selftext and selftext not in ("[deleted]", "[removed]") and len(selftext) > 20:
                    review = self._post_to_review(data)
                    if review.id not in seen_ids:
                        reviews.append(review)
                        seen_ids.add(review.id)

                # Fetch top comments for this post
                if len(reviews) < max_results and post_id:
                    try:
                        comments = await self._fetch_comments(client, data.get("permalink", ""), post_id, data)
                        for c in comments:
                            if c.id not in seen_ids:
                                reviews.append(c)
                                seen_ids.add(c.id)
                    except Exception:
                        logger.debug("Could not fetch comments for post %s", post_id)

                    await asyncio.sleep(random.uniform(0.5, 1.2))

                if len(reviews) >= max_results:
                    break

        logger.info("Collected %d reviews from Reddit", len(reviews))
        return reviews[:max_results]

    async def _search_posts(self, client: httpx.AsyncClient, query: str, limit: int) -> list[dict]:
        url = f"{_BASE}/search.json"
        params = {"q": query, "sort": "relevance", "limit": min(limit, 100), "type": "link"}
        try:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            return resp.json().get("data", {}).get("children", [])
        except Exception:
            logger.exception("Reddit search failed for query: %s", query)
            return []

    async def _fetch_comments(
        self, client: httpx.AsyncClient, permalink: str, post_id: str, post_data: dict
    ) -> list[Review]:
        if not permalink:
            return []
        url = f"{_BASE}{permalink}.json"
        try:
            resp = await client.get(url, params={"limit": 20, "depth": 1, "sort": "top"})
            resp.raise_for_status()
            payload = resp.json()
            if len(payload) < 2:
                return []
            comments = payload[1].get("data", {}).get("children", [])
            reviews = []
            for c in comments:
                cdata = c.get("data", {})
                body = cdata.get("body", "").strip()
                if not body or body in ("[deleted]", "[removed]") or len(body) < 20:
                    continue
                reviews.append(Review(
                    id=f"reddit_comment_{cdata.get('id', '')}",
                    source=SourceType.REDDIT,
                    author=cdata.get("author"),
                    text=body,
                    rating=None,
                    date=datetime.fromtimestamp(cdata["created_utc"], tz=timezone.utc) if cdata.get("created_utc") else None,
                    url=f"https://reddit.com{permalink}",
                    product_name=None,
                    metadata={
                        "subreddit": cdata.get("subreddit", ""),
                        "post_title": post_data.get("title", ""),
                        "score": cdata.get("score", 0),
                        "type": "comment",
                    },
                ))
            return reviews
        except Exception:
            logger.debug("Failed to fetch comments from %s", permalink)
            return []

    def _post_to_review(self, data: dict) -> Review:
        created = data.get("created_utc")
        return Review(
            id=f"reddit_post_{data.get('id', '')}",
            source=SourceType.REDDIT,
            author=data.get("author"),
            text=data.get("selftext", "").strip(),
            rating=None,
            date=datetime.fromtimestamp(created, tz=timezone.utc) if created else None,
            url=f"https://reddit.com{data.get('permalink', '')}",
            product_name=None,
            metadata={
                "subreddit": data.get("subreddit", ""),
                "title": data.get("title", ""),
                "score": data.get("score", 0),
                "num_comments": data.get("num_comments", 0),
                "type": "post",
            },
        )
