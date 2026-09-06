from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from hypothesis import HealthCheck, given, settings as hypothesis_settings
from hypothesis import strategies as st
from pydantic import ValidationError

from agentic_quant.config import AppEnvironment, Settings, TradingMode
from agentic_quant.domain import (
    AccountState,
    Direction,
    FeatureSnapshot,
    RiskEvaluationContext,
    SignalCandidate,
    Verdict,
)
from agentic_quant.ids import uuid7
from agentic_quant.risk import RestrictionRegistry, RiskPolicy, evaluate_candidate


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
    )
    assert production.data_operating_scope == "durable_long_horizon"
    production.validate_backfill_window(start=start, end=end, timeframe="1Min")


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


def test_restricted_symbol_always_rejected(settings: Settings) -> None:
    result = evaluate(settings, candidate("META"))
    assert result.verdict == Verdict.REJECT
    assert "SECURITY_RESTRICTED" in result.reason_codes
    assert result.max_quantity == 0


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
