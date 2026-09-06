from __future__ import annotations

import hashlib
import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from agentic_quant.api import create_app
from agentic_quant.document_store import DocumentStore
from agentic_quant.domain import (
    EvidencePacket,
    EvidenceReference,
    CorporateActionType,
    MLModelStatus,
    PointInTimeFeatureSnapshot,
    ResearchAnalysisRecord,
    ResearchAnalysisStatus,
    ResearchRecommendation,
    StockBar,
    StructuredResearchAnalysis,
)
from agentic_quant.ids import uuid7
from agentic_quant.intelligence import IntelligenceStore, ResearchEvidenceRetriever
from agentic_quant.ledger import EventLedger
from agentic_quant.market_store import MarketDataStore
from agentic_quant.migrations import upgrade_database
from agentic_quant.ml import (
    MLDatasetBuilder,
    MLPredictor,
    MLStore,
    WalkForwardMLTrainer,
    load_ml_policy,
)
from agentic_quant.research_store import ResearchStore


ROOT = Path(__file__).parents[1]


def _seed_snapshots(
    store: ResearchStore,
    *,
    count: int = 96,
) -> tuple[PointInTimeFeatureSnapshot, ...]:
    snapshots = []
    bars = []
    closes = [100.0 + 4.0 * math.sin(index / 3.0) + index * 0.04 for index in range(count)]
    for index, close in enumerate(closes):
        as_of = datetime(2025, 1, 1, tzinfo=UTC) + timedelta(days=index)
        digest = hashlib.sha256(f"snapshot-{index}".encode()).hexdigest()
        packet = store.record_evidence_packet(
            EvidencePacket(
                evidence_packet_id=uuid7(),
                symbol="AAPL",
                as_of=as_of,
                evidence_hash=digest,
                references=(
                    EvidenceReference(
                        evidence_type="market_bar",
                        evidence_id=f"bar-{index}",
                        event_time=as_of,
                        available_from=as_of,
                        source="fixture:sip",
                    ),
                ),
                source_max_available_from=as_of,
                created_at=as_of,
            )
        )
        return_1 = close / closes[index - 1] - 1 if index else 0.0
        return_5 = close / closes[max(0, index - 5)] - 1 if index else 0.0
        snapshot = store.record_feature_snapshot(
            PointInTimeFeatureSnapshot(
                feature_snapshot_id=uuid7(),
                evidence_packet_id=packet.evidence_packet_id,
                symbol="AAPL",
                timeframe="1Day",
                as_of=as_of,
                feature_set_version="price_event_pit@0.2.0",
                values={
                    "close": Decimal(str(close)),
                    "return_1": Decimal(str(return_1)),
                    "return_5": Decimal(str(return_5)),
                    "distance_sma_20": Decimal(str(math.sin(index / 4.0) / 20)),
                    "realized_vol_20": Decimal(str(0.15 + abs(return_1))),
                    "volume_ratio_20": Decimal(str(1.0 + (index % 7) / 20)),
                    "catalyst_count_90d": index % 3,
                    "corporate_fact_count": index % 5,
                    "corporate_action_count": 0,
                },
                source_max_available_from=as_of,
                data_hash=digest,
                created_at=as_of,
            )
        )
        snapshots.append(snapshot)
        bars.append(
            StockBar(
                bar_id=f"bar-{index}",
                symbol="AAPL",
                timeframe="1Day",
                event_time=as_of - timedelta(hours=7),
                available_from=as_of,
                open=Decimal(str(close - 0.25)),
                high=Decimal(str(close + 0.5)),
                low=Decimal(str(close - 0.5)),
                close=Decimal(str(close)),
                volume=1_000_000,
                trade_count=10_000,
                vwap=Decimal(str(close)),
                source="fixture",
                feed="test",
                raw_object_id="TEST_RAW",
                ingested_at=as_of,
            )
        )
    MarketDataStore(store.engine).insert_bars(tuple(bars), raw_object_id="TEST_RAW")
    return tuple(snapshots)


def test_walk_forward_training_calibration_drift_and_forecast(
    settings,  # type: ignore[no-untyped-def]
) -> None:
    upgrade_database(settings.database_url)
    ledger = EventLedger(settings.database_url)
    research = ResearchStore(ledger.engine)
    snapshots = _seed_snapshots(research)
    policy = load_ml_policy(ROOT / "configs/ml_policy.yaml")
    examples = MLDatasetBuilder(research).build(
        symbol="AAPL",
        timeframe="1Day",
        as_of_end=snapshots[-1].as_of,
        horizon_bars=1,
        policy=policy,
    )
    store = MLStore(ledger.engine, ledger)
    result = WalkForwardMLTrainer(
        store,
        policy,
        code_git_sha="test-sha",
    ).train(
        examples,
        symbol="AAPL",
        timeframe="1Day",
        horizon_bars=1,
        feature_set_version="price_event_pit@0.2.0",
    )

    assert len(result.models) == 2
    assert {model.kind.value for model in result.models} == {
        "logistic_regression",
        "boosted_stumps",
    }
    assert all(
        model.metrics["calibrated_final_holdout"]["sample_count"] > 0
        for model in result.models
    )
    assert all("maximum_psi" in model.drift for model in result.models)
    assert all(
        datetime.fromisoformat(fold["train_label_cutoff"])
        < datetime.fromisoformat(fold["test_start"])
        for model in result.models
        for fold in model.metrics["walk_forward_folds"]
    )
    assert all(model.status == MLModelStatus.CANDIDATE for model in result.models)
    assert all(
        "minimum_samples" in model.promotion_assessment["shortfalls"]
        for model in result.models
    )
    selected = store.model(result.run.selected_model_id)
    assert selected is not None
    forecast = MLPredictor(store).predict(model=selected, snapshot=snapshots[-1])
    replay = MLPredictor(store).predict(model=selected, snapshot=snapshots[-1])
    assert Decimal("0") <= forecast.probability_up <= Decimal("1")
    assert replay.forecast_id == forecast.forecast_id
    assert store.forecast(forecast.forecast_id) == forecast
    evidence = ResearchEvidenceRetriever(DocumentStore(ledger.engine)).retrieve(
        feature_snapshot=snapshots[-1],
        as_of=snapshots[-1].as_of,
        forecast=forecast,
    )
    assert f"FORECAST:{forecast.forecast_id}" in {
        item.citation_id for item in evidence.items
    }
    intelligence = IntelligenceStore(ledger.engine, ledger)
    analysis = ResearchAnalysisRecord(
        analysis_id=uuid7(),
        symbol="AAPL",
        as_of=snapshots[-1].as_of,
        status=ResearchAnalysisStatus.ABSTAINED,
        schema_version="research_analysis@0.2.0",
        prompt_version="test@1",
        evidence_bundle=evidence,
        feature_snapshot_id=snapshots[-1].feature_snapshot_id,
        forecast_id=forecast.forecast_id,
        analysis=StructuredResearchAnalysis(
            schema_version="research_analysis@0.2.0",
            symbol="AAPL",
            as_of=snapshots[-1].as_of,
            horizon="1 bar",
            recommendation=ResearchRecommendation.ABSTAIN,
            confidence=Decimal("0"),
            thesis="Fixture abstention.",
            ml_assessment="The model forecast was inspected.",
            abstain_reason="Fixture abstention.",
        ),
        citation_validation={"valid": True},
        code_git_sha="test-sha",
        created_at=snapshots[-1].as_of,
    )
    intelligence.record(analysis)
    graph = intelligence.decision_graph(analysis.analysis_id)
    assert graph is not None
    assert {node["type"] for node in graph["nodes"]} >= {
        "ml_training_run",
        "ml_model_version",
        "ml_forecast",
        "research_analysis",
    }
    assert store.health_summary() == {
        "ml_training_runs": 1,
        "ml_models": 2,
        "ml_forecasts": 1,
        "model_registry_events": 0,
    }
    with pytest.raises(ValueError, match="eligible challenger"):
        store.promote(
            model_id=selected.model_id,
            approved_by="human-reviewer",
            reason="test promotion",
        )


def test_ml_labels_follow_executable_bars_and_ignore_sparse_snapshot_spacing(
    settings,  # type: ignore[no-untyped-def]
) -> None:
    upgrade_database(settings.database_url)
    ledger = EventLedger(settings.database_url)
    research = ResearchStore(ledger.engine)
    snapshots = _seed_snapshots(research, count=40)
    policy = load_ml_policy(ROOT / "configs/ml_policy.yaml")
    bars = research.load_bars(
        symbol="AAPL",
        timeframe="1Day",
        as_of_end=snapshots[-1].as_of,
    )
    dense = MLDatasetBuilder(research).build(
        symbol="AAPL",
        timeframe="1Day",
        as_of_end=snapshots[-1].as_of,
        horizon_bars=1,
        policy=policy,
    )
    assert dense[0].forward_return == pytest.approx(
        float(bars[1].close / bars[1].open - Decimal("1"))
    )
    assert dense[0].label_available_from == bars[1].available_from

    class SparseSnapshotStore:
        engine = research.engine

        @staticmethod
        def feature_snapshots_for_training(**_kwargs):  # type: ignore[no-untyped-def]
            return snapshots[::3]

        @staticmethod
        def load_bars(**_kwargs):  # type: ignore[no-untyped-def]
            return bars

    sparse = MLDatasetBuilder(SparseSnapshotStore()).build(  # type: ignore[arg-type]
        symbol="AAPL",
        timeframe="1Day",
        as_of_end=snapshots[-1].as_of,
        horizon_bars=1,
        policy=policy,
    )
    assert sparse[0].label_available_from == bars[1].available_from
    assert sparse[1].label_available_from == bars[4].available_from


def test_ml_label_does_not_apply_split_that_precedes_next_open(
    settings,  # type: ignore[no-untyped-def]
) -> None:
    upgrade_database(settings.database_url)
    research = ResearchStore(EventLedger(settings.database_url).engine)
    snapshots = _seed_snapshots(research, count=24)
    bars = research.load_bars(
        symbol="AAPL",
        timeframe="1Day",
        as_of_end=snapshots[-1].as_of,
    )
    builder = MLDatasetBuilder(research)

    class PreEntrySplitStore:
        @staticmethod
        def corporate_actions_effective_between(**_kwargs):  # type: ignore[no-untyped-def]
            return (
                SimpleNamespace(
                    action_type=CorporateActionType.SPLIT,
                    split_ratio=Decimal("2"),
                    cash_amount=None,
                    effective_at=bars[1].event_time - timedelta(hours=1),
                ),
            )

    builder.reference_data = PreEntrySplitStore()  # type: ignore[assignment]
    examples = builder.build(
        symbol="AAPL",
        timeframe="1Day",
        as_of_end=snapshots[-1].as_of,
        horizon_bars=1,
        policy=load_ml_policy(ROOT / "configs/ml_policy.yaml"),
    )
    assert examples[0].forward_return == pytest.approx(
        float(bars[1].close / bars[1].open - Decimal("1"))
    )


def test_ml_oos_partitions_purge_label_overlap(
    settings,  # type: ignore[no-untyped-def]
) -> None:
    upgrade_database(settings.database_url)
    research = ResearchStore(EventLedger(settings.database_url).engine)
    snapshots = _seed_snapshots(research, count=60)
    policy = load_ml_policy(ROOT / "configs/ml_policy.yaml")
    examples = MLDatasetBuilder(research).build(
        symbol="AAPL",
        timeframe="1Day",
        as_of_end=snapshots[-1].as_of,
        horizon_bars=5,
        policy=policy,
    )
    calibration_end, selection_start, selection_end, evaluation_start = (
        WalkForwardMLTrainer._purged_oos_partitions(examples)
    )
    assert examples[calibration_end - 1].label_available_from <= examples[
        selection_start
    ].as_of
    assert examples[selection_end - 1].label_available_from <= examples[
        evaluation_start
    ].as_of


def test_registry_requires_ml_gate_and_human_approval(
    settings,  # type: ignore[no-untyped-def]
) -> None:
    upgrade_database(settings.database_url)
    ledger = EventLedger(settings.database_url)
    research = ResearchStore(ledger.engine)
    snapshots = _seed_snapshots(research)
    base_policy = load_ml_policy(ROOT / "configs/ml_policy.yaml")
    policy = base_policy.model_copy(
        update={
            "validation": base_policy.validation.model_copy(
                update={"minimum_samples": 30, "minimum_oos_samples": 10}
            ),
            "promotion": base_policy.promotion.model_copy(
                update={
                    "minimum_roc_auc": Decimal("0"),
                    "maximum_brier_score": Decimal("1"),
                    "maximum_expected_calibration_error": Decimal("1"),
                    "maximum_population_stability_index": Decimal("100"),
                }
            ),
        }
    )
    examples = MLDatasetBuilder(research).build(
        symbol="AAPL",
        timeframe="1Day",
        as_of_end=snapshots[-1].as_of,
        horizon_bars=1,
        policy=policy,
    )
    store = MLStore(ledger.engine, ledger)
    result = WalkForwardMLTrainer(store, policy).train(
        examples,
        symbol="AAPL",
        timeframe="1Day",
        horizon_bars=1,
        feature_set_version="price_event_pit@0.2.0",
    )
    challenger = store.model(result.run.selected_model_id)
    assert challenger is not None
    assert challenger.status == MLModelStatus.CHALLENGER
    with pytest.raises(ValueError, match="approver"):
        store.promote(
            model_id=challenger.model_id,
            approved_by="",
            reason="approved test",
        )
    event = store.promote(
        model_id=challenger.model_id,
        approved_by="human-reviewer",
        reason="approved test",
    )
    assert event.new_status == MLModelStatus.CHAMPION
    promoted = store.model(challenger.model_id)
    assert promoted is not None
    assert promoted.status == MLModelStatus.CHAMPION

    replacement_result = WalkForwardMLTrainer(store, policy).train(
        examples,
        symbol="AAPL",
        timeframe="1Day",
        horizon_bars=1,
        feature_set_version="price_event_pit@0.2.0",
    )
    replacement = store.model(replacement_result.run.selected_model_id)
    assert replacement is not None
    assert replacement.status == MLModelStatus.CHALLENGER
    store.promote(
        model_id=replacement.model_id,
        approved_by="human-reviewer",
        reason="replace prior champion",
    )
    retired = store.model(challenger.model_id)
    assert retired is not None
    assert retired.status == MLModelStatus.RETIRED
    promoted_replacement = store.model(replacement.model_id)
    assert promoted_replacement is not None
    assert promoted_replacement.status == MLModelStatus.CHAMPION
    assert len(store.recent_registry_events()) == 3


def test_ml_api_trains_lists_and_forecasts(settings) -> None:  # type: ignore[no-untyped-def]
    upgrade_database(settings.database_url)
    ledger = EventLedger(settings.database_url)
    snapshots = _seed_snapshots(ResearchStore(ledger.engine))
    with TestClient(create_app(settings)) as client:
        training = client.post(
            "/v1/ml/train",
            json={
                "symbol": "AAPL",
                "timeframe": "1Day",
                "as_of_end": snapshots[-1].as_of.isoformat(),
                "horizon_bars": 1,
            },
        )
        assert training.status_code == 200
        selected_model_id = training.json()["run"]["selected_model_id"]
        models = client.get("/v1/ml/models").json()
        assert len(models) == 2
        forecast = client.post(
            "/v1/ml/forecast",
            json={
                "model_id": selected_model_id,
                "feature_snapshot_id": snapshots[-1].feature_snapshot_id,
            },
        )
        assert forecast.status_code == 200
        assert forecast.json()["model_version"]
        assert len(client.get("/v1/ml/forecasts").json()) == 1
