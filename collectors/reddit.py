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
        return True  # Always try; PRAW used when credentials are set

    async def collect(self, query: str, max_results: int = 200) -> list[Review]:
        """Use PRAW (OAuth) when credentials are configured, else fall back to public API."""
        if self.settings.has_reddit():
            logger.info("Reddit: using PRAW (OAuth) collection")
            return await asyncio.to_thread(self._collect_praw, query, max_results)
        else:
            logger.info("Reddit: using public JSON API (add REDDIT_CLIENT_ID/SECRET for better cloud results)")
            return await self._collect_httpx(query, max_results)

    # --- PRAW (OAuth) — works reliably from cloud IPs ---

    def _collect_praw(self, query: str, max_results: int) -> list[Review]:
        import praw

        reddit = praw.Reddit(
            client_id=self.settings.reddit_client_id,
            client_secret=self.settings.reddit_client_secret,
            user_agent=self.settings.reddit_user_agent,
        )

        reviews: list[Review] = []
        seen_ids: set[str] = set()

        try:
            results = reddit.subreddit("all").search(
                query, sort="relevance", time_filter="year", limit=min(max_results, 100)
            )
            for post in results:
                if len(reviews) >= max_results:
                    break

                # Post body
                if post.selftext and post.selftext not in ("[deleted]", "[removed]") and len(post.selftext) > 20:
                    r = Review(
                        id=f"reddit_post_{post.id}",
                        source=SourceType.REDDIT,
                        author=str(post.author) if post.author else None,
                        text=post.selftext,
                        rating=None,
                        date=datetime.fromtimestamp(post.created_utc, tz=timezone.utc),
                        url=f"https://reddit.com{post.permalink}",
                        product_name=None,
                        metadata={
                            "subreddit": str(post.subreddit),
                            "title": post.title,
                            "score": post.score,
                            "num_comments": post.num_comments,
                            "type": "post",
                        },
                    )
                    if r.id not in seen_ids:
                        reviews.append(r)
                        seen_ids.add(r.id)

                # Top comments
                try:
                    post.comments.replace_more(limit=0)
                    for comment in post.comments[:10]:
                        if len(reviews) >= max_results:
                            break
                        body = getattr(comment, "body", "").strip()
                        if not body or body in ("[deleted]", "[removed]") or len(body) < 20:
                            continue
                        r = Review(
                            id=f"reddit_comment_{comment.id}",
                            source=SourceType.REDDIT,
                            author=str(comment.author) if comment.author else None,
                            text=body,
                            rating=None,
                            date=datetime.fromtimestamp(comment.created_utc, tz=timezone.utc),
                            url=f"https://reddit.com{post.permalink}",
                            product_name=None,
                            metadata={
                                "subreddit": str(post.subreddit),
                                "post_title": post.title,
                                "score": comment.score,
                                "type": "comment",
                            },
                        )
                        if r.id not in seen_ids:
                            reviews.append(r)
                            seen_ids.add(r.id)
                except Exception:
                    pass

        except Exception:
            logger.exception("PRAW Reddit collection failed for: %s", query)

        logger.info("Collected %d reviews from Reddit (PRAW)", len(reviews))
        return reviews[:max_results]

    # --- Public JSON API — works locally, may be blocked on cloud IPs ---

    async def _collect_httpx(self, query: str, max_results: int) -> list[Review]:
        reviews: list[Review] = []
        seen_ids: set[str] = set()

        try:
            async with httpx.AsyncClient(headers=_HEADERS, timeout=20, follow_redirects=True) as client:
                posts = await self._search_posts(client, query, min(max_results, 100))

                for post in posts:
                    data = post.get("data", {})
                    post_id = data.get("id", "")

                    selftext = data.get("selftext", "").strip()
                    if selftext and selftext not in ("[deleted]", "[removed]") and len(selftext) > 20:
                        review = self._post_to_review(data)
                        if review.id not in seen_ids:
                            reviews.append(review)
                            seen_ids.add(review.id)

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

        except Exception:
            logger.exception("httpx Reddit collection failed for: %s", query)

        logger.info("Collected %d reviews from Reddit (httpx)", len(reviews))
        return reviews[:max_results]

    async def _search_posts(self, client: httpx.AsyncClient, query: str, limit: int) -> list[dict]:
        url = f"{_BASE}/search.json"
        params = {"q": query, "sort": "relevance", "limit": min(limit, 100), "type": "link"}
        try:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            return resp.json().get("data", {}).get("children", [])
        except Exception:
            logger.warning("Reddit public API search failed (likely blocked on cloud IP) for: %s", query)
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
