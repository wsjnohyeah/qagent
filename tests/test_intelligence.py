from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from agentic_quant.document_store import DocumentStore
from agentic_quant.domain import (
    EvidencePacket,
    EvidenceReference,
    LLMProviderName,
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
    ) -> None:
        self.name = LLMProviderName.OPENAI
        self.config = routing.providers[self.name]
        self.invalid_citation = invalid_citation
        self.calls = 0

    async def complete(self, request: LLMRequest) -> LLMProviderResult:
        self.calls += 1
        payload = json.loads(request.input_text)
        citations = [item["citation_id"] for item in payload["evidence"]]
        claim_citations = ["DOC:invented"] if self.invalid_citation else citations
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
                            "claim": "The issuer published a recent operating update.",
                            "citations": claim_citations,
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


def _services(settings, *, invalid_citation: bool = False):  # type: ignore[no-untyped-def]
    upgrade_database(settings.database_url)
    ledger = EventLedger(settings.database_url)
    research_store = ResearchStore(ledger.engine)
    document_store = DocumentStore(ledger.engine)
    routing = load_llm_routing_config(ROOT / "configs/model_routing.yaml")
    provider = StructuredProvider(routing, invalid_citation=invalid_citation)
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
        "max_tokens": 5_000_000,
        "max_estimated_cost_usd": "200.00",
    }


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
        max_tokens=1,
        max_estimated_cost_usd=Decimal("100"),
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
        max_tokens=10,
        max_estimated_cost_usd=Decimal("5"),
    )

    preview = budget.preview_workload_limits(revised)
    assert preview["before"]["interactive_explanation"]["max_tokens"] == 150_000
    assert preview["after"]["interactive_explanation"]["max_tokens"] == 10
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
        "max_tokens"
    ] == 10
    workload_window = next(
        item
        for item in summary["windows"]
        if item["scope"] == "workload:interactive_explanation"
    )
    assert workload_window["consumed_tokens"] == 5
    assert workload_window["token_limit"] == 10
    assert len(budget.recent_revisions()) == 1
    assert ledger.by_correlation_id(revision["budget_revision_id"])[-1][
        "event_type"
    ] == "llm.budget.activated.v1"

    changed_policy = policy.model_copy(update={"version": "llm_budget@0.1.1"})
    changed_summary = LLMBudgetManager(ledger.engine, changed_policy).summary()
    assert changed_summary["policy_source"] == "yaml_base"
    assert changed_summary["limits"]["workload_daily"][
        "interactive_explanation"
    ]["max_tokens"] == 150_000

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
