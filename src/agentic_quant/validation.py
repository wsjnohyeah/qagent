from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from agentic_quant.domain import (
    BacktestCostModel,
    BacktestResult,
    EventEnvelope,
    MarketRegime,
    StockBar,
    WalkForwardFold,
    WalkForwardValidationReport,
)
from agentic_quant.ids import uuid7
from agentic_quant.ledger import EventLedger
from agentic_quant.research import (
    SUPPORTED_STRATEGIES,
    ResearchBacktester,
    default_strategy_spec,
    research_code_sha256,
)
from agentic_quant.research_store import ResearchStore, _canonical_hash


_ONE = Decimal("1")
_ZERO = Decimal("0")
SELECTION_METRICS = ("sharpe_ratio", "sortino_ratio", "total_return")


def _mean(values: list[Decimal]) -> Decimal:
    return sum(values, _ZERO) / Decimal(len(values)) if values else _ZERO


def _selection_value(result: BacktestResult, metric: str) -> Decimal:
    metrics = result.experiment.metrics
    if metric == "sharpe_ratio":
        return metrics.sharpe_ratio
    if metric == "sortino_ratio":
        return metrics.sortino_ratio
    if metric == "total_return":
        return metrics.total_return
    raise ValueError(f"Unsupported selection metric: {metric}")


class WalkForwardValidator:
    def __init__(
        self,
        store: ResearchStore,
        ledger: EventLedger | None = None,
        *,
        calendar_name: str = "XNYS",
    ) -> None:
        self.store = store
        self.ledger = ledger
        self.backtester = ResearchBacktester(
            store,
            ledger,
            calendar_name=calendar_name,
        )

    def run(
        self,
        *,
        symbol: str,
        timeframe: str,
        as_of_start: datetime,
        as_of_end: datetime,
        code_git_sha: str,
        strategy_types: tuple[str, ...] = SUPPORTED_STRATEGIES,
        selection_metric: str = "sharpe_ratio",
        train_bars: int = 40,
        test_bars: int = 10,
        step_bars: int = 10,
        embargo_bars: int = 1,
        initial_equity: Decimal = Decimal("100000"),
        cost_model: BacktestCostModel | None = None,
    ) -> WalkForwardValidationReport:
        self._validate_request(
            strategy_types=strategy_types,
            selection_metric=selection_metric,
            train_bars=train_bars,
            test_bars=test_bars,
            step_bars=step_bars,
            embargo_bars=embargo_bars,
        )
        bars = self.store.load_bars(
            symbol=symbol,
            timeframe=timeframe,
            as_of_end=as_of_end,
        )
        decision_indices = tuple(
            index
            for index in range(20, len(bars) - 1)
            if bars[index].available_from >= as_of_start
            and bars[index].available_from < as_of_end
            and bars[index + 1].event_time >= bars[index].available_from
            and bars[index + 1].available_from <= as_of_end
        )
        required = train_bars + embargo_bars + test_bars
        if len(decision_indices) < required:
            raise ValueError(
                "Walk-forward validation has insufficient decision bars: "
                f"requires {required}, found {len(decision_indices)}"
            )
        costs = cost_model or BacktestCostModel()
        strategy_code_hash = research_code_sha256()
        folds: list[WalkForwardFold] = []
        offset = 0
        while offset + required <= len(decision_indices):
            train_indices = decision_indices[offset : offset + train_bars]
            test_offset = offset + train_bars + embargo_bars
            test_indices = decision_indices[test_offset : test_offset + test_bars]
            train_results = self._run_candidates(
                symbol=symbol,
                timeframe=timeframe,
                indices=train_indices,
                bars=bars,
                strategy_types=strategy_types,
                strategy_code_hash=strategy_code_hash,
                code_git_sha=code_git_sha,
                initial_equity=initial_equity,
                cost_model=costs,
            )
            selected_strategy = max(
                sorted(strategy_types),
                key=lambda name: _selection_value(
                    train_results[name],
                    selection_metric,
                ),
            )
            test_results = self._run_candidates(
                symbol=symbol,
                timeframe=timeframe,
                indices=test_indices,
                bars=bars,
                strategy_types=strategy_types,
                strategy_code_hash=strategy_code_hash,
                code_git_sha=code_git_sha,
                initial_equity=initial_equity,
                cost_model=costs,
            )
            selected_test_value = _selection_value(
                test_results[selected_strategy],
                selection_metric,
            )
            selected_test_rank = 1 + sum(
                _selection_value(test_results[name], selection_metric)
                > selected_test_value
                for name in strategy_types
            )
            train_start, train_end = self._window_bounds(bars, train_indices)
            test_start, test_end = self._window_bounds(bars, test_indices)
            folds.append(
                WalkForwardFold(
                    validation_fold_id=uuid7(),
                    fold_number=len(folds) + 1,
                    train_start=train_start,
                    train_end=train_end,
                    test_start=test_start,
                    test_end=test_end,
                    selected_strategy=selected_strategy,
                    selection_metric=selection_metric,
                    train_experiment_ids={
                        name: result.experiment.experiment_run_id
                        for name, result in train_results.items()
                    },
                    test_experiment_ids={
                        name: result.experiment.experiment_run_id
                        for name, result in test_results.items()
                    },
                    selected_train_metrics=train_results[
                        selected_strategy
                    ].experiment.metrics,
                    selected_test_metrics=test_results[
                        selected_strategy
                    ].experiment.metrics,
                    selected_test_rank=selected_test_rank,
                    regime=self._classify_regime(bars, test_indices),
                )
            )
            offset += step_bars
        report = self._build_report(
            symbol=symbol,
            timeframe=timeframe,
            strategy_types=strategy_types,
            selection_metric=selection_metric,
            train_bars=train_bars,
            test_bars=test_bars,
            step_bars=step_bars,
            embargo_bars=embargo_bars,
            folds=tuple(folds),
            code_git_sha=code_git_sha,
        )
        self.store.record_validation_report(report)
        self._record_completion(report)
        return report

    def _run_candidates(
        self,
        *,
        symbol: str,
        timeframe: str,
        indices: tuple[int, ...],
        bars: tuple[StockBar, ...],
        strategy_types: tuple[str, ...],
        strategy_code_hash: str,
        code_git_sha: str,
        initial_equity: Decimal,
        cost_model: BacktestCostModel,
    ) -> dict[str, BacktestResult]:
        start, end = self._window_bounds(bars, indices)
        return {
            strategy_type: self.backtester.run(
                spec=default_strategy_spec(
                    strategy_type,
                    timeframe=timeframe,
                    code_sha256=strategy_code_hash,
                ),
                symbol=symbol,
                as_of_start=start,
                as_of_end=end,
                code_git_sha=code_git_sha,
                initial_equity=initial_equity,
                cost_model=cost_model,
            )
            for strategy_type in strategy_types
        }

    @staticmethod
    def _window_bounds(
        bars: tuple[StockBar, ...],
        indices: tuple[int, ...],
    ) -> tuple[datetime, datetime]:
        return bars[indices[0]].available_from, bars[indices[-1] + 1].available_from

    @staticmethod
    def _classify_regime(
        bars: tuple[StockBar, ...],
        test_indices: tuple[int, ...],
    ) -> MarketRegime:
        start_price = bars[test_indices[0]].close
        end_price = bars[test_indices[-1] + 1].close
        return_value = end_price / start_price - _ONE
        if return_value > Decimal("0.02"):
            return MarketRegime.UP
        if return_value < Decimal("-0.02"):
            return MarketRegime.DOWN
        return MarketRegime.SIDEWAYS

    @staticmethod
    def _validate_request(
        *,
        strategy_types: tuple[str, ...],
        selection_metric: str,
        train_bars: int,
        test_bars: int,
        step_bars: int,
        embargo_bars: int,
    ) -> None:
        if not strategy_types or len(set(strategy_types)) != len(strategy_types):
            raise ValueError("Strategy candidates must be nonempty and unique")
        unknown = set(strategy_types) - set(SUPPORTED_STRATEGIES)
        if unknown:
            raise ValueError(f"Unsupported strategies: {sorted(unknown)}")
        if selection_metric not in SELECTION_METRICS:
            raise ValueError(f"Unsupported selection metric: {selection_metric}")
        if train_bars < 22:
            raise ValueError("train_bars must be at least 22")
        if test_bars < 1 or step_bars < test_bars:
            raise ValueError("step_bars must be at least test_bars and both must be positive")
        if embargo_bars < 1:
            raise ValueError("embargo_bars must be at least 1")

    def _build_report(
        self,
        *,
        symbol: str,
        timeframe: str,
        strategy_types: tuple[str, ...],
        selection_metric: str,
        train_bars: int,
        test_bars: int,
        step_bars: int,
        embargo_bars: int,
        folds: tuple[WalkForwardFold, ...],
        code_git_sha: str,
    ) -> WalkForwardValidationReport:
        test_returns = [fold.selected_test_metrics.total_return for fold in folds]
        test_sharpes = [fold.selected_test_metrics.sharpe_ratio for fold in folds]
        degradation = [
            fold.selected_train_metrics.sharpe_ratio
            - fold.selected_test_metrics.sharpe_ratio
            for fold in folds
        ]
        compounded = _ONE
        for value in test_returns:
            compounded *= _ONE + value
        below_median = sum(
            1
            for fold in folds
            if fold.selected_test_rank > (len(strategy_types) + 1) // 2
        )
        switches = sum(
            1
            for previous, current in zip(folds, folds[1:])
            if previous.selected_strategy != current.selected_strategy
        )
        aggregate: dict[str, Decimal | int | str] = {
            "fold_count": len(folds),
            "compounded_selected_oos_return": compounded - _ONE,
            "mean_selected_oos_return": _mean(test_returns),
            "mean_selected_oos_sharpe": _mean(test_sharpes),
            "worst_selected_oos_drawdown": min(
                fold.selected_test_metrics.max_drawdown for fold in folds
            ),
            "positive_oos_fold_rate": Decimal(
                sum(value > 0 for value in test_returns)
            )
            / Decimal(len(folds)),
            "mean_train_to_test_sharpe_degradation": _mean(degradation),
            "selected_oos_below_median_rate": Decimal(below_median)
            / Decimal(len(folds)),
            "strategy_switch_count": switches,
        }
        regime_metrics = self._regime_metrics(folds)
        report_material = {
            "symbol": symbol.upper(),
            "timeframe": timeframe,
            "strategy_types": strategy_types,
            "selection_metric": selection_metric,
            "train_bars": train_bars,
            "test_bars": test_bars,
            "step_bars": step_bars,
            "embargo_bars": embargo_bars,
            "code_git_sha": code_git_sha,
            "folds": [
                {
                    "fold_number": fold.fold_number,
                    "train_start": fold.train_start.isoformat(),
                    "train_end": fold.train_end.isoformat(),
                    "test_start": fold.test_start.isoformat(),
                    "test_end": fold.test_end.isoformat(),
                    "selected_strategy": fold.selected_strategy,
                    "train_experiment_ids": fold.train_experiment_ids,
                    "test_experiment_ids": fold.test_experiment_ids,
                    "selected_train_metrics": fold.selected_train_metrics.model_dump(
                        mode="json"
                    ),
                    "selected_test_metrics": fold.selected_test_metrics.model_dump(
                        mode="json"
                    ),
                    "selected_test_rank": fold.selected_test_rank,
                    "regime": fold.regime.value,
                }
                for fold in folds
            ],
            "aggregate_metrics": {
                key: str(value) for key, value in aggregate.items()
            },
            "regime_metrics": {
                regime: {key: str(value) for key, value in metrics.items()}
                for regime, metrics in regime_metrics.items()
            },
        }
        return WalkForwardValidationReport(
            validation_report_id=uuid7(),
            symbol=symbol.upper(),
            timeframe=timeframe,
            strategy_types=strategy_types,
            selection_metric=selection_metric,
            train_bars=train_bars,
            test_bars=test_bars,
            step_bars=step_bars,
            embargo_bars=embargo_bars,
            folds=folds,
            aggregate_metrics=aggregate,
            regime_metrics=regime_metrics,
            report_hash=_canonical_hash(report_material),
            code_git_sha=code_git_sha,
            created_at=datetime.now(UTC),
        )

    @staticmethod
    def _regime_metrics(
        folds: tuple[WalkForwardFold, ...],
    ) -> dict[str, dict[str, Decimal | int | str]]:
        result: dict[str, dict[str, Decimal | int | str]] = {}
        for regime in MarketRegime:
            matching = [fold for fold in folds if fold.regime == regime]
            if not matching:
                continue
            result[regime.value] = {
                "fold_count": len(matching),
                "mean_total_return": _mean(
                    [fold.selected_test_metrics.total_return for fold in matching]
                ),
                "mean_sharpe_ratio": _mean(
                    [fold.selected_test_metrics.sharpe_ratio for fold in matching]
                ),
                "worst_max_drawdown": min(
                    fold.selected_test_metrics.max_drawdown for fold in matching
                ),
            }
        return result

    def _record_completion(self, report: WalkForwardValidationReport) -> None:
        if self.ledger is None:
            return
        self.ledger.append(
            EventEnvelope(
                event_id=uuid7(),
                event_type="research.validation.completed.v1",
                event_time=report.created_at,
                emitted_at=datetime.now(UTC),
                producer="walk-forward-validator",
                correlation_id=report.validation_report_id,
                payload={
                    "validation_report_id": report.validation_report_id,
                    "symbol": report.symbol,
                    "timeframe": report.timeframe,
                    "report_hash": report.report_hash,
                    "fold_count": len(report.folds),
                    "aggregate_metrics": {
                        key: str(value)
                        for key, value in report.aggregate_metrics.items()
                    },
                },
            )
        )
