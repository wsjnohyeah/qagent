from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from agentic_quant.config import AppEnvironment, Settings, TradingMode
from agentic_quant.domain import EventEnvelope
from agentic_quant.ids import uuid7
from agentic_quant.ledger import EventLedger
from agentic_quant.pipeline import run_synthetic_vertical_slice
from agentic_quant.risk import RestrictionRegistry, RiskPolicy


class OperatorCommand(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


def create_app(settings: Settings | None = None) -> FastAPI:
    app_settings = settings or Settings()
    ledger = EventLedger(app_settings.database_url)

    @asynccontextmanager
    async def lifespan(application: FastAPI):  # type: ignore[no-untyped-def]
        app_settings.object_store_root.mkdir(parents=True, exist_ok=True)
        ledger.initialize()
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
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"readiness check failed: {exc}") from exc
        return {
            "status": "ready",
            "database": database_ok,
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
            "phase": 0,
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

