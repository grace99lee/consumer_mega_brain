from __future__ import annotations

import asyncio
import logging
import random

from collectors.base import BaseCollector
from collectors.playwright_utils import stealth_browser
from config.settings import Settings
from models.schemas import Review, SourceType

logger = logging.getLogger(__name__)


class InstagramCollector(BaseCollector):
    """Instagram comment collector using Playwright.

    Note: Instagram aggressively blocks scraping and requires login for most content.
    This collector is best-effort and may return limited results.
    """

    source_type = SourceType.INSTAGRAM

    def is_available(self) -> bool:
        return True

    async def collect(self, query: str, max_results: int = 200) -> list[Review]:
        reviews: list[Review] = []

        try:
            async with stealth_browser() as (browser, context):
                page = await context.new_page()

                hashtag = query.replace(" ", "").lower()
                url = f"https://www.instagram.com/explore/tags/{hashtag}/"
                await page.goto(url, wait_until="domcontentloaded")
                await asyncio.sleep(random.uniform(3, 5))

                post_links = await page.eval_on_selector_all(
                    'a[href*="/p/"]',
                    "els => [...new Set(els.map(e => e.href))].slice(0, 10)",
                )

                for post_url in post_links:
                    if len(reviews) >= max_results:
                        break
                    try:
                        post_reviews = await self._scrape_post_comments(
                            page, post_url, max_results - len(reviews)
                        )
                        reviews.extend(post_reviews)
                    except Exception:
                        logger.warning("Error scraping Instagram comments for %s", post_url)
                    await asyncio.sleep(random.uniform(3, 6))

        except Exception:
            logger.exception("Error collecting Instagram data for query: %s", query)

        logger.info("Collected %d reviews from Instagram", len(reviews))
        return reviews[:max_results]

    async def _scrape_post_comments(self, page, post_url: str, max_comments: int) -> list[Review]:
        reviews: list[Review] = []
        await page.goto(post_url, wait_until="domcontentloaded")
        await asyncio.sleep(random.uniform(2, 4))

        for _ in range(3):
            try:
                load_more = await page.query_selector(
                    'button:has-text("View more comments"), button:has-text("Load more")'
                )
                if load_more:
                    await load_more.click()
                    await asyncio.sleep(random.uniform(1, 2))
            except Exception:
                break

        comment_elements = await page.query_selector_all('ul ul div[role="button"]')
        if not comment_elements:
            comment_elements = await page.query_selector_all('span[class*="comment"]')

        for el in comment_elements:
            if len(reviews) >= max_comments:
                break
            try:
                text = await el.inner_text()
                if len(text.strip()) < 5:
                    continue
                reviews.append(Review(
                    id=f"instagram_{hash(text) % 10**8}",
                    source=SourceType.INSTAGRAM,
                    author=None,
                    text=text.strip(),
                    rating=None,
                    date=None,
                    url=post_url,
                    product_name=None,
                    metadata={"type": "comment"},
                ))
            except Exception:
                continue

        return reviews
