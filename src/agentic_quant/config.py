from __future__ import annotations

from datetime import datetime, timedelta
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
    auth_required: bool = False
    admin_username: str | None = None
    admin_password: SecretStr | None = None
    admin_password_hash: SecretStr | None = None
    session_secret: SecretStr | None = None
    session_max_age_days: int = Field(default=90, ge=1, le=365)
    shadow_runtime_enabled: bool = True
    shadow_poll_seconds: int = Field(default=30, ge=5, le=3_600)
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
    sec_user_agent: str | None = None
    enable_social_aggregates: bool = False
    social_aggregate_url: str | None = None
    social_aggregate_token: SecretStr | None = None
    llm_openai_api_key: SecretStr | None = None
    llm_meta_api_key: SecretStr | None = None
    llm_routing_path: Path = Path("./configs/model_routing.yaml")
    llm_budget_path: Path = Path("./configs/llm_budget.yaml")
    research_promotion_policy_path: Path = Path(
        "./configs/research_promotion_policy.yaml"
    )
    ml_policy_path: Path = Path("./configs/ml_policy.yaml")
    market_calendar: str = "XNYS"
    development_max_backfill_days: int = Field(default=120, ge=1, le=3_650)
    development_max_intraday_backfill_days: int = Field(default=7, ge=1, le=365)
    source_git_sha: str | None = None
    api_host: str = "127.0.0.1"
    api_port: int = Field(default=8000, ge=1, le=65535)

    @model_validator(mode="after")
    def live_execution_is_impossible(self) -> Settings:
        if self.live_trading_enabled:
            raise ValueError("Live trading is prohibited; LIVE_TRADING_ENABLED must remain false")
        if self.app_env == AppEnvironment.PRODUCTION:
            if not self.global_new_exposure_paused:
                raise ValueError("Production must start with new exposure paused")
            if self.auto_migrate:
                raise ValueError("Production requires AUTO_MIGRATE=false")
            if not self.auth_required:
                raise ValueError("Production requires AUTH_REQUIRED=true")
            if self.admin_password is not None:
                raise ValueError("Production authentication requires ADMIN_PASSWORD_HASH")
        if self.auth_required:
            if not self.admin_username or not self.admin_username.strip():
                raise ValueError("AUTH_REQUIRED=true requires ADMIN_USERNAME")
            if not self.admin_password and not self.admin_password_hash:
                raise ValueError(
                    "AUTH_REQUIRED=true requires ADMIN_PASSWORD or ADMIN_PASSWORD_HASH"
                )
            if (
                self.session_secret is None
                or len(self.session_secret.get_secret_value()) < 32
            ):
                raise ValueError("AUTH_REQUIRED=true requires a 32+ character SESSION_SECRET")
        if self.object_store_backend == ObjectStoreBackend.S3 and not all(
            (
                self.object_store_endpoint,
                self.object_store_access_key,
                self.object_store_secret_key,
            )
        ):
            raise ValueError("S3 object storage requires endpoint, access key, and secret key")
        if self.enable_social_aggregates and not all(
            (self.social_aggregate_url, self.social_aggregate_token)
        ):
            raise ValueError(
                "Social aggregates require SOCIAL_AGGREGATE_URL and SOCIAL_AGGREGATE_TOKEN"
            )
        return self

    @property
    def data_operating_scope(self) -> str:
        if self.app_env == AppEnvironment.DEVELOPMENT:
            return "bounded_correctness_samples"
        return "durable_long_horizon"

    @property
    def openai_configured(self) -> bool:
        return bool(
            self.llm_openai_api_key
            and self.llm_openai_api_key.get_secret_value().strip()
        )

    @property
    def meta_model_configured(self) -> bool:
        return bool(
            self.llm_meta_api_key
            and self.llm_meta_api_key.get_secret_value().strip()
        )

    @property
    def admin_credential_fingerprint(self) -> str:
        import hashlib

        password_material = self.admin_password_hash or self.admin_password
        if not self.admin_username or password_material is None:
            return "UNCONFIGURED"
        material = (
            self.admin_username
            + ":"
            + password_material.get_secret_value()
        )
        return hashlib.sha256(material.encode()).hexdigest()

    def validate_backfill_window(
        self,
        *,
        start: datetime,
        end: datetime,
        timeframe: str | None = None,
    ) -> None:
        if start >= end:
            raise ValueError("Backfill start must be before end")
        maximum_days = (
            self.development_max_intraday_backfill_days
            if timeframe == "1Min"
            else self.development_max_backfill_days
        )
        if (
            self.app_env == AppEnvironment.DEVELOPMENT
            and end - start > timedelta(days=maximum_days)
        ):
            raise ValueError(
                "Development backfills are bounded to "
                f"{maximum_days} days for {timeframe or 'this data source'}; "
                "run long-horizon jobs in "
                "APP_ENV=production or explicitly change the development limit"
            )
