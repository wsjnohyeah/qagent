from __future__ import annotations

import argparse
import json
import subprocess
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from agentic_quant.config import Settings
from agentic_quant.domain import BacktestCostModel, BacktestResult, StockBar
from agentic_quant.ledger import EventLedger
from agentic_quant.market_store import MarketDataStore
from agentic_quant.migrations import upgrade_database
from agentic_quant.research import (
    SUPPORTED_STRATEGIES,
    ResearchBacktester,
    default_strategy_spec,
    research_code_sha256,
)
from agentic_quant.research_store import ResearchStore


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
    price = Decimal("100")
    index = 0
    while len(bars) < count:
        if day.weekday() < 5:
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
                    available_from=day + timedelta(days=1),
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
        "metrics": result.experiment.metrics.model_dump(mode="json"),
    }


def _services(settings: Settings) -> tuple[MarketDataStore, ResearchStore, ResearchBacktester]:
    upgrade_database(settings.database_url)
    ledger = EventLedger(settings.database_url)
    research_store = ResearchStore(ledger.engine)
    return (
        MarketDataStore(ledger.engine),
        research_store,
        ResearchBacktester(research_store, ledger),
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
    print(
        json.dumps(
            {
                "status": "COMPLETED",
                "mode": "deterministic_synthetic_research_smoke",
                "warning": "Synthetic smoke metrics are infrastructure checks, not alpha evidence.",
                "bars": len(bars),
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
        ),
    )
    print(json.dumps(_summary(result), indent=2))


def _list(settings: Settings, args: argparse.Namespace) -> None:
    _, research_store, _ = _services(settings)
    print(
        json.dumps(
            research_store.recent_experiments(limit=args.limit),
            indent=2,
            default=str,
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Point-in-time research and cost-aware backtest operations"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    smoke = subparsers.add_parser(
        "smoke",
        help="Run deterministic Phase 3A research vertical slices",
    )
    smoke.add_argument("--symbol", default="SYNTH")
    smoke.add_argument("--initial-equity", default="100000")
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
    list_runs = subparsers.add_parser("list", help="List recent immutable experiments")
    list_runs.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()
    settings = Settings()
    if args.command == "smoke":
        _smoke(settings, args)
    elif args.command == "run":
        _run(settings, args)
    else:
        _list(settings, args)


if __name__ == "__main__":
    main()
