from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient
from pydantic import SecretStr

from agentic_quant.api import create_app
from agentic_quant.config import AppEnvironment, Settings
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
        assert system_status["phase"] == (
            "6-authenticated-system-steward-control-center"
        )
        assert system_status["data_operating_scope"] == "bounded_correctness_samples"
        assert system_status["development_max_backfill_days"] == 120
        assert system_status["development_max_intraday_backfill_days"] == 7
        assert system_status["phase_1b_open_session_validation"] == "completed"
        assert system_status["llm_routing_version"] == "llm_routing@0.1.0"
        assert system_status["llm_budget_policy"] == "llm_budget@0.1.0"
        assert system_status["ml_policy"] == "ml_policy@0.1.0"
        assert system_status["openai_configured"] is False
        assert system_status["meta_model_configured"] is False
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
        assert data_health["validation_reports"] == 0
        assert data_health["validation_folds"] == 0
        assert data_health["llm_invocations"] == 0
        assert data_health["llm_routing_revisions"] == 0
        assert data_health["llm_budget_windows"] == 0
        assert data_health["llm_budget_reservations"] == 0
        assert data_health["research_analyses"] == 0
        assert data_health["ml_training_runs"] == 0
        assert data_health["ml_models"] == 0
        assert data_health["ml_forecasts"] == 0
        assert data_health["model_registry_events"] == 0
        assert data_health["data_quality_reports"] == 1
        assert data_health["failed_data_quality_reports"] == 0
        assert data_health["workflow_jobs"] == 0
        assert data_health["failed_workflow_jobs"] == 0
        assert data_health["reference_imports"] == 0
        assert data_health["alpaca_configured"] is False
        assert client.get("/v1/research/experiments").json() == []
        missing_events = client.get("/v1/research/experiments/missing/events")
        assert missing_events.status_code == 404
        assert client.get("/v1/research/validations").json() == []
        quality_reports = client.get("/v1/data-quality").json()
        assert len(quality_reports) == 1
        assert quality_reports[0]["status"] == "PASSED"
        assert client.get("/v1/workflow-jobs").json() == []
        missing_validation = client.get("/v1/research/validations/missing")
        assert missing_validation.status_code == 404
        routes = client.get("/v1/llm/routes").json()
        assert routes["routes"]["critical_research"] == "openai"
        assert routes["routes"]["interactive_explanation"] == "meta"
        assert routes["route_source"] == "yaml_base"
        assert routes["automatic_fallback"] is False
        route_update = client.put(
            "/v1/llm/routes",
            json={
                "routes": {
                    "interactive_explanation": "openai",
                    "routine_pipeline": "meta",
                    "critical_research": "openai",
                    "strategy_generation": "openai",
                    "strategy_critique": "openai",
                },
                "reason": "API route revision test",
            },
        )
        assert route_update.status_code == 200
        assert route_update.json()["status"] == "PENDING_CONFIRMATION"
        route_action = route_update.json()
        confirmed_route_update = client.post(
            f"/v1/actions/{route_action['action_request_id']}/confirm",
            json={"confirmation_phrase": route_action["confirmation_phrase"]},
        )
        assert confirmed_route_update.status_code == 200
        effective_routes = client.get("/v1/llm/routes").json()
        assert effective_routes["route_source"] == "control_center"
        assert effective_routes["routes"][
            "interactive_explanation"
        ] == "openai"
        history = client.get("/v1/llm/routes/history").json()
        assert len(history) == 1
        assert history[0]["reason"] == "API route revision test"
        assert client.get("/v1/llm/invocations").json() == []
        budget = client.get("/v1/llm/budget").json()
        assert budget["policy_version"] == "llm_budget@0.1.0"
        assert budget["limits"]["project_daily"] == {
            "max_tokens": 500_000,
            "max_estimated_cost_usd": "20.00",
        }
        assert set(budget["limits"]["provider_daily"]) == {"openai", "meta"}
        assert budget["windows"] == []
        assert client.get("/v1/intelligence/analyses").json() == []
        assert client.get("/v1/decision-inspector/missing").status_code == 404
        assert client.get("/v1/ml/training-runs").json() == []
        assert client.get("/v1/ml/models").json() == []
        assert client.get("/v1/ml/forecasts").json() == []
        assert client.get("/v1/ml/registry-events").json() == []
        missing_invocation = client.get("/v1/llm/invocations/missing")
        assert missing_invocation.status_code == 404
        missing_llm_credentials = client.post("/v1/llm/probe/openai")
        assert missing_llm_credentials.status_code == 503
        missing_chat_credentials = client.post(
            "/v1/llm/chat",
            json={"message": "hello", "provider": "meta"},
        )
        assert missing_chat_credentials.status_code == 503
        page = client.get("/")
        assert page.status_code == 200
        assert "Model routing" in page.text
        assert "System Steward" in page.text
        assert 'data-page="steward"' in page.text
        assert 'class="steward-page"' in page.text
        assert "function renderMarkdown" in page.text
        assert "LLM budget &amp; usage" in page.text
        assert "renderLLMBudget" in page.text
        assert '<aside id="steward"' not in page.text
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


def test_unauthenticated_llm_controls_fail_closed_in_production(
    settings: Settings,
) -> None:
    production = settings.model_copy(
        update={
            "app_env": AppEnvironment.PRODUCTION,
            "auto_migrate": False,
            "global_new_exposure_paused": True,
            "auth_required": True,
            "admin_username": "admin",
            "admin_password_hash": SecretStr("not-used-test-hash"),
            "session_secret": SecretStr("x" * 64),
        }
    )
    routes = {
        "interactive_explanation": "meta",
        "routine_pipeline": "meta",
        "critical_research": "openai",
        "strategy_generation": "openai",
        "strategy_critique": "openai",
    }
    with TestClient(create_app(production)) as client:
        route_update = client.put(
            "/v1/llm/routes",
            json={"routes": routes, "reason": "Must be rejected"},
        )
        chat = client.post(
            "/v1/llm/chat",
            json={"message": "Must be rejected", "provider": "openai"},
        )
        demo = client.post("/v1/demo/run")

    assert route_update.status_code == 401
    assert chat.status_code == 401
    assert demo.status_code == 401
