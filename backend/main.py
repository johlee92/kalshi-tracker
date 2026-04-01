"""
FastAPI backend for the Kalshi Prediction Tracker.
Serves the API + static frontend.
"""

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

from config import Settings
from tracker import PredictionTracker, parse_kalshi_url

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

settings = Settings()
tracker: PredictionTracker = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global tracker
    tracker = PredictionTracker(
        anthropic_api_key=settings.anthropic_api_key,
        telegram_bot_token=settings.telegram_bot_token,
        telegram_chat_id=settings.telegram_chat_id,
        poll_interval=settings.poll_interval_seconds,
        min_volume=settings.min_volume_usd,
        price_threshold=settings.price_change_threshold,
    )
    logger.info("Prediction Tracker initialized")

    # Restore any markets that were being tracked before the server restarted
    saved = PredictionTracker._load_saved_urls()
    if saved:
        logger.info(f"Auto-restoring {len(saved)} saved market(s) on startup")
        await tracker.restore_markets(saved)

    yield
    await tracker.stop_tracking(notify=False)
    await tracker.kalshi.close()
    logger.info("Prediction Tracker shut down")


app = FastAPI(
    title="Kalshi Prediction Tracker",
    description="Real-time prediction market tracker with AI-powered Telegram alerts",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── API models ──────────────────────────────────────────────────────────────

class TrackRequest(BaseModel):
    urls: list[str]           # one or more Kalshi market URLs


class AddMarketsRequest(BaseModel):
    urls: list[str]


class RemoveMarketRequest(BaseModel):
    ticker: str


# ── Routes ──────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    """Lightweight healthcheck — always 200 once the server is up."""
    return {"status": "ok"}


@app.get("/api/test-kalshi")
async def test_kalshi(ticker: str = ""):
    """
    Diagnostic endpoint.
    - With ?ticker=KXPGAMASTERS-26  → looks up that specific ticker and shows raw fields
    - Without ticker param           → fetches the first open market and self-tests the pipeline
    """
    import httpx
    from kalshi_client import KalshiClient as KC
    base = "https://api.elections.kalshi.com/trade-api/v2/"
    results = {}

    async with httpx.AsyncClient(base_url=base, timeout=15.0,
                                  headers={"Accept": "application/json"}) as client:

        if ticker:
            # Look up the specific ticker the user asked about
            t = ticker.upper()
            try:
                r = await client.get(f"markets/{t}")
                if r.status_code == 200:
                    m = r.json().get("market") or r.json()
                    # Show ALL dollar/price/volume fields so we can see what's populated
                    price_fields = {k: v for k, v in m.items()
                                    if any(x in k for x in ("price", "bid", "ask", "volume", "liquidity"))}
                    results["ticker_lookup"] = {
                        "status_code": 200,
                        "url_called": str(r.url),
                        "ticker": m.get("ticker"),
                        "title": m.get("title"),
                        "status": m.get("status"),
                        "all_price_volume_fields": price_fields,
                        "parsed_yes_price": KC.get_yes_price(m),
                        "parsed_volume": KC._parse_volume(m),
                        "verdict": "✅ Ticker found" if KC.get_yes_price(m) is not None else "⚠️ Ticker found but yes_price is None — check price fields above",
                    }
                else:
                    results["ticker_lookup"] = {
                        "status_code": r.status_code,
                        "url_called": str(r.url),
                        "body": r.text[:300],
                        "verdict": "❌ Ticker not found (404 = closed/resolved market)",
                    }
            except Exception as e:
                results["ticker_lookup"] = {"error": str(e)}

        else:
            # Self-test: fetch open market from list, then look it up individually
            open_ticker = None
            try:
                r = await client.get("markets", params={"limit": 1, "status": "open"})
                body = r.json() if r.status_code == 200 else {}
                sample = (body.get("markets") or [{}])[0]
                open_ticker = sample.get("ticker")
                results["step1_list"] = {
                    "status_code": r.status_code,
                    "url_called": str(r.url),
                    "found_ticker": open_ticker,
                    "found_title": sample.get("title"),
                    "parsed_yes_price": KC.get_yes_price(sample),
                    "parsed_volume": KC._parse_volume(sample),
                }
            except Exception as e:
                results["step1_list"] = {"error": str(e)}

            if open_ticker:
                try:
                    r2 = await client.get(f"markets/{open_ticker}")
                    if r2.status_code == 200:
                        m = r2.json().get("market") or r2.json()
                        results["step2_single_lookup"] = {
                            "status_code": 200,
                            "url_called": str(r2.url),
                            "ticker": m.get("ticker"),
                            "title": m.get("title"),
                            "status": m.get("status"),
                            "parsed_yes_price": KC.get_yes_price(m),
                            "parsed_volume": KC._parse_volume(m),
                            "verdict": "✅ Full pipeline working",
                        }
                    else:
                        results["step2_single_lookup"] = {
                            "status_code": r2.status_code,
                            "body": r2.text[:300],
                            "verdict": "❌ Single lookup failed",
                        }
                except Exception as e:
                    results["step2_single_lookup"] = {"error": str(e)}

    return results


@app.post("/api/track")
async def start_tracking(req: TrackRequest):
    """
    Set (replace) the list of tracked markets.
    Accepts one or more Kalshi market URLs.
    """
    urls = [u.strip() for u in req.urls if u.strip()]
    if not urls:
        raise HTTPException(400, "At least one URL is required.")

    missing = settings.missing_vars()
    if missing:
        raise HTTPException(
            400,
            f"Missing required environment variables: {', '.join(missing)}. "
            "Add them in your Railway Variables settings.",
        )

    tickers = [parse_kalshi_url(u) for u in urls]
    result = await tracker.set_markets(urls)
    return {"status": "ok", "tickers": tickers, "loaded": result.get("loaded", [])}


@app.post("/api/add")
async def add_markets(req: AddMarketsRequest):
    """Add more markets to the current tracking list without resetting."""
    urls = [u.strip() for u in req.urls if u.strip()]
    if not urls:
        raise HTTPException(400, "At least one URL is required.")

    missing = settings.missing_vars()
    if missing:
        raise HTTPException(400, f"Missing env vars: {', '.join(missing)}")

    result = await tracker.add_markets(urls)
    return {"status": "ok", **result}


@app.delete("/api/market/{ticker}")
async def remove_market(ticker: str):
    """Remove a single market from tracking by its ticker."""
    removed = tracker.remove_market(ticker)
    if not removed:
        raise HTTPException(404, f"Ticker '{ticker}' is not currently tracked.")
    return {"status": "ok", "removed": ticker}


@app.post("/api/stop")
async def stop_tracking():
    """Stop all tracking."""
    if tracker:
        await tracker.stop_tracking()
    return {"status": "stopped"}


@app.get("/api/status")
async def get_status():
    """Return current tracker state and recent alerts for the dashboard."""
    if tracker is None:
        return {"is_running": False, "market_count": 0, "markets": [], "recent_alerts": []}
    return tracker.get_status()


# ── Frontend ─────────────────────────────────────────────────────────────────

FRONTEND_DIR = Path(__file__).parent / "static"


@app.get("/")
async def serve_index():
    index = FRONTEND_DIR / "index.html"
    if index.exists():
        return FileResponse(index)
    return {"message": "Kalshi Prediction Tracker API is running. Frontend not found."}


if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
