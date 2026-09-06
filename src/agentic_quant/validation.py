from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from itertools import combinations
from math import comb, log, sqrt
from pathlib import Path
import random
from statistics import NormalDist, median
from typing import Any, Self

from pydantic import Field, model_validator
import yaml

from agentic_quant.domain import (
    BacktestCostModel,
    BacktestResult,
    EventEnvelope,
    FrozenModel,
    MarketRegime,
    StrategySpec,
    StockBar,
    WalkForwardFold,
    WalkForwardValidationReport,
)
from agentic_quant.ids import uuid7
from agentic_quant.ledger import EventLedger
from agentic_quant.research import (
    BACKTEST_ENGINE_VERSION,
    FEATURE_SET_VERSION,
    SUPPORTED_STRATEGIES,
    ResearchBacktester,
    default_strategy_spec,
    research_code_sha256,
)
from agentic_quant.research_store import ResearchStore, _canonical_hash
from agentic_quant.risk import (
    BASELINE_EXECUTION_PROFILE_VERSION,
    RestrictionRegistry,
    RiskPolicy,
)


_ONE = Decimal("1")
_ZERO = Decimal("0")
SELECTION_METRICS = ("sharpe_ratio", "sortino_ratio", "total_return")
_NORMAL = NormalDist()
_EULER_MASCHERONI = 0.5772156649015329


class PromotionGatePolicy(FrozenModel):
    version: str = Field(pattern=r"^research_gate@[0-9]+\.[0-9]+\.[0-9]+$")
    minimum_oos_folds: int = Field(ge=4)
    minimum_candidate_count: int = Field(ge=2)
    minimum_regime_count: int = Field(ge=1, le=3)
    maximum_probability_of_backtest_overfitting: Decimal = Field(ge=0, le=1)
    minimum_deflated_sharpe_probability: Decimal = Field(ge=0, le=1)
    minimum_positive_oos_fold_rate: Decimal = Field(ge=0, le=1)
    maximum_allowed_drawdown: Decimal = Field(le=0)

    @model_validator(mode="after")
    def thresholds_are_conservative(self) -> Self:
        if self.maximum_allowed_drawdown < Decimal("-1"):
            raise ValueError("maximum_allowed_drawdown cannot be below -1")
        return self


DEFAULT_PROMOTION_GATE_POLICY = PromotionGatePolicy(
    version="research_gate@0.2.0",
    minimum_oos_folds=12,
    minimum_candidate_count=3,
    minimum_regime_count=2,
    maximum_probability_of_backtest_overfitting=Decimal("0.20"),
    minimum_deflated_sharpe_probability=Decimal("0.95"),
    minimum_positive_oos_fold_rate=Decimal("0.55"),
    maximum_allowed_drawdown=Decimal("-0.20"),
)


def load_promotion_gate_policy(path: Path) -> PromotionGatePolicy:
    with path.open("r", encoding="utf-8") as handle:
        return PromotionGatePolicy.model_validate(yaml.safe_load(handle))


def _decimal(value: float) -> Decimal:
    return Decimal(str(round(value, 12)))


def _float_mean(values: list[float]) -> float:
    return sum(values) / len(values)


def _sample_test_combinations(
    *,
    group_count: int,
    test_group_count: int,
    seed: int,
    maximum: int = 512,
) -> tuple[tuple[int, ...], ...]:
    total = comb(group_count, test_group_count)
    if total <= maximum:
        return tuple(combinations(range(group_count), test_group_count))
    generator = random.Random(seed)
    selected: set[tuple[int, ...]] = set()
    while len(selected) < maximum:
        selected.add(
            tuple(sorted(generator.sample(range(group_count), test_group_count)))
        )
    return tuple(sorted(selected))


def combinatorial_purged_diagnostics(
    candidate_scores: dict[str, tuple[Decimal, ...]],
) -> dict[str, Any]:
    if len(candidate_scores) < 2:
        only = next(iter(candidate_scores.values()), ())
        return {
            "method": "not_applicable_to_single_static_strategy",
            "candidate_count": len(candidate_scores),
            "group_count": len(only),
            "test_group_count": 0,
            "combination_count_total": 0,
            "combination_count_evaluated": 0,
            "deterministically_sampled": False,
            "probability_of_backtest_overfitting": Decimal("0"),
            "median_oos_logit": Decimal("0"),
            "selected_strategy_frequency": {},
            "not_applicable": True,
        }
    lengths = {len(values) for values in candidate_scores.values()}
    if len(lengths) != 1:
        raise ValueError("Every PBO candidate must have the same number of OOS groups")
    group_count = lengths.pop()
    if group_count < 2:
        raise ValueError("PBO requires at least two non-overlapping OOS groups")
    test_group_count = max(1, group_count // 2)
    seed_material = {
        name: [str(value) for value in values]
        for name, values in sorted(candidate_scores.items())
    }
    seed = int(_canonical_hash(seed_material)[:16], 16)
    test_splits = _sample_test_combinations(
        group_count=group_count,
        test_group_count=test_group_count,
        seed=seed,
    )
    logits: list[float] = []
    selected_frequency = {name: 0 for name in sorted(candidate_scores)}
    all_indices = set(range(group_count))
    for test_indices in test_splits:
        train_indices = sorted(all_indices - set(test_indices))
        train_scores = {
            name: _float_mean([float(values[index]) for index in train_indices])
            for name, values in candidate_scores.items()
        }
        selected = max(sorted(train_scores), key=train_scores.__getitem__)
        selected_frequency[selected] += 1
        test_scores = {
            name: _float_mean([float(values[index]) for index in test_indices])
            for name, values in candidate_scores.items()
        }
        selected_score = test_scores[selected]
        lower = sum(value < selected_score for value in test_scores.values())
        equal = sum(value == selected_score for value in test_scores.values())
        relative_rank = (lower + 0.5 * equal) / len(test_scores)
        relative_rank = min(max(relative_rank, 1e-12), 1 - 1e-12)
        logits.append(log(relative_rank / (1 - relative_rank)))
    overfit_count = sum(value <= 0 for value in logits)
    total_combinations = comb(group_count, test_group_count)
    return {
        "method": "CSCV over pre-purged non-overlapping OOS folds",
        "candidate_count": len(candidate_scores),
        "group_count": group_count,
        "test_group_count": test_group_count,
        "combination_count_total": total_combinations,
        "combination_count_evaluated": len(test_splits),
        "deterministically_sampled": total_combinations > len(test_splits),
        "probability_of_backtest_overfitting": _decimal(
            overfit_count / len(logits)
        ),
        "median_oos_logit": _decimal(median(logits)),
        "selected_strategy_frequency": selected_frequency,
    }


def deflated_sharpe_diagnostics(
    returns: tuple[Decimal, ...],
    *,
    number_of_trials: int,
) -> dict[str, Any]:
    if number_of_trials < 1:
        raise ValueError("number_of_trials must be positive")
    observations = [float(value) for value in returns]
    sample_size = len(observations)
    base = {
        "method": "Bailey-Lopez-de-Prado DSR on non-overlapping OOS fold returns",
        "sample_size": sample_size,
        "number_of_trials": number_of_trials,
    }
    if sample_size < 3:
        return {
            **base,
            "observed_sharpe": Decimal("0"),
            "expected_maximum_sharpe": Decimal("0"),
            "skewness": Decimal("0"),
            "kurtosis": Decimal("0"),
            "deflated_sharpe_probability": Decimal("0"),
            "sufficient_observations": False,
        }
    mean_value = _float_mean(observations)
    centered = [value - mean_value for value in observations]
    second_moment = _float_mean([value**2 for value in centered])
    if second_moment <= 1e-18:
        return {
            **base,
            "observed_sharpe": Decimal("0"),
            "expected_maximum_sharpe": Decimal("0"),
            "skewness": Decimal("0"),
            "kurtosis": Decimal("0"),
            "deflated_sharpe_probability": Decimal("0"),
            "sufficient_observations": False,
            "degenerate_returns": True,
        }
    standard_deviation = sqrt(second_moment)
    observed_sharpe = mean_value / standard_deviation
    skewness = _float_mean([value**3 for value in centered]) / (
        standard_deviation**3
    )
    kurtosis = _float_mean([value**4 for value in centered]) / (
        standard_deviation**4
    )
    if number_of_trials == 1:
        expected_maximum = 0.0
    else:
        trial_variance = 1.0 / max(sample_size - 1, 1)
        expected_maximum = sqrt(trial_variance) * (
            (1 - _EULER_MASCHERONI)
            * _NORMAL.inv_cdf(1 - 1 / number_of_trials)
            + _EULER_MASCHERONI
            * _NORMAL.inv_cdf(1 - 1 / (number_of_trials * 2.718281828459045))
        )
    sharpe_variance = max(
        (
            1
            - skewness * observed_sharpe
            + ((kurtosis - 1) / 4) * observed_sharpe**2
        )
        / (sample_size - 1),
        1e-12,
    )
    probability = _NORMAL.cdf(
        (observed_sharpe - expected_maximum) / sqrt(sharpe_variance)
    )
    return {
        **base,
        "observed_sharpe": _decimal(observed_sharpe),
        "expected_maximum_sharpe": _decimal(expected_maximum),
        "skewness": _decimal(skewness),
        "kurtosis": _decimal(kurtosis),
        "deflated_sharpe_probability": _decimal(probability),
        "sufficient_observations": True,
    }


def assess_research_gate(
    *,
    policy: PromotionGatePolicy,
    fold_count: int,
    candidate_count: int,
    regime_count: int,
    positive_fold_rate: Decimal,
    worst_drawdown: Decimal,
    probability_of_backtest_overfitting: Decimal,
    deflated_sharpe_probability: Decimal,
    pbo_applicable: bool = True,
    validation_subject: str = "adaptive_selector",
) -> dict[str, Any]:
    if validation_subject not in {"static_strategy", "adaptive_selector"}:
        raise ValueError("Unsupported validation subject")
    static_strategy = validation_subject == "static_strategy"
    evidence_shortfalls: list[str] = []
    threshold_failures: list[str] = []
    if fold_count < policy.minimum_oos_folds:
        evidence_shortfalls.append(
            f"oos_folds {fold_count} < {policy.minimum_oos_folds}"
        )
    if not static_strategy and candidate_count < policy.minimum_candidate_count:
        evidence_shortfalls.append(
            f"candidates {candidate_count} < {policy.minimum_candidate_count}"
        )
    if regime_count < policy.minimum_regime_count:
        evidence_shortfalls.append(
            f"regimes {regime_count} < {policy.minimum_regime_count}"
        )
    if not pbo_applicable and not static_strategy:
        evidence_shortfalls.append(
            "probability_of_backtest_overfitting is not applicable to one candidate"
        )
    elif (
        probability_of_backtest_overfitting
        > policy.maximum_probability_of_backtest_overfitting
    ):
        threshold_failures.append(
            "probability_of_backtest_overfitting exceeds policy maximum"
        )
    if deflated_sharpe_probability < policy.minimum_deflated_sharpe_probability:
        threshold_failures.append("deflated_sharpe_probability is below policy minimum")
    if positive_fold_rate < policy.minimum_positive_oos_fold_rate:
        threshold_failures.append("positive_oos_fold_rate is below policy minimum")
    if worst_drawdown < policy.maximum_allowed_drawdown:
        threshold_failures.append("worst_selected_oos_drawdown exceeds policy loss limit")
    if evidence_shortfalls:
        status = "INSUFFICIENT_EVIDENCE"
    elif threshold_failures:
        status = "REJECTED"
    else:
        status = "ELIGIBLE_FOR_HUMAN_REVIEW"
    return {
        "policy_version": policy.version,
        "policy_sha256": _canonical_hash(policy.model_dump(mode="json")),
        "status": status,
        "eligible_for_human_review": status == "ELIGIBLE_FOR_HUMAN_REVIEW",
        "automatic_promotion": False,
        "validation_subject": validation_subject,
        "pbo_applicable": pbo_applicable,
        "evidence_shortfalls": evidence_shortfalls,
        "threshold_failures": threshold_failures,
    }


def _mean(values: list[Decimal]) -> Decimal:
    return sum(values, _ZERO) / Decimal(len(values)) if values else _ZERO


def continuous_oos_equity_and_drawdown(
    paths: tuple[tuple[Decimal, ...], ...],
) -> tuple[tuple[Decimal, ...], Decimal]:
    """Chain selected fold paths while preserving every intrafold observation."""
    continuous = [_ONE]
    for path in paths:
        if not path or path[0] <= 0:
            raise ValueError("Selected OOS equity paths require a positive base")
        for previous, current in zip(path, path[1:]):
            if previous <= 0:
                raise ValueError("Selected OOS equity path crossed zero")
            continuous.append(continuous[-1] * current / previous)
    peak = continuous[0]
    drawdown = _ZERO
    for value in continuous:
        peak = max(peak, value)
        drawdown = min(drawdown, value / peak - _ONE)
    return tuple(continuous), drawdown


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
        promotion_policy: PromotionGatePolicy = DEFAULT_PROMOTION_GATE_POLICY,
        risk_policy: RiskPolicy | None = None,
        restrictions: RestrictionRegistry | None = None,
    ) -> None:
        self.store = store
        self.ledger = ledger
        self.promotion_policy = promotion_policy
        self.backtester = ResearchBacktester(
            store,
            ledger,
            calendar_name=calendar_name,
            risk_policy=risk_policy,
            restrictions=restrictions,
        )
        self.risk_policy = self.backtester.risk_policy
        self.restrictions = self.backtester.restrictions

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
        strategy_spec: StrategySpec | None = None,
    ) -> WalkForwardValidationReport:
        if strategy_spec is not None:
            if strategy_spec.timeframe != timeframe:
                raise ValueError("Static strategy timeframe does not match validation")
            strategy_types = (str(strategy_spec.strategy_type),)
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
        validated_strategy_spec_ids: dict[str, str] = {}
        candidate_oos_scores: dict[str, list[Decimal]] = {
            strategy_type: [] for strategy_type in strategy_types
        }
        selected_oos_equity_paths: list[tuple[Decimal, ...]] = []
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
                strategy_spec=strategy_spec,
            )
            for name, result in train_results.items():
                prior = validated_strategy_spec_ids.setdefault(
                    name, result.strategy_spec.strategy_spec_id
                )
                if prior != result.strategy_spec.strategy_spec_id:
                    raise ValueError("Validation candidate identity changed between folds")
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
                strategy_spec=strategy_spec,
            )
            for name, result in test_results.items():
                if validated_strategy_spec_ids[name] != result.strategy_spec.strategy_spec_id:
                    raise ValueError("Train/test strategy specification identity mismatch")
            for name, result in test_results.items():
                candidate_oos_scores[name].append(
                    _selection_value(result, selection_metric)
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
            selected_oos_equity_paths.append(
                test_results[selected_strategy].equity_curve
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
            candidate_oos_scores={
                name: tuple(values) for name, values in candidate_oos_scores.items()
            },
            selected_oos_equity_paths=tuple(selected_oos_equity_paths),
            validated_strategy_spec_ids=validated_strategy_spec_ids,
            cost_model=costs,
            initial_equity=initial_equity,
            trial_count=self.store.strategy_trial_count(
                symbol=symbol,
                timeframe=timeframe,
            ),
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
        strategy_spec: StrategySpec | None = None,
    ) -> dict[str, BacktestResult]:
        start, end = self._window_bounds(bars, indices)
        return {
            strategy_type: self.backtester.run(
                spec=(
                    strategy_spec
                    if strategy_spec is not None
                    else default_strategy_spec(
                        strategy_type,
                        timeframe=timeframe,
                        code_sha256=strategy_code_hash,
                    )
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
        candidate_oos_scores: dict[str, tuple[Decimal, ...]],
        selected_oos_equity_paths: tuple[tuple[Decimal, ...], ...],
        validated_strategy_spec_ids: dict[str, str],
        cost_model: BacktestCostModel,
        initial_equity: Decimal,
        trial_count: int,
        code_git_sha: str,
    ) -> WalkForwardValidationReport:
        test_returns = [fold.selected_test_metrics.total_return for fold in folds]
        test_sharpes = [fold.selected_test_metrics.sharpe_ratio for fold in folds]
        degradation = [
            fold.selected_train_metrics.sharpe_ratio
            - fold.selected_test_metrics.sharpe_ratio
            for fold in folds
        ]
        continuous_oos_equity, cumulative_drawdown = (
            continuous_oos_equity_and_drawdown(selected_oos_equity_paths)
        )
        compounded = continuous_oos_equity[-1]
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
            "worst_selected_oos_drawdown": cumulative_drawdown,
            "worst_individual_fold_drawdown": min(
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
        pbo_metrics = combinatorial_purged_diagnostics(candidate_oos_scores)
        validation_subject = (
            "static_strategy" if len(strategy_types) == 1 else "adaptive_selector"
        )
        search_trial_count = max(len(strategy_types), trial_count)
        dsr_metrics = deflated_sharpe_diagnostics(
            tuple(test_returns),
            number_of_trials=search_trial_count,
        )
        execution_contract = {
            "subject": validation_subject,
            "strategy_spec_ids": validated_strategy_spec_ids,
            "feature_set_version": FEATURE_SET_VERSION,
            "backtest_engine_version": BACKTEST_ENGINE_VERSION,
            "cost_model": cost_model.model_dump(mode="json"),
            "risk_policy": self.risk_policy.model_dump(mode="json"),
            "restriction_registry_version": self.restrictions.version,
            "initial_equity": str(initial_equity),
            "execution_profile": BASELINE_EXECUTION_PROFILE_VERSION,
        }
        execution_contract_sha256 = _canonical_hash(execution_contract)
        robustness_metrics = {
            "combinatorial_purged_validation": pbo_metrics,
            "deflated_sharpe": dsr_metrics,
            "selected_oos_equity_path": [
                str(value) for value in continuous_oos_equity
            ],
            "historical_trial_count_diagnostic": trial_count,
            "selection_search_trial_count": search_trial_count,
        }
        gate_assessment = assess_research_gate(
            policy=self.promotion_policy,
            fold_count=len(folds),
            candidate_count=len(strategy_types),
            regime_count=len(regime_metrics),
            positive_fold_rate=Decimal(
                str(aggregate["positive_oos_fold_rate"])
            ),
            worst_drawdown=Decimal(
                str(aggregate["worst_selected_oos_drawdown"])
            ),
            probability_of_backtest_overfitting=Decimal(
                str(pbo_metrics["probability_of_backtest_overfitting"])
            ),
            deflated_sharpe_probability=Decimal(
                str(dsr_metrics["deflated_sharpe_probability"])
            ),
            pbo_applicable=not bool(pbo_metrics.get("not_applicable", False)),
            validation_subject=validation_subject,
        )
        report_material = {
            "symbol": symbol.upper(),
            "timeframe": timeframe,
            "strategy_types": strategy_types,
            "validation_subject": validation_subject,
            "validated_strategy_spec_ids": validated_strategy_spec_ids,
            "execution_contract": execution_contract,
            "execution_contract_sha256": execution_contract_sha256,
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
            "robustness_metrics": robustness_metrics,
            "gate_assessment": gate_assessment,
        }
        return WalkForwardValidationReport(
            validation_report_id=uuid7(),
            symbol=symbol.upper(),
            timeframe=timeframe,
            strategy_types=strategy_types,
            validation_subject=validation_subject,
            validated_strategy_spec_ids=validated_strategy_spec_ids,
            execution_contract=execution_contract,
            execution_contract_sha256=execution_contract_sha256,
            selection_metric=selection_metric,
            train_bars=train_bars,
            test_bars=test_bars,
            step_bars=step_bars,
            embargo_bars=embargo_bars,
            folds=folds,
            aggregate_metrics=aggregate,
            regime_metrics=regime_metrics,
            robustness_metrics=robustness_metrics,
            gate_assessment=gate_assessment,
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
        serialized = report.model_dump(mode="json")
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
                    "robustness_metrics": serialized["robustness_metrics"],
                    "gate_assessment": serialized["gate_assessment"],
                },
            )
        )
