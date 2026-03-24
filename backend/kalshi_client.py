"""
Kalshi API client for fetching market data.
Uses the public (no-auth) market data endpoints.

Rate-limit aware: uses exponential backoff on 429 responses and
caches the full market list so subsequent polls only hit individual
market endpoints.
"""

import httpx
import asyncio
import logging
import random
from typing import Optional

logger = logging.getLogger(__name__)

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2/"  # trailing slash required for httpx path merging

# Retry settings
MAX_RETRIES = 5
BASE_BACKOFF = 2.0  # seconds
MAX_BACKOFF = 60.0


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

    # ------------------------------------------------------------------
    # Low-level helpers with retry logic
    # ------------------------------------------------------------------

    async def _request_with_retry(self, method: str, url: str, **kwargs) -> httpx.Response:
        """Make an HTTP request with exponential backoff on 429 responses."""
        for attempt in range(MAX_RETRIES):
            resp = await self.client.request(method, url, **kwargs)

            if resp.status_code != 429:
                resp.raise_for_status()
                return resp

            # 429 – back off with jitter
            retry_after = resp.headers.get("Retry-After")
            if retry_after:
                wait = float(retry_after)
            else:
                wait = min(BASE_BACKOFF * (2 ** attempt), MAX_BACKOFF)
            wait += random.uniform(0, 1)  # jitter

            logger.warning(
                f"Rate limited (429) on {url}, attempt {attempt + 1}/{MAX_RETRIES}. "
                f"Waiting {wait:.1f}s"
            )
            await asyncio.sleep(wait)

        # Final attempt – let it raise if it fails
        resp = await self.client.request(method, url, **kwargs)
        resp.raise_for_status()
        return resp

    # ------------------------------------------------------------------
    # Market endpoints
    # ------------------------------------------------------------------

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
        resp = await self._request_with_retry("GET", "markets", params=params)
        return resp.json()

    async def get_all_open_markets(self) -> list[dict]:
        """
        Paginate through all open markets.
        Uses backoff-aware requests and a 0.5s delay between pages.
        """
        all_markets = []
        cursor = None
        page = 0
        while True:
            data = await self.get_markets(cursor=cursor)
            markets = data.get("markets", [])
            all_markets.extend(markets)
            page += 1
            cursor = data.get("cursor")
            if not cursor or not markets:
                break
            # Slower pagination to stay under rate limits
            await asyncio.sleep(0.5)
            if page % 10 == 0:
                logger.info(f"Paginated {page} pages ({len(all_markets)} markets so far)")
        logger.info(f"Full scan complete: {len(all_markets)} open markets across {page} pages")
        return all_markets

    async def get_market(self, ticker: str) -> Optional[dict]:
        """Get a single market by ticker. Returns None if not found."""
        try:
            resp = await self._request_with_retry("GET", f"markets/{ticker}")
            data = resp.json()
            market = data.get("market") or data  # handle both wrapped and unwrapped responses
            return market if market else None
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                logger.warning(f"Market {ticker} not found (404), may have closed")
                return None
            raise

    async def get_markets_batch(self, tickers: list[str]) -> list[dict]:
        """
        Fetch multiple individual markets by ticker with rate-limit-safe pacing.
        Returns list of market dicts (skips any that return None/404).
        """
        results = []
        for i, ticker in enumerate(tickers):
            market = await self.get_market(ticker)
            if market:
                results.append(market)
            # Small delay between individual requests
            if i < len(tickers) - 1:
                await asyncio.sleep(0.3)
        return results

    async def get_event(self, event_ticker: str) -> dict:
        """Get an event and its markets."""
        resp = await self._request_with_retry("GET", f"events/{event_ticker}")
        return resp.json()

    async def search_markets_by_topic(
        self, topic: str, min_volume: float = 10000
    ) -> list[dict]:
        """
        Search open markets by keyword matching on title/subtitle/ticker.
        Filters by minimum volume.

        WARNING: This does a full pagination of ALL open markets.
        Use sparingly — prefer get_markets_batch() for repeated polling.
        """
        all_markets = await self.get_all_open_markets()
        return self.filter_markets(all_markets, topic, min_volume)

    @staticmethod
    def filter_markets(
        markets: list[dict], topic: str, min_volume: float = 10000
    ) -> list[dict]:
        """Filter a list of markets by topic keywords and minimum volume."""
        topic_lower = topic.lower()
        keywords = topic_lower.split()

        matched = []
        for m in markets:
            title = (m.get("title") or "").lower()
            subtitle = (m.get("subtitle") or "").lower()
            ticker = (m.get("ticker") or "").lower()
            event_ticker = (m.get("event_ticker") or "").lower()
            category = (m.get("category") or "").lower()
            searchable = f"{title} {subtitle} {ticker} {event_ticker} {category}"

            if all(kw in searchable for kw in keywords):
                volume = KalshiClient._parse_volume(m)
                if volume >= min_volume:
                    matched.append(m)

        logger.info(
            f"Filtered {len(matched)} markets matching '{topic}' "
            f"with volume >= ${min_volume:,.0f} "
            f"(out of {len(markets)} total)"
        )
        return matched

    @staticmethod
    def _parse_volume(market: dict) -> float:
        """Extract volume in dollars from a market object.

        Kalshi API v2 uses 'volume_fp' (full-precision) and 'volume_24h_fp'.
        Older/alternate responses may use 'volume' or 'volume_24h'.
        """
        for key in ("volume_fp", "volume", "volume_24h_fp", "volume_24h"):
            val = market.get(key)
            if val is not None:
                try:
                    return float(val)
                except (TypeError, ValueError):
                    continue
        return 0.0

    @staticmethod
    def get_yes_price(market: dict) -> Optional[float]:
        """Get the current YES price (probability) as a float 0-1.

        Kalshi API v2 uses '_dollars' suffix fields (values are 0-1 range).
        Older responses used 'last_price' / 'yes_bid' (values 0-100 cents).
        """
        # New API field names (values already in 0-1 dollar range)
        for key in ("last_price_dollars", "yes_bid_dollars", "yes_ask_dollars",
                    "previous_price_dollars", "previous_yes_bid_dollars"):
            val = market.get(key)
            if val is not None:
                price = float(val)
                # Sanity-check: if somehow > 1 it was passed in cents, normalise
                return price / 100.0 if price > 1 else price

        # Legacy field names (values in 0-100 cents)
        for key in ("last_price", "yes_bid", "yes_ask"):
            val = market.get(key)
            if val is not None:
                price = float(val)
                return price / 100.0 if price > 1 else price

        return None
