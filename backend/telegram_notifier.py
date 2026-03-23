"""
Sends Telegram alerts when Kalshi market movements are detected.
"""

import logging
import httpx

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"


async def send_telegram_alert(
    bot_token: str,
    chat_id: str,
    market_title: str,
    market_url: str,
    yes_price: float,
    volume: float,
    changes: list[dict],
    analysis: str,
) -> bool:
    """
    Send a formatted Telegram alert about a Kalshi market movement.

    `changes` is a list of dicts:
        type      : "PRICE" | "VOLUME"
        direction : "UP" | "DOWN"
        old       : float
        new       : float
        pct       : float
    """

    # ── Header emoji based on what moved ──────────────────────────────
    has_price  = any(c["type"] == "PRICE"  for c in changes)
    has_volume = any(c["type"] == "VOLUME" for c in changes)

    if has_price:
        price_change = next(c for c in changes if c["type"] == "PRICE")
        header_emoji = "📈" if price_change["direction"] == "UP" else "📉"
    else:
        header_emoji = "📊"

    # ── "What changed" block ──────────────────────────────────────────
    change_lines = []
    for c in changes:
        if c["type"] == "PRICE":
            arrow = "⬆️" if c["direction"] == "UP" else "⬇️"
            change_lines.append(
                f"{arrow} <b>Prediction:</b> {c['old']:.0%} → {c['new']:.0%} "
                f"({c['direction']}, {c['pct']:.0%} move)"
            )
        elif c["type"] == "VOLUME":
            change_lines.append(
                f"⬆️ <b>Volume:</b> ${c['old']:,.0f} → ${c['new']:,.0f} "
                f"(+{c['pct']:.0%})"
            )
    changes_text = "\n".join(change_lines)

    # ── Build full message ────────────────────────────────────────────
    message = (
        f"{header_emoji} <b>Kalshi Market Alert</b>\n\n"
        f"📌 <b>{market_title}</b>\n\n"
        f"💰 <b>Volume:</b> ${volume:,.0f}\n"
        f"🎯 <b>Current prediction:</b> {yes_price:.0%} YES\n\n"
        f"⚡ <b>What changed:</b>\n"
        f"{changes_text}\n\n"
        f"🔍 <b>Why it changed:</b>\n"
        f"{analysis}\n\n"
        f'🔗 <a href="{market_url}">View market on Kalshi</a>'
    )

    # Telegram message limit: 4096 chars
    if len(message) > 4000:
        message = message[:3997] + "..."

    return await _send_message(bot_token, chat_id, message)


async def send_telegram_status(bot_token: str, chat_id: str, message: str) -> bool:
    """Send a plain status / informational message."""
    return await _send_message(bot_token, chat_id, message)


async def _send_message(bot_token: str, chat_id: str, text: str) -> bool:
    url = f"{TELEGRAM_API}/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            logger.info("Telegram message sent")
            return True
    except Exception as e:
        logger.error(f"Failed to send Telegram message: {e}")
        return False
