"""
Core tracking engine that monitors Kalshi markets and detects movements.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from kalshi_client import KalshiClient
from analyzer import analyze_market_movement
from telegram_notifier import send_telegram_alert, send_telegram_status

logger = logging.getLogger(__name__)


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
        self.is_running = False
        self._task: Optional[asyncio.Task] = None
        self.alert_log: list[dict] = []  # recent alerts for the dashboard
        self.tracked_markets: list[dict] = []  # current markets being tracked

    async def start_tracking(self, topic: str):
        """Start or restart tracking for a new topic."""
        # Stop existing tracking
        await self.stop_tracking(notify=False)

        self.current_topic = topic
        self.baseline = {}
        self.alert_log = []
        self.tracked_markets = []
        self.is_running = True

        logger.info(f"Starting tracker for topic: '{topic}'")

        # Initial market scan
        await self._refresh_baseline()

        if not self.baseline:
            logger.warning(f"No markets found for topic: '{topic}'")
            await send_telegram_status(
                self.telegram_bot_token,
                self.telegram_chat_id,
                f"⚠️ No Kalshi markets with volume ≥ ${self.min_volume:,.0f} "
                f"found for topic: <b>{topic}</b>",
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
                f"Polling every {self.poll_interval}s",
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

    async def _refresh_baseline(self):
        """Scan Kalshi for matching markets and set baseline prices."""
        try:
            markets = await self.kalshi.search_markets_by_topic(
                self.current_topic, self.min_volume
            )
            self.tracked_markets = markets

            for m in markets:
                ticker = m.get("ticker", "")
                title = m.get("title", "Unknown")
                price = KalshiClient.get_yes_price(m)
                volume = KalshiClient._parse_volume(m)

                if price is not None and ticker:
                    if ticker not in self.baseline:
                        self.baseline[ticker] = MarketSnapshot(
                            ticker, title, price, volume
                        )
                    else:
                        # Update volume but keep baseline price
                        self.baseline[ticker].volume = volume

        except Exception as e:
            logger.error(f"Error refreshing baseline: {e}")

    async def _poll_loop(self):
        """Main polling loop that checks for market movements."""
        while self.is_running:
            try:
                await asyncio.sleep(self.poll_interval)
                if not self.is_running:
                    break
                await self._check_movements()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Poll loop error: {e}")
                await asyncio.sleep(5)

    async def _check_movements(self):
        """Check all tracked markets for significant price changes."""
        try:
            markets = await self.kalshi.search_markets_by_topic(
                self.current_topic, self.min_volume
            )
            self.tracked_markets = markets

            for m in markets:
                ticker = m.get("ticker", "")
                title = m.get("title", "Unknown")
                new_price = KalshiClient.get_yes_price(m)
                volume = KalshiClient._parse_volume(m)

                if new_price is None or not ticker:
                    continue

                if ticker not in self.baseline:
                    # New market appeared, add to baseline
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
                    # Keep only last 50 alerts
                    self.alert_log = self.alert_log[-50:]

                    # Update baseline to new price (so we don't re-alert)
                    self.baseline[ticker] = MarketSnapshot(
                        ticker, title, new_price, volume
                    )
                else:
                    # Update volume
                    self.baseline[ticker].volume = volume

        except Exception as e:
            logger.error(f"Error checking movements: {e}")

    def get_status(self) -> dict:
        """Return current tracker status for the dashboard."""
        markets_info = []
        for ticker, snap in self.baseline.items():
            # Find current price from tracked_markets
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
