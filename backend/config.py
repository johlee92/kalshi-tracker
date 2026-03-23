from pydantic_settings import BaseSettings
from pydantic import Field
from typing import Optional


class Settings(BaseSettings):
    # Optional at startup so the app can boot and pass Railway's healthcheck
    # even before env vars are confirmed. Missing vars are caught at track time.
    telegram_bot_token: Optional[str] = Field(None, description="Telegram bot token from BotFather")
    telegram_chat_id: Optional[str] = Field(None, description="Your Telegram chat ID")
    anthropic_api_key: Optional[str] = Field(None, description="Anthropic API key for Claude")
    poll_interval_seconds: int = Field(60, description="How often to poll Kalshi (seconds)")
    min_volume_usd: float = Field(10000, description="Minimum volume filter in USD")
    price_change_threshold: float = Field(0.10, description="Alert threshold (0.10 = 10%)")
    kalshi_api_base: str = Field(
        "https://api.elections.kalshi.com/trade-api/v2",
        description="Kalshi API base URL"
    )

    def missing_vars(self) -> list[str]:
        """Return names of any required vars that haven't been set."""
        missing = []
        if not self.telegram_bot_token:
            missing.append("TELEGRAM_BOT_TOKEN")
        if not self.telegram_chat_id:
            missing.append("TELEGRAM_CHAT_ID")
        if not self.anthropic_api_key:
            missing.append("ANTHROPIC_API_KEY")
        return missing

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
