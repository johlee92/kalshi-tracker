"""
Kalshi API client for fetching market data.
Uses the public (no-auth) market data endpoints.
"""

import httpx
import asyncio
import logging
from typing import Optional

logger = logging.getLogger(__name__)

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"


class KalshiClient:
    def __init__(self, base_url: str = KALSHI_BASE):
        self.base_url = base_url
        self.client = httpx.AsyncClient(
            base_url=base_url,
            timeout=30.0,
            headers={"Accept": "application/json"},
        )

    async def close(self):
        await self.client.aclose()

    async def get_markets(
        self,
        status: str = "open",
        limit: int = 200,
        cursor: Optional[str] = None,
    ) -> dict:
        """Fetch a page of markets from Kalshi."""
        params = {"status": status, "limit": limit}
        if cursor:
            params["cursor"] = cursor
        resp = await self.client.get("/markets", params=params)
        resp.raise_for_status()
        return resp.json()

    async def get_all_open_markets(self) -> list[dict]:
        """Paginate through all open markets."""
        all_markets = []
        cursor = None
        while True:
            data = await self.get_markets(cursor=cursor)
            markets = data.get("markets", [])
            all_markets.extend(markets)
            cursor = data.get("cursor")
            if not cursor or not markets:
                break
            await asyncio.sleep(0.2)  # be kind to rate limits
        return all_markets

    async def get_market(self, ticker: str) -> dict:
        """Get a single market by ticker."""
        resp = await self.client.get(f"/markets/{ticker}")
        resp.raise_for_status()
        return resp.json().get("market", {})

    async def get_event(self, event_ticker: str) -> dict:
        """Get an event and its markets."""
        resp = await self.client.get(f"/events/{event_ticker}")
        resp.raise_for_status()
        return resp.json()

    async def search_markets_by_topic(
        self, topic: str, min_volume: float = 10000
    ) -> list[dict]:
        """
        Search open markets by keyword matching on title/subtitle/ticker.
        Filters by minimum volume.
        """
        all_markets = await self.get_all_open_markets()
        topic_lower = topic.lower()
        keywords = topic_lower.split()

        matched = []
        for m in all_markets:
            title = (m.get("title") or "").lower()
            subtitle = (m.get("subtitle") or "").lower()
            ticker = (m.get("ticker") or "").lower()
            event_ticker = (m.get("event_ticker") or "").lower()
            category = (m.get("category") or "").lower()
            searchable = f"{title} {subtitle} {ticker} {event_ticker} {category}"

            # Match if ALL keywords appear somewhere in the searchable text
            if all(kw in searchable for kw in keywords):
                # Volume filter
                volume = self._parse_volume(m)
                if volume >= min_volume:
                    matched.append(m)

        logger.info(
            f"Found {len(matched)} markets matching '{topic}' "
            f"with volume >= ${min_volume:,.0f} "
            f"(out of {len(all_markets)} total open markets)"
        )
        return matched

    @staticmethod
    def _parse_volume(market: dict) -> float:
        """Extract volume in dollars from a market object."""
        # Kalshi returns volume in cents in some fields, dollars in others
        # Try multiple fields
        vol = market.get("volume", 0)
        if isinstance(vol, (int, float)):
            return float(vol)
        vol_24h = market.get("volume_24h", 0)
        if isinstance(vol_24h, (int, float)):
            return float(vol_24h)
        return 0.0

    @staticmethod
    def get_yes_price(market: dict) -> Optional[float]:
        """Get the current YES price (probability) as a float 0-1."""
        # Try last_price first, then yes_bid
        last = market.get("last_price")
        if last is not None:
            return float(last) / 100.0 if float(last) > 1 else float(last)
        yes_bid = market.get("yes_bid")
        if yes_bid is not None:
            return float(yes_bid) / 100.0 if float(yes_bid) > 1 else float(yes_bid)
        return None
