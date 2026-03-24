"""
Core tracking engine for specific Kalshi markets, identified by URL.

Architecture:
- User pastes Kalshi market URLs (one or more).
- Tickers are parsed from the URLs and stored.
- Every 2 hours during active hours (7 AM – 9 PM PT):
    - Fetches latest data for each tracked ticker (individual API calls).
    - Fires a Telegram alert if:
        * YES price moved ≥ 10 % relative to the previous check, OR
        * Total volume increased ≥ 10 % relative to the previous check.
- After each check (alert or not) the baseline is updated to the latest
  values so the next comparison is always period-over-period.
"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from kalshi_client import KalshiClient
from analyzer import analyze_market_movement
from telegram_notifier import send_telegram_alert, send_telegram_status

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Scheduling constants
# --------------------------------------------------------------------------
POLL_INTERVAL_SECONDS = 2 * 60 * 60   # 2 hours
ACTIVE_HOURS_START = 7                  # 7 AM Los Angeles time
ACTIVE_HOURS_END   = 21                 # 9 PM Los Angeles time
LA_TZ = ZoneInfo("America/Los_Angeles")

# --------------------------------------------------------------------------
# Alert thresholds
# --------------------------------------------------------------------------
PRICE_THRESHOLD  = 0.10   # 10 % relative move in YES probability
VOLUME_THRESHOLD = 0.10   # 10 % increase in total volume

# Persisted state file — survives server restarts within the same deployment
STATE_FILE = Path(__file__).parent / "tracker_state.json"


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def parse_kalshi_url(raw: str) -> str:
    """
    Extract the market ticker from a Kalshi URL or return the string as-is
    if it is already a bare ticker.

    Example:
        https://kalshi.com/markets/kxgovca/california-governors-race/kxgovca-26
        → "kxgovca-26"
    """
    raw = raw.strip()
    if raw.startswith("http"):
        ticker = raw.rstrip("/").split("/")[-1]
    else:
        ticker = raw
    # Kalshi API tickers are uppercase (e.g. KXGOVCA-26),
    # but web URLs use lowercase slugs — normalise here.
    return ticker.upper()


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------

class MarketSnapshot:
    """Point-in-time state of a single market."""

    def __init__(
        self,
        ticker: str,
        title: str,
        url: str,
        yes_price: float,
        volume: float,
    ):
        self.ticker    = ticker
        self.title     = title
        self.url       = url
        self.yes_price = yes_price
        self.volume    = volume
        self.timestamp = datetime.now(timezone.utc)


# --------------------------------------------------------------------------
# Tracker
# --------------------------------------------------------------------------

class PredictionTracker:
    """
    Monitors a set of Kalshi markets specified by URL and fires Telegram
    alerts on significant price or volume moves.
    """

    def __init__(
        self,
        anthropic_api_key: str,
        telegram_bot_token: str,
        telegram_chat_id: str,
        poll_interval: int = POLL_INTERVAL_SECONDS,
        min_volume: float = 0,          # kept for API compat, not used
        price_threshold: float = PRICE_THRESHOLD,
    ):
        self.kalshi              = KalshiClient()
        self.anthropic_api_key   = anthropic_api_key
        self.telegram_bot_token  = telegram_bot_token
        self.telegram_chat_id    = telegram_chat_id
        self.poll_interval       = poll_interval
        self.price_threshold     = price_threshold
        self.volume_threshold    = VOLUME_THRESHOLD

        # ticker → original URL submitted by user
        self.tracked_urls: dict[str, str] = {}
        # ticker → last-seen snapshot (used as baseline for next comparison)
        self.baseline: dict[str, MarketSnapshot] = {}
        # latest raw API responses, for the dashboard
        self.tracked_markets: list[dict] = []

        self.is_running = False
        self._task: Optional[asyncio.Task] = None
        self.alert_log: list[dict] = []

    # ------------------------------------------------------------------
    # Public control methods
    # ------------------------------------------------------------------

    async def set_markets(self, urls: list[str]) -> dict:
        """
        Replace the current tracked-market list with the given URLs.
        Stops any existing poll loop, fetches an initial baseline, then
        restarts the poll loop.
        """
        await self.stop_tracking(notify=bool(self.tracked_urls))

        self.tracked_urls   = {}
        self.baseline       = {}
        self.alert_log      = []
        self.tracked_markets = []

        for url in urls:
            ticker = parse_kalshi_url(url)
            if ticker:
                self.tracked_urls[ticker] = url

        self.is_running = True
        logger.info(f"Tracking {len(self.tracked_urls)} markets: {list(self.tracked_urls)}")

        result = await self._fetch_and_set_baseline()

        if self.baseline:
            lines = "\n".join(
                f"  • {s.title}\n"
                f"    YES: {s.yes_price:.0%}  |  Vol: ${s.volume:,.0f}"
                for s in self.baseline.values()
            )
            await send_telegram_status(
                self.telegram_bot_token,
                self.telegram_chat_id,
                f"🟢 <b>Prediction Tracker Started</b>\n\n"
                f"Monitoring {len(self.baseline)} market(s):\n{lines}\n\n"
                f"⏰ Checks every 2 hours  •  7 AM – 9 PM PT\n"
                f"🔔 Alerts on ≥10 % price move or ≥10 % volume increase",
            )
        else:
            await send_telegram_status(
                self.telegram_bot_token,
                self.telegram_chat_id,
                f"⚠️ <b>Tracker started</b> but could not load initial market data.\n"
                f"Tickers tried: {', '.join(self.tracked_urls.keys())}\n"
                f"Will retry at next check (7 AM – 9 PM PT, every 2 hours).",
            )

        self._save_state()
        self._task = asyncio.create_task(self._poll_loop())
        return result

    async def add_markets(self, urls: list[str]) -> dict:
        """Add more markets without resetting existing ones."""
        new_map: dict[str, str] = {}
        for url in urls:
            ticker = parse_kalshi_url(url)
            if ticker and ticker not in self.tracked_urls:
                new_map[ticker] = url

        if not new_map:
            return {"added": [], "already_tracked": [parse_kalshi_url(u) for u in urls]}

        for ticker, url in new_map.items():
            self.tracked_urls[ticker] = url

        markets = await self.kalshi.get_markets_batch(list(new_map.keys()))
        added = []
        for m in markets:
            ticker = m.get("ticker", "")
            if not ticker or ticker in self.baseline:
                continue
            yes_price = KalshiClient.get_yes_price(m)
            volume    = KalshiClient._parse_volume(m)
            url       = new_map.get(ticker, f"https://kalshi.com/markets/{ticker}")
            if yes_price is not None:
                self.baseline[ticker] = MarketSnapshot(
                    ticker, m.get("title", ticker), url, yes_price, volume
                )
                added.append(ticker)

        logger.info(f"Added {len(added)} markets: {added}")
        self._save_state()
        return {"added": added}

    def remove_market(self, ticker: str) -> bool:
        """Stop tracking a market by ticker."""
        found = ticker in self.tracked_urls
        self.tracked_urls.pop(ticker, None)
        self.baseline.pop(ticker, None)
        self.tracked_markets = [
            m for m in self.tracked_markets if m.get("ticker") != ticker
        ]
        logger.info(f"Removed market: {ticker}")
        self._save_state()
        return found

    async def stop_tracking(self, notify: bool = True):
        """Cancel the poll loop and optionally send a Telegram notice."""
        self.is_running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

        if notify and self.tracked_urls:
            await send_telegram_status(
                self.telegram_bot_token,
                self.telegram_chat_id,
                f"🔴 Tracking stopped ({len(self.tracked_urls)} markets removed).",
            )
            # Clear saved state so markets don't auto-restore on next server start
            self.tracked_urls = {}
            self._save_state()
        logger.info("Tracker stopped")

    # ------------------------------------------------------------------
    # Internal: baseline fetch
    # ------------------------------------------------------------------

    async def _fetch_and_set_baseline(self) -> dict:
        """Fetch current data for all tracked tickers and store as baseline."""
        if not self.tracked_urls:
            return {"loaded": [], "failed": []}

        tickers = list(self.tracked_urls.keys())
        logger.info(f"Fetching baseline for tickers: {tickers}")
        markets = await self.kalshi.get_markets_batch(tickers)
        self.tracked_markets = markets
        logger.info(f"API returned {len(markets)} market(s) for {len(tickers)} ticker(s)")

        loaded = []
        for m in markets:
            ticker    = m.get("ticker", "")
            yes_price = KalshiClient.get_yes_price(m)
            volume    = KalshiClient._parse_volume(m)
            url       = self.tracked_urls.get(ticker, "")
            logger.info(
                f"Market data — ticker={ticker!r} title={m.get('title')!r} "
                f"last_price={m.get('last_price')} yes_bid={m.get('yes_bid')} "
                f"yes_price={yes_price} volume={volume}"
            )
            if ticker and yes_price is not None:
                self.baseline[ticker] = MarketSnapshot(
                    ticker, m.get("title", ticker), url, yes_price, volume
                )
                loaded.append(ticker)
            else:
                logger.warning(
                    f"Skipped {ticker!r}: yes_price={yes_price} "
                    f"(market keys: {list(m.keys())})"
                )

        failed = [t for t in tickers if t not in self.baseline]
        if failed:
            logger.warning(f"Could not load baseline for tickers: {failed}")
        logger.info(f"Baseline set for {len(loaded)} markets")
        return {"loaded": loaded, "failed": failed}

    # ------------------------------------------------------------------
    # Internal: poll cycle
    # ------------------------------------------------------------------

    async def _poll_markets(self):
        """
        Fetch latest data for all tracked markets and fire alerts when
        thresholds are crossed.  Always updates the baseline afterwards
        so each check compares period-over-period.
        """
        if not self.tracked_urls:
            return

        tickers = list(self.tracked_urls.keys())
        logger.info(f"Polling {len(tickers)} markets: {tickers}")

        try:
            markets = await self.kalshi.get_markets_batch(tickers)
            self.tracked_markets = markets

            for m in markets:
                ticker    = m.get("ticker", "")
                title     = m.get("title", ticker)
                new_price = KalshiClient.get_yes_price(m)
                new_vol   = KalshiClient._parse_volume(m)
                url       = self.tracked_urls.get(ticker, "")

                if not ticker or new_price is None:
                    continue

                # First time seeing this market — just record baseline
                if ticker not in self.baseline:
                    self.baseline[ticker] = MarketSnapshot(
                        ticker, title, url, new_price, new_vol
                    )
                    continue

                snap      = self.baseline[ticker]
                old_price = snap.yes_price
                old_vol   = snap.volume

                # ── Alert condition 1: price moved ≥10 % (relative) ──
                price_ratio  = abs(new_price - old_price) / old_price if old_price > 0 else 0
                price_alert  = price_ratio >= self.price_threshold

                # ── Alert condition 2: volume increased ≥10 % ──
                vol_ratio    = (new_vol - old_vol) / old_vol if old_vol > 0 else 0
                volume_alert = vol_ratio >= self.volume_threshold

                if price_alert or volume_alert:
                    changes: list[dict] = []
                    if price_alert:
                        changes.append({
                            "type":      "PRICE",
                            "direction": "UP" if new_price > old_price else "DOWN",
                            "old":       old_price,
                            "new":       new_price,
                            "pct":       price_ratio,
                        })
                    if volume_alert:
                        changes.append({
                            "type":      "VOLUME",
                            "direction": "UP",
                            "old":       old_vol,
                            "new":       new_vol,
                            "pct":       vol_ratio,
                        })

                    logger.info(f"ALERT on {ticker}: {[c['type'] for c in changes]}")

                    analysis = await analyze_market_movement(
                        api_key=self.anthropic_api_key,
                        market_title=title,
                        market_url=url,
                        changes=changes,
                        yes_price=new_price,
                    )

                    await send_telegram_alert(
                        bot_token=self.telegram_bot_token,
                        chat_id=self.telegram_chat_id,
                        market_title=title,
                        market_url=url,
                        yes_price=new_price,
                        volume=new_vol,
                        changes=changes,
                        analysis=analysis,
                    )

                    self.alert_log.append({
                        "ticker":        ticker,
                        "title":         title,
                        "url":           url,
                        "yes_price":     new_price,
                        "old_yes_price": old_price,
                        "volume":        new_vol,
                        "old_volume":    old_vol,
                        "changes":       changes,
                        "analysis":      analysis,
                        "timestamp":     datetime.now(timezone.utc).isoformat(),
                    })
                    self.alert_log = self.alert_log[-50:]

                # Always update baseline to this check's values
                self.baseline[ticker] = MarketSnapshot(
                    ticker, title, url, new_price, new_vol
                )

        except Exception as e:
            logger.error(f"Error polling markets: {e}")

    # ------------------------------------------------------------------
    # Internal: scheduling
    # ------------------------------------------------------------------

    @staticmethod
    def _is_active_hours() -> bool:
        now_la = datetime.now(LA_TZ)
        return ACTIVE_HOURS_START <= now_la.hour < ACTIVE_HOURS_END

    async def _poll_loop(self):
        """Sleep in 60-second ticks so cancellation is fast."""
        while self.is_running:
            try:
                elapsed = 0
                while elapsed < POLL_INTERVAL_SECONDS and self.is_running:
                    await asyncio.sleep(60)
                    elapsed += 60

                if not self.is_running:
                    break

                if not self._is_active_hours():
                    now_la = datetime.now(LA_TZ)
                    logger.info(
                        f"Outside active hours ({now_la.strftime('%I:%M %p')} PT). "
                        f"Skipping poll."
                    )
                    continue

                logger.info("Active hours — polling tracked markets")
                await self._poll_markets()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Poll loop error: {e}")
                await asyncio.sleep(5)

    # ------------------------------------------------------------------
    # State persistence
    # ------------------------------------------------------------------

    def _save_state(self):
        """Write tracked URLs to disk so they survive server restarts."""
        try:
            STATE_FILE.write_text(
                json.dumps({"tracked_urls": self.tracked_urls}, indent=2)
            )
            logger.info(f"State saved: {len(self.tracked_urls)} market(s)")
        except Exception as e:
            logger.error(f"Failed to save state: {e}")

    @classmethod
    def _load_saved_urls(cls) -> dict[str, str]:
        """Read previously saved tracked URLs from disk. Returns {} if none."""
        try:
            if STATE_FILE.exists():
                data = json.loads(STATE_FILE.read_text())
                urls = data.get("tracked_urls", {})
                if urls:
                    logger.info(f"Loaded {len(urls)} saved market(s) from {STATE_FILE}")
                return urls
        except Exception as e:
            logger.error(f"Failed to load saved state: {e}")
        return {}

    async def restore_markets(self, tracked_urls: dict[str, str]):
        """
        Re-establish tracking from a previously saved URL map, silently
        (no Telegram notification). Called on server startup.
        """
        if not tracked_urls:
            return
        self.tracked_urls = dict(tracked_urls)
        self.is_running = True
        await self._fetch_and_set_baseline()
        logger.info(
            f"Restored tracking for {len(self.baseline)} market(s) "
            f"from saved state"
        )
        self._task = asyncio.create_task(self._poll_loop())

    # ------------------------------------------------------------------
    # Status (for dashboard API)
    # ------------------------------------------------------------------

    def get_status(self) -> dict:
        markets_info = []

        # Markets with a loaded baseline — show full price/volume data
        for ticker, snap in self.baseline.items():
            current = next(
                (m for m in self.tracked_markets if m.get("ticker") == ticker),
                None,
            )
            cur_price = KalshiClient.get_yes_price(current) if current else snap.yes_price
            cur_vol   = KalshiClient._parse_volume(current) if current else snap.volume
            markets_info.append({
                "ticker":             ticker,
                "title":              snap.title,
                "url":                snap.url,
                "yes_price":          cur_price,
                "baseline_yes_price": snap.yes_price,
                "volume":             cur_vol,
                "baseline_volume":    snap.volume,
                "baseline_set_at":    snap.timestamp.isoformat(),
                "loading":            False,
            })

        # Markets tracked but baseline not yet loaded (e.g. API call failed)
        for ticker, url in self.tracked_urls.items():
            if ticker not in self.baseline:
                markets_info.append({
                    "ticker":             ticker,
                    "title":              ticker,   # placeholder until API loads
                    "url":                url,
                    "yes_price":          None,
                    "baseline_yes_price": None,
                    "volume":             None,
                    "baseline_volume":    None,
                    "baseline_set_at":    None,
                    "loading":            True,
                })

        return {
            "is_running":       self.is_running,
            "market_count":     len(self.tracked_urls),
            "markets":          markets_info,
            "recent_alerts":    self.alert_log[-10:],
            "price_threshold":  self.price_threshold,
            "volume_threshold": self.volume_threshold,
        }
