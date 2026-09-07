from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import insert

from agentic_quant.database import validation_reports
from agentic_quant.document_store import DocumentStore
from agentic_quant.domain import (
    EvidencePacket,
    EvidenceReference,
    LLMProviderName,
    LLMInvocationStatus,
    LLMUsage,
    LLMWorkload,
    PointInTimeFeatureSnapshot,
    ResearchAnalysisStatus,
    SourceDocument,
    SourceTier,
)
from agentic_quant.ids import uuid7
from agentic_quant.intelligence import (
    ANALYSIS_SCHEMA_VERSION,
    EvidenceBoundResearchAnalyst,
    IntelligenceStore,
    ResearchEvidenceRetriever,
)
from agentic_quant.ledger import EventLedger
from agentic_quant.llm import (
    LLMGateway,
    LLMProviderResult,
    LLMRequest,
    LLMRoutingConfig,
    load_llm_routing_config,
)
from agentic_quant.llm_budget import (
    LLMBudgetExceededError,
    LLMBudgetLimit,
    LLMBudgetManager,
    load_llm_budget_policy,
)
from agentic_quant.llm_store import LLMStore
from agentic_quant.migrations import upgrade_database
from agentic_quant.research_store import ResearchStore


ROOT = Path(__file__).parents[1]
AS_OF = datetime(2026, 9, 3, 20, tzinfo=UTC)


def _feature(store: ResearchStore) -> PointInTimeFeatureSnapshot:
    reference = EvidenceReference(
        evidence_type="market_bar",
        evidence_id="bar-1",
        event_time=AS_OF - timedelta(days=1),
        available_from=AS_OF - timedelta(days=1),
        source="fixture:sip",
    )
    packet = store.record_evidence_packet(
        EvidencePacket(
            evidence_packet_id=uuid7(),
            symbol="AAPL",
            as_of=AS_OF,
            evidence_hash="a" * 64,
            references=(reference,),
            source_max_available_from=reference.available_from,
            created_at=AS_OF,
        )
    )
    return store.record_feature_snapshot(
        PointInTimeFeatureSnapshot(
            feature_snapshot_id=uuid7(),
            evidence_packet_id=packet.evidence_packet_id,
            symbol="AAPL",
            timeframe="1Day",
            as_of=AS_OF,
            feature_set_version="fixture@1.0.0",
            values={"return_5": Decimal("0.03"), "realized_vol_20": Decimal("0.2")},
            source_max_available_from=reference.available_from,
            data_hash="b" * 64,
            created_at=AS_OF,
        )
    )


def _document(*, body: str, ingested_at: datetime) -> SourceDocument:
    return SourceDocument(
        document_id=uuid7(),
        provider_document_id="apple-update",
        provider="fixture_ir",
        canonical_url="https://example.invalid/apple-update",
        source_kind="ir_release",
        source_tier=SourceTier.PRIMARY,
        publisher="Apple Inc.",
        title="Apple issues operating update",
        summary=body,
        body_text=body,
        symbols=("AAPL",),
        issuer_name="Apple Inc.",
        cik="0000320193",
        published_at=AS_OF - timedelta(hours=2),
        updated_at=ingested_at,
        ingested_at=ingested_at,
        raw_object_id="RAW_FIXTURE",
    )


class StructuredProvider:
    def __init__(
        self,
        routing: LLMRoutingConfig,
        *,
        invalid_citation: bool = False,
        contradictory_claim: bool = False,
    ) -> None:
        self.name = LLMProviderName.OPENAI
        self.config = routing.providers[self.name]
        self.invalid_citation = invalid_citation
        self.contradictory_claim = contradictory_claim
        self.calls = 0

    async def complete(self, request: LLMRequest) -> LLMProviderResult:
        self.calls += 1
        payload = json.loads(request.input_text)
        citations = [item["citation_id"] for item in payload["evidence"]]
        claim_citations = ["DOC:invented"] if self.invalid_citation else citations[:1]
        quote_id = claim_citations[0]
        quote_text = payload["evidence"][0]["text"]
        return LLMProviderResult(
            response_id="response-structured",
            output_text=json.dumps(
                {
                    "schema_version": ANALYSIS_SCHEMA_VERSION,
                    "symbol": payload["symbol"],
                    "as_of": payload["as_of"],
                    "horizon": payload["horizon"],
                    "recommendation": "HOLD",
                    "confidence": "0.65",
                    "thesis": "The evidence supports caution.",
                    "claims": [
                        {
                            "claim": (
                                "Revenue increased 50%."
                                if self.contradictory_claim
                                else quote_text
                            ),
                            "citations": claim_citations,
                            "evidence_quotes": [
                                {"citation_id": quote_id, "quote": quote_text}
                            ],
                        }
                    ],
                    "risk_factors": ["The sample is bounded."],
                    "ml_assessment": None,
                    "abstain_reason": None,
                }
            ),
            usage=LLMUsage(input_tokens=120, output_tokens=80, total_tokens=200),
        )

    async def aclose(self) -> None:
        return None


def _services(
    settings,
    *,
    invalid_citation: bool = False,
    contradictory_claim: bool = False,
):  # type: ignore[no-untyped-def]
    upgrade_database(settings.database_url)
    ledger = EventLedger(settings.database_url)
    research_store = ResearchStore(ledger.engine)
    document_store = DocumentStore(ledger.engine)
    routing = load_llm_routing_config(ROOT / "configs/model_routing.yaml")
    provider = StructuredProvider(
        routing,
        invalid_citation=invalid_citation,
        contradictory_claim=contradictory_claim,
    )
    budget = LLMBudgetManager(
        ledger.engine,
        load_llm_budget_policy(ROOT / "configs/llm_budget.yaml"),
    )
    gateway = LLMGateway(
        routing=routing,
        providers={LLMProviderName.OPENAI: provider},
        store=LLMStore(ledger.engine),
        ledger=ledger,
        budget_manager=budget,
        code_git_sha="test-sha",
    )
    intelligence = IntelligenceStore(ledger.engine, ledger)
    return (
        ledger,
        research_store,
        document_store,
        provider,
        budget,
        gateway,
        intelligence,
    )


def test_retrieval_is_point_in_time_and_analysis_is_citation_bound(
    settings,  # type: ignore[no-untyped-def]
) -> None:
    (
        _,
        research_store,
        document_store,
        provider,
        budget,
        gateway,
        intelligence,
    ) = _services(settings)
    feature = _feature(research_store)
    document_store.upsert_document(
        _document(body="Known before cutoff", ingested_at=AS_OF - timedelta(hours=1)),
        "RAW_OLD",
    )
    document_store.upsert_document(
        _document(body="Future correction", ingested_at=AS_OF + timedelta(hours=1)),
        "RAW_FUTURE",
    )
    bundle = ResearchEvidenceRetriever(document_store).retrieve(
        feature_snapshot=feature,
        as_of=AS_OF,
    )

    assert any("Known before cutoff" in item.text for item in bundle.items)
    assert all("Future correction" not in item.text for item in bundle.items)
    record = asyncio.run(
        EvidenceBoundResearchAnalyst(
            gateway,
            intelligence,
            code_git_sha="test-sha",
        ).analyze(bundle)
    )

    assert provider.calls == 1
    assert record.status == ResearchAnalysisStatus.COMPLETED
    assert record.citation_validation["valid"] is True
    assert intelligence.get(record.analysis_id) == record
    graph = intelligence.decision_graph(record.analysis_id)
    assert graph is not None
    assert graph["schema_version"] == "ai_infrastructure_graph@0.1.0"
    assert any(edge["relationship"] == "generated" for edge in graph["edges"])
    budget_summary = budget.summary()
    assert budget_summary["reservation_counts"] == {"SETTLED": 1}
    assert budget_summary["limits"]["project_monthly"] == {
        "max_estimated_cost_usd": "200.00",
    }


def test_retrieval_includes_only_point_in_time_outcome_feedback(
    settings,  # type: ignore[no-untyped-def]
) -> None:
    upgrade_database(settings.database_url)
    ledger = EventLedger(settings.database_url)
    research = ResearchStore(ledger.engine)
    document_store = DocumentStore(ledger.engine)
    feature = _feature(research)
    common = {
        "symbol": "AAPL",
        "timeframe": "1Day",
        "strategy_types": ["momentum"],
        "validation_subject": "static_strategy",
        "validated_strategy_spec_ids": {"momentum": "strategy-1"},
        "execution_contract_json": {},
        "execution_contract_sha256": "c" * 64,
        "selection_metric": "sharpe_ratio",
        "train_bars": 40,
        "test_bars": 10,
        "step_bars": 10,
        "embargo_bars": 1,
        "aggregate_metrics": {"mean_test_sharpe": "0.5"},
        "regime_metrics": {},
        "robustness_metrics": {},
        "gate_assessment": {"eligible_for_human_review": False},
        "code_git_sha": "test-sha",
    }
    with ledger.engine.begin() as connection:
        connection.execute(
            insert(validation_reports),
            [
                {
                    **common,
                    "validation_report_id": "known-report",
                    "report_hash": "d" * 64,
                    "created_at": AS_OF - timedelta(minutes=1),
                },
                {
                    **common,
                    "validation_report_id": "future-report",
                    "report_hash": "e" * 64,
                    "created_at": AS_OF + timedelta(minutes=1),
                },
            ],
        )

    bundle = ResearchEvidenceRetriever(document_store).retrieve(
        feature_snapshot=feature,
        as_of=AS_OF,
    )
    feedback = next(
        item for item in bundle.items if item.evidence_type == "research_outcome_feedback"
    )
    assert feedback.citation_id.startswith("OUTCOMES:AAPL:")
    assert "known-report" in feedback.text
    assert "future-report" not in feedback.text


def test_unknown_citation_rejects_llm_output(
    settings,  # type: ignore[no-untyped-def]
) -> None:
    _, research_store, document_store, _, _, gateway, intelligence = _services(
        settings,
        invalid_citation=True,
    )
    feature = _feature(research_store)
    document_store.upsert_document(
        _document(body="Known before cutoff", ingested_at=AS_OF - timedelta(hours=1)),
        "RAW_OLD",
    )
    bundle = ResearchEvidenceRetriever(document_store).retrieve(
        feature_snapshot=feature,
        as_of=AS_OF,
    )

    record = asyncio.run(
        EvidenceBoundResearchAnalyst(gateway, intelligence).analyze(bundle)
    )
    assert record.status == ResearchAnalysisStatus.REJECTED
    assert record.analysis is None
    assert record.rejection_reason == "structured_output_validation_failed"


def test_failed_llm_invocation_is_not_cached_as_research_rejection(
    settings,  # type: ignore[no-untyped-def]
) -> None:
    upgrade_database(settings.database_url)
    ledger = EventLedger(settings.database_url)
    research = ResearchStore(ledger.engine)
    documents = DocumentStore(ledger.engine)
    feature = _feature(research)
    documents.upsert_document(
        _document(
            body="Known before cutoff",
            ingested_at=AS_OF - timedelta(hours=1),
        ),
        "RAW_OLD",
    )
    bundle = ResearchEvidenceRetriever(documents).retrieve(
        feature_snapshot=feature,
        as_of=AS_OF,
    )

    class FailedGateway:
        calls = 0

        async def complete(self, request):  # type: ignore[no-untyped-def]
            del request
            self.calls += 1
            return SimpleNamespace(
                invocation_id=uuid7(),
                status=LLMInvocationStatus.FAILED,
                error_code="upstream_transport_error",
                output_text=None,
            )

    gateway = FailedGateway()
    analyst = EvidenceBoundResearchAnalyst(  # type: ignore[arg-type]
        gateway,
        IntelligenceStore(ledger.engine),
    )
    for _ in range(2):
        with pytest.raises(RuntimeError, match="upstream_transport_error"):
            asyncio.run(analyst.analyze(bundle))

    assert gateway.calls == 2
    assert analyst.store.health_summary()["research_analyses"] == 0


def test_citation_id_does_not_authorize_a_contradictory_claim(
    settings,  # type: ignore[no-untyped-def]
) -> None:
    _, research_store, document_store, _, _, gateway, intelligence = _services(
        settings,
        contradictory_claim=True,
    )
    feature = _feature(research_store)
    document_store.upsert_document(
        _document(
            body="Revenue decreased 10%.",
            ingested_at=AS_OF - timedelta(hours=1),
        ),
        "RAW_OLD",
    )
    bundle = ResearchEvidenceRetriever(document_store).retrieve(
        feature_snapshot=feature,
        as_of=AS_OF,
    )

    record = asyncio.run(
        EvidenceBoundResearchAnalyst(gateway, intelligence).analyze(bundle)
    )
    assert record.status == ResearchAnalysisStatus.REJECTED
    assert record.rejection_reason == "structured_output_validation_failed"


def test_analyst_abstains_without_independent_evidence(
    settings,  # type: ignore[no-untyped-def]
) -> None:
    _, research_store, document_store, provider, _, gateway, intelligence = _services(
        settings
    )
    bundle = ResearchEvidenceRetriever(document_store).retrieve(
        feature_snapshot=_feature(research_store),
        as_of=AS_OF,
    )
    record = asyncio.run(
        EvidenceBoundResearchAnalyst(gateway, intelligence).analyze(bundle)
    )
    assert record.status == ResearchAnalysisStatus.ABSTAINED
    assert record.llm_invocation_id is None
    assert provider.calls == 0


def test_budget_breaker_blocks_before_provider_call(
    settings,  # type: ignore[no-untyped-def]
) -> None:
    upgrade_database(settings.database_url)
    ledger = EventLedger(settings.database_url)
    routing = load_llm_routing_config(ROOT / "configs/model_routing.yaml")
    provider = StructuredProvider(routing)
    base_policy = load_llm_budget_policy(ROOT / "configs/llm_budget.yaml")
    tiny_limit = LLMBudgetLimit(
        max_estimated_cost_usd=Decimal("0.000001"),
    )
    policy = base_policy.model_copy(
        update={
            "limits": base_policy.limits.model_copy(
                update={"project_daily": tiny_limit}
            )
        }
    )
    gateway = LLMGateway(
        routing=routing,
        providers={LLMProviderName.OPENAI: provider},
        store=LLMStore(ledger.engine),
        budget_manager=LLMBudgetManager(ledger.engine, policy),
    )

    with pytest.raises(LLMBudgetExceededError, match="project"):
        asyncio.run(
            gateway.complete(
                LLMRequest(
                    workload="critical_research",  # type: ignore[arg-type]
                    prompt_version="test@1",
                    instructions="Return JSON.",
                    input_text="Evidence.",
                    max_output_tokens=10,
                )
            )
        )
    assert provider.calls == 0
    assert LLMStore(ledger.engine).health_summary()["llm_invocations"] == 1


def test_workload_budget_revision_is_immutable_and_does_not_reset_usage(
    settings,  # type: ignore[no-untyped-def]
) -> None:
    upgrade_database(settings.database_url)
    ledger = EventLedger(settings.database_url)
    policy = load_llm_budget_policy(ROOT / "configs/llm_budget.yaml")
    budget = LLMBudgetManager(ledger.engine, policy, ledger=ledger)
    now = datetime.now(UTC)
    budget.reserve(
        invocation_id="budget-before-revision",
        provider=LLMProviderName.META,
        workload=LLMWorkload.INTERACTIVE_EXPLANATION,
        input_text="x",
        instructions="x",
        max_output_tokens=5,
        now=now,
    )
    budget.settle(
        invocation_id="budget-before-revision",
        usage=LLMUsage(input_tokens=3, output_tokens=2, total_tokens=5),
        now=now,
    )
    revised = dict(policy.limits.workload_daily)
    revised[LLMWorkload.INTERACTIVE_EXPLANATION] = LLMBudgetLimit(
        max_estimated_cost_usd=Decimal("0.000007"),
    )

    preview = budget.preview_workload_limits(revised)
    assert preview["before"]["interactive_explanation"][
        "max_estimated_cost_usd"
    ] == "5.00"
    assert preview["after"]["interactive_explanation"][
        "max_estimated_cost_usd"
    ] == "0.000007"
    revision = budget.activate_workload_limits(
        raw_limits=revised,
        reason="Lower interactive test budget",
        created_by="operator",
        now=now,
    )

    summary = budget.summary()
    assert summary["policy_source"] == "control_center"
    assert summary["active_revision_id"] == revision["budget_revision_id"]
    assert summary["limits"]["workload_daily"]["interactive_explanation"][
        "max_estimated_cost_usd"
    ] == "0.000007"
    workload_window = next(
        item
        for item in summary["windows"]
        if item["scope"] == "workload:interactive_explanation"
    )
    assert workload_window["consumed_tokens"] == 5
    assert workload_window["consumed_estimated_cost_usd"] == "0.000006"
    assert workload_window["estimated_cost_limit_usd"] == "0.000007"
    assert "token_limit" not in workload_window
    assert len(budget.recent_revisions()) == 1
    assert ledger.by_correlation_id(revision["budget_revision_id"])[-1][
        "event_type"
    ] == "llm.budget.activated.v1"

    same_version_new_hash = policy.model_copy(
        update={
            "reservation": policy.reservation.model_copy(
                update={"input_bytes_per_token": 2}
            )
        }
    )
    compatible_summary = LLMBudgetManager(
        ledger.engine,
        same_version_new_hash,
    ).summary()
    assert compatible_summary["policy_source"] == "control_center"
    assert compatible_summary["limits"]["workload_daily"][
        "interactive_explanation"
    ]["max_estimated_cost_usd"] == "0.000007"

    changed_policy = policy.model_copy(update={"version": "llm_budget@0.1.1"})
    changed_summary = LLMBudgetManager(ledger.engine, changed_policy).summary()
    assert changed_summary["policy_source"] == "yaml_base"
    assert changed_summary["limits"]["workload_daily"][
        "interactive_explanation"
    ]["max_estimated_cost_usd"] == "5.00"

    with pytest.raises(LLMBudgetExceededError, match="interactive_explanation"):
        budget.reserve(
            invocation_id="budget-after-revision",
            provider=LLMProviderName.META,
            workload=LLMWorkload.INTERACTIVE_EXPLANATION,
            input_text="x",
            instructions="x",
            max_output_tokens=5,
            now=now,
        )
