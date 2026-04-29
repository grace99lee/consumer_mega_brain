from __future__ import annotations

import asyncio
import logging
import random
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from urllib.parse import quote_plus

import httpx

from collectors.base import BaseCollector
from config.settings import Settings
from models.schemas import Review, SourceType

logger = logging.getLogger(__name__)

# Atom namespace Reddit uses in its RSS feeds
_ATOM = "http://www.w3.org/2005/Atom"

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
    "Accept-Language": "en-US,en;q=0.9",
}


class RedditCollector(BaseCollector):
    source_type = SourceType.REDDIT

    def is_available(self) -> bool:
        return True

    async def collect(self, query: str, max_results: int = 200) -> list[Review]:
        """
        Permanent approach: Reddit RSS feed for search + per-post JSON for comments.
        RSS has been stable for 15+ years and is explicitly designed for machine
        consumption. Per-post JSON (/r/sub/comments/id.json) is far less rate-limited
        than the search API. Neither requires credentials or a browser.
        """
        reviews: list[Review] = []
        seen_ids: set[str] = set()

        async with httpx.AsyncClient(headers=_HEADERS, timeout=20, follow_redirects=True) as client:
            posts = await self._rss_search(client, query, limit=25)
            logger.debug("Reddit RSS returned %d posts for: %s", len(posts), query)

            if not posts:
                logger.warning("Reddit RSS returned no posts for '%s'", query)
                return []

            for post in posts[:12]:
                if len(reviews) >= max_results:
                    break

                # Add the post body if substantive
                if post.get("body") and len(post["body"]) > 20:
                    rid = f"reddit_post_{post['id']}"
                    if rid not in seen_ids:
                        reviews.append(Review(
                            id=rid,
                            source=SourceType.REDDIT,
                            author=post.get("author"),
                            text=post["body"],
                            rating=None,
                            date=post.get("date"),
                            url=post["url"],
                            product_name=None,
                            metadata={
                                "subreddit": post.get("subreddit", ""),
                                "title": post.get("title", ""),
                                "type": "post",
                            },
                        ))
                        seen_ids.add(rid)

                # Fetch top comments via per-post JSON
                if len(reviews) < max_results:
                    try:
                        comments = await self._fetch_post_comments(client, post)
                        for c in comments:
                            if len(reviews) >= max_results:
                                break
                            if c.id not in seen_ids:
                                reviews.append(c)
                                seen_ids.add(c.id)
                    except Exception:
                        logger.debug("Could not fetch comments for %s", post["url"])

                await asyncio.sleep(random.uniform(0.5, 1.0))

        logger.info("Collected %d reviews from Reddit", len(reviews))
        return reviews[:max_results]

    async def _rss_search(self, client: httpx.AsyncClient, query: str, limit: int) -> list[dict]:
        """Fetch Reddit's search RSS feed — stable, cloud-friendly, no credentials needed."""
        url = f"https://www.reddit.com/search.rss?q={quote_plus(query)}&sort=relevance&t=year&limit={min(limit, 25)}"
        try:
            resp = await client.get(url)
            resp.raise_for_status()
        except Exception as exc:
            logger.warning("Reddit RSS fetch failed: %s", exc)
            return []

        posts = []
        try:
            root = ET.fromstring(resp.text)
            for entry in root.findall(f"{{{_ATOM}}}entry"):
                # Extract fields from Atom entry
                title = _atom_text(entry, "title")
                link_el = entry.find(f"{{{_ATOM}}}link")
                url = link_el.get("href", "") if link_el is not None else ""
                author_el = entry.find(f"{{{_ATOM}}}author/{{{_ATOM}}}name")
                author = author_el.text.strip() if author_el is not None and author_el.text else None

                date = None
                pub_el = entry.find(f"{{{_ATOM}}}published")
                if pub_el is not None and pub_el.text:
                    try:
                        date = datetime.fromisoformat(pub_el.text.replace("Z", "+00:00"))
                    except ValueError:
                        pass

                # Reddit puts the post selftext in <content>; strip HTML tags
                content_el = entry.find(f"{{{_ATOM}}}content")
                body = ""
                if content_el is not None and content_el.text:
                    body = re.sub(r"<[^>]+>", " ", content_el.text).strip()
                    body = re.sub(r"\s+", " ", body).strip()

                # Extract subreddit and post ID from URL
                # e.g. https://www.reddit.com/r/sub/comments/abc123/title/
                subreddit = ""
                post_id = ""
                m = re.search(r"/r/([^/]+)/comments/([^/]+)/", url)
                if m:
                    subreddit = m.group(1)
                    post_id = m.group(2)

                if url and post_id:
                    posts.append({
                        "id": post_id,
                        "title": title,
                        "body": body,
                        "author": author,
                        "url": url,
                        "subreddit": subreddit,
                        "date": date,
                    })
        except ET.ParseError as exc:
            logger.warning("Reddit RSS XML parse error: %s", exc)

        return posts

    async def _fetch_post_comments(self, client: httpx.AsyncClient, post: dict) -> list[Review]:
        """Fetch top comments for a post using the per-post JSON endpoint."""
        post_id = post.get("id", "")
        subreddit = post.get("subreddit", "")
        if not post_id or not subreddit:
            return []

        url = f"https://www.reddit.com/r/{subreddit}/comments/{post_id}.json?limit=20&depth=1&sort=top"
        try:
            resp = await client.get(url)
            resp.raise_for_status()
            payload = resp.json()
        except Exception:
            return []

        if not isinstance(payload, list) or len(payload) < 2:
            return []

        reviews = []
        children = payload[1].get("data", {}).get("children", [])
        for child in children:
            cdata = child.get("data", {})
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
                url=post["url"],
                product_name=None,
                metadata={
                    "subreddit": subreddit,
                    "post_title": post.get("title", ""),
                    "score": cdata.get("score", 0),
                    "type": "comment",
                },
            ))
        return reviews


def _atom_text(element, tag: str) -> str:
    el = element.find(f"{{{_ATOM}}}{tag}")
    return el.text.strip() if el is not None and el.text else ""
