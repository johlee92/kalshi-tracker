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
from tracker import PredictionTracker

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# Load settings
settings = Settings()

# Global tracker instance
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
    yield
    await tracker.stop_tracking(notify=False)
    await tracker.kalshi.close()
    logger.info("Prediction Tracker shut down")


app = FastAPI(
    title="Kalshi Prediction Tracker",
    description="Real-time prediction market sentiment tracker with AI-powered alerts",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS (allow the frontend)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- API Models ---


class TrackRequest(BaseModel):
    topic: str


class SettingsUpdate(BaseModel):
    poll_interval: int | None = None
    min_volume: float | None = None
    price_threshold: float | None = None


# --- API Routes ---


@app.get("/health")
async def health():
    """Lightweight healthcheck — always returns 200 once the server is up."""
    return {"status": "ok"}


@app.post("/api/track")
async def start_tracking(req: TrackRequest):
    """Start tracking a new topic."""
    if not req.topic.strip():
        raise HTTPException(400, "Topic cannot be empty")
    missing = settings.missing_vars()
    if missing:
        raise HTTPException(
            400,
            f"Missing required environment variables: {', '.join(missing)}. "
            "Add them in your Railway Variables settings."
        )
    await tracker.start_tracking(req.topic.strip())
    return {"status": "ok", "topic": req.topic.strip()}


@app.post("/api/stop")
async def stop_tracking():
    """Stop current tracking."""
    if tracker:
        await tracker.stop_tracking()
    return {"status": "stopped"}


@app.get("/api/status")
async def get_status():
    """Get current tracker status, markets, and recent alerts."""
    if tracker is None:
        return {"topic": None, "is_running": False, "market_count": 0,
                "markets": [], "recent_alerts": []}
    return tracker.get_status()


@app.patch("/api/settings")
async def update_settings(update: SettingsUpdate):
    """Update tracker settings on the fly."""
    if update.poll_interval is not None:
        tracker.poll_interval = max(10, update.poll_interval)
    if update.min_volume is not None:
        tracker.min_volume = max(0, update.min_volume)
    if update.price_threshold is not None:
        tracker.price_threshold = max(0.01, min(1.0, update.price_threshold))
    return {
        "poll_interval": tracker.poll_interval,
        "min_volume": tracker.min_volume,
        "price_threshold": tracker.price_threshold,
    }


# --- Serve Frontend ---

FRONTEND_DIR = Path(__file__).parent / "static"


@app.get("/")
async def serve_index():
    index = FRONTEND_DIR / "index.html"
    if index.exists():
        return FileResponse(index)
    return {"message": "Kalshi Prediction Tracker API is running. Frontend not found."}


# Mount static files (CSS, JS, etc.)
if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
