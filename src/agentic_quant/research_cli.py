from __future__ import annotations

import argparse
import json
import subprocess
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from agentic_quant.config import Settings
from agentic_quant.domain import BacktestCostModel, BacktestResult, StockBar
from agentic_quant.ledger import EventLedger
from agentic_quant.market_calendar import MarketSessionClock
from agentic_quant.market_store import MarketDataStore
from agentic_quant.migrations import prepare_database
from agentic_quant.ml import (
    MLDatasetBuilder,
    MLPredictor,
    MLStore,
    WalkForwardMLTrainer,
    load_ml_policy,
)
from agentic_quant.research import (
    FeatureParityChecker,
    SUPPORTED_STRATEGIES,
    ResearchBacktester,
    default_strategy_spec,
    research_code_sha256,
)
from agentic_quant.research_store import ResearchStore
from agentic_quant.reference_data import GovernedReferenceImporter, ReferenceDataStore
from agentic_quant.risk import RestrictionRegistry, RiskPolicy
from agentic_quant.data_quality import MarketDataQualityService
from agentic_quant.validation import (
    SELECTION_METRICS,
    WalkForwardValidator,
    load_promotion_gate_policy,
)


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("Timestamp must include a timezone, preferably Z")
    return parsed.astimezone(UTC)


def _git_sha(settings: Settings) -> str:
    if settings.source_git_sha:
        return settings.source_git_sha
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return "UNAVAILABLE"
    if result.returncode != 0:
        return "UNAVAILABLE"
    return result.stdout.strip() or "UNAVAILABLE"


def _synthetic_daily_bars(symbol: str, count: int = 100) -> tuple[StockBar, ...]:
    bars: list[StockBar] = []
    day = datetime(2025, 1, 2, 5, tzinfo=UTC)
    session_clock = MarketSessionClock()
    price = Decimal("100")
    index = 0
    while len(bars) < count:
        try:
            available_from = session_clock.daily_bar_available_from(day)
        except ValueError:
            day += timedelta(days=1)
            continue
        else:
            cycle = index % 20
            change = (
                Decimal("-0.018")
                if cycle in {8, 9}
                else Decimal("0.004") + Decimal(index % 3) * Decimal("0.0005")
            )
            open_price = price
            close_price = open_price * (_ONE + change)
            high = max(open_price, close_price) * Decimal("1.003")
            low = min(open_price, close_price) * Decimal("0.997")
            bar_identity = uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"agentic-quant:{symbol}:1Day:{day.isoformat()}",
            )
            bars.append(
                StockBar(
                    bar_id=str(bar_identity),
                    symbol=symbol,
                    timeframe="1Day",
                    event_time=day,
                    available_from=available_from,
                    open=open_price,
                    high=high,
                    low=low,
                    close=close_price,
                    volume=1_000_000 + index * 2_500,
                    trade_count=10_000 + index,
                    vwap=(open_price + close_price) / Decimal("2"),
                    source="synthetic",
                    feed="research-smoke",
                    raw_object_id="SYNTHETIC_RESEARCH_SMOKE",
                    ingested_at=datetime(2026, 9, 4, tzinfo=UTC),
                )
            )
            price = close_price
            index += 1
        day += timedelta(days=1)
    return tuple(bars)


_ONE = Decimal("1")


def _summary(result: BacktestResult) -> dict[str, object]:
    return {
        "experiment_run_id": result.experiment.experiment_run_id,
        "strategy": result.strategy_spec.strategy_type,
        "strategy_version": result.strategy_spec.version,
        "symbol": result.experiment.symbol,
        "timeframe": result.experiment.timeframe,
        "dataset_hash": result.experiment.dataset_hash,
        "feature_snapshots": len(result.experiment.feature_snapshot_ids),
        "portfolio_events": len(result.portfolio_events),
        "metrics": result.experiment.metrics.model_dump(mode="json"),
    }


def _services(settings: Settings) -> tuple[MarketDataStore, ResearchStore, ResearchBacktester]:
    prepare_database(settings)
    ledger = EventLedger(settings.database_url)
    research_store = ResearchStore(ledger.engine)
    return (
        MarketDataStore(ledger.engine),
        research_store,
        ResearchBacktester(
            research_store,
            ledger,
            calendar_name=settings.market_calendar,
            risk_policy=RiskPolicy.from_yaml(settings.risk_policy_path),
            restrictions=RestrictionRegistry.from_yaml(
                settings.restricted_securities_path
            ),
        ),
    )


def _smoke(settings: Settings, args: argparse.Namespace) -> None:
    market_store, research_store, backtester = _services(settings)
    symbol = args.symbol.upper()
    bars = _synthetic_daily_bars(symbol)
    market_store.insert_bars(bars, raw_object_id="SYNTHETIC_RESEARCH_SMOKE")
    code_sha256 = research_code_sha256()
    results = []
    for strategy_type in SUPPORTED_STRATEGIES:
        spec = default_strategy_spec(
            strategy_type,
            timeframe="1Day",
            code_sha256=code_sha256,
        )
        result = backtester.run(
            spec=spec,
            symbol=symbol,
            as_of_start=bars[20].available_from,
            as_of_end=bars[-1].available_from,
            code_git_sha=_git_sha(settings),
            initial_equity=Decimal(str(args.initial_equity)),
            cost_model=BacktestCostModel(),
        )
        results.append(_summary(result))
    parity = FeatureParityChecker(research_store).check(
        symbol=symbol,
        timeframe="1Day",
        as_of=bars[50].available_from,
    )
    print(
        json.dumps(
            {
                "status": "COMPLETED",
                "mode": "deterministic_synthetic_research_smoke",
                "warning": "Synthetic smoke metrics are infrastructure checks, not alpha evidence.",
                "bars": len(bars),
                "feature_parity": parity.model_dump(mode="json"),
                "results": results,
                "research_store": research_store.health_summary(),
            },
            indent=2,
        )
    )


def _run(settings: Settings, args: argparse.Namespace) -> None:
    _, _, backtester = _services(settings)
    code_sha256 = research_code_sha256()
    spec = default_strategy_spec(
        args.strategy,
        timeframe=args.timeframe,
        code_sha256=code_sha256,
    )
    result = backtester.run(
        spec=spec,
        symbol=args.symbol.upper(),
        as_of_start=args.start,
        as_of_end=args.end,
        code_git_sha=_git_sha(settings),
        initial_equity=Decimal(str(args.initial_equity)),
        cost_model=BacktestCostModel(
            commission_per_share=Decimal(str(args.commission_per_share)),
            minimum_commission_per_order=Decimal(str(args.minimum_commission)),
            slippage_bps_per_side=Decimal(str(args.slippage_bps)),
            half_spread_bps_per_side=Decimal(str(args.half_spread_bps)),
            market_impact_bps_per_side=Decimal(str(args.market_impact_bps)),
            max_volume_participation=Decimal(str(args.max_volume_participation)),
        ),
    )
    print(json.dumps(_summary(result), indent=2))


def _validate(settings: Settings, args: argparse.Namespace) -> None:
    _, research_store, _ = _services(settings)
    strategy_types = tuple(
        item.strip() for item in args.strategies.split(",") if item.strip()
    )
    strategy_spec = None
    if args.strategy_spec_id:
        strategy_spec = research_store.strategy_spec(args.strategy_spec_id)
        if strategy_spec is None:
            raise ValueError("Strategy specification not found")
    report = WalkForwardValidator(
        research_store,
        EventLedger(settings.database_url),
        calendar_name=settings.market_calendar,
        promotion_policy=load_promotion_gate_policy(
            settings.research_promotion_policy_path
        ),
        risk_policy=RiskPolicy.from_yaml(settings.risk_policy_path),
        restrictions=RestrictionRegistry.from_yaml(
            settings.restricted_securities_path
        ),
    ).run(
        symbol=args.symbol.upper(),
        timeframe=args.timeframe,
        as_of_start=args.start,
        as_of_end=args.end,
        code_git_sha=_git_sha(settings),
        strategy_types=strategy_types,
        selection_metric=args.selection_metric,
        train_bars=args.train_bars,
        test_bars=args.test_bars,
        step_bars=args.step_bars,
        embargo_bars=args.embargo_bars,
        initial_equity=Decimal(str(args.initial_equity)),
        cost_model=BacktestCostModel(
            commission_per_share=Decimal(str(args.commission_per_share)),
            minimum_commission_per_order=Decimal(str(args.minimum_commission)),
            slippage_bps_per_side=Decimal(str(args.slippage_bps)),
            half_spread_bps_per_side=Decimal(str(args.half_spread_bps)),
            market_impact_bps_per_side=Decimal(str(args.market_impact_bps)),
            max_volume_participation=Decimal(str(args.max_volume_participation)),
        ),
        strategy_spec=strategy_spec,
    )
    print(json.dumps(report.model_dump(mode="json"), indent=2))


def _validation_smoke(settings: Settings, args: argparse.Namespace) -> None:
    market_store, research_store, _ = _services(settings)
    symbol = args.symbol.upper()
    bars = _synthetic_daily_bars(symbol)
    market_store.insert_bars(bars, raw_object_id="SYNTHETIC_RESEARCH_SMOKE")
    report = WalkForwardValidator(
        research_store,
        EventLedger(settings.database_url),
        calendar_name=settings.market_calendar,
        promotion_policy=load_promotion_gate_policy(
            settings.research_promotion_policy_path
        ),
    ).run(
        symbol=symbol,
        timeframe="1Day",
        as_of_start=bars[20].available_from,
        as_of_end=bars[-1].available_from,
        code_git_sha=_git_sha(settings),
        train_bars=30,
        test_bars=10,
        step_bars=10,
        embargo_bars=1,
    )
    print(
        json.dumps(
            {
                "status": "COMPLETED",
                "mode": "deterministic_walk_forward_validation_smoke",
                "warning": "Synthetic validation checks machinery, not alpha.",
                "validation_report_id": report.validation_report_id,
                "report_hash": report.report_hash,
                "fold_count": len(report.folds),
                "aggregate_metrics": report.model_dump(mode="json")[
                    "aggregate_metrics"
                ],
                "regime_metrics": report.model_dump(mode="json")["regime_metrics"],
                "robustness_metrics": report.model_dump(mode="json")[
                    "robustness_metrics"
                ],
                "gate_assessment": report.model_dump(mode="json")[
                    "gate_assessment"
                ],
            },
            indent=2,
        )
    )


def _list(settings: Settings, args: argparse.Namespace) -> None:
    _, research_store, _ = _services(settings)
    print(
        json.dumps(
            research_store.recent_experiments(limit=args.limit),
            indent=2,
            default=str,
        )
    )


def _parity(settings: Settings, args: argparse.Namespace) -> None:
    _, research_store, _ = _services(settings)
    check = FeatureParityChecker(research_store).check(
        symbol=args.symbol.upper(),
        timeframe=args.timeframe,
        as_of=args.as_of,
    )
    print(json.dumps(check.model_dump(mode="json"), indent=2))
    if not check.matched:
        raise SystemExit(1)


def _quality(settings: Settings, args: argparse.Namespace) -> None:
    prepare_database(settings)
    ledger = EventLedger(settings.database_url)
    store = ResearchStore(ledger.engine)
    bars = store.load_bars(
        symbol=args.symbol.upper(),
        timeframe=args.timeframe,
        as_of_end=args.end,
    )
    report = MarketDataQualityService(
        ledger.engine,
        ledger,
        calendar_name=settings.market_calendar,
    ).assess_bars(
        bars,
        symbol=args.symbol,
        timeframe=args.timeframe,
        code_git_sha=_git_sha(settings),
    )
    print(json.dumps(report.model_dump(mode="json"), indent=2))
    if report.status.value == "FAILED":
        raise SystemExit(1)


def _import_reference(settings: Settings, args: argparse.Namespace) -> None:
    prepare_database(settings)
    payload = json.loads(Path(args.path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Reference import root must be an object")
    ledger = EventLedger(settings.database_url)
    result = GovernedReferenceImporter(
        ReferenceDataStore(ledger.engine),
        ledger,
    ).import_payload(payload)
    print(json.dumps(result.model_dump(mode="json"), indent=2))


def _ml_train(settings: Settings, args: argparse.Namespace) -> None:
    prepare_database(settings)
    ledger = EventLedger(settings.database_url)
    research_store = ResearchStore(ledger.engine)
    snapshots = research_store.feature_snapshots_for_training(
        symbol=args.symbol.upper(),
        timeframe=args.timeframe,
        as_of_end=args.end,
    )
    if not snapshots:
        raise ValueError("No feature snapshots found for ML training")
    policy = load_ml_policy(settings.ml_policy_path)
    examples = MLDatasetBuilder(research_store).build(
        symbol=args.symbol.upper(),
        timeframe=args.timeframe,
        as_of_end=args.end,
        horizon_bars=args.horizon_bars,
        policy=policy,
    )
    result = WalkForwardMLTrainer(
        MLStore(ledger.engine, ledger),
        policy,
        code_git_sha=_git_sha(settings),
    ).train(
        examples,
        symbol=args.symbol.upper(),
        timeframe=args.timeframe,
        horizon_bars=args.horizon_bars,
        feature_set_version=snapshots[0].feature_set_version,
    )
    print(json.dumps(result.model_dump(mode="json"), indent=2))


def _ml_models(settings: Settings, args: argparse.Namespace) -> None:
    prepare_database(settings)
    ledger = EventLedger(settings.database_url)
    print(
        json.dumps(
            MLStore(ledger.engine).recent_models(limit=args.limit),
            indent=2,
            default=str,
        )
    )


def _ml_forecast(settings: Settings, args: argparse.Namespace) -> None:
    prepare_database(settings)
    ledger = EventLedger(settings.database_url)
    ml_store = MLStore(ledger.engine, ledger)
    model = ml_store.model(args.model_id)
    if model is None:
        raise ValueError("ML model not found")
    snapshot = ResearchStore(ledger.engine).feature_snapshot(args.feature_snapshot_id)
    if snapshot is None:
        raise ValueError("Feature snapshot not found")
    forecast = MLPredictor(ml_store).predict(model=model, snapshot=snapshot)
    print(json.dumps(forecast.model_dump(mode="json"), indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Point-in-time research and cost-aware backtest operations"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    smoke = subparsers.add_parser(
        "smoke",
        help="Run deterministic Phase 3 research vertical slices",
    )
    smoke.add_argument("--symbol", default="SYNTH")
    smoke.add_argument("--initial-equity", default="100000")
    validation_smoke = subparsers.add_parser(
        "validation-smoke",
        help="Run bounded deterministic walk-forward validation",
    )
    validation_smoke.add_argument("--symbol", default="SYNTH")
    run = subparsers.add_parser("run", help="Backtest an approved baseline on stored bars")
    run.add_argument("symbol")
    run.add_argument("--strategy", choices=SUPPORTED_STRATEGIES, required=True)
    run.add_argument("--timeframe", choices=("1Min", "1Day"), default="1Day")
    run.add_argument("--start", required=True, type=_parse_time)
    run.add_argument("--end", required=True, type=_parse_time)
    run.add_argument("--initial-equity", default="100000")
    run.add_argument("--commission-per-share", default="0.0049")
    run.add_argument("--minimum-commission", default="0.99")
    run.add_argument("--slippage-bps", default="2.0")
    run.add_argument("--half-spread-bps", default="1.0")
    run.add_argument("--market-impact-bps", default="1.0")
    run.add_argument("--max-volume-participation", default="0.05")
    list_runs = subparsers.add_parser("list", help="List recent immutable experiments")
    list_runs.add_argument("--limit", type=int, default=20)
    parity = subparsers.add_parser(
        "parity",
        help="Compare offline full-history and online as-of feature materialization",
    )
    parity.add_argument("symbol")
    parity.add_argument("--timeframe", choices=("1Min", "1Day"), default="1Day")
    parity.add_argument("--as-of", required=True, type=_parse_time)
    quality = subparsers.add_parser(
        "quality",
        help="Run and persist fail-closed market-bar quality checks",
    )
    quality.add_argument("symbol")
    quality.add_argument("--timeframe", choices=("1Min", "1Day"), default="1Day")
    quality.add_argument("--end", required=True, type=_parse_time)
    reference_import = subparsers.add_parser(
        "import-reference",
        help="Import a governed corporate-action or universe-membership JSON batch",
    )
    reference_import.add_argument("path")
    ml_train = subparsers.add_parser(
        "ml-train",
        help="Train and register point-in-time logistic and boosted-stump candidates",
    )
    ml_train.add_argument("symbol")
    ml_train.add_argument("--timeframe", choices=("1Min", "1Day"), default="1Day")
    ml_train.add_argument("--end", required=True, type=_parse_time)
    ml_train.add_argument("--horizon-bars", type=int, default=1)
    ml_models = subparsers.add_parser("ml-models", help="List recent ML model versions")
    ml_models.add_argument("--limit", type=int, default=20)
    ml_forecast = subparsers.add_parser(
        "ml-forecast",
        help="Create a point-in-time forecast from a registered model",
    )
    ml_forecast.add_argument("model_id")
    ml_forecast.add_argument("feature_snapshot_id")
    validate = subparsers.add_parser(
        "validate",
        help="Run chronological train/embargo/test strategy validation",
    )
    validate.add_argument("symbol")
    validate.add_argument("--timeframe", choices=("1Min", "1Day"), default="1Day")
    validate.add_argument("--start", required=True, type=_parse_time)
    validate.add_argument("--end", required=True, type=_parse_time)
    validate.add_argument("--strategies", default=",".join(SUPPORTED_STRATEGIES))
    validate.add_argument(
        "--strategy-spec-id",
        help="Validate this exact immutable generated strategy specification",
    )
    validate.add_argument(
        "--selection-metric",
        choices=SELECTION_METRICS,
        default="sharpe_ratio",
    )
    validate.add_argument("--train-bars", type=int, default=40)
    validate.add_argument("--test-bars", type=int, default=10)
    validate.add_argument("--step-bars", type=int, default=10)
    validate.add_argument("--embargo-bars", type=int, default=1)
    validate.add_argument("--initial-equity", default="100000")
    validate.add_argument("--commission-per-share", default="0.0049")
    validate.add_argument("--minimum-commission", default="0.99")
    validate.add_argument("--slippage-bps", default="2.0")
    validate.add_argument("--half-spread-bps", default="1.0")
    validate.add_argument("--market-impact-bps", default="1.0")
    validate.add_argument("--max-volume-participation", default="0.05")
    args = parser.parse_args()
    settings = Settings()
    if args.command == "smoke":
        _smoke(settings, args)
    elif args.command == "validation-smoke":
        _validation_smoke(settings, args)
    elif args.command == "run":
        _run(settings, args)
    elif args.command == "parity":
        _parity(settings, args)
    elif args.command == "quality":
        _quality(settings, args)
    elif args.command == "import-reference":
        _import_reference(settings, args)
    elif args.command == "ml-train":
        _ml_train(settings, args)
    elif args.command == "ml-models":
        _ml_models(settings, args)
    elif args.command == "ml-forecast":
        _ml_forecast(settings, args)
    elif args.command == "validate":
        _validate(settings, args)
    else:
        _list(settings, args)


if __name__ == "__main__":
    main()
