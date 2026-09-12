from __future__ import annotations

import hashlib
import math
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from statistics import mean, stdev
from typing import Any

from agentic_quant.backtest_engine import EventDrivenPortfolio
from agentic_quant.data_quality import MarketDataQualityService
from agentic_quant.domain import (
    AccountState,
    BacktestCostModel,
    BacktestMetrics,
    BacktestPortfolioEvent,
    BacktestResult,
    BacktestTrade,
    CorporateAction,
    CorporateActionType,
    Direction,
    EventEnvelope,
    ExperimentRun,
    ExperimentStatus,
    FeatureSnapshot,
    FeatureParityCheck,
    PointInTimeFeatureSnapshot,
    RiskEvaluationContext,
    SignalAction,
    SignalCandidate,
    StockBar,
    StrategySpec,
    Verdict,
)
from agentic_quant.ids import uuid7
from agentic_quant.ledger import EventLedger
from agentic_quant.market_calendar import MarketSessionClock
from agentic_quant.reference_data import ReferenceDataStore
from agentic_quant.research_store import ResearchStore, _canonical_hash
from agentic_quant.risk import (
    RestrictionRegistry,
    RiskPolicy,
    baseline_long_exit,
    baseline_long_geometry,
    deployable_long_exit,
    deployable_long_limit_fill,
    evaluate_candidate,
    normalize_deployable_long_prices,
    strategy_execution_profile,
    strategy_signal_risk_policy,
)
from agentic_quant.config import TradingMode


FEATURE_SET_VERSION = "price_event_pit@0.3.0"
BACKTEST_ENGINE_VERSION = "event_driven_portfolio@0.5.0"
SUPPORTED_STRATEGIES = ("buy_and_hold", "momentum", "mean_reversion")
_MINIMUM_HISTORY = 21
_ZERO = Decimal("0")
_ONE = Decimal("1")


def _average(values: list[Decimal]) -> Decimal:
    return sum(values, _ZERO) / Decimal(len(values))


def _decimal(value: float | int | Decimal) -> Decimal:
    return Decimal(str(round(float(value), 10)))


def strategy_signal_action(
    *,
    strategy_type: str,
    parameters: dict[str, Any],
    values: dict[str, Any],
) -> SignalAction:
    """Evaluate the versioned baseline signal contract shared by replay and shadow."""
    if strategy_type == "buy_and_hold":
        return SignalAction.LONG
    close = Decimal(str(values["close"]))
    return_window = int(parameters["return_window"])
    slow_window = int(parameters["slow_window"])
    if not 1 <= return_window < _MINIMUM_HISTORY:
        raise ValueError("return_window must be between 1 and 20 bars")
    if not 1 <= slow_window <= _MINIMUM_HISTORY:
        raise ValueError("slow_window must be between 1 and 21 bars")
    return_value = Decimal(str(values[f"return_{return_window}"]))
    slow_average = Decimal(str(values[f"sma_{slow_window}"]))
    if strategy_type == "momentum":
        minimum_return = Decimal(str(parameters["minimum_return"]))
        return (
            SignalAction.LONG
            if return_value > minimum_return and close > slow_average
            else SignalAction.FLAT
        )
    if strategy_type == "mean_reversion":
        maximum_return = Decimal(str(parameters["maximum_return"]))
        return (
            SignalAction.LONG
            if return_value < maximum_return and close < slow_average
            else SignalAction.FLAT
        )
    raise ValueError(f"Unsupported daily strategy: {strategy_type}")


class PointInTimeFeatureBuilder:
    def __init__(self, store: ResearchStore) -> None:
        self.store = store
        self.reference_data = ReferenceDataStore(store.engine)

    def build(
        self,
        *,
        symbol: str,
        timeframe: str,
        as_of: datetime,
        bars: tuple[StockBar, ...],
    ) -> PointInTimeFeatureSnapshot:
        if as_of.tzinfo is None:
            raise ValueError("Feature as_of must be timezone-aware")
        eligible = tuple(
            bar
            for bar in sorted(bars, key=lambda item: item.event_time)
            if bar.event_time <= as_of and bar.available_from <= as_of
        )
        if len(eligible) < _MINIMUM_HISTORY:
            raise ValueError(
                f"Feature set requires at least {_MINIMUM_HISTORY} available bars"
            )
        source_bars = eligible[-_MINIMUM_HISTORY:]
        actions = self.reference_data.corporate_actions_as_of(
            symbol=symbol,
            as_of=as_of,
            effective_from=source_bars[0].event_time,
        )
        evidence = self.store.build_evidence_packet(
            symbol=symbol,
            as_of=as_of,
            bars=source_bars,
        )
        closes = [self._split_adjusted_value(bar.close, bar, actions) for bar in source_bars]
        volumes = [
            self._split_adjusted_volume(bar.volume, bar, actions) for bar in source_bars
        ]
        one_period_returns = [
            closes[index] / closes[index - 1] - _ONE
            for index in range(1, len(closes))
        ]
        return_mean = _average(one_period_returns)
        variance = _average(
            [(value - return_mean) ** 2 for value in one_period_returns]
        )
        periods_per_year = Decimal("98280") if timeframe == "1Min" else Decimal("252")
        latest_catalysts = [
            reference
            for reference in evidence.references
            if reference.evidence_type == "catalyst"
        ]
        latest_facts = [
            reference
            for reference in evidence.references
            if reference.evidence_type == "corporate_fact"
        ]
        latest_event = max(
            (reference.event_time for reference in latest_catalysts),
            default=None,
        )
        average_volume_20 = _average(volumes[-20:])
        values: dict[str, Decimal | int | bool | str | None] = {
            "close": closes[-1],
            "distance_sma_20": closes[-1] / _average(closes[-20:]) - _ONE,
            "realized_vol_20": variance.sqrt() * periods_per_year.sqrt(),
            # Provider-evidenced inactive windows can contain only zero-volume
            # bars. Treat that missing baseline conservatively instead of
            # raising Decimal DivisionUndefined and exhausting the workflow.
            "volume_ratio_20": (
                _ZERO
                if average_volume_20 == _ZERO
                else volumes[-1] / average_volume_20
            ),
            "catalyst_count_90d": len(latest_catalysts),
            "corporate_fact_count": len(latest_facts),
            "corporate_action_count": len(actions),
            "cash_dividend_count": sum(
                1
                for action in actions
                if action.action_type == CorporateActionType.CASH_DIVIDEND
            ),
            "split_adjustment_factor_oldest": self._split_factor(
                source_bars[0],
                actions,
            ),
            "latest_catalyst_age_hours": (
                Decimal(str((as_of - latest_event).total_seconds())) / Decimal("3600")
                if latest_event is not None
                else None
            ),
            "bar_count": len(eligible),
        }
        # Windowed features are materialized explicitly so an immutable strategy's
        # declared parameters select real inputs instead of silently falling back to
        # hard-coded return_5/sma_20 values.
        for window in range(1, _MINIMUM_HISTORY):
            values[f"return_{window}"] = closes[-1] / closes[-(window + 1)] - _ONE
        for window in range(1, _MINIMUM_HISTORY + 1):
            values[f"sma_{window}"] = _average(closes[-window:])
        data_hash = _canonical_hash(
            {
                "evidence_hash": evidence.evidence_hash,
                "feature_set_version": FEATURE_SET_VERSION,
                "values": {key: str(value) for key, value in values.items()},
            }
        )
        snapshot = PointInTimeFeatureSnapshot(
            feature_snapshot_id=uuid7(),
            evidence_packet_id=evidence.evidence_packet_id,
            symbol=symbol.upper(),
            timeframe=timeframe,
            as_of=as_of,
            feature_set_version=FEATURE_SET_VERSION,
            values=values,
            source_max_available_from=evidence.source_max_available_from,
            data_hash=data_hash,
            created_at=datetime.now(UTC),
        )
        return self.store.record_feature_snapshot(snapshot)

    @staticmethod
    def _split_factor(
        bar: StockBar,
        actions: tuple[CorporateAction, ...],
    ) -> Decimal:
        factor = _ONE
        for action in actions:
            if (
                action.action_type == CorporateActionType.SPLIT
                and action.split_ratio is not None
                and bar.event_time < action.effective_at
            ):
                factor *= action.split_ratio
        return factor

    @classmethod
    def _split_adjusted_value(
        cls,
        value: Decimal,
        bar: StockBar,
        actions: tuple[CorporateAction, ...],
    ) -> Decimal:
        return value / cls._split_factor(bar, actions)

    @classmethod
    def _split_adjusted_volume(
        cls,
        value: int,
        bar: StockBar,
        actions: tuple[CorporateAction, ...],
    ) -> Decimal:
        return Decimal(value) * cls._split_factor(bar, actions)


class FeatureParityChecker:
    def __init__(self, store: ResearchStore) -> None:
        self.store = store
        self.features = PointInTimeFeatureBuilder(store)

    def check(
        self,
        *,
        symbol: str,
        timeframe: str,
        as_of: datetime,
    ) -> FeatureParityCheck:
        complete_history = self.store.load_bars_for_parity_audit(
            symbol=symbol,
            timeframe=timeframe,
        )
        online_history = tuple(
            bar
            for bar in complete_history
            if bar.event_time <= as_of and bar.available_from <= as_of
        )
        offline_snapshot = self.features.build(
            symbol=symbol,
            timeframe=timeframe,
            as_of=as_of,
            bars=complete_history,
        )
        online_snapshot = self.features.build(
            symbol=symbol,
            timeframe=timeframe,
            as_of=as_of,
            bars=online_history,
        )
        check = FeatureParityCheck(
            parity_check_id=uuid7(),
            symbol=symbol.upper(),
            timeframe=timeframe,
            as_of=as_of,
            feature_set_version=FEATURE_SET_VERSION,
            offline_data_hash=offline_snapshot.data_hash,
            online_data_hash=online_snapshot.data_hash,
            matched=offline_snapshot.data_hash == online_snapshot.data_hash,
            checked_at=datetime.now(UTC),
        )
        self.store.record_feature_parity_check(check)
        return check


def default_strategy_spec(
    strategy_type: str,
    *,
    timeframe: str,
    code_sha256: str,
) -> StrategySpec:
    if strategy_type not in SUPPORTED_STRATEGIES:
        raise ValueError(f"Unsupported strategy: {strategy_type}")
    parameters: dict[str, object]
    if strategy_type == "momentum":
        parameters = {"return_window": 5, "slow_window": 20, "minimum_return": "0"}
    elif strategy_type == "mean_reversion":
        parameters = {"return_window": 5, "slow_window": 20, "maximum_return": "-0.02"}
    else:
        parameters = {
            "minimum_history": _MINIMUM_HISTORY,
            "holding_period": "backtest_end",
        }
    return StrategySpec(
        strategy_spec_id=uuid7(),
        name=(
            f"phase3b_buy_and_hold_benchmark_{timeframe.casefold()}"
            if strategy_type == "buy_and_hold"
            else f"phase3b_{strategy_type}_{timeframe.casefold()}"
        ),
        version=f"0.3.0+{code_sha256[:12]}",
        strategy_type=strategy_type,
        timeframe=timeframe,
        feature_set_version=FEATURE_SET_VERSION,
        parameters=parameters,
        data_requirements={
            "minimum_bars": _MINIMUM_HISTORY + 1,
            "execution": (
                "signal available at t; conditional market-on-open at the next "
                "bar with open-price risk revalidation"
            ),
            "holding_period": (
                "backtest_end" if strategy_type == "buy_and_hold" else "one_bar"
            ),
            **(
                {"holding_period_sessions": 1, "position_style": "day"}
                if strategy_type != "buy_and_hold"
                else {}
            ),
            "entry_liquidity_source": "latest completed decision bar volume",
            "shadow_deployable": strategy_type != "buy_and_hold",
            "paper_deployable": strategy_type != "buy_and_hold",
            "point_in_time_required": True,
            "backtest_engine": BACKTEST_ENGINE_VERSION,
            "corporate_action_accounting": ["split", "cash_dividend"],
            "unsupported_corporate_actions": ["symbol_change"],
        },
        code_sha256=code_sha256,
        created_at=datetime.now(UTC),
    )


class ResearchBacktester:
    def __init__(
        self,
        store: ResearchStore,
        ledger: EventLedger | None = None,
        *,
        calendar_name: str = "XNYS",
        risk_policy: RiskPolicy | None = None,
        restrictions: RestrictionRegistry | None = None,
    ) -> None:
        self.store = store
        self.ledger = ledger
        self.features = PointInTimeFeatureBuilder(store)
        self.reference_data = ReferenceDataStore(store.engine)
        self.session_clock = MarketSessionClock(calendar_name)
        self.data_quality = MarketDataQualityService(
            store.engine,
            ledger,
            calendar_name=calendar_name,
        )
        config_root = Path(__file__).resolve().parents[2] / "configs"
        self.risk_policy = risk_policy or RiskPolicy.from_yaml(
            config_root / "risk_policy.yaml"
        )
        self.restrictions = restrictions or RestrictionRegistry.from_yaml(
            config_root / "restricted_securities.yaml"
        )

    def run(
        self,
        *,
        spec: StrategySpec,
        symbol: str,
        as_of_start: datetime,
        as_of_end: datetime,
        code_git_sha: str,
        initial_equity: Decimal = Decimal("100000"),
        cost_model: BacktestCostModel | None = None,
        history_start: datetime | None = None,
    ) -> BacktestResult:
        if as_of_start.tzinfo is None or as_of_end.tzinfo is None:
            raise ValueError("Backtest boundaries must be timezone-aware")
        if as_of_start >= as_of_end:
            raise ValueError("Backtest start must be before end")
        if history_start is not None:
            if history_start.tzinfo is None:
                raise ValueError("Backtest history start must be timezone-aware")
            if history_start >= as_of_end:
                raise ValueError("Backtest history start must be before end")
        if initial_equity <= 0:
            raise ValueError("Initial equity must be positive")
        costs = cost_model or BacktestCostModel()
        stored_spec = self.store.record_strategy_spec(spec)
        bars = self.store.load_bars(
            symbol=symbol,
            timeframe=spec.timeframe,
            as_of_end=as_of_end,
        )
        if history_start is not None:
            normalized_history_start = history_start.astimezone(UTC)
            bars = tuple(
                bar for bar in bars if bar.event_time >= normalized_history_start
            )
        quality_report = self.data_quality.require_bars(
            bars,
            symbol=symbol,
            timeframe=spec.timeframe,
            code_git_sha=code_git_sha,
        )
        decision_indices = [
            index
            for index in range(_MINIMUM_HISTORY - 1, len(bars) - 1)
            if bars[index].available_from >= as_of_start
            and bars[index].available_from < as_of_end
            and bars[index + 1].event_time >= bars[index].available_from
            and bars[index + 1].event_time <= as_of_end
        ]
        if not decision_indices:
            raise ValueError(
                "Backtest requires at least 22 point-in-time-safe bars in the requested range"
            )
        actions = self.reference_data.corporate_actions_effective_between(
            symbol=symbol,
            start=as_of_start,
            end=as_of_end,
        )
        if any(
            action.action_type == CorporateActionType.SYMBOL_CHANGE
            for action in actions
        ):
            raise ValueError(
                "Symbol changes require a cross-symbol market-data replay"
            )
        started_at = datetime.now(UTC)
        experiment_id = uuid7()
        snapshots = tuple(
            self.features.build(
                symbol=symbol,
                timeframe=spec.timeframe,
                as_of=bars[index].available_from,
                bars=bars[: index + 1],
            )
            for index in decision_indices
        )
        decision_times = tuple(snapshot.as_of for snapshot in snapshots)
        if any(
            action.action_type == CorporateActionType.SPLIT
            and any(
                action.effective_at <= decision_time < action.available_from
                for decision_time in decision_times
            )
            for action in actions
        ):
            raise ValueError(
                "Split data arrived after its effective time; the affected feature window "
                "cannot be replayed without look-ahead"
            )
        portfolio_actions = tuple(
            action for action in actions if action.effective_at >= snapshots[0].as_of
        )
        if spec.strategy_type == "buy_and_hold":
            trades, equity_curve, portfolio_events = self._run_buy_and_hold(
                experiment_id=experiment_id,
                symbol=symbol.upper(),
                bars=bars,
                decision_index=decision_indices[0],
                snapshot=snapshots[0],
                initial_equity=initial_equity,
                costs=costs,
                actions=portfolio_actions,
            )
        else:
            trades, equity_curve, portfolio_events = self._run_daily_strategy(
                experiment_id=experiment_id,
                symbol=symbol.upper(),
                spec=stored_spec,
                bars=bars,
                decision_indices=decision_indices,
                snapshots=snapshots,
                initial_equity=initial_equity,
                costs=costs,
                actions=portfolio_actions,
                market_data_healthy=quality_report.status.value == "PASSED",
            )
        metrics = self._metrics(
            initial_equity=initial_equity,
            equity_curve=equity_curve,
            trades=trades,
            timeframe=spec.timeframe,
        )
        dataset_hash = _canonical_hash(
            {
                "symbol": symbol.upper(),
                "timeframe": spec.timeframe,
                "bars": [
                    {
                        "bar_id": bar.bar_id,
                        "event_time": bar.event_time.isoformat(),
                        "available_from": bar.available_from.isoformat(),
                        "open": str(bar.open),
                        "high": str(bar.high),
                        "low": str(bar.low),
                        "close": str(bar.close),
                        "volume": bar.volume,
                    }
                    for bar in bars
                    if bar.event_time <= as_of_end
                ],
                "feature_hashes": [snapshot.data_hash for snapshot in snapshots],
                "backtest_engine_version": BACKTEST_ENGINE_VERSION,
                "corporate_actions": [
                    action.model_dump(mode="json") for action in actions
                ],
            }
        )
        finished_at = datetime.now(UTC)
        experiment = ExperimentRun(
            experiment_run_id=experiment_id,
            strategy_spec_id=stored_spec.strategy_spec_id,
            symbol=symbol.upper(),
            timeframe=spec.timeframe,
            as_of_start=as_of_start,
            as_of_end=as_of_end,
            dataset_hash=dataset_hash,
            code_git_sha=code_git_sha,
            status=ExperimentStatus.COMPLETED,
            cost_model=costs,
            metrics=metrics,
            feature_snapshot_ids=tuple(
                snapshot.feature_snapshot_id for snapshot in snapshots
            ),
            started_at=started_at,
            finished_at=finished_at,
        )
        result = BacktestResult(
            experiment=experiment,
            strategy_spec=stored_spec,
            trades=trades,
            equity_curve=equity_curve,
            portfolio_events=portfolio_events,
        )
        self.store.record_backtest(result)
        self._record_completion(result)
        return result

    @staticmethod
    def _should_trade(
        spec: StrategySpec,
        snapshot: PointInTimeFeatureSnapshot,
    ) -> bool:
        return strategy_signal_action(
            strategy_type=spec.strategy_type,
            parameters=spec.parameters,
            values=dict(snapshot.values),
        ) == SignalAction.LONG

    def _run_daily_strategy(
        self,
        *,
        experiment_id: str,
        symbol: str,
        spec: StrategySpec,
        bars: tuple[StockBar, ...],
        decision_indices: list[int],
        snapshots: tuple[PointInTimeFeatureSnapshot, ...],
        initial_equity: Decimal,
        costs: BacktestCostModel,
        actions: tuple[CorporateAction, ...],
        market_data_healthy: bool,
    ) -> tuple[
        tuple[BacktestTrade, ...],
        tuple[Decimal, ...],
        tuple[BacktestPortfolioEvent, ...],
    ]:
        portfolio = EventDrivenPortfolio(
            experiment_run_id=experiment_id,
            symbol=symbol,
            initial_cash=initial_equity,
            cost_model=costs,
        )
        curve = [initial_equity]
        trades: list[BacktestTrade] = []
        action_index = 0
        day_start_cash = initial_equity
        current_day = None
        _, profile, _ = strategy_execution_profile(
            data_requirements=spec.data_requirements,
            account_policy=self.risk_policy,
            strategy_type=spec.strategy_type,
        )
        holding_sessions = int(profile.get("maximum_holding_sessions", 1))
        final_execution_index = decision_indices[-1] + 1
        last_exit_index = -1
        for index, snapshot in zip(decision_indices, snapshots, strict=True):
            if index < last_exit_index:
                continue
            execution_bar = bars[index + 1]
            should_trade = self._should_trade(spec, snapshot)
            portfolio.record_signal(
                snapshot=snapshot,
                action=SignalAction.LONG if should_trade else SignalAction.FLAT,
            )
            entry_time = self._bar_open_time(execution_bar)
            action_index = self._apply_actions_until(
                portfolio=portfolio,
                actions=actions,
                start_index=action_index,
                cutoff=entry_time,
            )
            entered = False
            if should_trade and index + holding_sessions <= final_execution_index:
                effective_policy = strategy_signal_risk_policy(
                    data_requirements=spec.data_requirements,
                    strategy_type=spec.strategy_type,
                    feature_values=dict(snapshot.values),
                    account_policy=self.risk_policy,
                )
                day = entry_time.astimezone(UTC).date()
                if day != current_day:
                    current_day = day
                    day_start_cash = portfolio.cash
                raw_invalidation, raw_target = baseline_long_geometry(
                    bars[index].close,
                    effective_policy,
                )
                entry_limit, target, invalidation = normalize_deployable_long_prices(
                    entry_limit_price=bars[index].close,
                    take_profit_price=raw_target,
                    stop_loss_price=raw_invalidation,
                )
                limit_fill = deployable_long_limit_fill(
                    open_price=execution_bar.open,
                    low_price=execution_bar.low,
                    limit_price=entry_limit,
                )
                # The bracket and maximum quantity are committed from the completed
                # decision bar. Replay rechecks final risk at the price that the same
                # DAY limit contract could actually have filled.
                candidate = SignalCandidate(
                    candidate_id=uuid7(),
                    symbol=symbol,
                    direction=Direction.LONG,
                    setup_type=f"baseline_backtest:{spec.strategy_type}",
                    strategy_version=spec.version,
                    as_of=snapshot.as_of,
                    feature_snapshot_id=snapshot.feature_snapshot_id,
                    catalyst_id="NOT_APPLICABLE_BASELINE",
                    planned_entry=(
                        limit_fill[0]
                        if limit_fill is not None
                        else entry_limit
                    ),
                    invalidation=invalidation,
                    targets=(target,),
                    expires_at=self._bar_close_time(
                        bars[index + holding_sessions]
                    ),
                )
                decision = evaluate_candidate(
                    candidate=candidate,
                    features=FeatureSnapshot(
                        feature_snapshot_id=snapshot.feature_snapshot_id,
                        as_of=snapshot.as_of,
                        relative_volume=Decimal(
                            str(snapshot.values.get("volume_ratio_20") or "0")
                        ),
                        vwap_confirmed=False,
                        opening_range_confirmed=False,
                        sector_compatible=False,
                        quote_age_seconds=0,
                    ),
                    account=AccountState(
                        equity=portfolio.cash,
                        daily_pnl=portfolio.cash - day_start_cash,
                        concurrent_planned_risk=_ZERO,
                    ),
                    mode=TradingMode.BACKTEST,
                    policy=effective_policy,
                    restrictions=self.restrictions,
                    context=RiskEvaluationContext(
                        catalyst_required=False,
                        catalyst_verified=False,
                        restriction_status_known=True,
                        liquidity_confirmed=bars[index].volume > 0,
                        market_data_healthy=market_data_healthy,
                        macro_calendar_status_known=False,
                        duplicate_order_detected=False,
                        evaluation_profile="baseline_shadow",
                    ),
                    evaluated_at=entry_time,
                    new_exposure_paused=False,
                )
                if decision.verdict != Verdict.APPROVE:
                    portfolio.mark(
                        event_time=self._bar_close_time(execution_bar),
                        raw_price=execution_bar.close,
                    )
                    curve.append(portfolio.cash)
                    continue
                entered = limit_fill is not None and portfolio.enter_long(
                    signal_as_of=snapshot.as_of,
                    entry_time=entry_time,
                    raw_price=limit_fill[0],
                    available_volume=execution_bar.volume,
                    feature_snapshot_id=snapshot.feature_snapshot_id,
                    quantity_limit=decision.max_quantity,
                )
            if entered:
                assert limit_fill is not None
                planned_exit_index = index + holding_sessions
                for held_index in range(index + 1, planned_exit_index + 1):
                    held_bar = bars[held_index]
                    exit_time = self._bar_close_time(held_bar)
                    previous_action_index = action_index
                    action_index = self._apply_actions_until(
                        portfolio=portfolio,
                        actions=actions,
                        start_index=action_index,
                        cutoff=exit_time,
                    )
                    for action in actions[previous_action_index:action_index]:
                        if action.action_type == CorporateActionType.SPLIT:
                            assert action.split_ratio is not None
                            invalidation /= action.split_ratio
                            target /= action.split_ratio
                    if held_index == index + 1:
                        exit_price, exit_reason = deployable_long_exit(
                            entry_kind=limit_fill[1],
                            open_price=held_bar.open,
                            high_price=held_bar.high,
                            low_price=held_bar.low,
                            close_price=held_bar.close,
                            invalidation=invalidation,
                            target=target,
                        )
                    else:
                        exit_price, exit_reason = baseline_long_exit(
                            open_price=held_bar.open,
                            high_price=held_bar.high,
                            low_price=held_bar.low,
                            close_price=held_bar.close,
                            invalidation=invalidation,
                            target=target,
                        )
                    forced_exit = held_index == planned_exit_index
                    triggered = exit_reason not in {"session_close", "market_on_close"}
                    if triggered or forced_exit:
                        trade = portfolio.exit_long(
                            exit_time=exit_time,
                            raw_price=(
                                held_bar.close
                                if forced_exit and not triggered
                                else exit_price
                            ),
                            available_volume=held_bar.volume,
                            exit_reason=(
                                "maximum_holding_period"
                                if forced_exit and not triggered
                                else exit_reason
                            ),
                        )
                        if trade is not None:
                            trades.append(trade)
                        curve.append(portfolio.cash)
                        last_exit_index = held_index
                        break
                    curve.append(
                        portfolio.mark(event_time=exit_time, raw_price=held_bar.close)
                    )
            else:
                exit_time = self._bar_close_time(execution_bar)
                action_index = self._apply_actions_until(
                    portfolio=portfolio,
                    actions=actions,
                    start_index=action_index,
                    cutoff=exit_time,
                )
                portfolio.mark(event_time=exit_time, raw_price=execution_bar.close)
                curve.append(portfolio.cash)
        return tuple(trades), tuple(curve), portfolio.events

    def _run_buy_and_hold(
        self,
        *,
        experiment_id: str,
        symbol: str,
        bars: tuple[StockBar, ...],
        decision_index: int,
        snapshot: PointInTimeFeatureSnapshot,
        initial_equity: Decimal,
        costs: BacktestCostModel,
        actions: tuple[CorporateAction, ...],
    ) -> tuple[
        tuple[BacktestTrade, ...],
        tuple[Decimal, ...],
        tuple[BacktestPortfolioEvent, ...],
    ]:
        """Run the non-deployable benchmark with its literal buy-and-hold contract."""
        entry_bar = bars[decision_index + 1]
        exit_bar = bars[-1]
        portfolio = EventDrivenPortfolio(
            experiment_run_id=experiment_id,
            symbol=symbol,
            initial_cash=initial_equity,
            cost_model=costs,
        )
        portfolio.record_signal(snapshot=snapshot, action=SignalAction.LONG)
        action_index = self._apply_actions_until(
            portfolio=portfolio,
            actions=actions,
            start_index=0,
            cutoff=self._bar_open_time(entry_bar),
        )
        entered = portfolio.enter_long(
            signal_as_of=snapshot.as_of,
            entry_time=self._bar_open_time(entry_bar),
            raw_price=entry_bar.open,
            # Sizing at the next open may only use the completed decision bar.
            available_volume=bars[decision_index].volume,
            feature_snapshot_id=snapshot.feature_snapshot_id,
        )
        if not entered:
            return (), (initial_equity,), portfolio.events
        curve = [initial_equity]
        for bar in bars[decision_index + 1 :]:
            close_time = self._bar_close_time(bar)
            action_index = self._apply_actions_until(
                portfolio=portfolio,
                actions=actions,
                start_index=action_index,
                cutoff=close_time,
            )
            curve.append(portfolio.mark(event_time=close_time, raw_price=bar.close))
        trade = portfolio.exit_long(
            exit_time=self._bar_close_time(exit_bar),
            raw_price=exit_bar.close,
            available_volume=exit_bar.volume,
            exit_reason="backtest_end",
        )
        if trade is None:
            raise RuntimeError("Event-driven portfolio lost its open position")
        curve[-1] = portfolio.cash
        return (trade,), tuple(curve), portfolio.events

    def _bar_open_time(self, bar: StockBar) -> datetime:
        if bar.timeframe == "1Day":
            return self.session_clock.daily_bar_session_open(bar.event_time)
        return bar.event_time

    @staticmethod
    def _bar_close_time(bar: StockBar) -> datetime:
        return bar.available_from

    @staticmethod
    def _apply_actions_until(
        *,
        portfolio: EventDrivenPortfolio,
        actions: tuple[CorporateAction, ...],
        start_index: int,
        cutoff: datetime,
    ) -> int:
        index = start_index
        while index < len(actions) and actions[index].effective_at <= cutoff:
            portfolio.apply_corporate_action(actions[index])
            index += 1
        return index

    @staticmethod
    def _metrics(
        *,
        initial_equity: Decimal,
        equity_curve: tuple[Decimal, ...],
        trades: tuple[BacktestTrade, ...],
        timeframe: str,
    ) -> BacktestMetrics:
        final_equity = equity_curve[-1]
        returns = [
            float(equity_curve[index] / equity_curve[index - 1] - _ONE)
            for index in range(1, len(equity_curve))
            if equity_curve[index - 1] > 0
        ]
        periods_per_year = 98280 if timeframe == "1Min" else 252
        periods = max(len(returns), 1)
        total_return = final_equity / initial_equity - _ONE
        annualized = (
            (float(final_equity / initial_equity) ** (periods_per_year / periods)) - 1
            if final_equity > 0
            else -1.0
        )
        volatility = stdev(returns) if len(returns) > 1 else 0.0
        sharpe = (
            mean(returns) / volatility * math.sqrt(periods_per_year)
            if volatility > 1e-12
            else 0.0
        )
        downside_deviation = (
            math.sqrt(sum(min(value, 0.0) ** 2 for value in returns) / len(returns))
            if returns
            else 0.0
        )
        sortino = (
            mean(returns) / downside_deviation * math.sqrt(periods_per_year)
            if downside_deviation > 1e-12
            else 0.0
        )
        peak = equity_curve[0]
        max_drawdown = _ZERO
        for equity in equity_curve:
            peak = max(peak, equity)
            drawdown = equity / peak - _ONE if peak > 0 else _ZERO
            max_drawdown = min(max_drawdown, drawdown)
        wins = sum(1 for trade in trades if trade.net_pnl > 0)
        total_notional = sum(
            (
                Decimal(trade.quantity) * trade.entry_price
                + (trade.exit_quantity or Decimal(trade.quantity)) * trade.exit_price
                for trade in trades
            ),
            _ZERO,
        )
        return BacktestMetrics(
            initial_equity=initial_equity,
            final_equity=final_equity,
            net_profit=final_equity - initial_equity,
            total_return=total_return,
            annualized_return=_decimal(annualized),
            sharpe_ratio=_decimal(sharpe),
            sortino_ratio=_decimal(sortino),
            max_drawdown=max_drawdown,
            trade_count=len(trades),
            win_rate=Decimal(wins) / Decimal(len(trades)) if trades else _ZERO,
            turnover=total_notional / initial_equity,
            total_cost=sum((trade.transaction_cost for trade in trades), _ZERO),
        )

    def _record_completion(self, result: BacktestResult) -> None:
        if self.ledger is None:
            return
        experiment = result.experiment
        event = EventEnvelope(
            event_id=uuid7(),
            event_type="research.experiment.completed.v1",
            event_time=experiment.finished_at,
            emitted_at=datetime.now(UTC),
            producer="research-backtester",
            correlation_id=experiment.experiment_run_id,
            payload={
                "experiment_run_id": experiment.experiment_run_id,
                "strategy_spec_id": experiment.strategy_spec_id,
                "symbol": experiment.symbol,
                "timeframe": experiment.timeframe,
                "dataset_hash": experiment.dataset_hash,
                "metrics": experiment.metrics.model_dump(mode="json"),
                "trade_count": len(result.trades),
                "portfolio_event_count": len(result.portfolio_events),
            },
        )
        self.ledger.append(event)


def research_code_sha256() -> str:
    digest = hashlib.sha256()
    for path in sorted(
        (
            Path(__file__),
            Path(__file__).with_name("backtest_engine.py"),
        )
    ):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()
