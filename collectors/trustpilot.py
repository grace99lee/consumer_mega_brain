from __future__ import annotations

import asyncio
import json
import logging
import random
import re

import httpx
from bs4 import BeautifulSoup

from collectors.base import BaseCollector
from config.settings import Settings
from models.schemas import Review, SourceType

logger = logging.getLogger(__name__)

_UA_LIST = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
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


class TrustpilotCollector(BaseCollector):
    source_type = SourceType.TRUSTPILOT

    def is_available(self) -> bool:
        return True

    async def collect(self, query: str, max_results: int = 200) -> list[Review]:
        reviews: list[Review] = []

        try:
            async with _make_client() as client:
                biz_urls = await self._search_businesses(client, query)
                logger.debug("Found %d Trustpilot businesses for query: %s", len(biz_urls), query)

                for biz_url in biz_urls[:5]:
                    if len(reviews) >= max_results:
                        break
                    try:
                        batch = await self._scrape_business_reviews(client, biz_url, max_results - len(reviews))
                        reviews.extend(batch)
                        logger.debug("Got %d reviews from %s", len(batch), biz_url)
                    except Exception:
                        logger.debug("Failed to scrape %s", biz_url)
                    await asyncio.sleep(random.uniform(1.5, 3))

        except Exception:
            logger.exception("Error collecting Trustpilot data for query: %s", query)

        logger.info("Collected %d reviews from Trustpilot", len(reviews))
        return reviews[:max_results]

    async def _search_businesses(self, client: httpx.AsyncClient, query: str) -> list[str]:
        url = f"https://www.trustpilot.com/search?query={query.replace(' ', '+')}"
        try:
            resp = await client.get(url)
            if resp.status_code != 200:
                return []

            soup = BeautifulSoup(resp.text, "lxml")

            # Try __NEXT_DATA__ JSON embedded in page
            next_data_el = soup.find("script", id="__NEXT_DATA__")
            if next_data_el and next_data_el.string:
                try:
                    data = json.loads(next_data_el.string)
                    business_units = (
                        data.get("props", {})
                        .get("pageProps", {})
                        .get("businessUnits", [])
                    )
                    if business_units:
                        urls = []
                        for bu in business_units[:5]:
                            slug = bu.get("identifyingName") or bu.get("name", "").replace(" ", "")
                            if slug:
                                urls.append(f"https://www.trustpilot.com/review/{slug}")
                        if urls:
                            return urls
                except Exception:
                    pass

            # Fallback: extract /review/ slugs from page HTML
            slugs = re.findall(r'href="/review/([\w.\-]+)"', resp.text)
            seen: set[str] = set()
            unique: list[str] = []
            for s in slugs:
                if s not in seen:
                    seen.add(s)
                    unique.append(s)
            return [f"https://www.trustpilot.com/review/{s}" for s in unique[:5]]

        except Exception:
            logger.debug("Trustpilot search failed")
            return []

    async def _scrape_business_reviews(
        self, client: httpx.AsyncClient, biz_url: str, max_reviews: int
    ) -> list[Review]:
        reviews: list[Review] = []
        business_name = ""

        for page_num in range(1, 6):
            if len(reviews) >= max_reviews:
                break

            url = biz_url if page_num == 1 else f"{biz_url}?page={page_num}"
            try:
                resp = await client.get(url)
                if resp.status_code != 200:
                    break

                soup = BeautifulSoup(resp.text, "lxml")

                # Try __NEXT_DATA__ first (most reliable)
                next_data_el = soup.find("script", id="__NEXT_DATA__")
                if next_data_el and next_data_el.string:
                    try:
                        data = json.loads(next_data_el.string)
                        page_props = data.get("props", {}).get("pageProps", {})

                        if page_num == 1:
                            biz_info = page_props.get("businessUnit", {})
                            business_name = biz_info.get("displayName") or biz_info.get("name", "")

                        review_list = page_props.get("reviews", [])
                        for r in review_list:
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
                            if len(reviews) >= max_reviews:
                                break

                        if review_list:
                            await asyncio.sleep(random.uniform(1, 2))
                            continue

                    except Exception:
                        pass

                # HTML fallback
                if page_num == 1 and not business_name:
                    h1 = soup.select_one("h1")
                    if h1:
                        business_name = h1.get_text(strip=True)

                review_cards = soup.select('article[class*="review"]')
                if not review_cards:
                    break

                for card in review_cards:
                    text_el = card.select_one(
                        '[data-service-review-text-typography], p[class*="content"], .review-content p'
                    )
                    text = text_el.get_text(strip=True) if text_el else ""
                    if len(text) < 10:
                        continue

                    author_el = card.select_one(
                        '[data-consumer-name-typography], [class*="consumerName"]'
                    )
                    author = author_el.get_text(strip=True) if author_el else None

                    reviews.append(Review(
                        id=f"trustpilot_{business_name[:20]}_{len(reviews)}",
                        source=SourceType.TRUSTPILOT,
                        author=author,
                        text=text,
                        rating=None,
                        date=None,
                        url=url,
                        product_name=business_name or None,
                        metadata={"type": "review"},
                    ))
                    if len(reviews) >= max_reviews:
                        break

                await asyncio.sleep(random.uniform(1, 2))

            except Exception:
                logger.debug("Trustpilot fetch failed for %s page %d", biz_url, page_num)
                break

        return reviews
