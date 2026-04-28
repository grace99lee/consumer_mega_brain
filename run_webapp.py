"""Launch the Consumer Insights webapp."""
import os
import sys
from pathlib import Path

# Ensure project root is on path
sys.path.insert(0, str(Path(__file__).parent))

# On Windows, uvicorn defaults to SelectorEventLoop which cannot spawn
# subprocesses — required by Playwright to launch browser instances.
# Force ProactorEventLoop before uvicorn starts so Playwright works.
if sys.platform == "win32":
    import asyncio
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

import uvicorn

if __name__ == "__main__":
    # Railway (and other cloud platforms) inject PORT via environment variable.
    # Fall back to 8000 for local development.
    port = int(os.environ.get("PORT", 8000))
    # Bind to 0.0.0.0 in production so the app is reachable externally.
    # Fall back to 127.0.0.1 locally.
    host = os.environ.get("HOST", "127.0.0.1")
    # Disable hot-reload in production (Railway sets RAILWAY_ENVIRONMENT).
    dev = not os.environ.get("RAILWAY_ENVIRONMENT")

    uvicorn.run(
        "webapp.app:app",
        host=host,
        port=port,
        reload=dev,
        reload_dirs=[str(Path(__file__).parent)] if dev else None,
    )
