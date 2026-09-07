from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Callable

import exchange_calendars as exchange_calendars  # type: ignore[import-untyped]

from agentic_quant.archive import RawArchive
from agentic_quant.config import AppEnvironment, Settings
from agentic_quant.control_plane import SystemObjectStore
from agentic_quant.data_quality import MarketDataQualityService
from agentic_quant.document_ingestion import DocumentIngestionService
from agentic_quant.document_store import DocumentStore
from agentic_quant.domain import BacktestCostModel, ResearchAnalysisStatus, WorkflowJob
from agentic_quant.intelligence import (
    ANALYSIS_PROMPT_VERSION,
    EvidenceBoundResearchAnalyst,
    ResearchEvidenceRetriever,
)
from agentic_quant.ledger import EventLedger
from agentic_quant.llm import LLMConfigurationError
from agentic_quant.llm_budget import LLMBudgetExceededError
from agentic_quant.market_ingestion import MarketDataIngestionService
from agentic_quant.market_store import MarketDataStore
from agentic_quant.ml import (
    MLDatasetBuilder,
    MLPredictor,
    MLPolicy,
    MLStore,
    WalkForwardMLTrainer,
    ml_dataset_sha256,
)
from agentic_quant.providers.alpaca import AlpacaMarketDataProvider
from agentic_quant.providers.base import (
    DocumentFetchRequest,
    EventPublisher,
    StockBarsRequest,
)
from agentic_quant.providers.documents import AlpacaNewsProvider
from agentic_quant.research import FEATURE_SET_VERSION, PointInTimeFeatureBuilder
from agentic_quant.research_store import ResearchStore
from agentic_quant.risk import RestrictionRegistry
from agentic_quant.shadow import ShadowRuntime
from agentic_quant.strategy_generation import HybridStrategyGenerator
from agentic_quant.validation import (
    WalkForwardValidator,
    load_promotion_gate_policy,
    validation_execution_contract,
    validation_input_fingerprint,
)


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
            }
        )
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
        stored = self.market.bars_between(
            symbol=str(context["symbol"]),
            timeframe="1Day",
            start=desired_start,
            end=as_of,
            source="alpaca",
            feed=self.settings.alpaca_stock_feed,
        )
        windows = daily_bar_gap_windows(
            start=desired_start,
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
                symbol=str(context["symbol"]),
                timeframe="1Day",
                code_git_sha=self.settings.source_git_sha or "UNAVAILABLE",
                expected_start=desired_start,
                expected_end=as_of,
            )
            return {
                "outcome": "UP_TO_DATE",
                "latest_bar_event_time": latest.isoformat() if latest else None,
                "verified_window_start": desired_start.isoformat(),
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
                        symbol=str(context["symbol"]),
                        start=window_start,
                        end=window_end,
                        timeframe="1Day",
                        feed=self.settings.alpaca_stock_feed,
                        adjustment="raw",
                    )
                )
                summaries.append(summary.model_dump(mode="json"))
        repaired = self.market.bars_between(
            symbol=str(context["symbol"]),
            timeframe="1Day",
            start=desired_start,
            end=as_of,
            source="alpaca",
            feed=self.settings.alpaca_stock_feed,
        )
        quality = MarketDataQualityService(
            self.market.engine,
            self.ledger,
            calendar_name=self.settings.market_calendar,
        ).require_bars(
            repaired,
            symbol=str(context["symbol"]),
            timeframe="1Day",
            code_git_sha=self.settings.source_git_sha or "UNAVAILABLE",
            expected_start=desired_start,
            expected_end=as_of,
        )
        return {
            "outcome": "COMPLETED",
            "gap_windows_repaired": len(windows),
            "ingestions": summaries,
            "data_quality_report_id": quality.data_quality_report_id,
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
        earliest = as_of - timedelta(
            days=self.settings.coordinator_document_lookback_days
        )
        # Re-read one day to catch provider corrections while persistence remains
        # idempotent by provider document ID and content hash.
        start = max(earliest, latest - timedelta(days=1)) if latest else earliest
        if start >= as_of:
            return {
                "outcome": "UP_TO_DATE",
                "latest_document_published_at": latest.isoformat() if latest else None,
            }
        provider = AlpacaNewsProvider(
            api_key=self.settings.alpaca_api_key.get_secret_value(),
            api_secret=self.settings.alpaca_api_secret.get_secret_value(),
            base_url=self.settings.alpaca_data_base_url,
        )
        async with provider:
            summary = await DocumentIngestionService(
                provider=provider,
                archive=self.archive,
                market_store=self.market,
                document_store=self.documents,
                ledger=self.ledger,
                publisher=self.publisher,
            ).ingest_documents(
                DocumentFetchRequest(
                    symbols=(symbol,),
                    start=start,
                    end=as_of,
                    limit=50,
                    max_pages=self.settings.coordinator_document_max_pages,
                )
            )
        return {
            "outcome": "COMPLETED",
            "document_ingestion": summary.model_dump(mode="json"),
        }

    async def _materialize_features(self, context: dict[str, Any]) -> dict[str, Any]:
        as_of = datetime.fromisoformat(str(context["as_of"]))
        bars = self.research.load_bars(
            symbol=str(context["symbol"]),
            timeframe=str(context["timeframe"]),
            as_of_end=as_of,
        )
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
        return {
            "outcome": "COMPLETED",
            "bar_count": len(bars),
            "feature_snapshot_id": latest.feature_snapshot_id,
            "feature_as_of": latest.as_of.isoformat(),
        }

    async def _train_ml(self, context: dict[str, Any]) -> dict[str, Any]:
        snapshot_id = context.get("feature_snapshot_id")
        if not snapshot_id:
            return {"outcome": "WAITING_FEATURES"}
        snapshot = self.research.feature_snapshot(str(snapshot_id))
        if snapshot is None:
            return {"outcome": "WAITING_FEATURES"}
        try:
            examples = MLDatasetBuilder(self.research).build(
                symbol=snapshot.symbol,
                timeframe=snapshot.timeframe,
                as_of_end=snapshot.as_of,
                horizon_bars=1,
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
        existing = next(
            (
                item
                for item in self.ml.recent_training_runs(limit=500)
                if item["symbol"] == snapshot.symbol
                and item["timeframe"] == snapshot.timeframe
                and int(item["horizon_bars"]) == 1
                and item["feature_set_version"] == FEATURE_SET_VERSION
                and item["dataset_sha256"] == dataset_sha256
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
            horizon_bars=1,
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
            ),
            None,
        )
        if prior is not None:
            return {
                "outcome": (
                    "REUSED"
                    if prior["status"] == ResearchAnalysisStatus.COMPLETED.value
                    else f"WAITING_ANALYSIS_{prior['status']}"
                ),
                "analysis_id": str(prior["analysis_id"]),
                "analysis_status": str(prior["status"]),
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
                else f"WAITING_ANALYSIS_{analysis.status.value}"
            ),
            "analysis_id": analysis.analysis_id,
            "analysis_status": analysis.status.value,
        }

    async def _generate_strategy(self, context: dict[str, Any]) -> dict[str, Any]:
        if context.get("analysis_status") != ResearchAnalysisStatus.COMPLETED.value:
            return {"outcome": "WAITING_COMPLETED_ANALYSIS"}
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
            return {"outcome": "WAITING_STRATEGY_SPEC"}
        spec = self.research.strategy_spec(str(spec_id))
        if spec is None:
            return {"outcome": "WAITING_STRATEGY_SPEC"}
        as_of = datetime.fromisoformat(str(context["as_of"]))
        bars = self.research.load_bars(
            symbol=str(context["symbol"]),
            timeframe=str(context["timeframe"]),
            as_of_end=as_of,
        )
        if len(bars) < 72:
            return {
                "outcome": "WAITING_VALIDATION_HISTORY",
                "bar_count": len(bars),
                "required_bars": 72,
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
        )
        expected_input = validation_input_fingerprint(
            bars=bars,
            as_of_start=validation_start,
            selection_metric="sharpe_ratio",
            train_bars=40,
            test_bars=10,
            step_bars=10,
            embargo_bars=1,
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
            ),
            None,
        )
        if prior is not None:
            return {
                "outcome": "REUSED",
                "validation_report_id": str(prior["validation_report_id"]),
                "eligible_for_human_review": bool(
                    dict(prior["gate_assessment"]).get("eligible_for_human_review")
                ),
            }
        try:
            report = WalkForwardValidator(
                self.research,
                self.ledger,
                calendar_name=self.settings.market_calendar,
                promotion_policy=load_promotion_gate_policy(
                    self.settings.research_promotion_policy_path
                ),
                risk_policy=self.shadow.effective_risk_policy(),
                restrictions=self.restrictions,
            ).run(
                symbol=str(context["symbol"]),
                timeframe=str(context["timeframe"]),
                as_of_start=validation_start,
                as_of_end=as_of,
                code_git_sha=self.settings.source_git_sha or "UNAVAILABLE",
                train_bars=40,
                test_bars=10,
                step_bars=10,
                embargo_bars=1,
                initial_equity=initial_equity,
                cost_model=costs,
                strategy_spec=spec,
            )
        except ValueError as exc:
            return {
                "outcome": "WAITING_VALIDATION_REQUIREMENTS",
                "detail": str(exc),
            }
        return {
            "outcome": "COMPLETED",
            "validation_report_id": report.validation_report_id,
            "eligible_for_human_review": bool(
                report.gate_assessment.get("eligible_for_human_review")
            ),
        }

    async def _await_shadow_adoption(self, context: dict[str, Any]) -> dict[str, Any]:
        if not context.get("validation_report_id"):
            return {"outcome": "WAITING_EXACT_VALIDATION"}
        if not context.get("eligible_for_human_review"):
            return {"outcome": "WAITING_FUTURE_RESEARCH_EVIDENCE"}
        return {
            "outcome": "WAITING_HUMAN_CONFIRMATION",
            "required_actions": ["strategy.adopt", "shadow.start"],
            "automatic_broker_orders": False,
        }
