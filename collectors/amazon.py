from __future__ import annotations

import asyncio
import logging
import random
import re
from datetime import datetime

import httpx
from bs4 import BeautifulSoup

from collectors.base import BaseCollector
from config.settings import Settings
from models.schemas import Review, SourceType

logger = logging.getLogger(__name__)

# Mobile user-agents — Amazon's mobile pages are less aggressively gated
_UA_LIST = [
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1",
]


def _make_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers={
            "User-Agent": random.choice(_UA_LIST),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
        },
        timeout=30,
        follow_redirects=True,
    )


class AmazonCollector(BaseCollector):
    source_type = SourceType.AMAZON

    def is_available(self) -> bool:
        return True

    async def collect(self, query: str, max_results: int = 200) -> list[Review]:
        reviews: list[Review] = []

        try:
            async with _make_client() as client:
                asins = await self._search_asins(client, query)
                logger.debug("Found %d Amazon ASINs for: %s", len(asins), query)

                if not asins:
                    logger.warning("Amazon: no products found for '%s' — bot detection likely", query)
                    return []

                for asin in asins[:5]:
                    if len(reviews) >= max_results:
                        break
                    try:
                        batch = await self._scrape_reviews(client, asin, max_results - len(reviews))
                        reviews.extend(batch)
                        logger.debug("Got %d reviews for ASIN %s", len(batch), asin)
                    except Exception as exc:
                        logger.warning("Amazon reviews failed for %s: %s", asin, exc)
                    await asyncio.sleep(random.uniform(1.5, 3))

        except Exception:
            logger.exception("Amazon collection failed for query: %s", query)

        logger.info("Collected %d reviews from Amazon", len(reviews))
        return reviews[:max_results]

    async def _search_asins(self, client: httpx.AsyncClient, query: str) -> list[str]:
        """Search Amazon and return up to 5 ASINs."""
        url = f"https://www.amazon.com/s?k={query.replace(' ', '+')}&ref=nb_sb_noss"
        try:
            resp = await client.get(url)
            if resp.status_code != 200:
                logger.warning("Amazon search returned %d", resp.status_code)
                return []
            if "captcha" in resp.text.lower() or "Type the characters" in resp.text:
                logger.warning("Amazon search blocked by CAPTCHA (cloud IP detected)")
                return []

            asins = re.findall(r'data-asin="([A-Z0-9]{10})"', resp.text)
            return list(dict.fromkeys(a for a in asins if a))[:5]
        except Exception as exc:
            logger.warning("Amazon search request failed: %s", exc)
            return []

    async def _scrape_reviews(
        self, client: httpx.AsyncClient, asin: str, max_reviews: int
    ) -> list[Review]:
        reviews: list[Review] = []
        product_name = ""

        for page_num in range(1, 6):
            if len(reviews) >= max_reviews:
                break

            url = (
                f"https://www.amazon.com/product-reviews/{asin}"
                f"?reviewerType=all_reviews&sortBy=recent&pageNumber={page_num}"
            )
            try:
                resp = await client.get(url)
                if resp.status_code != 200:
                    break
                if "captcha" in resp.text.lower() or "Type the characters" in resp.text:
                    logger.warning("Amazon reviews blocked by CAPTCHA for %s", asin)
                    break
                if "Sign in" in resp.text and len(resp.text) < 5000:
                    logger.warning("Amazon redirected to sign-in for %s", asin)
                    break

                soup = BeautifulSoup(resp.text, "lxml")

                if page_num == 1 and not product_name:
                    link = soup.select_one('[data-hook="product-link"]')
                    if link:
                        product_name = link.get_text(strip=True)

                cards = soup.select('[data-hook="review"]')
                if not cards:
                    break

                for card in cards:
                    if len(reviews) >= max_reviews:
                        break
                    try:
                        body_el = card.select_one('[data-hook="review-body"] span')
                        body = body_el.get_text(strip=True) if body_el else ""
                        if len(body) < 20:
                            continue

                        title_el = card.select_one('[data-hook="review-title"] span:not(.a-icon-alt)')
                        title = title_el.get_text(strip=True) if title_el else ""
                        full_text = f"{title}\n\n{body}" if title else body

                        author_el = card.select_one("span.a-profile-name")
                        author = author_el.get_text(strip=True) if author_el else None

                        rating = None
                        rating_el = card.select_one('[data-hook="review-star-rating"] .a-icon-alt')
                        if rating_el:
                            try:
                                rating = float(rating_el.get_text().split(" ")[0])
                            except (ValueError, IndexError):
                                pass

                        review_id = card.get("id") or f"amazon_{asin}_{len(reviews)}"
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

            except Exception as exc:
                logger.warning("Amazon page %d failed for %s: %s", page_num, asin, exc)
                break

        return reviews
