from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from agentic_quant.archive import build_raw_archive
from agentic_quant.config import AppEnvironment, Settings, TradingMode
from agentic_quant.domain import EventEnvelope
from agentic_quant.event_bus import NullEventPublisher, RedisStreamPublisher
from agentic_quant.ids import uuid7
from agentic_quant.ledger import EventLedger
from agentic_quant.market_ingestion import MarketDataIngestionService
from agentic_quant.market_store import MarketDataStore
from agentic_quant.migrations import upgrade_database
from agentic_quant.option_ingestion import OptionDataIngestionService
from agentic_quant.pipeline import run_synthetic_vertical_slice
from agentic_quant.providers.alpaca import (
    AlpacaConfigurationError,
    AlpacaMarketDataProvider,
    AlpacaResponseError,
)
from agentic_quant.providers.alpaca_stream import AlpacaStockStream, AlpacaStreamError
from agentic_quant.providers.base import OptionChainRequest, StockBarsRequest
from agentic_quant.providers.synthetic import SyntheticMarketDataProvider
from agentic_quant.risk import RestrictionRegistry, RiskPolicy


class OperatorCommand(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


class BackfillRequest(BaseModel):
    symbol: str = Field(min_length=1, max_length=24, pattern=r"^[A-Za-z.\-]+$")
    start: datetime
    end: datetime


class OptionSnapshotRequest(BaseModel):
    underlying_symbol: str = Field(min_length=1, max_length=24, pattern=r"^[A-Za-z.\-]+$")
    limit: int = Field(default=100, ge=1, le=1_000)
    max_pages: int = Field(default=1, ge=1, le=1_000)


def create_app(settings: Settings | None = None) -> FastAPI:
    app_settings = settings or Settings()
    ledger = EventLedger(app_settings.database_url)

    @asynccontextmanager
    async def lifespan(application: FastAPI):  # type: ignore[no-untyped-def]
        if app_settings.auto_migrate:
            upgrade_database(app_settings.database_url)
        archive = build_raw_archive(app_settings)
        publisher = (
            RedisStreamPublisher(app_settings.redis_url, app_settings.redis_stream_name)
            if app_settings.redis_url
            else NullEventPublisher()
        )
        application.state.archive = archive
        application.state.publisher = publisher
        application.state.market_store = MarketDataStore(ledger.engine)
        application.state.new_exposure_paused = app_settings.global_new_exposure_paused
        yield
        ledger.engine.dispose()

    application = FastAPI(
        title="Agentic Quant Control API",
        version="0.1.0",
        lifespan=lifespan,
    )

    @application.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(Path(__file__).parent / "static" / "index.html")

    @application.get("/health/live")
    def liveness() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/health/ready")
    def readiness() -> dict[str, Any]:
        try:
            policy = RiskPolicy.from_yaml(app_settings.risk_policy_path)
            restrictions = RestrictionRegistry.from_yaml(app_settings.restricted_securities_path)
            database_ok = ledger.health()
            object_store_ok = bool(application.state.archive.health())
            event_bus_ok = bool(application.state.publisher.health())
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"readiness check failed: {exc}") from exc
        return {
            "status": "ready",
            "database": database_ok,
            "object_store": object_store_ok,
            "event_bus": event_bus_ok,
            "risk_policy": policy.version,
            "restricted_list": restrictions.version,
            "live_trading_enabled": False,
        }

    @application.get("/v1/system/status")
    def system_status() -> dict[str, Any]:
        return {
            "environment": app_settings.app_env,
            "trading_mode": app_settings.trading_mode,
            "live_trading_enabled": False,
            "new_exposure_paused": application.state.new_exposure_paused,
            "database": "healthy" if ledger.health() else "unhealthy",
            "phase": "1-in-progress",
            "alpaca_configured": bool(
                app_settings.alpaca_api_key and app_settings.alpaca_api_secret
            ),
        }

    @application.get("/v1/data-health")
    def data_health() -> dict[str, Any]:
        return {
            **application.state.market_store.health_summary(),
            "raw_archive": "healthy" if application.state.archive.health() else "unhealthy",
            "event_bus": "healthy" if application.state.publisher.health() else "unhealthy",
            "alpaca_configured": bool(
                app_settings.alpaca_api_key and app_settings.alpaca_api_secret
            ),
            "stock_feed": app_settings.alpaca_stock_feed,
            "option_feed": app_settings.alpaca_option_feed,
        }

    @application.get("/v1/events")
    def events(limit: int = Query(default=50, ge=1, le=500)) -> list[dict[str, Any]]:
        return ledger.recent(limit)

    @application.get("/v1/decisions/{correlation_id}")
    def decision(correlation_id: str) -> dict[str, Any]:
        lineage = ledger.by_correlation_id(correlation_id)
        if not lineage:
            raise HTTPException(status_code=404, detail="decision not found")
        return {"correlation_id": correlation_id, "lineage": lineage}

    @application.post("/v1/demo/run")
    def demo_run() -> dict[str, Any]:
        bundle = run_synthetic_vertical_slice(
            settings=app_settings,
            ledger=ledger,
            new_exposure_paused=application.state.new_exposure_paused,
        )
        return bundle.model_dump(mode="json")

    @application.post("/v1/demo/market-data")
    async def demo_market_data() -> dict[str, Any]:
        require_development()
        now = datetime.now(UTC).replace(second=0, microsecond=0)
        service = MarketDataIngestionService(
            provider=SyntheticMarketDataProvider(),
            archive=application.state.archive,
            store=application.state.market_store,
            ledger=ledger,
            publisher=application.state.publisher,
        )
        summary = await service.ingest_stock_bars(
            StockBarsRequest(
                symbol="DEMO",
                start=now,
                end=now + timedelta(minutes=1),
                feed="test",
            )
        )
        return summary.model_dump(mode="json")

    def require_development() -> None:
        if app_settings.app_env != AppEnvironment.DEVELOPMENT:
            raise HTTPException(
                status_code=403,
                detail="Unauthenticated Phase 1 data controls are development-only",
            )

    def alpaca_provider() -> AlpacaMarketDataProvider:
        if app_settings.alpaca_api_key is None or app_settings.alpaca_api_secret is None:
            raise HTTPException(
                status_code=503,
                detail="Alpaca credentials are not configured in the local environment",
            )
        return AlpacaMarketDataProvider(
            api_key=app_settings.alpaca_api_key.get_secret_value(),
            api_secret=app_settings.alpaca_api_secret.get_secret_value(),
            base_url=app_settings.alpaca_data_base_url,
        )

    @application.post("/v1/market-data/alpaca/probe")
    async def alpaca_probe() -> dict[str, Any]:
        require_development()
        try:
            async with alpaca_provider() as provider:
                checks = await provider.probe_entitlements(
                    stock_feed=app_settings.alpaca_stock_feed,
                    option_feed=app_settings.alpaca_option_feed,
                )
            if app_settings.alpaca_api_key is None or app_settings.alpaca_api_secret is None:
                raise AlpacaConfigurationError("Alpaca credentials are not configured")
            stream = AlpacaStockStream(
                api_key=app_settings.alpaca_api_key.get_secret_value(),
                api_secret=app_settings.alpaca_api_secret.get_secret_value(),
                feed=app_settings.alpaca_stock_feed,
                base_url=app_settings.alpaca_stock_stream_base_url,
            )
            stream_check = await stream.probe()
        except (AlpacaConfigurationError, AlpacaResponseError, AlpacaStreamError) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {
            "checks": [item.model_dump(mode="json") for item in checks],
            "stock_stream": stream_check,
        }

    @application.post("/v1/market-data/alpaca/backfill")
    async def alpaca_backfill(request: BackfillRequest) -> dict[str, Any]:
        require_development()
        if request.start.tzinfo is None or request.end.tzinfo is None:
            raise HTTPException(status_code=422, detail="start and end must include timezones")
        if request.start >= request.end:
            raise HTTPException(status_code=422, detail="start must be before end")
        try:
            async with alpaca_provider() as provider:
                service = MarketDataIngestionService(
                    provider=provider,
                    archive=application.state.archive,
                    store=application.state.market_store,
                    ledger=ledger,
                    publisher=application.state.publisher,
                )
                summary = await service.ingest_stock_bars(
                    StockBarsRequest(
                        symbol=request.symbol.upper(),
                        start=request.start,
                        end=request.end,
                        feed=app_settings.alpaca_stock_feed,
                    )
                )
        except (AlpacaConfigurationError, AlpacaResponseError) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return summary.model_dump(mode="json")

    @application.post("/v1/market-data/alpaca/option-snapshot")
    async def alpaca_option_snapshot(request: OptionSnapshotRequest) -> dict[str, Any]:
        require_development()
        try:
            async with alpaca_provider() as provider:
                service = OptionDataIngestionService(
                    provider=provider,
                    archive=application.state.archive,
                    store=application.state.market_store,
                    ledger=ledger,
                    publisher=application.state.publisher,
                )
                summary = await service.ingest_option_chain(
                    OptionChainRequest(
                        underlying_symbol=request.underlying_symbol.upper(),
                        feed=app_settings.alpaca_option_feed,
                        limit=request.limit,
                        max_pages=request.max_pages,
                    )
                )
        except (AlpacaConfigurationError, AlpacaResponseError) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return summary.model_dump(mode="json")

    def record_command(command: str, reason: str) -> None:
        now = datetime.now(UTC)
        event = EventEnvelope(
            event_id=uuid7(),
            event_type="system.kill_switch.changed.v1",
            event_time=now,
            emitted_at=now,
            producer="control-api",
            correlation_id=uuid7(),
            payload={"command": command, "reason": reason},
        )
        ledger.append(event)

    @application.post("/v1/commands/pause")
    def pause(command: OperatorCommand) -> dict[str, Any]:
        application.state.new_exposure_paused = True
        record_command("pause_new_exposure", command.reason)
        return {"new_exposure_paused": True}

    @application.post("/v1/commands/resume")
    def resume(command: OperatorCommand) -> dict[str, Any]:
        if not (
            app_settings.app_env == AppEnvironment.DEVELOPMENT
            and app_settings.trading_mode == TradingMode.SHADOW
        ):
            raise HTTPException(
                status_code=403,
                detail="Phase 0 resume is permitted only in development shadow mode",
            )
        application.state.new_exposure_paused = False
        record_command("resume_new_exposure", command.reason)
        return {"new_exposure_paused": False}

    return application


app = create_app()


def run() -> None:
    import uvicorn

    settings = Settings()
    uvicorn.run("agentic_quant.api:app", host=settings.api_host, port=settings.api_port)
