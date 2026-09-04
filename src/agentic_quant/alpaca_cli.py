from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime

from agentic_quant.archive import build_raw_archive
from agentic_quant.config import Settings
from agentic_quant.event_bus import NullEventPublisher, RedisStreamPublisher
from agentic_quant.ledger import EventLedger
from agentic_quant.live_ingestion import LiveMarketDataService
from agentic_quant.market_ingestion import MarketDataIngestionService
from agentic_quant.market_calendar import MarketGapDetector
from agentic_quant.market_store import MarketDataStore
from agentic_quant.migrations import upgrade_database
from agentic_quant.option_ingestion import OptionDataIngestionService
from agentic_quant.providers.alpaca import AlpacaConfigurationError, AlpacaMarketDataProvider
from agentic_quant.providers.alpaca_stream import AlpacaStockStream
from agentic_quant.providers.base import OptionChainRequest, StockBarsRequest


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("Timestamp must include a timezone, preferably Z")
    return parsed.astimezone(UTC)


def _provider(settings: Settings) -> AlpacaMarketDataProvider:
    if settings.alpaca_api_key is None or settings.alpaca_api_secret is None:
        raise AlpacaConfigurationError(
            "Set ALPACA_API_KEY and ALPACA_API_SECRET in the ignored .env file"
        )
    return AlpacaMarketDataProvider(
        api_key=settings.alpaca_api_key.get_secret_value(),
        api_secret=settings.alpaca_api_secret.get_secret_value(),
        base_url=settings.alpaca_data_base_url,
    )


async def _probe(settings: Settings) -> None:
    async with _provider(settings) as provider:
        result = await provider.probe_entitlements(
            stock_feed=settings.alpaca_stock_feed,
            option_feed=settings.alpaca_option_feed,
        )
    if settings.alpaca_api_key is None or settings.alpaca_api_secret is None:
        raise AlpacaConfigurationError("Alpaca credentials are not configured")
    stream = AlpacaStockStream(
        api_key=settings.alpaca_api_key.get_secret_value(),
        api_secret=settings.alpaca_api_secret.get_secret_value(),
        feed=settings.alpaca_stock_feed,
        base_url=settings.alpaca_stock_stream_base_url,
    )
    stream_result = await stream.probe()
    print(
        json.dumps(
            {
                "rest": [item.model_dump(mode="json") for item in result],
                "stock_stream": stream_result,
            },
            indent=2,
        )
    )


async def _backfill(settings: Settings, args: argparse.Namespace) -> None:
    settings.validate_backfill_window(
        start=args.start,
        end=args.end,
        timeframe=args.timeframe,
    )
    upgrade_database(settings.database_url)
    ledger = EventLedger(settings.database_url)
    publisher = (
        RedisStreamPublisher(settings.redis_url, settings.redis_stream_name)
        if settings.redis_url
        else NullEventPublisher()
    )
    async with _provider(settings) as provider:
        service = MarketDataIngestionService(
            provider=provider,
            archive=build_raw_archive(settings),
            store=MarketDataStore(ledger.engine),
            ledger=ledger,
            publisher=publisher,
        )
        result = await service.ingest_stock_bars(
            StockBarsRequest(
                symbol=args.symbol,
                start=args.start,
                end=args.end,
                timeframe=args.timeframe,
                feed=settings.alpaca_stock_feed,
            )
        )
    print(json.dumps(result.model_dump(mode="json"), indent=2))


async def _option_snapshot(settings: Settings, args: argparse.Namespace) -> None:
    upgrade_database(settings.database_url)
    ledger = EventLedger(settings.database_url)
    publisher = (
        RedisStreamPublisher(settings.redis_url, settings.redis_stream_name)
        if settings.redis_url
        else NullEventPublisher()
    )
    async with _provider(settings) as provider:
        service = OptionDataIngestionService(
            provider=provider,
            archive=build_raw_archive(settings),
            store=MarketDataStore(ledger.engine),
            ledger=ledger,
            publisher=publisher,
        )
        result = await service.ingest_option_chain(
            OptionChainRequest(
                underlying_symbol=args.underlying,
                feed=settings.alpaca_option_feed,
                limit=args.limit,
                max_pages=args.max_pages,
            )
        )
    print(json.dumps(result.model_dump(mode="json"), indent=2))


async def _stream(settings: Settings, args: argparse.Namespace) -> None:
    if settings.alpaca_api_key is None or settings.alpaca_api_secret is None:
        raise AlpacaConfigurationError("Alpaca credentials are not configured")
    upgrade_database(settings.database_url)
    ledger = EventLedger(settings.database_url)
    publisher = (
        RedisStreamPublisher(settings.redis_url, settings.redis_stream_name)
        if settings.redis_url
        else NullEventPublisher()
    )
    service = LiveMarketDataService(
        archive=build_raw_archive(settings),
        store=MarketDataStore(ledger.engine),
        ledger=ledger,
        publisher=publisher,
        feed=settings.alpaca_stock_feed,
        gap_detector=MarketGapDetector(settings.market_calendar),
    )
    stream = AlpacaStockStream(
        api_key=settings.alpaca_api_key.get_secret_value(),
        api_secret=settings.alpaca_api_secret.get_secret_value(),
        feed=settings.alpaca_stock_feed,
        base_url=settings.alpaca_stock_stream_base_url,
    )
    totals = {"frames": 0, "messages_received": 0, "records_inserted": 0}
    async with _provider(settings) as backfill_provider:
        backfill_service = MarketDataIngestionService(
            provider=backfill_provider,
            archive=build_raw_archive(settings),
            store=MarketDataStore(ledger.engine),
            ledger=ledger,
            publisher=publisher,
        )
        await _consume_stream(
            stream=stream,
            service=service,
            backfill_service=backfill_service,
            symbols=args.symbols,
            seconds=args.seconds,
            max_frames=args.max_frames,
            totals=totals,
            feed=settings.alpaca_stock_feed,
        )
    print(json.dumps({**totals, "status": "COMPLETED"}, indent=2))


async def _consume_stream(
    *,
    stream: AlpacaStockStream,
    service: LiveMarketDataService,
    backfill_service: MarketDataIngestionService,
    symbols: str,
    seconds: int,
    max_frames: int,
    totals: dict[str, int],
    feed: str,
) -> None:
    try:
        async with asyncio.timeout(seconds):
            async for frame in stream.frames(symbols.split(",")):
                summary = service.ingest_frame(frame)
                totals["frames"] += 1
                totals["messages_received"] += summary.messages_received
                totals["records_inserted"] += summary.records_inserted
                for gap in summary.gaps:
                    await backfill_service.ingest_stock_bars(
                        StockBarsRequest(
                            symbol=gap.symbol,
                            start=gap.missing_from,
                            end=gap.missing_to,
                            feed=feed,
                        )
                    )
                if max_frames and totals["frames"] >= max_frames:
                    break
    except TimeoutError:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only Alpaca market-data operations")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("probe", help="Check SIP and OPRA data entitlements")
    backfill = subparsers.add_parser("backfill", help="Ingest raw equity bars")
    backfill.add_argument("symbol")
    backfill.add_argument("--start", required=True, type=_parse_time)
    backfill.add_argument("--end", required=True, type=_parse_time)
    backfill.add_argument("--timeframe", choices=("1Min", "1Day"), default="1Min")
    options = subparsers.add_parser(
        "option-snapshot",
        help="Ingest a read-only option-chain snapshot",
    )
    options.add_argument("underlying")
    options.add_argument("--limit", type=int, default=100)
    options.add_argument("--max-pages", type=int, default=1)
    stream = subparsers.add_parser(
        "stream",
        help="Persist a bounded live stock trade/quote/bar stream",
    )
    stream.add_argument("--symbols", default="SPY")
    stream.add_argument("--seconds", type=int, default=60)
    stream.add_argument("--max-frames", type=int, default=100)
    args = parser.parse_args()
    settings = Settings()
    if args.command == "probe":
        asyncio.run(_probe(settings))
    elif args.command == "backfill":
        asyncio.run(_backfill(settings, args))
    elif args.command == "option-snapshot":
        asyncio.run(_option_snapshot(settings, args))
    else:
        asyncio.run(_stream(settings, args))


if __name__ == "__main__":
    main()
