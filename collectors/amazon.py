from __future__ import annotations

import asyncio
import logging
import random
import re
from urllib.parse import quote_plus

import httpx
from bs4 import BeautifulSoup

from collectors.base import BaseCollector
from config.settings import Settings
from models.schemas import Review, SourceType

logger = logging.getLogger(__name__)

_UA_LIST = [
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Mobile Safari/537.36",
]

_SCRAPERAPI_URL = "http://api.scraperapi.com"


class AmazonCollector(BaseCollector):
    source_type = SourceType.AMAZON

    def is_available(self) -> bool:
        return True

    def _proxy_url(self, target_url: str) -> str:
        """Wrap a URL through ScraperAPI's basic proxy (works on all tiers)."""
        return f"{_SCRAPERAPI_URL}?api_key={self.settings.scraperapi_key}&url={quote_plus(target_url)}&country_code=us"

    async def collect(self, query: str, max_results: int = 200) -> list[Review]:
        if self.settings.has_scraperapi():
            logger.info("Amazon: using ScraperAPI proxy")
            return await self._collect_scraperapi(query, max_results)
        else:
            logger.info("Amazon: using direct httpx (add SCRAPERAPI_KEY for cloud reliability)")
            return await self._collect_httpx(query, max_results)

    # --- ScraperAPI basic proxy (works on free tier) ---

    async def _collect_scraperapi(self, query: str, max_results: int) -> list[Review]:
        reviews: list[Review] = []

        async with httpx.AsyncClient(timeout=60) as client:
            # Step 1: search for ASINs
            search_target = f"https://www.amazon.com/s?k={query.replace(' ', '+')}"
            try:
                resp = await client.get(self._proxy_url(search_target))
                logger.debug("ScraperAPI search status: %d", resp.status_code)
                if resp.status_code != 200:
                    logger.warning("ScraperAPI Amazon search returned %d", resp.status_code)
                    return []
                asins = re.findall(r'data-asin="([A-Z0-9]{10})"', resp.text)
                asins = list(dict.fromkeys(a for a in asins if a))[:5]
                logger.debug("ScraperAPI found ASINs: %s", asins)
            except Exception as exc:
                logger.warning("ScraperAPI Amazon search failed: %s", exc)
                return []

            if not asins:
                logger.warning("Amazon: no ASINs found via ScraperAPI for '%s'", query)
                return []

            # Step 2: fetch reviews for each ASIN
            for asin in asins:
                if len(reviews) >= max_results:
                    break
                for page_num in range(1, 5):
                    if len(reviews) >= max_results:
                        break
                    reviews_target = (
                        f"https://www.amazon.com/product-reviews/{asin}"
                        f"?reviewerType=all_reviews&sortBy=recent&pageNumber={page_num}"
                    )
                    try:
                        resp = await client.get(self._proxy_url(reviews_target))
                        if resp.status_code != 200:
                            break
                        batch = self._parse_reviews_html(resp.text, asin)
                        if not batch:
                            break
                        reviews.extend(batch[:max_results - len(reviews)])
                        await asyncio.sleep(random.uniform(0.5, 1))
                    except Exception as exc:
                        logger.warning("ScraperAPI reviews failed for %s p%d: %s", asin, page_num, exc)
                        break

        logger.info("Collected %d reviews from Amazon (ScraperAPI)", len(reviews))
        return reviews[:max_results]

    def _parse_reviews_html(self, html: str, asin: str) -> list[Review]:
        """Parse Amazon product-reviews HTML into Review objects."""
        soup = BeautifulSoup(html, "lxml")
        reviews = []

        product_name = ""
        link = soup.select_one('[data-hook="product-link"]')
        if link:
            product_name = link.get_text(strip=True)

        for card in soup.select('[data-hook="review"]'):
            body_el = card.select_one('[data-hook="review-body"] span')
            body = body_el.get_text(strip=True) if body_el else ""
            if len(body) < 20:
                continue
            title_el = card.select_one('[data-hook="review-title"] span:not(.a-icon-alt)')
            title = title_el.get_text(strip=True) if title_el else ""
            full_text = f"{title}\n\n{body}" if title else body

            author_el = card.select_one("span.a-profile-name")
            rating = None
            rating_el = card.select_one('[data-hook="review-star-rating"] .a-icon-alt')
            if rating_el:
                try:
                    rating = float(rating_el.get_text().split()[0])
                except (ValueError, IndexError):
                    pass

            reviews.append(Review(
                id=f"amazon_{card.get('id', f'{asin}_{len(reviews)}')}",
                source=SourceType.AMAZON,
                author=author_el.get_text(strip=True) if author_el else None,
                text=full_text,
                rating=rating,
                date=None,
                url=f"https://www.amazon.com/product-reviews/{asin}",
                product_name=product_name or None,
                metadata={"asin": asin, "type": "review"},
            ))
        return reviews

    # --- Direct httpx fallback (works locally, blocked on cloud IPs) ---

    async def _collect_httpx(self, query: str, max_results: int) -> list[Review]:
        reviews: list[Review] = []

        try:
            async with httpx.AsyncClient(
                headers={"User-Agent": random.choice(_UA_LIST), "Accept-Language": "en-US,en;q=0.9"},
                timeout=30,
                follow_redirects=True,
            ) as client:
                asins = await self._search_asins(client, query)
                if not asins:
                    return []

                for asin in asins[:5]:
                    if len(reviews) >= max_results:
                        break
                    try:
                        batch = await self._scrape_reviews(client, asin, max_results - len(reviews))
                        reviews.extend(batch)
                    except Exception as exc:
                        logger.warning("Amazon reviews failed for %s: %s", asin, exc)
                    await asyncio.sleep(random.uniform(1.5, 3))

        except Exception:
            logger.exception("Amazon httpx collection failed for: %s", query)

        logger.info("Collected %d reviews from Amazon (httpx)", len(reviews))
        return reviews[:max_results]

    async def _search_asins(self, client: httpx.AsyncClient, query: str) -> list[str]:
        try:
            resp = await client.get(f"https://www.amazon.com/s?k={query.replace(' ', '+')}")
            if resp.status_code != 200 or "captcha" in resp.text.lower():
                logger.warning("Amazon search blocked (cloud IP detected)")
                return []
            asins = re.findall(r'data-asin="([A-Z0-9]{10})"', resp.text)
            return list(dict.fromkeys(a for a in asins if a))[:5]
        except Exception as exc:
            logger.warning("Amazon search failed: %s", exc)
            return []

    async def _scrape_reviews(self, client: httpx.AsyncClient, asin: str, max_reviews: int) -> list[Review]:
        reviews = []
        product_name = ""
        for page_num in range(1, 4):
            if len(reviews) >= max_reviews:
                break
            url = f"https://www.amazon.com/product-reviews/{asin}?reviewerType=all_reviews&pageNumber={page_num}"
            try:
                resp = await client.get(url)
                if resp.status_code != 200 or "captcha" in resp.text.lower():
                    break
                soup = BeautifulSoup(resp.text, "lxml")
                if page_num == 1:
                    link = soup.select_one('[data-hook="product-link"]')
                    if link:
                        product_name = link.get_text(strip=True)
                cards = soup.select('[data-hook="review"]')
                if not cards:
                    break
                for card in cards:
                    if len(reviews) >= max_reviews:
                        break
                    body_el = card.select_one('[data-hook="review-body"] span')
                    body = body_el.get_text(strip=True) if body_el else ""
                    if len(body) < 20:
                        continue
                    title_el = card.select_one('[data-hook="review-title"] span:not(.a-icon-alt)')
                    title = title_el.get_text(strip=True) if title_el else ""
                    full_text = f"{title}\n\n{body}" if title else body
                    author_el = card.select_one("span.a-profile-name")
                    rating_el = card.select_one('[data-hook="review-star-rating"] .a-icon-alt')
                    rating = None
                    if rating_el:
                        try:
                            rating = float(rating_el.get_text().split()[0])
                        except (ValueError, IndexError):
                            pass
                    reviews.append(Review(
                        id=f"amazon_{card.get('id', f'{asin}_{len(reviews)}')}",
                        source=SourceType.AMAZON,
                        author=author_el.get_text(strip=True) if author_el else None,
                        text=full_text,
                        rating=rating,
                        date=None,
                        url=f"https://www.amazon.com/product-reviews/{asin}",
                        product_name=product_name or None,
                        metadata={"asin": asin, "type": "review"},
                    ))
                await asyncio.sleep(random.uniform(1, 2))
            except Exception as exc:
                logger.warning("Amazon page %d failed for %s: %s", page_num, asin, exc)
                break
        return reviews
