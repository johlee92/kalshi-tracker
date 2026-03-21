"""
Uses Claude API with web search to analyze why a prediction market moved.
"""

import logging
import anthropic

logger = logging.getLogger(__name__)


async def analyze_market_movement(
    api_key: str,
    market_title: str,
    direction: str,
    old_price: float,
    new_price: float,
    topic: str,
) -> str:
    """
    Ask Claude to research and explain why a Kalshi market moved.
    Uses Claude's web search capability for real-time news analysis.
    """
    client = anthropic.AsyncAnthropic(api_key=api_key)

    prompt = (
        f"A prediction market on Kalshi titled \"{market_title}\" has just moved "
        f"significantly {direction}. The YES price moved from "
        f"{old_price:.0%} to {new_price:.0%} "
        f"(a {abs(new_price - old_price):.0%} change). "
        f"This market is related to the topic: \"{topic}\".\n\n"
        f"Please search for the latest news and explain in 2-3 concise paragraphs "
        f"what recent events or developments most likely caused this market movement. "
        f"Focus on specific, concrete news events from the last 24-48 hours. "
        f"If you can't find a clear cause, say so and offer your best hypothesis."
    )

    try:
        # Use Claude with web search tool
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

        # Extract text from response
        text_parts = []
        for block in response.content:
            if hasattr(block, "text"):
                text_parts.append(block.text)

        analysis = "\n".join(text_parts).strip()
        if not analysis:
            analysis = "Unable to determine the cause of this market movement."

        return analysis

    except Exception as e:
        logger.error(f"Claude analysis failed: {e}")
        return f"Analysis unavailable (error: {str(e)[:100]})"
