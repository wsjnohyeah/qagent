from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime
from typing import Any

from agentic_quant.archive import build_raw_archive
from agentic_quant.config import Settings
from agentic_quant.document_ingestion import (
    DocumentIngestionService,
    FundamentalsIngestionService,
)
from agentic_quant.document_store import DocumentStore
from agentic_quant.event_bus import NullEventPublisher, RedisStreamPublisher
from agentic_quant.ledger import EventLedger
from agentic_quant.market_store import MarketDataStore
from agentic_quant.migrations import prepare_database
from agentic_quant.providers.base import CorporateFactsRequest, DocumentFetchRequest
from agentic_quant.providers.documents import (
    AlpacaNewsProvider,
    DocumentProviderConfigurationError,
    InvestorRelationsFeedProvider,
    SecEdgarProvider,
)


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("Timestamp must include a timezone, preferably Z")
    return parsed.astimezone(UTC)


def _runtime(settings: Settings) -> tuple[Any, MarketDataStore, DocumentStore, EventLedger, Any]:
    prepare_database(settings)
    ledger = EventLedger(settings.database_url)
    market_store = MarketDataStore(ledger.engine)
    document_store = DocumentStore(ledger.engine)
    publisher = (
        RedisStreamPublisher(settings.redis_url, settings.redis_stream_name)
        if settings.redis_url
        else NullEventPublisher()
    )
    return build_raw_archive(settings), market_store, document_store, ledger, publisher


def _alpaca_news_provider(settings: Settings) -> AlpacaNewsProvider:
    if settings.alpaca_api_key is None or settings.alpaca_api_secret is None:
        raise DocumentProviderConfigurationError(
            "Set ALPACA_API_KEY and ALPACA_API_SECRET in the ignored .env file"
        )
    return AlpacaNewsProvider(
        api_key=settings.alpaca_api_key.get_secret_value(),
        api_secret=settings.alpaca_api_secret.get_secret_value(),
        base_url=settings.alpaca_data_base_url,
    )


def _sec_provider(settings: Settings) -> SecEdgarProvider:
    if not settings.sec_user_agent:
        raise DocumentProviderConfigurationError(
            "Set SEC_USER_AGENT to an operator name and contact email before using SEC APIs"
        )
    return SecEdgarProvider(user_agent=settings.sec_user_agent)


async def _ingest_news(settings: Settings, args: argparse.Namespace) -> None:
    archive, market_store, document_store, ledger, publisher = _runtime(settings)
    async with _alpaca_news_provider(settings) as provider:
        result = await DocumentIngestionService(
            provider=provider,
            archive=archive,
            market_store=market_store,
            document_store=document_store,
            ledger=ledger,
            publisher=publisher,
        ).ingest_documents(
            DocumentFetchRequest(
                symbols=tuple(args.symbols.upper().split(",")),
                start=args.start,
                end=args.end,
                limit=args.limit,
                max_pages=args.max_pages,
            )
        )
    print(json.dumps(result.model_dump(mode="json"), indent=2))


async def _ingest_sec_filings(settings: Settings, args: argparse.Namespace) -> None:
    archive, market_store, document_store, ledger, publisher = _runtime(settings)
    async with _sec_provider(settings) as provider:
        result = await DocumentIngestionService(
            provider=provider,
            archive=archive,
            market_store=market_store,
            document_store=document_store,
            ledger=ledger,
            publisher=publisher,
        ).ingest_documents(
            DocumentFetchRequest(
                symbols=(args.symbol.upper(),),
                cik=args.cik,
                forms=tuple(value.upper() for value in args.forms.split(",") if value),
                limit=args.limit,
            )
        )
    print(json.dumps(result.model_dump(mode="json"), indent=2))


async def _ingest_sec_facts(settings: Settings, args: argparse.Namespace) -> None:
    archive, market_store, document_store, ledger, publisher = _runtime(settings)
    async with _sec_provider(settings) as provider:
        page = await provider.fetch_company_facts(
            CorporateFactsRequest(
                symbol=args.symbol.upper(),
                cik=args.cik,
                max_facts=args.max_facts,
            )
        )
    result = FundamentalsIngestionService(
        archive=archive,
        market_store=market_store,
        document_store=document_store,
        ledger=ledger,
        publisher=publisher,
    ).ingest_page(page)
    print(json.dumps(result.model_dump(mode="json"), indent=2))


async def _ingest_ir(settings: Settings, args: argparse.Namespace) -> None:
    archive, market_store, document_store, ledger, publisher = _runtime(settings)
    async with InvestorRelationsFeedProvider(
        feed_url=args.url,
        expected_hostname=args.host,
        issuer_name=args.issuer,
    ) as provider:
        result = await DocumentIngestionService(
            provider=provider,
            archive=archive,
            market_store=market_store,
            document_store=document_store,
            ledger=ledger,
            publisher=publisher,
        ).ingest_documents(
            DocumentFetchRequest(symbols=(args.symbol.upper(),), limit=args.limit)
        )
    print(json.dumps(result.model_dump(mode="json"), indent=2))


def _search(settings: Settings, args: argparse.Namespace) -> None:
    _, _, store, _, _ = _runtime(settings)
    result = store.search_documents(
        query=args.query,
        symbol=args.symbol,
        limit=args.limit,
    )
    print(json.dumps(result, indent=2, default=str))


def _health(settings: Settings) -> None:
    _, _, store, _, _ = _runtime(settings)
    print(json.dumps(store.health_summary(), indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="Point-in-time event/document operations")
    commands = parser.add_subparsers(dest="command", required=True)
    news = commands.add_parser("news", help="Ingest bounded Alpaca news pages")
    news.add_argument("symbols", help="Comma-separated equity symbols")
    news.add_argument("--start", type=_parse_time)
    news.add_argument("--end", type=_parse_time)
    news.add_argument("--limit", type=int, default=50)
    news.add_argument("--max-pages", type=int, default=1)
    filings = commands.add_parser("sec-filings", help="Ingest recent SEC filing metadata")
    filings.add_argument("symbol")
    filings.add_argument("--cik", required=True)
    filings.add_argument("--forms", default="8-K,10-K,10-Q,6-K")
    filings.add_argument("--limit", type=int, default=50)
    facts = commands.add_parser("sec-facts", help="Ingest normalized SEC company facts")
    facts.add_argument("symbol")
    facts.add_argument("--cik", required=True)
    facts.add_argument("--max-facts", type=int, default=1000)
    ir = commands.add_parser("ir-feed", help="Ingest one explicitly approved IR RSS/Atom feed")
    ir.add_argument("symbol")
    ir.add_argument("--url", required=True)
    ir.add_argument("--host", required=True)
    ir.add_argument("--issuer", required=True)
    ir.add_argument("--limit", type=int, default=50)
    search = commands.add_parser("search", help="Search normalized source documents")
    search.add_argument("query")
    search.add_argument("--symbol")
    search.add_argument("--limit", type=int, default=50)
    commands.add_parser("health", help="Report Phase 2 normalized record counts")
    args = parser.parse_args()
    settings = Settings()
    if args.command == "news":
        asyncio.run(_ingest_news(settings, args))
    elif args.command == "sec-filings":
        asyncio.run(_ingest_sec_filings(settings, args))
    elif args.command == "sec-facts":
        asyncio.run(_ingest_sec_facts(settings, args))
    elif args.command == "ir-feed":
        asyncio.run(_ingest_ir(settings, args))
    elif args.command == "search":
        _search(settings, args)
    else:
        _health(settings)


if __name__ == "__main__":
    main()
