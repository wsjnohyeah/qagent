from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

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
from agentic_quant.strategy_generation import (
    ConstrainedStrategyProposal,
    HybridStrategyGenerator,
)


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
            if request.workload.value == "strategy_generation":
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

    generator = HybridStrategyGenerator(  # type: ignore[arg-type]
        FakeGateway(),
        research,
        intelligence,
        ml,
        ledger,
        code_git_sha="test-sha",
    )
    result = asyncio.run(
        generator.generate(
            feature_snapshot_id=snapshot.feature_snapshot_id,
            analysis_id=analysis.analysis_id,
            forecast_id=forecast.forecast_id,
        )
    )
    retry = asyncio.run(
        generator.generate(
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
    assert retry["strategy_spec"]["strategy_spec_id"] == spec["strategy_spec_id"]
    assert "generation_invocation_id" not in spec["data_requirements"]
    attempts = research.generation_attempts()
    assert len(attempts) == 2
    assert {attempt["status"] for attempt in attempts} == {"ACCEPT"}
    assert len({attempt["generation_invocation_id"] for attempt in attempts}) == 2
    assert ledger.by_correlation_id(spec["strategy_spec_id"])[-1][
        "event_type"
    ] == "research.strategy_candidate.compiled.v1"

    class UnsupportedFieldGateway:
        async def complete(self, request, provider_override=None):  # type: ignore[no-untyped-def]
            del request, provider_override
            return SimpleNamespace(
                invocation_id="unsupported-generation",
                output_text=json.dumps(
                    {
                        **result["proposal"],
                        "stop_loss": "0.01",
                    }
                ),
            )

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        asyncio.run(
            HybridStrategyGenerator(  # type: ignore[arg-type]
                UnsupportedFieldGateway(),
                research,
                intelligence,
                ml,
            ).generate(
                feature_snapshot_id=snapshot.feature_snapshot_id,
                analysis_id=analysis.analysis_id,
                forecast_id=forecast.forecast_id,
            )
        )
    failed = research.generation_attempts(limit=1)[0]
    assert failed["status"] == "FAILED"
    assert failed["proposal_json"]["stop_loss"] == "0.01"


def test_generated_strategy_dsl_rejects_unsupported_fields() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ConstrainedStrategyProposal.model_validate(
            {
                "schema_version": "strategy_proposal@0.1.0",
                "strategy_type": "momentum",
                "timeframe": "1Day",
                "return_window": 5,
                "slow_window": 20,
                "threshold": "0.01",
                "thesis": "A sufficiently long bounded test thesis.",
                "evidence_ids": ("FEATURE:x", "ANALYSIS:y", "FORECAST:z"),
                "stop_loss": "0.01",
            }
        )
