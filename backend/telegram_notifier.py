"""
Sends Telegram alerts when market movements are detected.
"""

import logging
import httpx

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"


async def send_telegram_alert(
    bot_token: str,
    chat_id: str,
    market_title: str,
    old_price: float,
    new_price: float,
    volume: float,
    analysis: str,
    topic: str,
) -> bool:
    """Send a formatted Telegram message about a market movement."""

    change = new_price - old_price
    direction_emoji = "📈" if change > 0 else "📉"
    direction_word = "UP" if change > 0 else "DOWN"

    message = (
        f"{direction_emoji} <b>Kalshi Alert: {topic}</b>\n\n"
        f"<b>{market_title}</b>\n\n"
        f"Price: {old_price:.0%} → {new_price:.0%} "
        f"({direction_word} {abs(change):.0%})\n"
        f"Volume: ${volume:,.0f}\n\n"
        f"<b>Why did this move?</b>\n"
        f"{analysis}\n\n"
        f"🔗 <a href='https://kalshi.com'>View on Kalshi</a>"
    )

    # Telegram has a 4096 char limit
    if len(message) > 4000:
        message = message[:3997] + "..."

    url = f"{TELEGRAM_API}/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            logger.info(f"Telegram alert sent for: {market_title}")
            return True
    except Exception as e:
        logger.error(f"Failed to send Telegram alert: {e}")
        return False


async def send_telegram_status(bot_token: str, chat_id: str, message: str) -> bool:
    """Send a simple status message."""
    url = f"{TELEGRAM_API}/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            return True
    except Exception as e:
        logger.error(f"Failed to send Telegram status: {e}")
        return False
