"""
Uses Claude API with web search to analyze why a prediction market moved.
"""

import logging
import anthropic

logger = logging.getLogger(__name__)


async def analyze_market_movement(
    api_key: str,
    market_title: str,
    market_url: str,
    changes: list[dict],
    yes_price: float,
) -> str:
    """
    Ask Claude to research and explain why a Kalshi market moved.
    Uses Claude's web search capability for real-time news analysis.

    `changes` is a list of dicts with keys:
        type      : "PRICE" | "VOLUME"
        direction : "UP" | "DOWN"
        old       : float
        new       : float
        pct       : float  (e.g. 0.12 = 12%)
    """
    client = anthropic.AsyncAnthropic(api_key=api_key)

    # Build a human-readable description of what moved
    change_descriptions = []
    for c in changes:
        if c["type"] == "PRICE":
            change_descriptions.append(
                f"The YES probability moved {c['direction']} from "
                f"{c['old']:.0%} to {c['new']:.0%} "
                f"(a {c['pct']:.0%} relative change)."
            )
        elif c["type"] == "VOLUME":
            change_descriptions.append(
                f"Trading volume increased from "
                f"${c['old']:,.0f} to ${c['new']:,.0f} "
                f"(+{c['pct']:.0%})."
            )

    changes_text = " ".join(change_descriptions)

    prompt = (
        f'A Kalshi prediction market titled "{market_title}" just had a significant move.\n\n'
        f"What changed: {changes_text}\n"
        f"Current YES probability: {yes_price:.0%}\n"
        f"Market URL: {market_url}\n\n"
        f"Please do a thorough web search and explain in 2–3 concise paragraphs what "
        f"recent news, events, or developments most likely caused this movement. "
        f"Search for news from the past 24–48 hours related to the subject of this market. "
        f"Be specific — name actual events, statements, or data releases if you find them. "
        f"If you cannot find a clear cause, say so plainly and offer your best hypothesis "
        f"based on what you do find."
    )

    try:
        response = await client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
            tools=[
                {
                    "type": "web_search_20250305",
                    "name": "web_search",
                    "max_uses": 5,
                }
            ],
        )

        text_parts = [
            block.text
            for block in response.content
            if hasattr(block, "text")
        ]
        analysis = "\n".join(text_parts).strip()
        return analysis or "Unable to determine the cause of this market movement."

    except Exception as e:
        logger.error(f"Claude analysis failed: {e}")
        return f"Analysis unavailable (error: {str(e)[:100]})"
