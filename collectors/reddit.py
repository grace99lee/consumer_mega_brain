from __future__ import annotations

import asyncio
import logging
import random
import re
from datetime import datetime, timezone

from collectors.base import BaseCollector
from collectors.playwright_utils import run_in_playwright_thread, stealth_browser
from config.settings import Settings
from models.schemas import Review, SourceType

logger = logging.getLogger(__name__)


class RedditCollector(BaseCollector):
    source_type = SourceType.REDDIT

    def is_available(self) -> bool:
        return True

    async def collect(self, query: str, max_results: int = 200) -> list[Review]:
        return await run_in_playwright_thread(
            lambda: self._collect_playwright(query, max_results)
        )

    async def _collect_playwright(self, query: str, max_results: int) -> list[Review]:
        reviews: list[Review] = []
        seen_ids: set[str] = set()

        async with stealth_browser() as (browser, context):
            page = await context.new_page()
            try:
                search_url = (
                    f"https://www.reddit.com/search/?q={query.replace(' ', '+')}"
                    f"&sort=relevance&t=year&type=link"
                )
                logger.debug("Reddit search: %s", search_url)
                await page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(random.uniform(2, 3))

                # Collect post links from search results
                post_links = await self._extract_post_links(page)
                logger.debug("Found %d Reddit post links", len(post_links))

                if not post_links:
                    logger.warning("Reddit: no posts found for '%s'", query)

                for link in post_links[:15]:
                    if len(reviews) >= max_results:
                        break
                    try:
                        batch = await self._scrape_post(page, link, seen_ids)
                        reviews.extend(batch)
                        for r in batch:
                            seen_ids.add(r.id)
                    except Exception as exc:
                        logger.debug("Reddit post failed %s: %s", link, exc)
                    await asyncio.sleep(random.uniform(1, 2))

            except Exception:
                logger.exception("Reddit collection failed for query: %s", query)
            finally:
                await page.close()

        logger.info("Collected %d reviews from Reddit", len(reviews))
        return reviews[:max_results]

    async def _extract_post_links(self, page) -> list[str]:
        """Pull post URLs from a Reddit search results page."""
        try:
            # New Reddit renders posts as <a> tags with /r/.../comments/ paths
            links = await page.eval_on_selector_all(
                'a[href*="/comments/"]',
                'els => [...new Set(els.map(e => e.href))]'
            )
            # Keep only full reddit.com post URLs, strip query params
            clean = []
            seen: set[str] = set()
            for link in links:
                m = re.match(r'(https://www\.reddit\.com/r/[^/]+/comments/[^/?#]+)', link)
                if m and m.group(1) not in seen:
                    seen.add(m.group(1))
                    clean.append(m.group(1))
            return clean[:20]
        except Exception:
            return []

    async def _scrape_post(self, page, url: str, seen_ids: set[str]) -> list[Review]:
        """Visit a Reddit post page and collect the selftext + top comments."""
        reviews: list[Review] = []

        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(random.uniform(1.5, 2.5))

        # Extract post metadata from URL
        # e.g. /r/AskReddit/comments/abc123/title_slug/
        subreddit = ""
        m = re.search(r'/r/([^/]+)/comments/', url)
        if m:
            subreddit = m.group(1)

        post_title = ""
        try:
            h1 = await page.query_selector('h1')
            if h1:
                post_title = (await h1.inner_text()).strip()
        except Exception:
            pass

        # Post body (selftext)
        try:
            body_el = await page.query_selector('[data-testid="post-container"] [data-click-id="text"]')
            if not body_el:
                body_el = await page.query_selector('.Post [data-click-id="text"]')
            if body_el:
                body = (await body_el.inner_text()).strip()
                if len(body) > 30:
                    post_id = re.search(r'/comments/([^/]+)/', url)
                    rid = f"reddit_post_{post_id.group(1) if post_id else hash(url)}"
                    if rid not in seen_ids:
                        reviews.append(Review(
                            id=rid,
                            source=SourceType.REDDIT,
                            author=None,
                            text=body,
                            rating=None,
                            date=None,
                            url=url,
                            product_name=None,
                            metadata={"subreddit": subreddit, "title": post_title, "type": "post"},
                        ))
        except Exception:
            pass

        # Comments
        try:
            comment_els = await page.query_selector_all('[data-testid="comment"]')
            if not comment_els:
                # Fallback selector for newer Reddit layout
                comment_els = await page.query_selector_all('div[id^="t1_"]')

            for i, el in enumerate(comment_els[:20]):
                try:
                    text_el = await el.query_selector('p, [data-click-id="text"]')
                    if not text_el:
                        continue
                    text = (await text_el.inner_text()).strip()
                    if len(text) < 20 or text in ("[deleted]", "[removed]"):
                        continue

                    cid = await el.get_attribute("id") or f"reddit_comment_{hash(url)}_{i}"
                    rid = f"reddit_{cid}"
                    if rid not in seen_ids:
                        reviews.append(Review(
                            id=rid,
                            source=SourceType.REDDIT,
                            author=None,
                            text=text,
                            rating=None,
                            date=None,
                            url=url,
                            product_name=None,
                            metadata={"subreddit": subreddit, "post_title": post_title, "type": "comment"},
                        ))
                except Exception:
                    continue
        except Exception:
            pass

        return reviews
