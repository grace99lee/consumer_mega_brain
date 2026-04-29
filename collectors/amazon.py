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

_SCRAPERAPI = "https://api.scraperapi.com"


class AmazonCollector(BaseCollector):
    source_type = SourceType.AMAZON

    def is_available(self) -> bool:
        return True

    def _proxy(self, target_url: str, autoparse: bool = False) -> str:
        base = f"{_SCRAPERAPI}?api_key={self.settings.scraperapi_key}&url={quote_plus(target_url)}&country_code=us"
        if autoparse:
            base += "&autoparse=true"
        return base

    async def collect(self, query: str, max_results: int = 200) -> list[Review]:
        if self.settings.has_scraperapi():
            logger.info("Amazon: SCRAPERAPI_KEY found, using ScraperAPI")
            return await self._collect_scraperapi(query, max_results)
        else:
            logger.warning("Amazon: no SCRAPERAPI_KEY set — direct requests will likely be blocked on cloud")
            return await self._collect_httpx(query, max_results)

    # --- ScraperAPI path ---

    async def _collect_scraperapi(self, query: str, max_results: int) -> list[Review]:
        reviews: list[Review] = []

        async with httpx.AsyncClient(timeout=60) as client:

            # Step 1: search Amazon for ASINs
            search_target = f"https://www.amazon.com/s?k={quote_plus(query)}"
            logger.info("Amazon ScraperAPI search: %s", search_target)
            try:
                resp = await client.get(self._proxy(search_target))
                logger.info("Amazon search response: status=%d, length=%d", resp.status_code, len(resp.text))
                if resp.status_code != 200:
                    logger.warning("Amazon ScraperAPI search failed with status %d", resp.status_code)
                    return []

                asins = re.findall(r'data-asin="([A-Z0-9]{10})"', resp.text)
                asins = list(dict.fromkeys(a for a in asins if a))[:5]
                logger.info("Amazon ASINs found: %s", asins)

            except Exception as exc:
                logger.warning("Amazon ScraperAPI search error: %s", exc)
                return []

            if not asins:
                logger.warning("Amazon: 0 ASINs found for '%s' — ScraperAPI may have returned a CAPTCHA or redirect", query)
                return []

            # Step 2: fetch reviews for each ASIN using autoparse=true
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
                        resp = await client.get(self._proxy(reviews_target, autoparse=True))
                        logger.info("Amazon reviews ASIN=%s page=%d status=%d", asin, page_num, resp.status_code)

                        if resp.status_code != 200:
                            break

                        # autoparse=true returns JSON for Amazon pages
                        try:
                            data = resp.json()
                            review_list = data.get("reviews", [])
                            product_name = data.get("product_name", "") or data.get("name", "")
                            logger.info("Amazon autoparse: %d reviews for ASIN %s", len(review_list), asin)

                            if not review_list:
                                # autoparse returned no reviews — fall back to HTML parsing
                                logger.info("Amazon: autoparse empty, trying HTML parse for %s", asin)
                                batch = self._parse_reviews_html(resp.text, asin)
                                if not batch:
                                    break
                                reviews.extend(batch[:max_results - len(reviews)])
                                continue

                            for r in review_list:
                                if len(reviews) >= max_results:
                                    break
                                body = (r.get("review_content") or r.get("body") or r.get("text") or "").strip()
                                title = (r.get("review_title") or r.get("title") or "").strip()
                                if not body and not title:
                                    continue
                                full_text = f"{title}\n\n{body}" if title and body else (body or title)
                                if len(full_text) < 20:
                                    continue
                                rating = None
                                try:
                                    raw = r.get("rating") or r.get("review_star_rating") or ""
                                    rating = float(str(raw).split()[0].replace(",", "."))
                                except (ValueError, TypeError):
                                    pass
                                reviews.append(Review(
                                    id=f"amazon_{asin}_{r.get('review_id', len(reviews))}",
                                    source=SourceType.AMAZON,
                                    author=r.get("reviewer_name") or r.get("author"),
                                    text=full_text,
                                    rating=rating,
                                    date=None,
                                    url=f"https://www.amazon.com/product-reviews/{asin}",
                                    product_name=product_name or None,
                                    metadata={"asin": asin, "type": "review"},
                                ))

                        except ValueError:
                            # Response is HTML, not JSON — parse directly
                            logger.info("Amazon: response is HTML for %s, parsing directly", asin)
                            batch = self._parse_reviews_html(resp.text, asin)
                            if not batch:
                                break
                            reviews.extend(batch[:max_results - len(reviews)])

                        await asyncio.sleep(random.uniform(0.5, 1))

                    except Exception as exc:
                        logger.warning("Amazon reviews error ASIN=%s page=%d: %s", asin, page_num, exc)
                        break

        logger.info("Amazon: collected %d reviews total", len(reviews))
        return reviews[:max_results]

    def _parse_reviews_html(self, html: str, asin: str) -> list[Review]:
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

    # --- Direct httpx fallback (local use only) ---

    async def _collect_httpx(self, query: str, max_results: int) -> list[Review]:
        reviews: list[Review] = []
        try:
            async with httpx.AsyncClient(
                headers={"User-Agent": random.choice(_UA_LIST), "Accept-Language": "en-US,en;q=0.9"},
                timeout=30, follow_redirects=True,
            ) as client:
                resp = await client.get(f"https://www.amazon.com/s?k={quote_plus(query)}")
                if resp.status_code != 200 or "captcha" in resp.text.lower():
                    logger.warning("Amazon direct request blocked")
                    return []
                asins = re.findall(r'data-asin="([A-Z0-9]{10})"', resp.text)
                asins = list(dict.fromkeys(a for a in asins if a))[:5]
                for asin in asins:
                    if len(reviews) >= max_results:
                        break
                    url = f"https://www.amazon.com/product-reviews/{asin}?reviewerType=all_reviews"
                    try:
                        r = await client.get(url)
                        if r.status_code == 200:
                            batch = self._parse_reviews_html(r.text, asin)
                            reviews.extend(batch[:max_results - len(reviews)])
                    except Exception:
                        pass
                    await asyncio.sleep(random.uniform(1.5, 3))
        except Exception:
            logger.exception("Amazon direct collection failed")
        logger.info("Amazon (direct): collected %d reviews", len(reviews))
        return reviews[:max_results]
