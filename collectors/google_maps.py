from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime

from collectors.base import BaseCollector
from collectors.playwright_utils import stealth_browser, run_in_playwright_thread
from config.settings import Settings
from models.schemas import Review, SourceType

logger = logging.getLogger(__name__)


class GoogleMapsCollector(BaseCollector):
    source_type = SourceType.GOOGLE_MAPS

    def is_available(self) -> bool:
        try:
            import playwright  # noqa: F401
            return True
        except ImportError:
            return False

    async def collect(self, query: str, max_results: int = 200) -> list[Review]:
        try:
            return await run_in_playwright_thread(lambda: self._collect_playwright(query, max_results))
        except Exception as exc:
            logger.warning("Google Maps Playwright unavailable: %s", exc)
            return []

    async def _collect_playwright(self, query: str, max_results: int) -> list[Review]:
        reviews: list[Review] = []

        try:
            async with stealth_browser() as (browser, context):
                page = await context.new_page()

                search_url = f"https://www.google.com/maps/search/{query.replace(' ', '+')}"
                await page.goto(search_url, wait_until="domcontentloaded")
                await asyncio.sleep(random.uniform(3, 5))

                results = await page.query_selector_all('a[href*="/maps/place/"]')
                place_links = []
                for r in results[:5]:
                    href = await r.get_attribute("href")
                    if href and href not in place_links:
                        place_links.append(href)

                for place_url in place_links[:3]:
                    if len(reviews) >= max_results:
                        break
                    try:
                        place_reviews = await self._scrape_place_reviews(
                            page, place_url, max_results - len(reviews)
                        )
                        reviews.extend(place_reviews)
                    except Exception:
                        logger.warning("Error scraping Google Maps reviews for %s", place_url[:80])
                    await asyncio.sleep(random.uniform(2, 4))

        except Exception:
            logger.exception("Error collecting Google Maps data for query: %s", query)

        logger.info("Collected %d reviews from Google Maps", len(reviews))
        return reviews[:max_results]

    async def _scrape_place_reviews(self, page, place_url: str, max_reviews: int) -> list[Review]:
        reviews: list[Review] = []
        await page.goto(place_url, wait_until="domcontentloaded")
        await asyncio.sleep(random.uniform(2, 4))

        place_name = ""
        try:
            place_name = await page.inner_text("h1")
        except Exception:
            pass

        try:
            reviews_btn = await page.query_selector(
                'button[aria-label*="Reviews"], button[aria-label*="reviews"]'
            )
            if reviews_btn:
                await reviews_btn.click()
                await asyncio.sleep(random.uniform(2, 3))
        except Exception:
            pass

        reviews_panel = await page.query_selector('div[role="main"]')
        for _ in range(min(max_reviews // 10, 20)):
            if reviews_panel:
                await page.evaluate("(el) => el.scrollBy(0, 1000)", reviews_panel)
                await asyncio.sleep(random.uniform(0.5, 1.5))

        review_elements = await page.query_selector_all('div[data-review-id]')

        for el in review_elements:
            if len(reviews) >= max_reviews:
                break
            try:
                more_btn = await el.query_selector('button[aria-label="See more"]')
                if more_btn:
                    await more_btn.click()
                    await asyncio.sleep(0.3)

                text_el = await el.query_selector(
                    'span[class*="review-full-text"], div[class*="MyEned"]'
                )
                text = await text_el.inner_text() if text_el else ""
                if len(text.strip()) < 10:
                    continue

                author_el = await el.query_selector(
                    'div[class*="d4r55"] span, button[data-review-id] div'
                )
                author = await author_el.inner_text() if author_el else None

                rating = None
                rating_el = await el.query_selector('span[role="img"]')
                if rating_el:
                    aria = await rating_el.get_attribute("aria-label") or ""
                    try:
                        rating = float(aria.split(" ")[0])
                    except (ValueError, IndexError):
                        pass

                review_id = await el.get_attribute("data-review-id") or f"gm_{len(reviews)}"
                reviews.append(Review(
                    id=f"google_maps_{review_id}",
                    source=SourceType.GOOGLE_MAPS,
                    author=author,
                    text=text.strip(),
                    rating=rating,
                    date=None,
                    url=page.url,
                    product_name=place_name,
                    metadata={"type": "review"},
                ))
            except Exception:
                continue

        return reviews
