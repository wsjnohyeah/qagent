from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings as hypothesis_settings
from hypothesis import strategies as st
from pydantic import ValidationError
from pydantic import SecretStr
from sqlalchemy import insert

from agentic_quant.api import create_app
from agentic_quant.config import AppEnvironment, Settings, TradingMode
from agentic_quant.database import runtime_controls
from agentic_quant.domain import (
    AccountState,
    Direction,
    FeatureSnapshot,
    RiskEvaluationContext,
    SignalCandidate,
    Verdict,
)
from agentic_quant.ids import uuid7
from agentic_quant.ledger import EventLedger
from agentic_quant.migrations import upgrade_database
from agentic_quant.production_bootstrap import bootstrap_production
from agentic_quant.risk import (
    MULTI_SESSION_EXECUTION_PROFILE_VERSION,
    RestrictionRegistry,
    RiskPolicy,
    baseline_long_exit,
    deployable_long_exit,
    deployable_long_limit_fill,
    evaluate_candidate,
    strategy_execution_profile,
    strategy_holding_period_sessions,
    strategy_signal_risk_policy,
)


def test_production_worker_healthchecks_allow_cold_import_latency() -> None:
    compose = yaml.safe_load(Path("compose.production.yml").read_text())

    for service_name in ("worker", "coordinator"):
        healthcheck = compose["services"][service_name]["healthcheck"]
        assert healthcheck["interval"] == "60s"
        assert healthcheck["timeout"] == "20s"
        assert healthcheck["retries"] == 3
    coordinator_environment = compose["services"]["coordinator"]["environment"]
    api_environment = compose["services"]["api"]["environment"]
    assert api_environment["SHADOW_RUNTIME_ENABLED"].endswith(":-true}")
    assert api_environment["PAPER_TRADING_ENABLED"].endswith(":-false}")
    assert api_environment["AUTONOMOUS_COORDINATOR_ENABLED"].endswith(":-true}")
    assert coordinator_environment["COORDINATOR_AUTO_SHADOW_ENABLED"].endswith(
        ":-false}"
    )
    assert compose["services"]["worker"]["environment"][
        "COORDINATOR_AUTO_SHADOW_ENABLED"
    ] == "false"
    assert api_environment["ROBINHOOD_MCP_BRIDGE_ENABLED"].endswith(":-false}")
    assert api_environment["ROBINHOOD_ORDER_SUBMISSION_ENABLED"] == "false"
    assert compose["services"]["worker"]["environment"][
        "ROBINHOOD_MCP_BRIDGE_ENABLED"
    ] == "false"
    assert compose["services"]["coordinator"]["environment"][
        "ROBINHOOD_MCP_BRIDGE_ENABLED"
    ] == "false"
    assert coordinator_environment["MARKET_SCANNER_ENABLED"].endswith(":-false}")
    assert coordinator_environment["MARKET_SCANNER_LLM_ENABLED"].endswith(
        ":-false}"
    )
    assert coordinator_environment[
        "MARKET_SCANNER_AUTO_TRADING_POOL_ENABLED"
    ].endswith(":-false}")


def test_baseline_bracket_executes_stop_first_when_intrabar_order_is_unknown() -> None:
    price, reason = baseline_long_exit(
        open_price=Decimal("100"),
        high_price=Decimal("105"),
        low_price=Decimal("95"),
        close_price=Decimal("102"),
        invalidation=Decimal("98"),
        target=Decimal("104"),
    )
    assert (price, reason) == (Decimal("98"), "protective_stop")


def test_multi_session_profile_widens_price_stop_without_expanding_account_caps(
    settings,  # type: ignore[no-untyped-def]
) -> None:
    account_policy = RiskPolicy.from_yaml(settings.risk_policy_path)
    profile, parameters, effective = strategy_execution_profile(
        data_requirements={"holding_period_sessions": 252},
        account_policy=account_policy,
        strategy_type="momentum",
    )

    assert profile == MULTI_SESSION_EXECUTION_PROFILE_VERSION
    assert parameters["maximum_holding_sessions"] == 252
    assert parameters["scheduled_exit"]["paper_compatible"] is False
    assert effective.baseline_stop_fraction == Decimal("0.15")
    assert parameters["price_stop_fraction"] == "point_in_time_volatility_derived"
    signal_policy = strategy_signal_risk_policy(
        data_requirements={"holding_period_sessions": 252},
        strategy_type="momentum",
        feature_values={"realized_vol_20": "0.80"},
        account_policy=account_policy,
    )
    assert signal_policy.baseline_stop_fraction == Decimal("0.15")
    assert signal_policy.baseline_target_r_multiple == Decimal("2.25")
    assert effective.maximum_trade_risk_usd == account_policy.maximum_trade_risk_usd
    assert (
        effective.maximum_concurrent_risk_usd
        == account_policy.maximum_concurrent_risk_usd
    )
    assert strategy_holding_period_sessions({}) == 1
    with pytest.raises(ValueError, match="must be one of"):
        strategy_holding_period_sessions({"holding_period_sessions": 2})


def test_deployable_limit_entry_caps_price_and_rejects_untouched_order() -> None:
    assert deployable_long_limit_fill(
        open_price=Decimal("99"),
        low_price=Decimal("97"),
        limit_price=Decimal("100"),
    ) == (Decimal("99"), "opening_auction")
    assert deployable_long_limit_fill(
        open_price=Decimal("103"),
        low_price=Decimal("99"),
        limit_price=Decimal("100"),
    ) == (Decimal("100"), "intraday_limit")
    assert (
        deployable_long_limit_fill(
            open_price=Decimal("103"),
            low_price=Decimal("101"),
            limit_price=Decimal("100"),
        )
        is None
    )


def test_intraday_limit_fill_never_claims_ambiguous_profit_target() -> None:
    price, reason = deployable_long_exit(
        entry_kind="intraday_limit",
        open_price=Decimal("103"),
        high_price=Decimal("110"),
        low_price=Decimal("99"),
        close_price=Decimal("101"),
        invalidation=Decimal("98"),
        target=Decimal("104"),
    )
    assert (price, reason) == (Decimal("101"), "market_on_close")


def test_sandbox_position_risk_is_two_percent_of_current_marked_equity(
    settings,  # type: ignore[no-untyped-def]
) -> None:
    item = candidate(stop="90")
    policy = RiskPolicy.from_yaml(settings.risk_policy_path).model_copy(
        update={
            "position_sizing_mode": "equity_fraction",
            "initial_risk_fraction": Decimal("0.02"),
            "daily_loss_limit_enabled": False,
        }
    )
    decision = evaluate_candidate(
        candidate=item.model_copy(
            update={
                "planned_entry": Decimal("100"),
                "invalidation": Decimal("90"),
                "targets": (Decimal("122.50"),),
            }
        ),
        features=features().model_copy(
            update={"feature_snapshot_id": item.feature_snapshot_id}
        ),
        account=AccountState(
            equity=Decimal("9500"),
            daily_pnl=Decimal("-500"),
            concurrent_planned_risk=Decimal("9000"),
        ),
        mode=TradingMode.SHADOW,
        policy=policy,
        restrictions=RestrictionRegistry.from_yaml(
            settings.restricted_securities_path
        ),
        context=risk_context(),
        evaluated_at=item.as_of,
        new_exposure_paused=False,
    )

    assert decision.verdict == Verdict.APPROVE
    assert decision.risk_budget_usd == Decimal("190.00")
    assert decision.max_quantity == 18


def candidate(symbol: str = "DEMO", stop: str = "16.65") -> SignalCandidate:
    now = datetime(2026, 9, 4, 14, 45, tzinfo=UTC)
    return SignalCandidate(
        candidate_id=uuid7(), symbol=symbol, direction=Direction.LONG,
        setup_type="event_pullback_continuation", strategy_version="test@1",
        as_of=now, feature_snapshot_id=uuid7(), catalyst_id=uuid7(),
        planned_entry=Decimal("17.50"), invalidation=Decimal(stop),
        targets=(Decimal("18.90"),), expires_at=now + timedelta(hours=1),
    )


def features() -> FeatureSnapshot:
    return FeatureSnapshot(
        feature_snapshot_id=uuid7(), as_of=datetime(2026, 9, 4, 14, 45, tzinfo=UTC),
        relative_volume=Decimal("2.50"), vwap_confirmed=True,
        opening_range_confirmed=True, sector_compatible=True, quote_age_seconds=1,
    )


def risk_context(**updates: object) -> RiskEvaluationContext:
    return RiskEvaluationContext(
        catalyst_required=True,
        catalyst_verified=True,
        restriction_status_known=True,
        liquidity_confirmed=True,
        market_data_healthy=True,
        macro_calendar_status_known=True,
    ).model_copy(update=updates)


def evaluate(
    settings: Settings,
    item: SignalCandidate,
    *,
    max_risk: str = "130",
    context: RiskEvaluationContext | None = None,
    feature_snapshot: FeatureSnapshot | None = None,
):
    policy = RiskPolicy.from_yaml(settings.risk_policy_path).model_copy(
        update={"maximum_trade_risk_usd": Decimal(max_risk)}
    )
    return evaluate_candidate(
        candidate=item,
        features=feature_snapshot or features().model_copy(
            update={"feature_snapshot_id": item.feature_snapshot_id}
        ),
        account=AccountState(
            equity=Decimal("52000"),
            daily_pnl=Decimal("0"),
            concurrent_planned_risk=Decimal("0"),
        ),
        mode=TradingMode.SHADOW,
        policy=policy,
        restrictions=RestrictionRegistry.from_yaml(settings.restricted_securities_path),
        context=context or risk_context(),
        evaluated_at=item.as_of,
        new_exposure_paused=False,
    )


def test_live_mode_is_not_representable() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, trading_mode="live")
    with pytest.raises(ValidationError):
        Settings(_env_file=None, live_trading_enabled=True)


def test_market_scanner_asset_metadata_is_pinned_to_paper_host() -> None:
    with pytest.raises(ValidationError, match="requires MARKET_SCANNER_ENABLED"):
        Settings(
            _env_file=None,
            market_scanner_llm_enabled=True,
        )
    with pytest.raises(ValidationError, match="asset metadata is hard-pinned"):
        Settings(
            _env_file=None,
            market_scanner_enabled=True,
            alpaca_paper_base_url="https://api.alpaca.markets",
        )
    with pytest.raises(ValidationError, match="requires both"):
        Settings(
            _env_file=None,
            market_scanner_enabled=True,
            market_scanner_auto_trading_pool_enabled=True,
        )
    with pytest.raises(ValidationError, match="requires AUTONOMOUS_COORDINATOR"):
        Settings(
            _env_file=None,
            coordinator_auto_shadow_enabled=True,
        )


def test_risk_inputs_require_aware_ordered_timestamps() -> None:
    feature_values = features().model_dump()
    feature_values["as_of"] = datetime(2026, 9, 4, 14, 45)
    with pytest.raises(ValidationError, match="Feature snapshot as_of"):
        FeatureSnapshot.model_validate(feature_values)
    with pytest.raises(ValidationError, match="Signal expiry"):
        item = candidate()
        SignalCandidate.model_validate(
            item.model_copy(update={"expires_at": item.as_of}).model_dump()
        )
    with pytest.raises(ValidationError, match="Major macro event timestamp"):
        RiskEvaluationContext(
            catalyst_required=False,
            catalyst_verified=False,
            restriction_status_known=True,
            liquidity_confirmed=True,
            market_data_healthy=True,
            macro_calendar_status_known=True,
            nearest_major_macro_event_at=datetime(2026, 9, 4, 14, 45),
        )


def test_development_backfills_are_bounded_but_production_is_not() -> None:
    start = datetime(2025, 1, 1, tzinfo=UTC)
    end = start + timedelta(days=121)
    development = Settings(
        _env_file=None,
        app_env=AppEnvironment.DEVELOPMENT,
        development_max_backfill_days=120,
    )
    assert development.data_operating_scope == "bounded_correctness_samples"
    with pytest.raises(ValueError, match="Development backfills are bounded"):
        development.validate_backfill_window(start=start, end=end)
    with pytest.raises(ValueError, match="bounded to 7 days for 1Min"):
        development.validate_backfill_window(
            start=start,
            end=start + timedelta(days=8),
            timeframe="1Min",
        )

    production = Settings(
        _env_file=None,
        app_env=AppEnvironment.PRODUCTION,
        auto_migrate=False,
        auth_required=True,
        admin_username="admin",
        admin_password_hash="not-used-test-hash",
        session_secret="x" * 64,
        source_git_sha="a" * 40,
    )
    assert production.data_operating_scope == "durable_long_horizon"
    production.validate_backfill_window(start=start, end=end, timeframe="1Min")

    with pytest.raises(ValidationError, match="40-character SOURCE_GIT_SHA"):
        Settings(
            _env_file=None,
            app_env=AppEnvironment.PRODUCTION,
            auto_migrate=False,
            auth_required=True,
            admin_username="admin",
            admin_password_hash="not-used-test-hash",
            session_secret="x" * 64,
        )


def test_production_boots_paused_and_never_auto_migrates() -> None:
    with pytest.raises(ValidationError, match="new exposure paused"):
        Settings(
            _env_file=None,
            app_env=AppEnvironment.PRODUCTION,
            global_new_exposure_paused=False,
            auto_migrate=False,
        )
    with pytest.raises(ValidationError, match="AUTO_MIGRATE=false"):
        Settings(
            _env_file=None,
            app_env=AppEnvironment.PRODUCTION,
            global_new_exposure_paused=True,
            auto_migrate=True,
        )
    with pytest.raises(ValidationError, match="AUTH_REQUIRED=true"):
        Settings(
            _env_file=None,
            app_env=AppEnvironment.PRODUCTION,
            global_new_exposure_paused=True,
            auto_migrate=False,
            auth_required=False,
        )


def test_production_bootstrap_rejects_local_database(settings: Settings) -> None:
    production = settings.model_copy(
        update={
            "app_env": AppEnvironment.PRODUCTION,
            "auto_migrate": False,
            "auth_required": True,
            "admin_username": "admin",
            "admin_password_hash": SecretStr("test-hash"),
            "session_secret": SecretStr("x" * 64),
            "global_new_exposure_paused": True,
            "redis_url": "redis://redis:6379/0",
            "deployment_environment_id": "prod-test",
            "source_git_sha": "a" * 40,
        }
    )
    with pytest.raises(ValueError, match="requires PostgreSQL"):
        bootstrap_production(production)


def test_production_worker_restart_pauses_but_api_restart_preserves_control(
    settings: Settings,
) -> None:
    upgrade_database(settings.database_url)
    ledger = EventLedger(settings.database_url)
    with ledger.engine.begin() as connection:
        connection.execute(
            insert(runtime_controls).values(
                control_key="new_exposure_paused",
                state_json={"paused": False},
                updated_by="previous-human-confirmation",
                updated_at=datetime.now(UTC),
            )
        )
    production = settings.model_copy(
        update={
            "app_env": AppEnvironment.PRODUCTION,
            "auto_migrate": False,
            "auth_required": True,
            "admin_username": "admin",
            "admin_password_hash": SecretStr("test-hash"),
            "session_secret": SecretStr("x" * 64),
            "global_new_exposure_paused": True,
            "shadow_runtime_enabled": False,
            "source_git_sha": "a" * 40,
        }
    )
    with TestClient(create_app(production, process_role="api")) as client:
        assert client.app.state.actions.new_exposure_paused(default=True) is False
    with TestClient(create_app(production, process_role="worker")) as client:
        assert client.app.state.actions.new_exposure_paused(default=False) is True


def test_restricted_symbol_always_rejected(settings: Settings) -> None:
    result = evaluate(settings, candidate("META"))
    assert result.verdict == Verdict.REJECT
    assert "SECURITY_RESTRICTED" in result.reason_codes
    assert result.max_quantity == 0


def test_decision_created_after_earliest_execution_is_rejected(
    settings: Settings,
) -> None:
    result = evaluate(
        settings,
        candidate(),
        context=risk_context(decision_before_execution=False),
    )

    assert result.verdict == Verdict.REJECT
    assert "DECISION_TOO_LATE_FOR_EARLIEST_EXECUTION" in result.reason_codes


@given(st.decimals(min_value="15.00", max_value="16.60", places=2))
@hypothesis_settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_increasing_risk_distance_cannot_increase_quantity(
    settings: Settings, farther_stop: Decimal
) -> None:
    near = evaluate(settings, candidate(stop="16.65"))
    farther = evaluate(settings, candidate(stop=str(farther_stop)))
    assert farther.max_quantity <= near.max_quantity


def test_stricter_cap_cannot_increase_quantity(settings: Settings) -> None:
    strict = evaluate(settings, candidate(), max_risk="65")
    normal = evaluate(settings, candidate(), max_risk="130")
    assert strict.max_quantity <= normal.max_quantity


def test_pause_fails_closed(settings: Settings) -> None:
    item = candidate()
    result = evaluate_candidate(
        candidate=item,
        features=features(),
        account=AccountState(
            equity=Decimal("52000"),
            daily_pnl=Decimal("0"),
            concurrent_planned_risk=Decimal("0"),
        ),
        mode=TradingMode.SHADOW,
        policy=RiskPolicy.from_yaml(settings.risk_policy_path),
        restrictions=RestrictionRegistry.from_yaml(settings.restricted_securities_path),
        context=risk_context(),
        evaluated_at=item.as_of,
        new_exposure_paused=True,
    )
    assert result.verdict == Verdict.REJECT
    assert "GLOBAL_NEW_EXPOSURE_PAUSED" in result.reason_codes


def test_paper_mode_cannot_target_the_live_alpaca_host() -> None:
    with pytest.raises(ValidationError, match="TRADING_MODE=paper"):
        Settings(
            _env_file=None,
            trading_mode=TradingMode.SHADOW,
            paper_trading_enabled=True,
            alpaca_api_key=SecretStr("paper-key"),
            alpaca_api_secret=SecretStr("paper-secret"),
        )
    with pytest.raises(ValidationError, match="hard-pinned"):
        Settings(
            _env_file=None,
            trading_mode=TradingMode.PAPER,
            paper_trading_enabled=True,
            alpaca_api_key=SecretStr("paper-key"),
            alpaca_api_secret=SecretStr("paper-secret"),
            alpaca_paper_base_url="https://api.alpaca.markets",
        )


@pytest.mark.parametrize(
    ("updates", "reason_code"),
    [
        ({"catalyst_verified": False}, "CATALYST_UNVERIFIED"),
        ({"restriction_status_known": False}, "RESTRICTION_STATUS_UNKNOWN"),
        ({"liquidity_confirmed": False}, "LIQUIDITY_NOT_CONFIRMED"),
        ({"market_data_healthy": False}, "MARKET_DATA_UNHEALTHY"),
        ({"macro_calendar_status_known": False}, "MACRO_CALENDAR_STATUS_UNKNOWN"),
        ({"duplicate_order_detected": True}, "DUPLICATE_ORDER"),
    ],
)
def test_external_risk_facts_fail_closed(
    settings: Settings,
    updates: dict[str, object],
    reason_code: str,
) -> None:
    result = evaluate(settings, candidate(), context=risk_context(**updates))
    assert result.verdict == Verdict.REJECT
    assert reason_code in result.reason_codes
    assert result.max_quantity == 0


def test_major_macro_event_blackout_requires_strategy_approval(settings: Settings) -> None:
    item = candidate()
    macro_event = item.as_of + timedelta(hours=12)
    rejected = evaluate(
        settings,
        item,
        context=risk_context(nearest_major_macro_event_at=macro_event),
    )
    assert rejected.verdict == Verdict.REJECT
    assert "MACRO_EVENT_BLACKOUT" in rejected.reason_codes

    approved = evaluate(
        settings,
        item,
        context=risk_context(
            nearest_major_macro_event_at=macro_event,
            macro_event_strategy_approved=True,
        ),
    )
    assert approved.verdict == Verdict.APPROVE


def test_risk_gate_rejects_mismatched_or_future_inputs(settings: Settings) -> None:
    item = candidate()
    mismatch = evaluate(settings, item, feature_snapshot=features())
    assert mismatch.verdict == Verdict.REJECT
    assert "FEATURE_SNAPSHOT_MISMATCH" in mismatch.reason_codes

    future_feature = features().model_copy(
        update={
            "feature_snapshot_id": item.feature_snapshot_id,
            "as_of": item.as_of + timedelta(minutes=1),
        }
    )
    future = evaluate(settings, item, feature_snapshot=future_feature)
    assert future.verdict == Verdict.REJECT
    assert "FEATURE_FROM_FUTURE" in future.reason_codes
