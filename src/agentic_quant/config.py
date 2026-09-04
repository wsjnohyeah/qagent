from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppEnvironment(StrEnum):
    DEVELOPMENT = "development"
    PRODUCTION = "production"


class TradingMode(StrEnum):
    RESEARCH = "research"
    BACKTEST = "backtest"
    SHADOW = "shadow"
    PAPER = "paper"


class ObjectStoreBackend(StrEnum):
    LOCAL = "local"
    S3 = "s3"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: AppEnvironment = AppEnvironment.DEVELOPMENT
    trading_mode: TradingMode = TradingMode.SHADOW
    live_trading_enabled: bool = False
    global_new_exposure_paused: bool = True
    auto_migrate: bool = True
    database_url: str = "sqlite+pysqlite:///./work/agentic_quant.db"
    redis_url: str | None = None
    redis_stream_name: str = "market-events"
    object_store_backend: ObjectStoreBackend = ObjectStoreBackend.LOCAL
    object_store_root: Path = Path("./work/object-store")
    object_store_endpoint: str | None = None
    object_store_bucket: str = "quant-raw"
    object_store_access_key: SecretStr | None = None
    object_store_secret_key: SecretStr | None = None
    risk_policy_path: Path = Path("./configs/risk_policy.yaml")
    restricted_securities_path: Path = Path("./configs/restricted_securities.yaml")
    alpaca_api_key: SecretStr | None = None
    alpaca_api_secret: SecretStr | None = None
    alpaca_data_base_url: str = "https://data.alpaca.markets"
    alpaca_stock_stream_base_url: str = "wss://stream.data.alpaca.markets/v2"
    alpaca_stock_feed: str = "sip"
    alpaca_option_feed: str = "opra"
    market_calendar: str = "XNYS"
    api_host: str = "127.0.0.1"
    api_port: int = Field(default=8000, ge=1, le=65535)

    @model_validator(mode="after")
    def live_execution_is_impossible(self) -> Settings:
        if self.live_trading_enabled:
            raise ValueError("Live trading is prohibited; LIVE_TRADING_ENABLED must remain false")
        if self.object_store_backend == ObjectStoreBackend.S3 and not all(
            (
                self.object_store_endpoint,
                self.object_store_access_key,
                self.object_store_secret_key,
            )
        ):
            raise ValueError("S3 object storage requires endpoint, access key, and secret key")
        return self
