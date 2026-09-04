from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from hypothesis import HealthCheck, given, settings as hypothesis_settings
from hypothesis import strategies as st
from pydantic import ValidationError

from agentic_quant.config import AppEnvironment, Settings, TradingMode
from agentic_quant.domain import AccountState, Direction, FeatureSnapshot, SignalCandidate, Verdict
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


def evaluate(settings: Settings, item: SignalCandidate, *, max_risk: str = "130"):
    policy = RiskPolicy.from_yaml(settings.risk_policy_path).model_copy(
        update={"maximum_trade_risk_usd": Decimal(max_risk)}
    )
    return evaluate_candidate(
        candidate=item,
        features=features(),
        account=AccountState(
            equity=Decimal("52000"),
            daily_pnl=Decimal("0"),
            concurrent_planned_risk=Decimal("0"),
        ),
        mode=TradingMode.SHADOW,
        policy=policy,
        restrictions=RestrictionRegistry.from_yaml(settings.restricted_securities_path),
        evaluated_at=item.as_of,
        new_exposure_paused=False,
    )


def test_live_mode_is_not_representable() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, trading_mode="live")
    with pytest.raises(ValidationError):
        Settings(_env_file=None, live_trading_enabled=True)


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
        evaluated_at=item.as_of,
        new_exposure_paused=True,
    )
    assert result.verdict == Verdict.REJECT
    assert "GLOBAL_NEW_EXPOSURE_PAUSED" in result.reason_codes
