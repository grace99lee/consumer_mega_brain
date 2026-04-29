from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from datetime import datetime
from urllib.parse import quote_plus

import httpx
from bs4 import BeautifulSoup

from collectors.base import BaseCollector
from config.settings import Settings
from models.schemas import Review, SourceType

logger = logging.getLogger(__name__)

_SCRAPERAPI_URL = "http://api.scraperapi.com"

_UA_LIST = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
]


class TrustpilotCollector(BaseCollector):
    source_type = SourceType.TRUSTPILOT

    def is_available(self) -> bool:
        return True

    def _proxy_url(self, target_url: str) -> str:
        return f"{_SCRAPERAPI_URL}?api_key={self.settings.scraperapi_key}&url={quote_plus(target_url)}"

    async def collect(self, query: str, max_results: int = 200) -> list[Review]:
        if self.settings.has_scraperapi():
            logger.info("Trustpilot: using ScraperAPI proxy")
            return await self._collect_via_scraperapi(query, max_results)
        else:
            logger.info("Trustpilot: using direct httpx (add SCRAPERAPI_KEY for cloud reliability)")
            return await self._collect_via_httpx(query, max_results)

    # --- ScraperAPI path (works from cloud, free tier) ---

    async def _collect_via_scraperapi(self, query: str, max_results: int) -> list[Review]:
        reviews: list[Review] = []

        async with httpx.AsyncClient(timeout=60) as client:
            # Step 1: search for businesses
            search_url = f"https://www.trustpilot.com/search?query={quote_plus(query)}"
            try:
                resp = await client.get(self._proxy_url(search_url))
                if resp.status_code != 200:
                    logger.warning("Trustpilot search via ScraperAPI returned %d", resp.status_code)
                    return []
                biz_urls = self._extract_biz_urls_from_html(resp.text, query)
                logger.debug("Trustpilot found %d businesses for '%s'", len(biz_urls), query)
            except Exception as exc:
                logger.warning("Trustpilot ScraperAPI search failed: %s", exc)
                return []

            if not biz_urls:
                logger.warning("Trustpilot: no businesses found for '%s'", query)
                return []

            # Step 2: scrape reviews for each business
            for biz_url in biz_urls[:3]:
                if len(reviews) >= max_results:
                    break
                for page_num in range(1, 5):
                    if len(reviews) >= max_results:
                        break
                    paged_url = biz_url if page_num == 1 else f"{biz_url}?page={page_num}"
                    try:
                        resp = await client.get(self._proxy_url(paged_url))
                        if resp.status_code != 200:
                            break
                        batch = self._parse_reviews_html(resp.text, biz_url)
                        if not batch:
                            break
                        reviews.extend(batch[:max_results - len(reviews)])
                        await asyncio.sleep(random.uniform(0.5, 1))
                    except Exception as exc:
                        logger.warning("Trustpilot ScraperAPI page %d failed for %s: %s", page_num, biz_url, exc)
                        break

        logger.info("Collected %d reviews from Trustpilot (ScraperAPI)", len(reviews))
        return reviews[:max_results]

    def _extract_biz_urls_from_html(self, html: str, query: str) -> list[str]:
        """Extract and filter business URLs from Trustpilot search results HTML."""
        primary = query.strip().lower().split()[0]
        urls: list[str] = []

        # Try __NEXT_DATA__ JSON first
        try:
            match = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
            if match:
                data = json.loads(match.group(1))
                business_units = (
                    data.get("props", {})
                    .get("pageProps", {})
                    .get("businessUnits", [])
                )
                for bu in business_units[:8]:
                    slug = bu.get("identifyingName") or bu.get("name", "").replace(" ", "")
                    if slug:
                        urls.append(f"https://www.trustpilot.com/review/{slug}")
        except Exception:
            pass

        # Fallback: parse /review/ links from HTML
        if not urls:
            slugs = re.findall(r'href="/review/([\w.\-]+)"', html)
            seen: set[str] = set()
            for s in slugs:
                if s not in seen:
                    seen.add(s)
                    urls.append(f"https://www.trustpilot.com/review/{s}")

        # Filter to businesses matching the query's first word
        matched = [u for u in urls if primary in u.lower()]
        return (matched or urls[:1])[:5]

    def _parse_reviews_html(self, html: str, biz_url: str) -> list[Review]:
        """Parse Trustpilot business page HTML into Review objects."""
        reviews = []
        business_name = ""

        # Try __NEXT_DATA__ first (most reliable)
        try:
            match = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
            if match:
                data = json.loads(match.group(1))
                page_props = data.get("props", {}).get("pageProps", {})
                biz_info = page_props.get("businessUnit", {})
                business_name = biz_info.get("displayName") or biz_info.get("name", "")

                for r in page_props.get("reviews", []):
                    text = (r.get("text") or "").strip()
                    title = (r.get("title") or "").strip()
                    if not text and not title:
                        continue
                    full_text = f"{title}\n\n{text}" if title and text and title != text else (text or title)
                    if len(full_text) < 10:
                        continue
                    rating = None
                    try:
                        rating = float(r.get("rating") or 0) or None
                    except (TypeError, ValueError):
                        pass
                    reviews.append(Review(
                        id=f"trustpilot_{r.get('id', len(reviews))}",
                        source=SourceType.TRUSTPILOT,
                        author=(r.get("consumer") or {}).get("displayName"),
                        text=full_text,
                        rating=rating,
                        date=None,
                        url=biz_url,
                        product_name=business_name or None,
                        metadata={"type": "review"},
                    ))
                if reviews:
                    return reviews
        except Exception:
            pass

        # HTML fallback
        soup = BeautifulSoup(html, "lxml")
        if not business_name:
            h1 = soup.select_one("h1")
            if h1:
                business_name = h1.get_text(strip=True)

        for card in soup.select('article[class*="review"]'):
            text_el = card.select_one('[data-service-review-text-typography], p[class*="content"]')
            text = text_el.get_text(strip=True) if text_el else ""
            if len(text) < 10:
                continue
            author_el = card.select_one('[data-consumer-name-typography], [class*="consumerName"]')
            reviews.append(Review(
                id=f"trustpilot_{business_name[:20]}_{len(reviews)}",
                source=SourceType.TRUSTPILOT,
                author=author_el.get_text(strip=True) if author_el else None,
                text=text,
                rating=None,
                date=None,
                url=biz_url,
                product_name=business_name or None,
                metadata={"type": "review"},
            ))

        return reviews

    # --- Direct httpx fallback (for local use without ScraperAPI) ---

    async def _collect_via_httpx(self, query: str, max_results: int) -> list[Review]:
        reviews: list[Review] = []
        headers = {
            "User-Agent": random.choice(_UA_LIST),
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
        async with httpx.AsyncClient(headers=headers, timeout=30, follow_redirects=True) as client:
            try:
                search_url = f"https://www.trustpilot.com/search?query={quote_plus(query)}"
                resp = await client.get(search_url)
                if resp.status_code != 200:
                    return []
                biz_urls = self._extract_biz_urls_from_html(resp.text, query)
                for biz_url in biz_urls[:3]:
                    if len(reviews) >= max_results:
                        break
                    for page_num in range(1, 4):
                        if len(reviews) >= max_results:
                            break
                        paged_url = biz_url if page_num == 1 else f"{biz_url}?page={page_num}"
                        try:
                            resp = await client.get(paged_url)
                            if resp.status_code != 200:
                                break
                            batch = self._parse_reviews_html(resp.text, biz_url)
                            if not batch:
                                break
                            reviews.extend(batch[:max_results - len(reviews)])
                            await asyncio.sleep(random.uniform(1, 2))
                        except Exception:
                            break
            except Exception:
                logger.exception("Trustpilot httpx collection failed for: %s", query)

        logger.info("Collected %d reviews from Trustpilot (httpx)", len(reviews))
        return reviews[:max_results]
