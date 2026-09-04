from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, insert, select

from agentic_quant.database import market_bars
from agentic_quant.domain import (
    CorporateAction,
    CorporateActionType,
    StockBar,
    UniverseMembership,
)
from agentic_quant.ids import uuid7
from agentic_quant.ledger import EventLedger
from agentic_quant.market_calendar import MarketSessionClock
from agentic_quant.market_store import MarketDataStore
from agentic_quant.migrations import upgrade_database
from agentic_quant.reference_data import (
    ReferenceDataStore,
    corporate_action_fingerprint,
)
from agentic_quant.research import (
    FeatureParityChecker,
    PointInTimeFeatureBuilder,
    ResearchBacktester,
    default_strategy_spec,
    research_code_sha256,
)
from agentic_quant.research_store import ResearchStore


def _daily_bars(
    *,
    symbol: str = "AAPL",
    count: int = 40,
    split_index: int | None = None,
) -> tuple[StockBar, ...]:
    clock = MarketSessionClock("XNYS")
    sessions = clock.calendar.sessions_in_range("2025-01-02", "2025-06-30")[:count]
    bars: list[StockBar] = []
    for index, session in enumerate(sessions):
        event_time = datetime.combine(
            session.date(),
            datetime.min.time(),
            tzinfo=UTC,
        )
        raw_price = (
            Decimal("50")
            if split_index is not None and index >= split_index
            else Decimal("100")
        )
        bars.append(
            StockBar(
                bar_id=uuid7(),
                symbol=symbol,
                timeframe="1Day",
                event_time=event_time,
                available_from=clock.daily_bar_available_from(event_time),
                open=raw_price,
                high=raw_price,
                low=raw_price,
                close=raw_price,
                volume=2_000_000 if split_index is not None and index >= split_index else 1_000_000,
                trade_count=10_000,
                vwap=raw_price,
                source="fixture",
                feed="test",
                raw_object_id="TEST_RAW",
                ingested_at=datetime(2026, 9, 4, tzinfo=UTC),
            )
        )
    return tuple(bars)


def _stores(settings):  # type: ignore[no-untyped-def]
    upgrade_database(settings.database_url)
    ledger = EventLedger(settings.database_url)
    return (
        ledger,
        MarketDataStore(ledger.engine),
        ResearchStore(ledger.engine),
        ReferenceDataStore(ledger.engine),
    )


def _split_action(
    *,
    effective_at: datetime,
    available_from: datetime,
) -> CorporateAction:
    fingerprint = corporate_action_fingerprint(
        symbol="AAPL",
        action_type=CorporateActionType.SPLIT.value,
        effective_at=effective_at,
        split_ratio="2",
        source="fixture",
    )
    return CorporateAction(
        corporate_action_id=uuid7(),
        action_fingerprint=fingerprint,
        symbol="AAPL",
        action_type=CorporateActionType.SPLIT,
        effective_at=effective_at,
        available_from=available_from,
        split_ratio=Decimal("2"),
        source="fixture",
        raw_object_id="TEST_ACTION_RAW",
        ingested_at=datetime(2026, 9, 4, tzinfo=UTC),
    )


def test_migration_rewrites_existing_daily_bar_to_exact_session_close(
    settings,  # type: ignore[no-untyped-def]
) -> None:
    project_root = Path(__file__).parents[1]
    config = Config(str(project_root / "alembic.ini"))
    config.set_main_option("script_location", str(project_root / "migrations"))
    config.attributes["database_url"] = settings.database_url
    command.upgrade(config, "20260904_0007")
    engine = create_engine(settings.database_url)
    event_time = datetime(2026, 9, 2, 4, tzinfo=UTC)
    bar = _daily_bars(count=1)[0].model_copy(
        update={
            "event_time": event_time,
            "available_from": event_time + timedelta(days=1),
        }
    )
    with engine.begin() as connection:
        connection.execute(insert(market_bars).values(**bar.model_dump()))

    command.upgrade(config, "head")

    with engine.connect() as connection:
        available_from = connection.execute(
            select(market_bars.c.available_from).where(
                market_bars.c.bar_id == bar.bar_id
            )
        ).scalar_one()
    assert available_from.replace(tzinfo=UTC) == datetime(
        2026, 9, 2, 20, tzinfo=UTC
    )


def test_reference_data_is_idempotent_and_point_in_time_safe(
    settings,  # type: ignore[no-untyped-def]
) -> None:
    _, _, _, store = _stores(settings)
    effective_at = datetime(2025, 2, 3, tzinfo=UTC)
    action = _split_action(
        effective_at=effective_at,
        available_from=effective_at - timedelta(days=5),
    )
    assert store.insert_corporate_actions((action,)) == (action.corporate_action_id,)
    assert store.insert_corporate_actions((action,)) == ()
    delayed_fingerprint = corporate_action_fingerprint(
        symbol="AAPL",
        action_type=CorporateActionType.CASH_DIVIDEND.value,
        effective_at=effective_at - timedelta(days=2),
        cash_amount="0.25",
        source="fixture",
    )
    delayed = CorporateAction(
        corporate_action_id=uuid7(),
        action_fingerprint=delayed_fingerprint,
        symbol="AAPL",
        action_type=CorporateActionType.CASH_DIVIDEND,
        effective_at=effective_at - timedelta(days=2),
        available_from=effective_at + timedelta(days=1),
        cash_amount=Decimal("0.25"),
        currency="USD",
        source="fixture",
        raw_object_id="TEST_ACTION_RAW",
        ingested_at=datetime(2026, 9, 4, tzinfo=UTC),
    )
    assert store.insert_corporate_actions((delayed,)) == (
        delayed.corporate_action_id,
    )
    assert store.corporate_actions_as_of(
        symbol="AAPL",
        as_of=effective_at - timedelta(days=1),
    ) == ()
    assert store.corporate_actions_as_of(
        symbol="AAPL",
        as_of=effective_at,
    ) == (action,)
    known_later = store.corporate_actions_as_of(
        symbol="AAPL",
        as_of=effective_at + timedelta(days=1),
    )
    assert tuple(item.action_type for item in known_later) == (
        CorporateActionType.CASH_DIVIDEND,
        CorporateActionType.SPLIT,
    )


def test_universe_membership_reconstructs_historical_constituents(
    settings,  # type: ignore[no-untyped-def]
) -> None:
    _, _, _, store = _stores(settings)
    january = datetime(2025, 1, 1, tzinfo=UTC)
    memberships = (
        UniverseMembership(
            membership_id=uuid7(),
            universe="SP500_TEST",
            symbol="AAA",
            effective_from=january,
            effective_to=datetime(2025, 2, 1, tzinfo=UTC),
            available_from=january - timedelta(days=1),
            source="fixture",
            source_version="2025-01",
            created_at=datetime(2026, 9, 4, tzinfo=UTC),
        ),
        UniverseMembership(
            membership_id=uuid7(),
            universe="SP500_TEST",
            symbol="BBB",
            effective_from=january,
            available_from=datetime(2025, 2, 15, tzinfo=UTC),
            source="fixture",
            source_version="2025-02",
            created_at=datetime(2026, 9, 4, tzinfo=UTC),
        ),
    )
    assert len(store.insert_universe_memberships(memberships)) == 2
    assert store.insert_universe_memberships(memberships) == ()
    assert store.universe_symbols_as_of(
        universe="SP500_TEST",
        as_of=datetime(2025, 1, 15, tzinfo=UTC),
    ) == ("AAA",)
    assert store.universe_symbols_as_of(
        universe="SP500_TEST",
        as_of=datetime(2025, 3, 1, tzinfo=UTC),
    ) == ("BBB",)


def test_split_adjustment_prevents_false_price_return(
    settings,  # type: ignore[no-untyped-def]
) -> None:
    _, market_store, research_store, reference_store = _stores(settings)
    bars = _daily_bars(count=25, split_index=20)
    market_store.insert_bars(bars, raw_object_id="TEST_RAW")
    reference_store.insert_corporate_actions(
        (
            _split_action(
                effective_at=bars[20].event_time,
                available_from=bars[19].available_from,
            ),
        )
    )

    snapshot = PointInTimeFeatureBuilder(research_store).build(
        symbol="AAPL",
        timeframe="1Day",
        as_of=bars[20].available_from,
        bars=bars,
    )

    assert Decimal(str(snapshot.values["return_1"])) == Decimal("0")
    assert Decimal(str(snapshot.values["split_adjustment_factor_oldest"])) == Decimal(
        "2"
    )
    assert snapshot.values["corporate_action_count"] == 1


def test_feature_parity_excludes_future_rows_and_persists_result(
    settings,  # type: ignore[no-untyped-def]
) -> None:
    _, market_store, research_store, _ = _stores(settings)
    bars = _daily_bars(count=30)
    market_store.insert_bars(bars, raw_object_id="TEST_RAW")

    check = FeatureParityChecker(research_store).check(
        symbol="AAPL",
        timeframe="1Day",
        as_of=bars[20].available_from,
    )

    assert check.matched is True
    assert check.offline_data_hash == check.online_data_hash
    assert research_store.health_summary()["feature_parity_checks"] == 1


def test_backtester_fails_closed_when_window_contains_corporate_action(
    settings,  # type: ignore[no-untyped-def]
) -> None:
    ledger, market_store, research_store, reference_store = _stores(settings)
    bars = _daily_bars(count=40, split_index=25)
    market_store.insert_bars(bars, raw_object_id="TEST_RAW")
    reference_store.insert_corporate_actions(
        (
            _split_action(
                effective_at=bars[25].event_time,
                available_from=bars[24].available_from,
            ),
        )
    )
    spec = default_strategy_spec(
        "buy_and_hold",
        timeframe="1Day",
        code_sha256=research_code_sha256(),
    )

    with pytest.raises(ValueError, match="does not simulate corporate-action"):
        ResearchBacktester(research_store, ledger).run(
            spec=spec,
            symbol="AAPL",
            as_of_start=bars[20].available_from,
            as_of_end=bars[-1].available_from,
            code_git_sha="test-git-sha",
        )
