from __future__ import annotations

import asyncio
import logging
import random
import re

from collectors.base import BaseCollector
from collectors.playwright_utils import stealth_browser, run_in_playwright_thread
from config.settings import Settings
from models.schemas import Review, SourceType

logger = logging.getLogger(__name__)


class AmazonCollector(BaseCollector):
    source_type = SourceType.AMAZON

    def is_available(self) -> bool:
        return True

    async def collect(self, query: str, max_results: int = 200) -> list[Review]:
        return await run_in_playwright_thread(lambda: self._collect_playwright(query, max_results))

    async def _collect_playwright(self, query: str, max_results: int) -> list[Review]:
        reviews: list[Review] = []

        try:
            async with stealth_browser() as (browser, context):
                page = await context.new_page()

                search_url = f"https://www.amazon.com/s?k={query.replace(' ', '+')}"
                await page.goto(search_url, wait_until="domcontentloaded")
                await asyncio.sleep(random.uniform(3, 5))

                content = await page.content()
                if "Type the characters" in content or "captcha" in content.lower():
                    logger.warning("Amazon bot detection triggered on search")
                    return []

                # Extract ASINs from search results
                asins = re.findall(r'data-asin="([A-Z0-9]{10})"', content)
                asins = list(dict.fromkeys(a for a in asins if a))[:5]
                logger.debug("Found %d Amazon ASINs", len(asins))

                for asin in asins:
                    if len(reviews) >= max_results:
                        break
                    try:
                        batch = await self._fetch_product_reviews(page, asin, max_results - len(reviews))
                        reviews.extend(batch)
                        logger.debug("Got %d reviews for ASIN %s", len(batch), asin)
                    except Exception:
                        logger.debug("Failed reviews for ASIN %s", asin)
                    await asyncio.sleep(random.uniform(2, 4))

        except Exception:
            logger.exception("Error collecting Amazon data for query: %s", query)

        logger.info("Collected %d reviews from Amazon", len(reviews))
        return reviews[:max_results]

    async def _fetch_product_reviews(self, page, asin: str, max_reviews: int) -> list[Review]:
        """Scrape reviews from Amazon's dedicated product-reviews page (paginated)."""
        reviews: list[Review] = []
        product_name = ""

        for page_num in range(1, 6):
            if len(reviews) >= max_reviews:
                break

            url = (
                f"https://www.amazon.com/product-reviews/{asin}"
                f"?reviewerType=all_reviews&sortBy=recent&pageNumber={page_num}"
            )
            await page.goto(url, wait_until="domcontentloaded")
            await asyncio.sleep(random.uniform(2, 4))

            content = await page.content()
            if "Type the characters" in content or "captcha" in content.lower():
                logger.warning("Amazon bot detection on reviews page %s", asin)
                break
            if "Sign in" in content and "review" not in content.lower():
                logger.warning("Amazon redirected to sign-in for %s", asin)
                break

            # Grab product name from page header (first page only)
            if page_num == 1 and not product_name:
                try:
                    el = await page.query_selector('[data-hook="product-link"]')
                    if el:
                        product_name = (await el.inner_text()).strip()
                except Exception:
                    pass

            review_els = await page.query_selector_all('[data-hook="review"]')
            if not review_els:
                break

            for el in review_els:
                if len(reviews) >= max_reviews:
                    break
                try:
                    body_el = await el.query_selector('[data-hook="review-body"] span')
                    body = (await body_el.inner_text()).strip() if body_el else ""
                    if len(body) < 20:
                        continue

                    title_el = await el.query_selector('[data-hook="review-title"] span:not(.a-icon-alt)')
                    title = (await title_el.inner_text()).strip() if title_el else ""
                    full_text = f"{title}\n\n{body}" if title else body

                    author_el = await el.query_selector("span.a-profile-name")
                    author = (await author_el.inner_text()).strip() if author_el else None

                    rating = None
                    rating_el = await el.query_selector('[data-hook="review-star-rating"] .a-icon-alt')
                    if rating_el:
                        try:
                            rating = float((await rating_el.inner_text()).split(" ")[0])
                        except (ValueError, IndexError):
                            pass

                    review_id = await el.get_attribute("id") or f"amazon_{asin}_{len(reviews)}"
                    reviews.append(Review(
                        id=f"amazon_{review_id}",
                        source=SourceType.AMAZON,
                        author=author,
                        text=full_text,
                        rating=rating,
                        date=None,
                        url=f"https://www.amazon.com/product-reviews/{asin}",
                        product_name=product_name or None,
                        metadata={"asin": asin, "type": "review"},
                    ))
                except Exception:
                    continue

            await asyncio.sleep(random.uniform(1, 2))

        return reviews
