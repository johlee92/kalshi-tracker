"""
Core tracking engine that monitors Kalshi markets and detects movements.

Architecture:
- On start_tracking(), does ONE full scan of all Kalshi markets to find
  matches for the topic.  Caches the matched tickers.
- Every poll cycle, fetches ONLY those cached tickers individually
  (1 API call per market instead of 80+).
- Every RESCAN_INTERVAL cycles, does a fresh full scan to discover any
  new markets that appeared since the last scan.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from kalshi_client import KalshiClient
from analyzer import analyze_market_movement
from telegram_notifier import send_telegram_alert, send_telegram_status

logger = logging.getLogger(__name__)

# Polling schedule
POLL_INTERVAL_SECONDS = 2 * 60 * 60  # 2 hours between checks
ACTIVE_HOURS_START = 7   # 7 AM Los Angeles time
ACTIVE_HOURS_END = 21    # 9 PM Los Angeles time
LA_TZ = ZoneInfo("America/Los_Angeles")


class MarketSnapshot:
    """Stores a point-in-time snapshot of a market's price."""

    def __init__(self, ticker: str, title: str, price: float, volume: float):
        self.ticker = ticker
        self.title = title
        self.price = price
        self.volume = volume
        self.timestamp = datetime.now(timezone.utc)


class PredictionTracker:
    """
    Monitors Kalshi markets for a given topic and sends alerts on big moves.
    """

    def __init__(
        self,
        anthropic_api_key: str,
        telegram_bot_token: str,
        telegram_chat_id: str,
        poll_interval: int = 60,
        min_volume: float = 10000,
        price_threshold: float = 0.10,
    ):
        self.kalshi = KalshiClient()
        self.anthropic_api_key = anthropic_api_key
        self.telegram_bot_token = telegram_bot_token
        self.telegram_chat_id = telegram_chat_id
        self.poll_interval = poll_interval
        self.min_volume = min_volume
        self.price_threshold = price_threshold

        # State
        self.current_topic: Optional[str] = None
        self.baseline: dict[str, MarketSnapshot] = {}  # ticker -> snapshot
        self.cached_tickers: list[str] = []  # tickers to poll each cycle
        self.is_running = False
        self._task: Optional[asyncio.Task] = None
        self.alert_log: list[dict] = []  # recent alerts for the dashboard
        self.tracked_markets: list[dict] = []  # latest market data
        self._cycles_since_rescan = 0

    async def start_tracking(self, topic: str):
        """Start or restart tracking for a new topic."""
        await self.stop_tracking(notify=False)

        self.current_topic = topic
        self.baseline = {}
        self.cached_tickers = []
        self.alert_log = []
        self.tracked_markets = []
        self._cycles_since_rescan = 0
        self.is_running = True

        logger.info(f"Starting tracker for topic: '{topic}'")

        # Do ONE full scan to discover matching markets
        await self._full_scan()

        if not self.baseline:
            logger.warning(f"No markets found for topic: '{topic}'")
            await send_telegram_status(
                self.telegram_bot_token,
                self.telegram_chat_id,
                f"⚠️ No Kalshi markets with volume ≥ ${self.min_volume:,.0f} "
                f"found for topic: <b>{topic}</b>\n\n"
                f"Will re-check every 2 hours (7 AM – 9 PM LA time).",
            )
        else:
            market_list = "\n".join(
                f"  • {s.title} ({s.price:.0%})"
                for s in list(self.baseline.values())[:10]
            )
            await send_telegram_status(
                self.telegram_bot_token,
                self.telegram_chat_id,
                f"🟢 Tracking started for: <b>{topic}</b>\n\n"
                f"Monitoring {len(self.baseline)} market(s):\n{market_list}\n\n"
                f"Alert threshold: {self.price_threshold:.0%} change\n"
                f"Polling every 2 hours (7 AM – 9 PM LA time)",
            )

        # Start background polling loop
        self._task = asyncio.create_task(self._poll_loop())

    async def stop_tracking(self, notify: bool = True):
        """Stop the current tracking session."""
        self.is_running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

        if notify and self.current_topic:
            await send_telegram_status(
                self.telegram_bot_token,
                self.telegram_chat_id,
                f"🔴 Tracking stopped for: <b>{self.current_topic}</b>",
            )
        logger.info("Tracker stopped")

    async def _full_scan(self):
        """
        Do a full pagination of all Kalshi markets to find topic matches.
        This is the expensive operation — only done once at startup and
        periodically thereafter.
        """
        try:
            logger.info(f"Starting full market scan for topic: '{self.current_topic}'")
            markets = await self.kalshi.search_markets_by_topic(
                self.current_topic, self.min_volume
            )
            self.tracked_markets = markets

            # Cache the matched tickers
            new_tickers = []
            for m in markets:
                ticker = m.get("ticker", "")
                title = m.get("title", "Unknown")
                price = KalshiClient.get_yes_price(m)
                volume = KalshiClient._parse_volume(m)

                if price is not None and ticker:
                    new_tickers.append(ticker)
                    if ticker not in self.baseline:
                        self.baseline[ticker] = MarketSnapshot(
                            ticker, title, price, volume
                        )
                    else:
                        self.baseline[ticker].volume = volume

            self.cached_tickers = new_tickers
            self._cycles_since_rescan = 0
            logger.info(
                f"Full scan complete. Cached {len(self.cached_tickers)} tickers "
                f"for fast polling."
            )

        except Exception as e:
            logger.error(f"Error during full scan: {e}")

    async def _poll_cached_markets(self):
        """
        Fetch only the cached tickers individually — much lighter than a
        full scan. Typically 1-20 API calls instead of 80+.
        """
        if not self.cached_tickers:
            return

        try:
            logger.info(
                f"Polling {len(self.cached_tickers)} cached markets individually"
            )
            markets = await self.kalshi.get_markets_batch(self.cached_tickers)
            self.tracked_markets = markets

            for m in markets:
                ticker = m.get("ticker", "")
                title = m.get("title", "Unknown")
                new_price = KalshiClient.get_yes_price(m)
                volume = KalshiClient._parse_volume(m)

                if new_price is None or not ticker:
                    continue

                if ticker not in self.baseline:
                    self.baseline[ticker] = MarketSnapshot(
                        ticker, title, new_price, volume
                    )
                    continue

                old_price = self.baseline[ticker].price
                change = abs(new_price - old_price)

                if change >= self.price_threshold:
                    direction = "UP" if new_price > old_price else "DOWN"
                    logger.info(
                        f"ALERT: {title} moved {direction} "
                        f"{old_price:.0%} -> {new_price:.0%}"
                    )

                    # Get Claude's analysis
                    analysis = await analyze_market_movement(
                        api_key=self.anthropic_api_key,
                        market_title=title,
                        direction=direction,
                        old_price=old_price,
                        new_price=new_price,
                        topic=self.current_topic,
                    )

                    # Send Telegram alert
                    await send_telegram_alert(
                        bot_token=self.telegram_bot_token,
                        chat_id=self.telegram_chat_id,
                        market_title=title,
                        old_price=old_price,
                        new_price=new_price,
                        volume=volume,
                        analysis=analysis,
                        topic=self.current_topic,
                    )

                    # Log the alert
                    alert_entry = {
                        "ticker": ticker,
                        "title": title,
                        "old_price": old_price,
                        "new_price": new_price,
                        "direction": direction,
                        "analysis": analysis,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                    self.alert_log.append(alert_entry)
                    self.alert_log = self.alert_log[-50:]

                    # Update baseline to new price
                    self.baseline[ticker] = MarketSnapshot(
                        ticker, title, new_price, volume
                    )
                else:
                    self.baseline[ticker].volume = volume

            # Remove any tickers that no longer exist (404'd during batch)
            live_tickers = {m.get("ticker") for m in markets}
            stale = [t for t in self.cached_tickers if t not in live_tickers]
            if stale:
                logger.info(f"Removing {len(stale)} stale tickers: {stale}")
                self.cached_tickers = [
                    t for t in self.cached_tickers if t in live_tickers
                ]
                for t in stale:
                    self.baseline.pop(t, None)

        except Exception as e:
            logger.error(f"Error polling cached markets: {e}")

    @staticmethod
    def _is_active_hours() -> bool:
        """Return True if current LA time is between 7 AM and 9 PM."""
        now_la = datetime.now(LA_TZ)
        return ACTIVE_HOURS_START <= now_la.hour < ACTIVE_HOURS_END

    async def _poll_loop(self):
        """
        Main polling loop. Checks cached markets every 2 hours,
        but only during active hours (7 AM – 9 PM Los Angeles time).
        Sleeps in 60-second increments so the loop can be cancelled quickly.
        """
        while self.is_running:
            try:
                # Sleep for POLL_INTERVAL_SECONDS in small increments
                elapsed = 0
                while elapsed < POLL_INTERVAL_SECONDS and self.is_running:
                    await asyncio.sleep(60)
                    elapsed += 60

                if not self.is_running:
                    break

                # Only poll during active hours
                if not self._is_active_hours():
                    now_la = datetime.now(LA_TZ)
                    logger.info(
                        f"Outside active hours ({now_la.strftime('%I:%M %p')} LA). "
                        f"Skipping poll, will check again in 2 hours."
                    )
                    continue

                logger.info("Active hours — polling cached markets")
                await self._poll_cached_markets()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Poll loop error: {e}")
                await asyncio.sleep(5)

    def get_status(self) -> dict:
        """Return current tracker status for the dashboard."""
        markets_info = []
        for ticker, snap in self.baseline.items():
            current = next(
                (m for m in self.tracked_markets if m.get("ticker") == ticker),
                None,
            )
            current_price = (
                KalshiClient.get_yes_price(current) if current else snap.price
            )
            markets_info.append(
                {
                    "ticker": ticker,
                    "title": snap.title,
                    "baseline_price": snap.price,
                    "current_price": current_price,
                    "volume": snap.volume,
                    "baseline_set_at": snap.timestamp.isoformat(),
                }
            )

        return {
            "topic": self.current_topic,
            "is_running": self.is_running,
            "market_count": len(self.baseline),
            "markets": markets_info,
            "recent_alerts": self.alert_log[-10:],
            "poll_interval": self.poll_interval,
            "threshold": self.price_threshold,
            "min_volume": self.min_volume,
        }
