from functools import lru_cache
from pathlib import Path
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AOTS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "AOTS Paper Trading"
    environment: str = "development"
    broker: str = "groww"
    trading_mode: str = "paper"
    underlying: str = "NIFTY"
    capital_inr: float = Field(default=100_000, gt=0)
    max_daily_loss_pct: float = Field(default=0.02, gt=0, le=0.10)
    max_trade_risk_pct: float = Field(default=0.005, gt=0, le=0.01)
    max_consecutive_losses: int = Field(default=3, ge=1, le=10)
    max_open_positions: int = Field(default=3, ge=1, le=20)
    max_lots_per_order: int = Field(default=1, ge=1, le=10)
    market_data_max_age_seconds: int = Field(default=5, ge=1, le=60)
    square_off_time: str = "15:15"
    database_path: str = "./data/aots.db"
    live_trading_enabled: bool = False

    @model_validator(mode="after")
    def paper_only(self):
        if self.trading_mode.lower() != "paper":
            raise ValueError("Only AOTS_TRADING_MODE=paper is supported in this starter.")
        if self.live_trading_enabled:
            raise ValueError("Live trading is not implemented. AOTS_LIVE_TRADING_ENABLED must remain false.")
        if self.broker.lower() not in {"groww", "broker-independent"}:
            raise ValueError("This starter is configured for Groww integration boundaries or broker-independent mode.")
        Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
