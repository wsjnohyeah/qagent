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
        assert data_health["alpaca_configured"] is False
        missing_credentials = client.post("/v1/market-data/alpaca/probe")
        assert missing_credentials.status_code == 503
