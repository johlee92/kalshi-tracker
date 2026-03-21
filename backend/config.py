from pydantic_settings import BaseSettings
from pydantic import Field


class Settings(BaseSettings):
    telegram_bot_token: str = Field(..., description="Telegram bot token from BotFather")
    telegram_chat_id: str = Field(..., description="Your Telegram chat ID")
    anthropic_api_key: str = Field(..., description="Anthropic API key for Claude")
    poll_interval_seconds: int = Field(60, description="How often to poll Kalshi (seconds)")
    min_volume_usd: float = Field(10000, description="Minimum volume filter in USD")
    price_change_threshold: float = Field(0.10, description="Alert threshold (0.10 = 10%)")
    kalshi_api_base: str = Field(
        "https://api.elections.kalshi.com/trade-api/v2",
        description="Kalshi API base URL"
    )

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
