from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from datetime import datetime

from collectors.base import BaseCollector
from collectors.playwright_utils import run_in_playwright_thread, stealth_browser
from config.settings import Settings
from models.schemas import Review, SourceType

logger = logging.getLogger(__name__)


class TrustpilotCollector(BaseCollector):
    source_type = SourceType.TRUSTPILOT

    def is_available(self) -> bool:
        return True

    async def collect(self, query: str, max_results: int = 200) -> list[Review]:
        return await run_in_playwright_thread(
            lambda: self._collect_playwright(query, max_results)
        )

    async def _collect_playwright(self, query: str, max_results: int) -> list[Review]:
        reviews: list[Review] = []

        async with stealth_browser() as (browser, context):
            page = await context.new_page()
            try:
                # Search for businesses matching the query
                search_url = f"https://www.trustpilot.com/search?query={query.replace(' ', '+')}"
                logger.debug("Trustpilot search: %s", search_url)

                await page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(random.uniform(1.5, 2.5))

                biz_urls = await self._extract_business_urls(page)
                logger.debug("Found %d Trustpilot businesses for: %s", len(biz_urls), query)

                # Filter to businesses whose slug/name actually matches the query.
                # Trustpilot's search sometimes autocorrects (e.g. "nello" → "nelly").
                # Use the first word of the query as the primary match token.
                primary = query.strip().lower().split()[0]
                matched = [u for u in biz_urls if primary in u.lower()]
                if matched:
                    biz_urls = matched
                    logger.debug("Filtered to %d businesses matching '%s'", len(biz_urls), primary)
                else:
                    logger.warning(
                        "Trustpilot: no businesses matched '%s' — using top result as fallback", primary
                    )
                    biz_urls = biz_urls[:1]  # use top result rather than nothing

                if not biz_urls:
                    logger.warning("Trustpilot: no businesses found for query '%s'", query)
                    return []

                for biz_url in biz_urls[:5]:
                    if len(reviews) >= max_results:
                        break
                    try:
                        batch = await self._scrape_business(page, biz_url, max_results - len(reviews))
                        reviews.extend(batch)
                        logger.debug("Got %d reviews from %s", len(batch), biz_url)
                    except Exception as exc:
                        logger.warning("Trustpilot: failed to scrape %s — %s", biz_url, exc)
                    await asyncio.sleep(random.uniform(1, 2))

            except Exception:
                logger.exception("Trustpilot collection failed for query: %s", query)
            finally:
                await page.close()

        logger.info("Collected %d reviews from Trustpilot", len(reviews))
        return reviews[:max_results]

    async def _extract_business_urls(self, page) -> list[str]:
        """Pull business review URLs from the search results page."""
        # Try __NEXT_DATA__ first
        try:
            content = await page.content()
            match = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', content, re.DOTALL)
            if match:
                data = json.loads(match.group(1))
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

        # Fallback: find /review/ links in the page HTML
        try:
            links = await page.eval_on_selector_all(
                'a[href*="/review/"]',
                'els => els.map(e => e.href)'
            )
            seen: set[str] = set()
            unique: list[str] = []
            for link in links:
                clean = re.sub(r'\?.*$', '', link)  # strip query params
                if clean not in seen and "/review/" in clean:
                    seen.add(clean)
                    unique.append(clean)
            return unique[:5]
        except Exception:
            pass

        return []

    async def _scrape_business(self, page, biz_url: str, max_reviews: int) -> list[Review]:
        reviews: list[Review] = []
        business_name = ""

        for page_num in range(1, 6):
            if len(reviews) >= max_reviews:
                break

            url = biz_url if page_num == 1 else f"{biz_url}?page={page_num}"
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(random.uniform(1, 2))

                content = await page.content()

                # Try __NEXT_DATA__ (most reliable)
                match = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', content, re.DOTALL)
                if match:
                    try:
                        data = json.loads(match.group(1))
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

                            date = None
                            try:
                                dates = r.get("dates") or {}
                                published = dates.get("publishedDate") or r.get("dates", {}).get("publishedDate")
                                if published:
                                    date = datetime.fromisoformat(published.replace("Z", "+00:00"))
                            except Exception:
                                pass

                            reviews.append(Review(
                                id=f"trustpilot_{r.get('id', len(reviews))}",
                                source=SourceType.TRUSTPILOT,
                                author=(r.get("consumer") or {}).get("displayName"),
                                text=full_text,
                                rating=rating,
                                date=date,
                                url=biz_url,
                                product_name=business_name or None,
                                metadata={"type": "review"},
                            ))
                            if len(reviews) >= max_reviews:
                                break

                        if review_list:
                            continue

                    except Exception:
                        pass

                # HTML fallback — find review cards via Playwright selectors
                if page_num == 1 and not business_name:
                    try:
                        h1 = await page.query_selector("h1")
                        if h1:
                            business_name = (await h1.inner_text()).strip()
                    except Exception:
                        pass

                cards = await page.query_selector_all('article[class*="review"], [data-service-review-card]')
                if not cards:
                    break

                for card in cards:
                    try:
                        text_el = await card.query_selector(
                            '[data-service-review-text-typography], p[class*="content"]'
                        )
                        text = (await text_el.inner_text()).strip() if text_el else ""
                        if len(text) < 10:
                            continue

                        author_el = await card.query_selector('[data-consumer-name-typography]')
                        author = (await author_el.inner_text()).strip() if author_el else None

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
                    except Exception:
                        continue

            except Exception as exc:
                logger.warning("Trustpilot page %d failed for %s: %s", page_num, biz_url, exc)
                break

        return reviews
