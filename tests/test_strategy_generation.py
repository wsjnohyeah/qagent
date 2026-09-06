from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
import json
from types import SimpleNamespace

from agentic_quant.domain import (
    AnalystClaim,
    EvidencePacket,
    EvidenceQuote,
    EvidenceReference,
    Forecast,
    PointInTimeFeatureSnapshot,
    ResearchAnalysisRecord,
    ResearchAnalysisStatus,
    ResearchEvidenceBundle,
    ResearchEvidenceItem,
    ResearchRecommendation,
    StructuredResearchAnalysis,
)
from agentic_quant.ids import uuid7
from agentic_quant.intelligence import IntelligenceStore
from agentic_quant.ledger import EventLedger
from agentic_quant.migrations import upgrade_database
from agentic_quant.ml import MLStore
from agentic_quant.research import FEATURE_SET_VERSION
from agentic_quant.research_store import ResearchStore
from agentic_quant.strategy_generation import HybridStrategyGenerator


def test_hybrid_generator_compiles_only_a_bounded_research_spec(
    settings,  # type: ignore[no-untyped-def]
) -> None:
    upgrade_database(settings.database_url)
    ledger = EventLedger(settings.database_url)
    research = ResearchStore(ledger.engine)
    ml = MLStore(ledger.engine)
    intelligence = IntelligenceStore(ledger.engine)
    as_of = datetime(2026, 9, 5, 20, tzinfo=UTC)
    packet = research.record_evidence_packet(
        EvidencePacket(
            evidence_packet_id=uuid7(),
            symbol="AAPL",
            as_of=as_of,
            evidence_hash="a" * 64,
            references=(
                EvidenceReference(
                    evidence_type="market_bar",
                    evidence_id="bar-1",
                    event_time=as_of,
                    available_from=as_of,
                    source="fixture",
                ),
            ),
            source_max_available_from=as_of,
            created_at=as_of,
        )
    )
    snapshot = research.record_feature_snapshot(
        PointInTimeFeatureSnapshot(
            feature_snapshot_id=uuid7(),
            evidence_packet_id=packet.evidence_packet_id,
            symbol="AAPL",
            timeframe="1Day",
            as_of=as_of,
            feature_set_version=FEATURE_SET_VERSION,
            values={"return_5": Decimal("0.03"), "sma_20": Decimal("100")},
            source_max_available_from=as_of,
            data_hash="b" * 64,
            created_at=as_of,
        )
    )
    forecast = ml.record_forecast(
        "fixture-model",
        Forecast(
            forecast_id=uuid7(),
            symbol="AAPL",
            as_of=as_of,
            horizon="1 bar",
            expected_return=Decimal("0.01"),
            probability_up=Decimal("0.64"),
            uncertainty=Decimal("0.20"),
            model_version="fixture-model@1",
            training_data_cutoff=as_of,
            feature_snapshot_id=snapshot.feature_snapshot_id,
            created_at=as_of,
        ),
    )
    evidence = ResearchEvidenceItem(
        citation_id="DOC:one",
        evidence_type="source_document",
        event_time=as_of,
        available_from=as_of,
        source="fixture",
        text="Revenue increased 10%.",
        content_sha256="c" * 64,
    )
    analysis = ResearchAnalysisRecord(
        analysis_id=uuid7(),
        symbol="AAPL",
        as_of=as_of,
        status=ResearchAnalysisStatus.COMPLETED,
        schema_version="research_analysis@0.2.0",
        prompt_version="fixture@1",
        evidence_bundle=ResearchEvidenceBundle(
            symbol="AAPL",
            as_of=as_of,
            feature_snapshot_id=snapshot.feature_snapshot_id,
            forecast_id=forecast.forecast_id,
            items=(evidence,),
            evidence_bundle_hash="d" * 64,
        ),
        feature_snapshot_id=snapshot.feature_snapshot_id,
        forecast_id=forecast.forecast_id,
        analysis=StructuredResearchAnalysis(
            schema_version="research_analysis@0.2.0",
            symbol="AAPL",
            as_of=as_of,
            horizon="1 bar",
            recommendation=ResearchRecommendation.RESEARCH_LONG,
            confidence=Decimal("0.6"),
            thesis="Evidence and ML agree directionally.",
            claims=(
                AnalystClaim(
                    claim="Revenue increased 10%.",
                    citations=("DOC:one",),
                    evidence_quotes=(
                        EvidenceQuote(
                            citation_id="DOC:one",
                            quote="Revenue increased 10%.",
                        ),
                    ),
                ),
            ),
            risk_factors=("Bounded fixture",),
            ml_assessment="Probability up is above one half.",
        ),
        llm_invocation_id=None,
        citation_validation={"valid": True},
        code_git_sha="test-sha",
        created_at=as_of,
    )
    intelligence.record(analysis)
    evidence_ids = sorted(
        (
            f"FEATURE:{snapshot.feature_snapshot_id}",
            f"ANALYSIS:{analysis.analysis_id}",
            f"FORECAST:{forecast.forecast_id}",
        )
    )

    class FakeGateway:
        calls = 0

        async def complete(self, request, provider_override=None):  # type: ignore[no-untyped-def]
            del provider_override
            self.calls += 1
            if self.calls == 1:
                assert request.workload.value == "strategy_generation"
                output = {
                    "schema_version": "strategy_proposal@0.1.0",
                    "strategy_type": "momentum",
                    "timeframe": "1Day",
                    "return_window": 5,
                    "slow_window": 20,
                    "threshold": "0.01",
                    "thesis": "Use corroborated trend and evidence as a research candidate.",
                    "evidence_ids": evidence_ids,
                }
            else:
                assert request.workload.value == "strategy_critique"
                output = {
                    "schema_version": "strategy_critique@0.1.0",
                    "verdict": "ACCEPT",
                    "reasons": ["Parameters are bounded and evidence IDs are complete."],
                    "evidence_ids": evidence_ids,
                }
            return SimpleNamespace(
                invocation_id=f"invocation-{self.calls}",
                output_text=json.dumps(output),
            )

    result = asyncio.run(
        HybridStrategyGenerator(  # type: ignore[arg-type]
            FakeGateway(),
            research,
            intelligence,
            ml,
            ledger,
            code_git_sha="test-sha",
        ).generate(
            feature_snapshot_id=snapshot.feature_snapshot_id,
            analysis_id=analysis.analysis_id,
            forecast_id=forecast.forecast_id,
        )
    )
    spec = result["strategy_spec"]
    assert result["status"] == "ACCEPT"
    assert result["automatic_adoption"] is False
    assert spec["parameters"] == {
        "return_window": 5,
        "slow_window": 20,
        "minimum_return": "0.01",
    }
    assert spec["data_requirements"]["origin"] == (
        "hybrid_ml_llm_constrained_dsl"
    )
    assert ledger.by_correlation_id(spec["strategy_spec_id"])[-1][
        "event_type"
    ] == "research.strategy_candidate.compiled.v1"
