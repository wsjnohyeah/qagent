from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppEnvironment(StrEnum):
    DEVELOPMENT = "development"
    PRODUCTION = "production"


class TradingMode(StrEnum):
    RESEARCH = "research"
    BACKTEST = "backtest"
    SHADOW = "shadow"
    PAPER = "paper"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: AppEnvironment = AppEnvironment.DEVELOPMENT
    trading_mode: TradingMode = TradingMode.SHADOW
    live_trading_enabled: bool = False
    global_new_exposure_paused: bool = True
    database_url: str = "sqlite+pysqlite:///./work/agentic_quant.db"
    redis_url: str | None = None
    object_store_root: Path = Path("./work/object-store")
    risk_policy_path: Path = Path("./configs/risk_policy.yaml")
    restricted_securities_path: Path = Path("./configs/restricted_securities.yaml")
    api_host: str = "127.0.0.1"
    api_port: int = Field(default=8000, ge=1, le=65535)

    @model_validator(mode="after")
    def live_execution_is_impossible(self) -> Settings:
        if self.live_trading_enabled:
            raise ValueError("Live trading is prohibited; LIVE_TRADING_ENABLED must remain false")
        return self

