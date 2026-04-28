from __future__ import annotations

import asyncio
import json
import logging
import random
import re

from collectors.base import BaseCollector
from collectors.playwright_utils import stealth_browser
from config.settings import Settings
from models.schemas import Review, SourceType

logger = logging.getLogger(__name__)


class WalmartCollector(BaseCollector):
    """Collect Walmart product reviews via Playwright + Walmart's internal review API."""

    source_type = SourceType.WALMART

    def is_available(self) -> bool:
        return True

    async def collect(self, query: str, max_results: int = 200) -> list[Review]:
        reviews: list[Review] = []

        try:
            async with stealth_browser() as (browser, context):
                page = await context.new_page()

                search_url = f"https://www.walmart.com/search?q={query.replace(' ', '+')}"
                await page.goto(search_url, wait_until="domcontentloaded")
                await asyncio.sleep(random.uniform(3, 5))

                content = await page.content()
                if "Robot or human" in content or "captcha" in content.lower():
                    logger.warning("Walmart bot detection triggered")
                    return []

                item_ids = await self._extract_item_ids(page, content)
                logger.debug("Found %d Walmart item IDs", len(item_ids))

                for item_id in item_ids[:6]:
                    if len(reviews) >= max_results:
                        break
                    try:
                        batch = await self._fetch_reviews_api(page, item_id, max_results - len(reviews))
                        reviews.extend(batch)
                        logger.debug("Got %d reviews for Walmart item %s", len(batch), item_id)
                    except Exception:
                        logger.debug("Failed Walmart reviews for item %s", item_id)
                    await asyncio.sleep(random.uniform(2, 4))

        except Exception:
            logger.exception("Error collecting Walmart data for query: %s", query)

        logger.info("Collected %d reviews from Walmart", len(reviews))
        return reviews[:max_results]

    async def _extract_item_ids(self, page, content: str) -> list[str]:
        item_ids: list[str] = []

        # Try __NEXT_DATA__ JSON
        try:
            import json as _json
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(content, "lxml")
            nd = soup.find("script", id="__NEXT_DATA__")
            if nd and nd.string:
                data = _json.loads(nd.string)
                items = (
                    data.get("props", {})
                    .get("pageProps", {})
                    .get("initialData", {})
                    .get("searchResult", {})
                    .get("itemStacks", [{}])[0]
                    .get("items", [])
                )
                ids = [
                    str(item.get("usItemId") or item.get("itemId", ""))
                    for item in items
                    if item.get("usItemId") or item.get("itemId")
                ]
                item_ids = [i for i in ids if i]
        except Exception:
            pass

        if not item_ids:
            # Regex fallback on page source
            ids = re.findall(r'"/ip/[^"]+/(\d{7,12})"', content)
            item_ids = list(dict.fromkeys(ids))

        if not item_ids:
            # Playwright fallback: extract from href attributes
            try:
                links = await page.eval_on_selector_all(
                    'a[href*="/ip/"]',
                    "els => els.map(e => e.href)",
                )
                for link in links:
                    m = re.search(r'/ip/[^/]+/(\d{7,12})', link)
                    if m and m.group(1) not in item_ids:
                        item_ids.append(m.group(1))
            except Exception:
                pass

        return item_ids[:6]

    async def _fetch_reviews_api(self, page, item_id: str, max_reviews: int) -> list[Review]:
        reviews = []
        product_name = ""

        for page_num in range(1, 6):
            if len(reviews) >= max_reviews:
                break

            api_url = (
                f"https://www.walmart.com/reviews/api/data"
                f"?itemId={item_id}&page={page_num}&limit=20&sort=relevancy"
            )
            try:
                result = await page.evaluate(f"""
                    fetch('{api_url}', {{
                        headers: {{
                            'Accept': 'application/json',
                            'x-requested-with': 'XMLHttpRequest',
                        }}
                    }}).then(r => r.json()).catch(() => null)
                """)

                if not result:
                    break

                if page_num == 1:
                    product_name = result.get("displayName") or result.get("name", "")

                review_list = result.get("reviews", [])
                if not review_list:
                    break

                for r in review_list:
                    text = (r.get("reviewText") or "").strip()
                    if len(text) < 15:
                        continue

                    title = (r.get("title") or "").strip()
                    full_text = f"{title}\n\n{text}" if title else text

                    rating = None
                    try:
                        rating = float(r.get("rating") or 0) or None
                    except (TypeError, ValueError):
                        pass

                    reviews.append(Review(
                        id=f"walmart_{item_id}_{r.get('reviewId', len(reviews))}",
                        source=SourceType.WALMART,
                        author=r.get("userNickname") or r.get("authorId"),
                        text=full_text,
                        rating=rating,
                        date=None,
                        url=f"https://www.walmart.com/ip/{item_id}",
                        product_name=product_name or None,
                        metadata={
                            "item_id": item_id,
                            "helpful_votes": r.get("positiveFeedback", 0),
                            "type": "review",
                        },
                    ))

                    if len(reviews) >= max_reviews:
                        break

                pagination = result.get("paginationData", {})
                total_pages = pagination.get("totalPages", 1)
                if page_num >= total_pages:
                    break

                page_num += 1
                await asyncio.sleep(random.uniform(1, 2))

            except Exception:
                logger.debug("Walmart API call failed for item %s page %d", item_id, page_num)
                break

        return reviews
