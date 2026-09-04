from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from agentic_quant.api import create_app
from agentic_quant.config import Settings
from agentic_quant.domain import Verdict
from agentic_quant.ledger import EventLedger
from agentic_quant.pipeline import run_synthetic_vertical_slice


def test_vertical_slice_records_complete_lineage(settings: Settings) -> None:
    ledger = EventLedger(settings.database_url)
    ledger.initialize()
    result = run_synthetic_vertical_slice(
        settings=settings,
        ledger=ledger,
        new_exposure_paused=False,
        as_of=datetime(2026, 9, 4, 14, 45, tzinfo=UTC),
    )
    assert result.risk_decision.verdict == Verdict.APPROVE
    assert result.shadow_order is not None
    event_types = [item["event_type"] for item in ledger.by_correlation_id(result.correlation_id)]
    assert event_types == [
        "catalyst.normalized.v1",
        "feature.snapshot.created.v1",
        "signal.candidate.created.v1",
        "risk.decision.created.v1",
        "trade.plan.approved.v1",
        "order.state.changed.v1",
    ]


def test_api_health_and_demo(settings: Settings) -> None:
    with TestClient(create_app(settings)) as client:
        assert client.get("/health/live").json() == {"status": "ok"}
        ready = client.get("/health/ready")
        assert ready.status_code == 200
        assert ready.json()["live_trading_enabled"] is False
        system_status = client.get("/v1/system/status").json()
        assert system_status["phase"] == "3b-event-driven-backtest"
        assert system_status["data_operating_scope"] == "bounded_correctness_samples"
        assert system_status["development_max_backfill_days"] == 120
        assert system_status["development_max_intraday_backfill_days"] == 7
        assert system_status["phase_1b_open_session_validation"] == "pending"
        demo = client.post("/v1/demo/run")
        assert demo.status_code == 200
        correlation_id = demo.json()["correlation_id"]
        lineage = client.get(f"/v1/decisions/{correlation_id}")
        assert len(lineage.json()["lineage"]) == 6
        market_demo = client.post("/v1/demo/market-data")
        assert market_demo.status_code == 200
        assert market_demo.json()["records_inserted"] == 1
        data_health = client.get("/v1/data-health").json()
        assert data_health["market_bars"] == 1
        assert data_health["source_documents"] == 0
        assert data_health["catalysts"] == 0
        assert data_health["feature_snapshots"] == 0
        assert data_health["experiment_runs"] == 0
        assert data_health["backtest_portfolio_events"] == 0
        assert data_health["alpaca_configured"] is False
        assert client.get("/v1/research/experiments").json() == []
        missing_events = client.get("/v1/research/experiments/missing/events")
        assert missing_events.status_code == 404
        oversized_backfill = client.post(
            "/v1/market-data/alpaca/backfill",
            json={
                "symbol": "AAPL",
                "start": "2025-01-01T00:00:00Z",
                "end": "2025-06-01T00:00:00Z",
                "timeframe": "1Day",
            },
        )
        assert oversized_backfill.status_code == 422
        missing_credentials = client.post("/v1/market-data/alpaca/probe")
        assert missing_credentials.status_code == 503
