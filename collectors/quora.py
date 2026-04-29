from __future__ import annotations

import asyncio
import logging
import random

from collectors.base import BaseCollector
from collectors.playwright_utils import stealth_browser, run_in_playwright_thread
from config.settings import Settings
from models.schemas import Review, SourceType

logger = logging.getLogger(__name__)


class QuoraCollector(BaseCollector):
    """Collect Quora Q&A content via Playwright.

    Uses Yahoo site:quora.com search to find question URLs, then
    scrapes answer text from each Quora page.
    """

    source_type = SourceType.QUORA

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
            logger.warning("Quora Playwright unavailable: %s", exc)
            return []

    async def _collect_playwright(self, query: str, max_results: int) -> list[Review]:
        reviews: list[Review] = []

        try:
            async with stealth_browser() as (browser, context):
                page = await context.new_page()

                quora_urls = await self._find_via_yahoo(page, query)
                logger.debug("Found %d Quora URLs for: %s", len(quora_urls), query)

                for url in quora_urls:
                    if len(reviews) >= max_results:
                        break
                    try:
                        page_reviews = await self._scrape_question_page(
                            page, url, max_results - len(reviews)
                        )
                        reviews.extend(page_reviews)
                    except Exception:
                        logger.debug("Failed to scrape Quora page: %s", url)
                    await asyncio.sleep(random.uniform(2, 4))

        except Exception:
            logger.exception("Error collecting Quora data for query: %s", query)

        logger.info("Collected %d items from Quora", len(reviews))
        return reviews[:max_results]

    async def _find_via_yahoo(self, page, query: str) -> list[str]:
        """Use Yahoo search to find Quora question URLs (more accessible than Google)."""
        import urllib.parse
        search_query = urllib.parse.quote(f"site:quora.com {query}")
        url = f"https://search.yahoo.com/search?p={search_query}&n=10"
        try:
            await page.goto(url, wait_until="domcontentloaded")
            await asyncio.sleep(random.uniform(2, 3))

            links = await page.eval_on_selector_all(
                'a[href]',
                "els => els.map(e => e.href).filter(h => h.includes('quora.com') && !h.includes('yahoo'))",
            )
            seen = set()
            clean = []
            for link in links:
                if "quora.com" in link and link not in seen and "/profile/" not in link and "q.quora.com" not in link and "business.quora.com" not in link:
                    seen.add(link)
                    clean.append(link)
            return clean[:8]
        except Exception:
            logger.debug("Yahoo search for Quora URLs failed")
            return []

    async def _scrape_question_page(self, page, url: str, max_items: int) -> list[Review]:
        reviews = []
        await page.goto(url, wait_until="domcontentloaded")
        await asyncio.sleep(random.uniform(2, 4))

        # Dismiss login modal if present
        try:
            close_btn = await page.query_selector(
                '[class*="modal"] [aria-label="Close"], button[class*="close"]'
            )
            if close_btn:
                await close_btn.click()
                await asyncio.sleep(0.5)
        except Exception:
            pass

        question = ""
        for selector in ['h1', '[class*="question_title"]', '[class*="QuestionText"]']:
            try:
                el = await page.query_selector(selector)
                if el:
                    question = (await el.inner_text()).strip()
                    if question:
                        break
            except Exception:
                pass

        # Expand collapsed answers
        for _ in range(3):
            try:
                more_btns = await page.query_selector_all(
                    'button[class*="more"], span:has-text("more")'
                )
                for btn in more_btns[:5]:
                    await btn.click()
                    await asyncio.sleep(0.3)
            except Exception:
                break

        # Collect all substantial paragraphs — Quora's class names change frequently
        # so we use a broad paragraph approach and filter UI noise
        skip_phrases = {
            "sign in", "sign up", "log in", "related questions", "sponsored",
            "see more", "read more", "continue reading", "view more",
            "quora user", "originally answered", "profile photo",
            "something went wrong", "wait a moment and try again",
            "reload the page", "try again", "page not found",
        }

        raw_paras: list[str] = []
        para_els = await page.query_selector_all("p, [class*='qtext_para']")
        for el in para_els:
            try:
                text = (await el.inner_text()).strip()
                if len(text) < 40:
                    continue
                if any(kw in text.lower() for kw in skip_phrases):
                    continue
                raw_paras.append(text)
            except Exception:
                pass

        # Group consecutive paragraphs into answer-length chunks
        chunk_size = 5
        chunks: list[str] = []
        for i in range(0, len(raw_paras), chunk_size):
            chunk = "\n\n".join(raw_paras[i:i + chunk_size])
            if len(chunk) >= 80:
                chunks.append(chunk)

        for chunk in chunks[:max_items]:
            full_text = f"Q: {question}\n\nA: {chunk}" if question else chunk
            reviews.append(Review(
                id=f"quora_{hash(url + chunk) % 10**9}",
                source=SourceType.QUORA,
                author=None,
                text=full_text[:3000],
                rating=None,
                date=None,
                url=url,
                product_name=None,
                metadata={"question": question, "type": "answer"},
            ))

        return reviews
