from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
import hashlib
from typing import Any, Callable

import exchange_calendars as exchange_calendars  # type: ignore[import-untyped]
from sqlalchemy import func, select

from agentic_quant.archive import RawArchive
from agentic_quant.config import AppEnvironment, Settings
from agentic_quant.control_plane import SystemObjectStore
from agentic_quant.data_quality import MarketDataQualityService
from agentic_quant.document_ingestion import (
    DocumentIngestionService,
    FundamentalsIngestionService,
)
from agentic_quant.document_store import DocumentStore
from agentic_quant.database import ledger_events
from agentic_quant.domain import (
    BacktestCostModel,
    EventEnvelope,
    ResearchAnalysisStatus,
    StockBar,
    WorkflowJob,
)
from agentic_quant.ids import stable_uuid
from agentic_quant.intelligence import (
    ANALYSIS_PROMPT_VERSION,
    EvidenceBoundResearchAnalyst,
    ResearchEvidenceRetriever,
)
from agentic_quant.ledger import EventLedger
from agentic_quant.llm import LLMConfigurationError
from agentic_quant.llm_budget import LLMBudgetExceededError
from agentic_quant.market_ingestion import MarketDataIngestionService
from agentic_quant.market_scanner import AUTO_TRADING_POOL_SLUG, MARKET_SCAN_EVENT
from agentic_quant.market_store import MarketDataStore
from agentic_quant.ml import (
    MLDatasetBuilder,
    MLPredictor,
    MLPolicy,
    MLStore,
    WalkForwardMLTrainer,
    ml_dataset_sha256,
    ml_training_contract_sha256,
)
from agentic_quant.providers.alpaca import AlpacaMarketDataProvider
from agentic_quant.providers.base import (
    CorporateFactsRequest,
    DocumentFetchRequest,
    EventPublisher,
    StockBarsRequest,
)
from agentic_quant.providers.documents import AlpacaNewsProvider, SecEdgarProvider
from agentic_quant.research import FEATURE_SET_VERSION, PointInTimeFeatureBuilder
from agentic_quant.research_store import ResearchStore
from agentic_quant.risk import RestrictionRegistry, strategy_holding_period_sessions
from agentic_quant.shadow import ShadowRuntime
from agentic_quant.strategy_generation import HybridStrategyGenerator
from agentic_quant.validation import (
    WalkForwardValidator,
    load_promotion_gate_policy,
    promotion_policy_sha256,
    validation_execution_contract,
    validation_input_fingerprint,
)


MARKET_HISTORY_BOUNDARY_EVENT = "market.history.boundary.observed.v1"
DOCUMENT_HISTORY_COVERAGE_EVENT = "document.history.coverage.v1"
SEC_REFERENCE_REFRESH_EVENT = "sec.reference.refresh.v1"
MINIMUM_SUSPENSION_SESSIONS = 20


def daily_bar_gap_windows(
    *,
    start: datetime,
    end: datetime,
    existing_event_times: tuple[datetime, ...],
    calendar_name: str = "XNYS",
) -> tuple[tuple[datetime, datetime], ...]:
    """Plan minimal half-open requests covering every missing completed session."""
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("Gap-planning timestamps must be timezone-aware")
    if start >= end:
        return ()
    start_utc = start.astimezone(UTC)
    end_utc = end.astimezone(UTC)
    calendar = exchange_calendars.get_calendar(calendar_name)
    sessions = tuple(
        session
        for session in calendar.sessions_in_range(
            start_utc.date().isoformat(),
            end_utc.date().isoformat(),
        )
        if calendar.session_close(session).to_pydatetime() < end_utc
    )
    present_dates = {value.astimezone(UTC).date() for value in existing_event_times}
    missing_indexes = [
        index
        for index, session in enumerate(sessions)
        if session.date() not in present_dates
    ]
    if not missing_indexes:
        return ()
    groups: list[list[int]] = []
    for index in missing_indexes:
        if not groups or index != groups[-1][-1] + 1:
            groups.append([index])
        else:
            groups[-1].append(index)
    windows = []
    for group in groups:
        first_day: date = sessions[group[0]].date()
        last_day: date = sessions[group[-1]].date()
        window_start = datetime.combine(first_day, time.min, tzinfo=UTC)
        window_end = min(
            end_utc,
            datetime.combine(last_day + timedelta(days=1), time.min, tzinfo=UTC),
        )
        windows.append((window_start, window_end))
    return tuple(windows)


def resumed_daily_history_start(
    *,
    bars: tuple[StockBar, ...],
    start: datetime,
    end: datetime,
    calendar_name: str = "XNYS",
    minimum_suspension_sessions: int = MINIMUM_SUSPENSION_SESSIONS,
) -> datetime | None:
    """Find a provider-evidenced current segment after an extended suspension.

    A missing interval alone remains a data-quality failure. It is considered a security
    inactivity boundary only when it spans the configured minimum, is immediately preceded
    by the same number of explicit zero-volume provider bars, and is followed by a traded bar.
    """
    if minimum_suspension_sessions < 1:
        raise ValueError("Suspension boundary requires at least one session")
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("Suspension-boundary timestamps must be timezone-aware")
    calendar = exchange_calendars.get_calendar(calendar_name)
    sessions = tuple(
        session.date()
        for session in calendar.sessions_in_range(
            start.astimezone(UTC).date().isoformat(),
            end.astimezone(UTC).date().isoformat(),
        )
        if calendar.session_close(session).to_pydatetime() < end.astimezone(UTC)
    )
    bars_by_date = {item.event_time.astimezone(UTC).date(): item for item in bars}
    missing_indexes = [
        index for index, session_date in enumerate(sessions)
        if session_date not in bars_by_date
    ]
    groups: list[list[int]] = []
    for index in missing_indexes:
        if not groups or index != groups[-1][-1] + 1:
            groups.append([index])
        else:
            groups[-1].append(index)
    candidate: datetime | None = None
    for group in groups:
        if (
            len(group) < minimum_suspension_sessions
            or group[0] < minimum_suspension_sessions
            or group[-1] + 1 >= len(sessions)
        ):
            continue
        prior_dates = sessions[
            group[0] - minimum_suspension_sessions : group[0]
        ]
        if any(
            bars_by_date.get(session_date) is None
            or bars_by_date[session_date].volume != 0
            for session_date in prior_dates
        ):
            continue
        resumed_date = sessions[group[-1] + 1]
        resumed_bar = bars_by_date.get(resumed_date)
        if resumed_bar is None or resumed_bar.volume <= 0:
            continue
        candidate = datetime.combine(resumed_date, time.min, tzinfo=UTC)
    return candidate


class ResearchCoordinatorHandler:
    """Execute one coordinator DAG stage using the production service contracts."""

    def __init__(
        self,
        *,
        settings: Settings,
        ledger: EventLedger,
        objects: SystemObjectStore,
        market: MarketDataStore,
        documents: DocumentStore,
        research: ResearchStore,
        ml: MLStore,
        ml_policy: MLPolicy,
        evidence: ResearchEvidenceRetriever,
        analyst: EvidenceBoundResearchAnalyst,
        generator: HybridStrategyGenerator,
        shadow: ShadowRuntime,
        restrictions: RestrictionRegistry,
        archive: RawArchive,
        publisher: EventPublisher,
        pipeline_enabled: Callable[[str], bool],
    ) -> None:
        self.settings = settings
        self.ledger = ledger
        self.objects = objects
        self.market = market
        self.documents = documents
        self.research = research
        self.ml = ml
        self.ml_policy = ml_policy
        self.evidence = evidence
        self.analyst = analyst
        self.generator = generator
        self.shadow = shadow
        self.restrictions = restrictions
        self.archive = archive
        self.publisher = publisher
        self.pipeline_enabled = pipeline_enabled
        self.features = PointInTimeFeatureBuilder(research)
        self._sec_ticker_map: dict[str, str] | None = None

    def _known_history_boundary(
        self,
        *,
        symbol: str,
        timeframe: str,
        desired_start: datetime,
    ) -> tuple[str, datetime] | None:
        with self.ledger.engine.connect() as connection:
            rows = connection.execute(
                select(ledger_events.c.event_id, ledger_events.c.payload)
                .where(
                    ledger_events.c.event_type == MARKET_HISTORY_BOUNDARY_EVENT,
                    ledger_events.c.payload["symbol"].as_string()
                    == symbol.upper(),
                    ledger_events.c.payload["timeframe"].as_string()
                    == timeframe,
                )
                .order_by(ledger_events.c.sequence.desc())
                .limit(100)
            ).all()
        for row in rows:
            payload = dict(row.payload)
            if (
                payload.get("source") != "alpaca"
                or payload.get("feed") != self.settings.alpaca_stock_feed
            ):
                continue
            probed_start = datetime.fromisoformat(str(payload["probed_start"]))
            observed_start = datetime.fromisoformat(str(payload["observed_start"]))
            if probed_start <= desired_start:
                return str(row.event_id), max(desired_start, observed_start)
        return None

    def _record_history_boundary(
        self,
        *,
        symbol: str,
        timeframe: str,
        probed_start: datetime,
        observed_start: datetime,
        observed_at: datetime,
        ingestion_run_ids: list[str],
        interpretation: str = "PROVIDER_OBSERVED_HISTORY_START",
    ) -> str:
        event_id = stable_uuid(
            "market-history-boundary",
            self.settings.deployment_environment_id,
            symbol.upper(),
            timeframe,
            self.settings.alpaca_stock_feed,
            probed_start.isoformat(),
            observed_start.isoformat(),
        )
        self.ledger.append(
            EventEnvelope(
                event_id=event_id,
                event_type=MARKET_HISTORY_BOUNDARY_EVENT,
                event_time=observed_at,
                emitted_at=datetime.now(UTC),
                producer="research-coordinator",
                correlation_id=stable_uuid(
                    "market-history-boundary",
                    self.settings.deployment_environment_id,
                    symbol.upper(),
                    timeframe,
                ),
                payload={
                    "symbol": symbol.upper(),
                    "timeframe": timeframe,
                    "source": "alpaca",
                    "feed": self.settings.alpaca_stock_feed,
                    "probed_start": probed_start.isoformat(),
                    "observed_start": observed_start.isoformat(),
                    "observed_at": observed_at.isoformat(),
                    "evidence_ingestion_run_ids": ingestion_run_ids,
                    "interpretation": interpretation,
                },
            )
        )
        return event_id

    async def __call__(
        self,
        job: WorkflowJob,
        dependencies: tuple[WorkflowJob, ...],
    ) -> dict[str, Any]:
        stage = str(job.payload["stage"])
        context = dict(dependencies[-1].result) if dependencies else {}
        context.update(
            {
                "symbol": str(job.payload["symbol"]),
                "timeframe": str(job.payload["timeframe"]),
                "as_of": str(job.payload["as_of"]),
                "cycle_key": str(job.payload.get("cycle_key", "legacy")),
                # The research horizon is part of the immutable job request.  It
                # must be re-applied at every stage instead of relying on a
                # dependency result to happen to carry it forward.  Legacy jobs
                # created before horizon partitioning are explicitly one-day.
                "horizon_bars": int(job.payload.get("horizon_bars", 1)),
            }
        )
        if job.payload.get("universe_scan_id") is not None:
            context["universe_scan_id"] = job.payload["universe_scan_id"]
        required_pipelines = {
            "collect_market_data": ("market-data",),
            "collect_research_evidence": ("documents",),
            "materialize_features": ("research",),
            "train_ml": ("research", "ml"),
            "forecast_ml": ("research", "ml"),
            "research_llm": ("research", "llm"),
            "generate_strategy": ("research", "llm"),
            "validate_strategy": ("research",),
            "await_shadow_adoption": ("research",),
        }
        paused = [
            name
            for name in required_pipelines[stage]
            if not self.pipeline_enabled(name)
        ]
        if paused:
            return {
                **context,
                "outcome": "WAITING_PIPELINE_PAUSED",
                "paused_pipelines": paused,
                "stage": stage,
            }
        handler = getattr(self, f"_{stage}")
        result = await handler(context)
        return {**context, **result, "stage": stage}

    async def _collect_market_data(self, context: dict[str, Any]) -> dict[str, Any]:
        if self.settings.alpaca_api_key is None or self.settings.alpaca_api_secret is None:
            return {"outcome": "WAITING_CREDENTIALS"}
        as_of = datetime.fromisoformat(str(context["as_of"]))
        lookback_days = self.settings.coordinator_initial_lookback_days
        if self.settings.app_env == AppEnvironment.DEVELOPMENT:
            lookback_days = min(
                lookback_days,
                self.settings.development_max_backfill_days,
            )
        desired_start = datetime.combine(
            (as_of.astimezone(UTC) - timedelta(days=lookback_days)).date(),
            time.min,
            tzinfo=UTC,
        )
        symbol = str(context["symbol"]).upper()
        timeframe = str(context["timeframe"])
        known_boundary = self._known_history_boundary(
            symbol=symbol,
            timeframe=timeframe,
            desired_start=desired_start,
        )
        coverage_start = known_boundary[1] if known_boundary else desired_start
        stored = self.market.bars_between(
            symbol=symbol,
            timeframe="1Day",
            start=coverage_start,
            end=as_of,
            source="alpaca",
            feed=self.settings.alpaca_stock_feed,
        )
        windows = daily_bar_gap_windows(
            start=coverage_start,
            end=as_of,
            existing_event_times=tuple(item.event_time for item in stored),
            calendar_name=self.settings.market_calendar,
        )
        if not windows:
            latest = stored[-1].event_time if stored else None
            quality = MarketDataQualityService(
                self.market.engine,
                self.ledger,
                calendar_name=self.settings.market_calendar,
            ).require_bars(
                stored,
                symbol=symbol,
                timeframe="1Day",
                code_git_sha=self.settings.source_git_sha or "UNAVAILABLE",
                expected_start=coverage_start,
                expected_end=as_of,
            )
            return {
                "outcome": "UP_TO_DATE",
                "latest_bar_event_time": latest.isoformat() if latest else None,
                "requested_window_start": desired_start.isoformat(),
                "verified_window_start": coverage_start.isoformat(),
                "history_boundary_event_id": (
                    known_boundary[0] if known_boundary else None
                ),
                "data_quality_report_id": quality.data_quality_report_id,
            }
        provider = AlpacaMarketDataProvider(
            api_key=self.settings.alpaca_api_key.get_secret_value(),
            api_secret=self.settings.alpaca_api_secret.get_secret_value(),
            base_url=self.settings.alpaca_data_base_url,
            calendar_name=self.settings.market_calendar,
        )
        summaries = []
        async with provider:
            service = MarketDataIngestionService(
                provider=provider,
                archive=self.archive,
                store=self.market,
                ledger=self.ledger,
                publisher=self.publisher,
                calendar_name=self.settings.market_calendar,
                code_git_sha=self.settings.source_git_sha or "UNAVAILABLE",
            )
            for window_start, window_end in windows:
                summary = await service.ingest_stock_bars(
                    StockBarsRequest(
                        symbol=symbol,
                        start=window_start,
                        end=window_end,
                        timeframe="1Day",
                        feed=self.settings.alpaca_stock_feed,
                        adjustment="raw",
                    ),
                    validate_quality=False,
                )
                summaries.append(summary.model_dump(mode="json"))
        repaired = self.market.bars_between(
            symbol=symbol,
            timeframe="1Day",
            start=coverage_start,
            end=as_of,
            source="alpaca",
            feed=self.settings.alpaca_stock_feed,
        )
        if not repaired:
            return {
                "outcome": "WAITING_MARKET_HISTORY",
                "requested_window_start": desired_start.isoformat(),
                "verified_window_start": None,
                "gap_windows_repaired": len(windows),
                "ingestions": summaries,
            }
        boundary_event_id = known_boundary[0] if known_boundary else None
        boundary_interpretation: str | None = None
        if known_boundary is None and windows:
            earliest = repaired[0].event_time
            if windows[0][0].date() < earliest.date():
                coverage_start = datetime.combine(
                    earliest.date(),
                    time.min,
                    tzinfo=UTC,
                )
                repaired = tuple(
                    item for item in repaired if item.event_time >= coverage_start
                )
                boundary_interpretation = "PROVIDER_OBSERVED_HISTORY_START"
        resumed_start = resumed_daily_history_start(
            bars=repaired,
            start=coverage_start,
            end=as_of,
            calendar_name=self.settings.market_calendar,
        )
        if resumed_start is not None and resumed_start > coverage_start:
            coverage_start = resumed_start
            repaired = tuple(
                item for item in repaired if item.event_time >= coverage_start
            )
            boundary_interpretation = "PROVIDER_OBSERVED_POST_SUSPENSION_START"
        quality = MarketDataQualityService(
            self.market.engine,
            self.ledger,
            calendar_name=self.settings.market_calendar,
        ).require_bars(
            repaired,
            symbol=symbol,
            timeframe="1Day",
            code_git_sha=self.settings.source_git_sha or "UNAVAILABLE",
            expected_start=coverage_start,
            expected_end=as_of,
        )
        if boundary_interpretation is not None:
            boundary_event_id = self._record_history_boundary(
                symbol=symbol,
                timeframe=timeframe,
                probed_start=desired_start,
                observed_start=coverage_start,
                observed_at=as_of,
                ingestion_run_ids=[
                    str(item["ingestion_run_id"]) for item in summaries
                ],
                interpretation=boundary_interpretation,
            )
        return {
            "outcome": "COMPLETED",
            "gap_windows_repaired": len(windows),
            "ingestions": summaries,
            "data_quality_report_id": quality.data_quality_report_id,
            "requested_window_start": desired_start.isoformat(),
            "verified_window_start": coverage_start.isoformat(),
            "history_boundary_event_id": boundary_event_id,
        }

    async def _collect_research_evidence(
        self,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        if self.settings.alpaca_api_key is None or self.settings.alpaca_api_secret is None:
            return {"outcome": "WAITING_CREDENTIALS"}
        as_of = datetime.fromisoformat(str(context["as_of"]))
        symbol = str(context["symbol"])
        latest = self.documents.latest_document_published_at(
            symbol=symbol,
            provider="alpaca_news",
        )
        lookback_days = self.settings.coordinator_document_lookback_days
        if self.settings.app_env == AppEnvironment.DEVELOPMENT:
            lookback_days = min(
                lookback_days,
                self.settings.development_max_backfill_days,
            )
        desired_start = as_of - timedelta(days=lookback_days)
        recorded_start = self._document_coverage_start(symbol=symbol)
        # Only an explicit, untruncated coverage certificate may advance the next
        # request. Existing rows can include older articles corrected inside a recent
        # query window; their publication dates do not prove intervening coverage.
        known_start = recorded_start
        partition = timedelta(days=self.settings.coordinator_document_partition_days)
        requests: list[tuple[str, datetime, datetime]] = []
        if known_start is None:
            requests.append(("INITIAL_RECENT", max(desired_start, as_of - partition), as_of))
        elif known_start > desired_start:
            requests.append(
                (
                    "HISTORICAL_LEADING",
                    max(desired_start, known_start - partition),
                    min(as_of, known_start + timedelta(seconds=1)),
                )
            )
        # Re-read one day to catch provider corrections while persistence remains
        # idempotent by provider document ID and content hash. Backfill and refresh
        # may run together so building history never leaves current evidence stale.
        if latest is not None:
            refresh_start = max(desired_start, as_of - timedelta(days=1))
            if refresh_start < as_of and not any(
                start <= refresh_start and end >= as_of for _, start, end in requests
            ):
                requests.append(("INCREMENTAL_REFRESH", refresh_start, as_of))
        if not requests:
            sec_summary = await self._collect_sec_evidence(
                symbol=symbol,
                start=desired_start,
                as_of=as_of,
            )
            return {
                "outcome": "UP_TO_DATE",
                "target_window_start": desired_start.isoformat(),
                "verified_coverage_start": known_start.isoformat() if known_start else None,
                "latest_document_published_at": latest.isoformat() if latest else None,
                "sec_evidence": sec_summary,
            }
        provider = AlpacaNewsProvider(
            api_key=self.settings.alpaca_api_key.get_secret_value(),
            api_secret=self.settings.alpaca_api_secret.get_secret_value(),
            base_url=self.settings.alpaca_data_base_url,
        )
        summaries: list[dict[str, Any]] = []
        historical_truncated = False
        async with provider:
            service = DocumentIngestionService(
                provider=provider,
                archive=self.archive,
                market_store=self.market,
                document_store=self.documents,
                ledger=self.ledger,
                publisher=self.publisher,
            )
            for request_kind, start, end in requests:
                summary = await service.ingest_documents(
                    DocumentFetchRequest(
                        symbols=(symbol,),
                        start=start,
                        end=end,
                        limit=50,
                        max_pages=self.settings.coordinator_document_max_pages,
                    )
                )
                summaries.append(
                    {
                        "request_kind": request_kind,
                        "start": start.isoformat(),
                        "end": end.isoformat(),
                        **summary.model_dump(mode="json"),
                    }
                )
                if request_kind != "INCREMENTAL_REFRESH":
                    historical_truncated = summary.truncated
                    if not summary.truncated:
                        self._record_document_coverage(
                            symbol=symbol,
                            covered_start=start,
                            covered_end=end,
                            observed_at=as_of,
                            ingestion_run_id=summary.ingestion_run_id,
                        )
        verified_start = self._document_coverage_start(symbol=symbol)
        sec_summary = await self._collect_sec_evidence(
            symbol=symbol,
            start=desired_start,
            as_of=as_of,
        )
        return {
            "outcome": (
                "WAITING_DOCUMENT_PAGE_BOUND"
                if historical_truncated
                else "COMPLETED_BACKFILL_PROGRESS"
                if verified_start is not None and verified_start > desired_start
                else "COMPLETED"
            ),
            "target_window_start": desired_start.isoformat(),
            "verified_coverage_start": (
                verified_start.isoformat() if verified_start is not None else None
            ),
            "document_ingestions": summaries,
            "sec_evidence": sec_summary,
        }

    def _document_coverage_start(self, *, symbol: str) -> datetime | None:
        with self.ledger.engine.connect() as connection:
            rows = connection.execute(
                select(ledger_events.c.payload)
                .where(
                    ledger_events.c.event_type == DOCUMENT_HISTORY_COVERAGE_EVENT,
                    ledger_events.c.payload["symbol"].as_string() == symbol.upper(),
                    ledger_events.c.payload["provider"].as_string() == "alpaca_news",
                )
                .order_by(ledger_events.c.sequence.desc())
                .limit(500)
            ).all()
        starts = [
            datetime.fromisoformat(str(dict(row.payload)["covered_start"]))
            for row in rows
        ]
        return min(starts) if starts else None

    def _record_document_coverage(
        self,
        *,
        symbol: str,
        covered_start: datetime,
        covered_end: datetime,
        observed_at: datetime,
        ingestion_run_id: str,
    ) -> str:
        event_id = stable_uuid(
            "document-history-coverage",
            self.settings.deployment_environment_id,
            symbol.upper(),
            "alpaca_news",
            covered_start.isoformat(),
            covered_end.isoformat(),
        )
        self.ledger.append(
            EventEnvelope(
                event_id=event_id,
                event_type=DOCUMENT_HISTORY_COVERAGE_EVENT,
                event_time=observed_at,
                emitted_at=datetime.now(UTC),
                producer="research-coordinator",
                correlation_id=stable_uuid(
                    "document-history-coverage",
                    self.settings.deployment_environment_id,
                    symbol.upper(),
                    "alpaca_news",
                ),
                payload={
                    "symbol": symbol.upper(),
                    "provider": "alpaca_news",
                    "covered_start": covered_start.isoformat(),
                    "covered_end": covered_end.isoformat(),
                    "observed_at": observed_at.isoformat(),
                    "evidence_ingestion_run_id": ingestion_run_id,
                    "interpretation": "PROVIDER_QUERY_COMPLETED_WITHOUT_PAGE_TRUNCATION",
                },
            )
        )
        return event_id

    async def _collect_sec_evidence(
        self,
        *,
        symbol: str,
        start: datetime,
        as_of: datetime,
    ) -> dict[str, Any]:
        if not self.settings.sec_user_agent:
            return {"status": "DISABLED_MISSING_SEC_USER_AGENT"}
        existing = self._latest_sec_refresh(symbol=symbol)
        if existing is not None and existing >= datetime.now(UTC) - timedelta(hours=24):
            return {"status": "UP_TO_DATE", "last_refreshed_at": existing.isoformat()}
        async with SecEdgarProvider(user_agent=self.settings.sec_user_agent) as provider:
            ticker_map = getattr(self, "_sec_ticker_map", None)
            ticker_map_raw_object_id: str | None = None
            if ticker_map is None:
                ticker_page = await provider.fetch_company_ticker_map()
                archived = self.archive.store_json(
                    provider=ticker_page.provider,
                    data_type=ticker_page.data_type,
                    payload=ticker_page.raw_payload,
                    request_metadata=ticker_page.request_metadata,
                    provider_received_at=ticker_page.provider_received_at,
                )
                ticker_map_raw_object_id = self.market.register_raw_object(archived)
                ticker_map = ticker_page.cik_by_symbol
                self._sec_ticker_map = ticker_map
            cik = ticker_map.get(symbol.upper())
            if cik is None:
                self._record_sec_refresh(
                    symbol=symbol,
                    start=start,
                    as_of=as_of,
                    status="NOT_APPLICABLE_NO_CIK",
                    cik=None,
                    ingestion_run_ids=[],
                    ticker_map_raw_object_id=ticker_map_raw_object_id,
                )
                return {"status": "NOT_APPLICABLE_NO_CIK"}
            document_summary = await DocumentIngestionService(
                provider=provider,
                archive=self.archive,
                market_store=self.market,
                document_store=self.documents,
                ledger=self.ledger,
                publisher=self.publisher,
            ).ingest_documents(
                DocumentFetchRequest(
                    symbols=(symbol.upper(),),
                    start=start,
                    end=as_of,
                    cik=cik,
                    forms=("8-K", "10-K", "10-Q", "6-K", "20-F", "40-F"),
                    limit=1_000,
                    max_pages=self.settings.coordinator_document_max_pages,
                )
            )
            facts_page = await provider.fetch_company_facts(
                CorporateFactsRequest(
                    symbol=symbol.upper(),
                    cik=cik,
                    max_facts=20_000,
                    start=start,
                    end=as_of,
                )
            )
        facts_summary = FundamentalsIngestionService(
            archive=self.archive,
            market_store=self.market,
            document_store=self.documents,
            ledger=self.ledger,
            publisher=self.publisher,
        ).ingest_page(facts_page)
        ingestion_run_ids = [
            document_summary.ingestion_run_id,
            facts_summary.ingestion_run_id,
        ]
        self._record_sec_refresh(
            symbol=symbol,
            start=start,
            as_of=as_of,
            status="COMPLETED",
            cik=cik,
            ingestion_run_ids=ingestion_run_ids,
            ticker_map_raw_object_id=ticker_map_raw_object_id,
        )
        return {
            "status": "COMPLETED",
            "cik": cik,
            "filings": document_summary.model_dump(mode="json"),
            "company_facts": facts_summary.model_dump(mode="json"),
        }

    def _latest_sec_refresh(self, *, symbol: str) -> datetime | None:
        with self.ledger.engine.connect() as connection:
            value = connection.execute(
                select(func.max(ledger_events.c.event_time)).where(
                    ledger_events.c.event_type == SEC_REFERENCE_REFRESH_EVENT,
                    ledger_events.c.payload["symbol"].as_string() == symbol.upper(),
                )
            ).scalar_one()
        if not isinstance(value, datetime):
            return None
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)

    def _record_sec_refresh(
        self,
        *,
        symbol: str,
        start: datetime,
        as_of: datetime,
        status: str,
        cik: str | None,
        ingestion_run_ids: list[str],
        ticker_map_raw_object_id: str | None,
    ) -> None:
        observed_at = datetime.now(UTC)
        self.ledger.append(
            EventEnvelope(
                event_id=stable_uuid(
                    "sec-reference-refresh",
                    self.settings.deployment_environment_id,
                    symbol.upper(),
                    observed_at.date().isoformat(),
                ),
                event_type=SEC_REFERENCE_REFRESH_EVENT,
                event_time=observed_at,
                emitted_at=observed_at,
                producer="research-coordinator",
                correlation_id=stable_uuid(
                    "sec-reference-refresh",
                    self.settings.deployment_environment_id,
                    symbol.upper(),
                ),
                payload={
                    "symbol": symbol.upper(),
                    "cik": cik,
                    "status": status,
                    "requested_start": start.isoformat(),
                    "as_of": as_of.isoformat(),
                    "ingestion_run_ids": ingestion_run_ids,
                    "ticker_map_raw_object_id": ticker_map_raw_object_id,
                },
            )
        )

    async def _materialize_features(self, context: dict[str, Any]) -> dict[str, Any]:
        as_of = datetime.fromisoformat(str(context["as_of"]))
        bars = self._verified_research_bars(context=context, as_of=as_of)
        if len(bars) < 21:
            return {
                "outcome": "WAITING_MORE_HISTORY",
                "bar_count": len(bars),
                "required_bars": 21,
            }
        latest = None
        for index in range(20, len(bars)):
            bar = bars[index]
            if bar.available_from > as_of:
                break
            latest = self.features.build(
                symbol=str(context["symbol"]),
                timeframe=str(context["timeframe"]),
                as_of=bar.available_from,
                bars=bars[: index + 1],
            )
        if latest is None:
            return {"outcome": "WAITING_COMPLETED_BAR", "bar_count": len(bars)}
        # The price features remain based only on completed bars, while the decision
        # snapshot advances to the actual research time. This lets a pre-open/holiday
        # cycle use documents that became available after the prior session close
        # without pretending those documents were known by an older historical bar.
        observed_now = datetime.now(UTC)
        decision_as_of = (
            observed_now
            if abs(observed_now - as_of) <= timedelta(hours=1)
            else as_of
        )
        if latest.as_of < decision_as_of:
            latest = self.features.build(
                symbol=str(context["symbol"]),
                timeframe=str(context["timeframe"]),
                as_of=decision_as_of,
                bars=bars,
            )
        return {
            "outcome": "COMPLETED",
            "bar_count": len(bars),
            "feature_snapshot_id": latest.feature_snapshot_id,
            "feature_as_of": latest.as_of.isoformat(),
            "as_of": latest.as_of.isoformat(),
            "latest_completed_bar_available_from": bars[-1].available_from.isoformat(),
        }

    async def _train_ml(self, context: dict[str, Any]) -> dict[str, Any]:
        snapshot_id = context.get("feature_snapshot_id")
        if not snapshot_id:
            return {"outcome": "WAITING_FEATURES"}
        snapshot = self.research.feature_snapshot(str(snapshot_id))
        if snapshot is None:
            return {"outcome": "WAITING_FEATURES"}
        horizon_bars = int(context.get("horizon_bars", 1))
        try:
            examples = MLDatasetBuilder(self.research).build(
                symbol=snapshot.symbol,
                timeframe=snapshot.timeframe,
                as_of_end=snapshot.as_of,
                horizon_bars=horizon_bars,
                policy=self.ml_policy,
                feature_set_version=FEATURE_SET_VERSION,
            )
        except ValueError as exc:
            return {
                "outcome": "WAITING_MORE_ML_SAMPLES",
                "detail": str(exc),
                "required_samples": self.ml_policy.validation.minimum_samples,
            }
        if len(examples) < self.ml_policy.validation.minimum_samples:
            return {
                "outcome": "WAITING_MORE_ML_SAMPLES",
                "sample_count": len(examples),
                "required_samples": self.ml_policy.validation.minimum_samples,
            }
        dataset_sha256 = ml_dataset_sha256(examples)
        training_contract_sha256 = ml_training_contract_sha256(
            policy=self.ml_policy,
            horizon_bars=horizon_bars,
            feature_set_version=FEATURE_SET_VERSION,
        )
        existing = next(
            (
                item
                for item in self.ml.recent_training_runs(limit=500)
                if item["symbol"] == snapshot.symbol
                and item["timeframe"] == snapshot.timeframe
                and int(item["horizon_bars"]) == horizon_bars
                and item["feature_set_version"] == FEATURE_SET_VERSION
                and item["dataset_sha256"] == dataset_sha256
                and item.get("training_contract_sha256")
                == training_contract_sha256
            ),
            None,
        )
        if existing is not None:
            return {
                "outcome": "REUSED",
                "training_run_id": str(existing["training_run_id"]),
                "model_id": str(existing["selected_model_id"]),
                "sample_count": len(examples),
            }
        result = WalkForwardMLTrainer(
            self.ml,
            self.ml_policy,
            code_git_sha=self.settings.source_git_sha or "UNAVAILABLE",
        ).train(
            examples,
            symbol=snapshot.symbol,
            timeframe=snapshot.timeframe,
            horizon_bars=horizon_bars,
            feature_set_version=FEATURE_SET_VERSION,
        )
        return {
            "outcome": "COMPLETED",
            "training_run_id": result.run.training_run_id,
            "model_id": result.run.selected_model_id,
            "sample_count": len(examples),
        }

    async def _forecast_ml(self, context: dict[str, Any]) -> dict[str, Any]:
        if not context.get("model_id") or not context.get("feature_snapshot_id"):
            return {"outcome": "WAITING_TRAINED_MODEL"}
        model = self.ml.model(str(context["model_id"]))
        snapshot = self.research.feature_snapshot(str(context["feature_snapshot_id"]))
        if model is None or snapshot is None:
            return {"outcome": "WAITING_TRAINED_MODEL"}
        forecast = MLPredictor(self.ml).predict(model=model, snapshot=snapshot)
        return {
            "outcome": "COMPLETED",
            "forecast_id": forecast.forecast_id,
        }

    async def _research_llm(self, context: dict[str, Any]) -> dict[str, Any]:
        if not self.settings.coordinator_paid_research_enabled:
            return {"outcome": "WAITING_PAID_RESEARCH_ENABLEMENT"}
        snapshot = self.research.feature_snapshot(
            str(context.get("feature_snapshot_id", ""))
        )
        forecast = self.ml.forecast(str(context.get("forecast_id", "")))
        if snapshot is None or forecast is None:
            return {"outcome": "WAITING_HYBRID_EVIDENCE"}
        bundle = self.evidence.retrieve(
            feature_snapshot=snapshot,
            as_of=snapshot.as_of,
            forecast=forecast,
        )
        prior = next(
            (
                item
                for item in self.analyst.store.recent(limit=500)
                if item["feature_snapshot_id"] == snapshot.feature_snapshot_id
                and item.get("forecast_id") == forecast.forecast_id
                and item["prompt_version"] == ANALYSIS_PROMPT_VERSION
                and item["evidence_bundle"]["evidence_bundle_hash"]
                == bundle.evidence_bundle_hash
                and item.get("llm_invocation_status") != "FAILED"
            ),
            None,
        )
        if prior is not None:
            prior_status = str(prior["status"])
            return {
                "outcome": (
                    "REUSED"
                    if prior_status == ResearchAnalysisStatus.COMPLETED.value
                    else "COMPLETED_ADVISORY_ABSTAINED"
                    if prior_status == ResearchAnalysisStatus.ABSTAINED.value
                    else f"WAITING_ANALYSIS_{prior_status}"
                ),
                "analysis_id": str(prior["analysis_id"]),
                "analysis_status": prior_status,
            }
        try:
            # Keep the qualitative judgment on the same forecast horizon. A
            # hard-coded multi-day horizon makes a valid one-bar model look
            # contradictory and forces otherwise healthy hybrid runs to abstain.
            analysis = await self.analyst.analyze(bundle, horizon=forecast.horizon)
        except LLMBudgetExceededError as exc:
            return {"outcome": "WAITING_LLM_BUDGET", "detail": str(exc)}
        except LLMConfigurationError as exc:
            return {"outcome": "WAITING_LLM_CONFIGURATION", "detail": str(exc)}
        return {
            "outcome": (
                "COMPLETED"
                if analysis.status == ResearchAnalysisStatus.COMPLETED
                else "COMPLETED_ADVISORY_ABSTAINED"
                if analysis.status == ResearchAnalysisStatus.ABSTAINED
                else f"WAITING_ANALYSIS_{analysis.status.value}"
            ),
            "analysis_id": analysis.analysis_id,
            "analysis_status": analysis.status.value,
        }

    async def _generate_strategy(self, context: dict[str, Any]) -> dict[str, Any]:
        if context.get("analysis_status") not in {
            ResearchAnalysisStatus.COMPLETED.value,
            ResearchAnalysisStatus.ABSTAINED.value,
        }:
            return {"outcome": "WAITING_VALID_ANALYSIS"}
        prior = next(
            (
                item
                for item in self.research.generation_attempts(limit=500)
                if str(item["feature_snapshot_id"])
                == str(context["feature_snapshot_id"])
                and str(item["analysis_id"]) == str(context["analysis_id"])
                and str(item["forecast_id"]) == str(context["forecast_id"])
                and str(item["status"]) in {"ACCEPT", "REJECT"}
            ),
            None,
        )
        if prior is not None:
            return {
                "outcome": (
                    "REUSED" if prior["strategy_spec_id"] else "WAITING_ACCEPTED_PROPOSAL"
                ),
                "generation_attempt_id": str(prior["generation_attempt_id"]),
                "strategy_spec_id": (
                    str(prior["strategy_spec_id"])
                    if prior["strategy_spec_id"]
                    else None
                ),
            }
        try:
            result = await self.generator.generate(
                feature_snapshot_id=str(context["feature_snapshot_id"]),
                analysis_id=str(context["analysis_id"]),
                forecast_id=str(context["forecast_id"]),
            )
        except LLMBudgetExceededError as exc:
            return {"outcome": "WAITING_LLM_BUDGET", "detail": str(exc)}
        except LLMConfigurationError as exc:
            return {"outcome": "WAITING_LLM_CONFIGURATION", "detail": str(exc)}
        spec = result.get("strategy_spec")
        return {
            "outcome": (
                "COMPLETED" if isinstance(spec, dict) else "WAITING_ACCEPTED_PROPOSAL"
            ),
            "generation_attempt_id": result["generation_attempt_id"],
            "strategy_spec_id": (
                str(spec["strategy_spec_id"]) if isinstance(spec, dict) else None
            ),
        }

    async def _validate_strategy(self, context: dict[str, Any]) -> dict[str, Any]:
        spec_id = context.get("strategy_spec_id")
        if not spec_id:
            reusable = self.research.generated_strategy_specs(
                symbol=str(context["symbol"]),
                timeframe=str(context["timeframe"]),
                holding_period_sessions=int(context.get("horizon_bars", 1)),
                feature_set_version=FEATURE_SET_VERSION,
            )
            if not reusable:
                return {"outcome": "WAITING_STRATEGY_SPEC"}
            rotation_key = (
                f"{context.get('cycle_key', context['as_of'])}:"
                f"{context['symbol']}:{context.get('horizon_bars', 1)}"
            )
            selected_index = int(
                hashlib.sha256(rotation_key.encode()).hexdigest()[:16],
                16,
            ) % len(reusable)
            spec_id = reusable[selected_index].strategy_spec_id
        spec = self.research.strategy_spec(str(spec_id))
        if spec is None:
            return {"outcome": "WAITING_STRATEGY_SPEC"}
        holding_sessions = int(context.get("horizon_bars", 1))
        if strategy_holding_period_sessions(spec.data_requirements) != holding_sessions:
            raise ValueError(
                "Strategy holding period does not match its coordinator research horizon"
            )
        train_bars, test_bars = {
            1: (40, 10),
            5: (80, 20),
            20: (160, 60),
            63: (252, 126),
            126: (378, 252),
            252: (504, 504),
        }[holding_sessions]
        as_of = datetime.fromisoformat(str(context["as_of"]))
        bars = self._verified_research_bars(context=context, as_of=as_of)
        required_bars = 21 + train_bars + test_bars
        if len(bars) < required_bars:
            return {
                "outcome": "WAITING_VALIDATION_HISTORY",
                "bar_count": len(bars),
                "required_bars": required_bars,
            }
        validation_start = bars[20].available_from
        costs = BacktestCostModel()
        initial_equity = Decimal(
            str(self.shadow.virtual_account()["initial_cash"])
        )
        expected_contract = validation_execution_contract(
            validation_subject="static_strategy",
            validated_strategy_spec_ids={
                str(spec.strategy_type): spec.strategy_spec_id
            },
            cost_model=costs,
            risk_policy=self.shadow.effective_risk_policy(),
            restriction_registry_version=self.restrictions.version,
            initial_equity=initial_equity,
            strategy_spec=spec,
        )
        expected_input = validation_input_fingerprint(
            bars=bars,
            as_of_start=validation_start,
            selection_metric="sharpe_ratio",
            train_bars=train_bars,
            test_bars=test_bars,
            step_bars=test_bars,
            embargo_bars=1,
        )
        promotion_policy = load_promotion_gate_policy(
            self.settings.research_promotion_policy_path
        )
        expected_policy_sha256 = promotion_policy_sha256(promotion_policy)
        expected_trial_count = max(
            1,
            self.research.strategy_trial_count(
                symbol=str(context["symbol"]),
                timeframe=str(context["timeframe"]),
                holding_period_sessions=holding_sessions,
            ),
        )
        prior = next(
            (
                item
                for item in self.research.recent_validation_reports(limit=500)
                if dict(item.get("validated_strategy_spec_ids") or {}).get(
                    str(spec.strategy_type)
                )
                == spec.strategy_spec_id
                and dict(item.get("execution_contract_json") or {})
                == expected_contract
                and dict(item.get("robustness_metrics") or {}).get(
                    "validation_input"
                )
                == expected_input
                and dict(item.get("gate_assessment") or {}).get("policy_sha256")
                == expected_policy_sha256
                and int(
                    dict(item.get("robustness_metrics") or {}).get(
                        "selection_search_trial_count",
                        0,
                    )
                )
                == expected_trial_count
            ),
            None,
        )
        if prior is not None:
            prior_gate = dict(prior["gate_assessment"])
            return {
                "outcome": "REUSED",
                "strategy_spec_id": spec.strategy_spec_id,
                "validation_report_id": str(prior["validation_report_id"]),
                "eligible_for_human_review": bool(
                    prior_gate.get("eligible_for_human_review")
                ),
                "eligible_for_candidate_shadow_review": bool(
                    dict(prior_gate.get("candidate_shadow") or {}).get(
                        "eligible_for_human_review"
                    )
                ),
            }
        try:
            report = WalkForwardValidator(
                self.research,
                self.ledger,
                calendar_name=self.settings.market_calendar,
                promotion_policy=promotion_policy,
                risk_policy=self.shadow.effective_risk_policy(),
                restrictions=self.restrictions,
            ).run(
                symbol=str(context["symbol"]),
                timeframe=str(context["timeframe"]),
                as_of_start=validation_start,
                as_of_end=as_of,
                code_git_sha=self.settings.source_git_sha or "UNAVAILABLE",
                train_bars=train_bars,
                test_bars=test_bars,
                step_bars=test_bars,
                embargo_bars=1,
                initial_equity=initial_equity,
                cost_model=costs,
                strategy_spec=spec,
                history_start=bars[0].event_time,
            )
        except ValueError as exc:
            return {
                "outcome": "WAITING_VALIDATION_REQUIREMENTS",
                "detail": str(exc),
            }
        return {
            "outcome": "COMPLETED",
            "strategy_spec_id": spec.strategy_spec_id,
            "validation_report_id": report.validation_report_id,
            "eligible_for_human_review": bool(
                report.gate_assessment.get("eligible_for_human_review")
            ),
            "eligible_for_candidate_shadow_review": bool(
                dict(report.gate_assessment.get("candidate_shadow") or {}).get(
                    "eligible_for_human_review"
                )
            ),
        }

    def _verified_research_bars(
        self,
        *,
        context: dict[str, Any],
        as_of: datetime,
    ) -> tuple[StockBar, ...]:
        bars = self.research.load_bars(
            symbol=str(context["symbol"]),
            timeframe=str(context["timeframe"]),
            as_of_end=as_of,
        )
        raw_start = context.get("verified_window_start")
        if raw_start is None:
            return bars
        verified_start = datetime.fromisoformat(str(raw_start)).astimezone(UTC)
        return tuple(item for item in bars if item.event_time >= verified_start)

    async def _await_shadow_adoption(self, context: dict[str, Any]) -> dict[str, Any]:
        if not context.get("validation_report_id"):
            return {"outcome": "WAITING_EXACT_VALIDATION"}
        qualified = bool(context.get("eligible_for_human_review"))
        candidate = bool(context.get("eligible_for_candidate_shadow_review"))
        if not qualified and not candidate:
            return {"outcome": "WAITING_FUTURE_RESEARCH_EVIDENCE"}
        universe = self.objects.get_list("trading-universe")
        governed = {
            str(symbol).upper()
            for symbol in (universe["members"] if universe else [])
        }
        symbol = str(context["symbol"]).upper()
        scanner_admission = self._scanner_pool_admission(
            symbol=symbol,
            scan_id=(
                str(context["universe_scan_id"])
                if context.get("universe_scan_id")
                else None
            ),
        )
        if symbol not in governed and scanner_admission is None:
            return {
                "outcome": "WAITING_TRADING_UNIVERSE_APPROVAL",
                "required_actions": ["list.replace_members"],
                "automatic_execution": False,
            }
        return {
            "outcome": "WAITING_HUMAN_CONFIRMATION",
            "required_actions": ["strategy.adopt", "shadow.start"],
            "available_admission_tiers": [
                tier
                for tier, available in (
                    ("QUALIFIED", qualified),
                    ("CANDIDATE", candidate),
                )
                if available
            ],
            "trading_pool_authority": (
                "MANUAL_TRADING_UNIVERSE"
                if symbol in governed
                else "SCANNER_LLM_TRADING_POOL"
            ),
            "scanner_admission": scanner_admission,
            "automatic_broker_orders": False,
        }

    def _scanner_pool_admission(
        self,
        *,
        symbol: str,
        scan_id: str | None,
    ) -> dict[str, Any] | None:
        if (
            not self.settings.market_scanner_auto_trading_pool_enabled
            or scan_id is None
        ):
            return None
        with self.ledger.engine.connect() as connection:
            row = connection.execute(
                select(ledger_events.c.payload)
                .where(
                    ledger_events.c.event_type == MARKET_SCAN_EVENT,
                    ledger_events.c.correlation_id == scan_id,
                )
                .order_by(ledger_events.c.sequence.desc())
                .limit(1)
            ).one_or_none()
        if row is None:
            return None
        admission = dict(dict(row.payload).get("trading_pool_admission") or {})
        if admission.get("status") not in {
            "UPDATED",
            "UNCHANGED",
            "HELD_PREVIOUS_LLM_REVIEW",
        }:
            return None
        pool = self.objects.get_list(AUTO_TRADING_POOL_SLUG)
        if pool is None or int(pool["current_revision"]) != int(
            admission.get("list_revision", -1)
        ):
            return None
        admitted = {str(value).upper() for value in admission.get("admitted_symbols", [])}
        current = {str(value).upper() for value in pool["members"]}
        if symbol not in admitted or symbol not in current:
            return None
        return {
            "scan_id": scan_id,
            "list_slug": AUTO_TRADING_POOL_SLUG,
            "list_revision": int(pool["current_revision"]),
            "basis_scan_id": admission.get("basis_scan_id"),
            "basis_llm_invocation_id": admission.get(
                "basis_llm_invocation_id"
            ),
        }
