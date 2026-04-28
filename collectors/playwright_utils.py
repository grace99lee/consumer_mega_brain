"""Shared Playwright browser setup with bot-detection evasion."""
from __future__ import annotations

import asyncio
import sys
import random
from contextlib import asynccontextmanager
from typing import Any, Callable, Coroutine, TypeVar

_T = TypeVar("_T")


async def run_in_playwright_thread(coro_fn: Callable[[], Coroutine[Any, Any, _T]]) -> _T:
    """
    Run a Playwright coroutine in a dedicated thread with a ProactorEventLoop
    (Windows) or a fresh default loop (other platforms).

    This is needed because uvicorn on Windows uses SelectorEventLoop, which
    cannot spawn subprocesses — a hard requirement for Playwright to launch
    Chromium.  Running Playwright in its own thread+loop sidesteps the issue.
    """
    def _thread_main() -> _T:
        if sys.platform == "win32":
            loop: asyncio.AbstractEventLoop = asyncio.ProactorEventLoop()
        else:
            loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(coro_fn())
        finally:
            loop.close()
            asyncio.set_event_loop(None)

    return await asyncio.to_thread(_thread_main)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36 Edg/121.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
]

# Minimal args — avoid flags that are known bot-detection signals
LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
]

# Injected into every page to mask automation signals
STEALTH_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
Object.defineProperty(navigator, 'mimeTypes', {get: () => [1, 2, 3]});
window.chrome = {runtime: {}, loadTimes: function(){}, csi: function(){}, app: {}};
const originalQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (parameters) => (
  parameters.name === 'notifications'
    ? Promise.resolve({ state: Notification.permission })
    : originalQuery(parameters)
);
"""


@asynccontextmanager
async def stealth_browser(headless: bool = True):
    """Async context manager yielding a (browser, context) tuple with stealth config."""
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless, args=LAUNCH_ARGS)
        context = await browser.new_context(
            user_agent=random.choice(USER_AGENTS),
            locale="en-US",
            timezone_id="America/New_York",
            viewport={"width": 1280, "height": 800},
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
                "sec-ch-ua": '"Chromium";v="122", "Not(A:Brand";v="24", "Google Chrome";v="122"',
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Windows"',
            },
        )
        await context.add_init_script(STEALTH_SCRIPT)
        try:
            yield browser, context
        finally:
            await browser.close()
