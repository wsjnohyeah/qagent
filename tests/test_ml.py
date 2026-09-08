from __future__ import annotations

import asyncio
import hashlib
import json
import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from agentic_quant.api import create_app
from agentic_quant.coordinator_runtime import ResearchCoordinatorHandler
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
from agentic_quant.market_calendar import MarketSessionClock
from agentic_quant.migrations import upgrade_database
from agentic_quant.ml import (
    MLDatasetBuilder,
    MLPredictor,
    MLStore,
    MLTrainingExample,
    MLTrainingRequirementsError,
    WalkForwardMLTrainer,
    load_ml_policy,
)
from agentic_quant.research import FEATURE_SET_VERSION
from agentic_quant.research_store import ResearchStore


ROOT = Path(__file__).parents[1]


def _seed_snapshots(
    store: ResearchStore,
    *,
    count: int = 96,
) -> tuple[PointInTimeFeatureSnapshot, ...]:
    snapshots = []
    bars = []
    clock = MarketSessionClock("XNYS")
    sessions = clock.calendar.sessions_in_range("2025-01-02", "2025-12-31")[
        :count
    ]
    closes = [100.0 + 4.0 * math.sin(index / 3.0) + index * 0.04 for index in range(count)]
    for index, (close, session) in enumerate(zip(closes, sessions, strict=True)):
        event_time = datetime.combine(
            session.date(),
            datetime.min.time(),
            tzinfo=UTC,
        )
        as_of = clock.daily_bar_available_from(event_time)
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
                event_time=event_time,
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
    assert forecast.horizon == "1 bar"
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
    forecast_evidence = next(
        item for item in evidence.items if item.evidence_type == "ml_forecast"
    )
    model_evidence = json.loads(forecast_evidence.text)["model_evidence"]
    assert model_evidence["label"] == policy.label.model_dump(mode="json")
    assert model_evidence["final_holdout_metrics"]["sample_count"] > 0
    assert model_evidence["promotion_assessment"]["eligible_for_review"] is False
    assert len(model_evidence["training_contract_sha256"]) == 64
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


def test_training_can_pin_one_feature_version_when_legacy_rows_remain(
    settings,  # type: ignore[no-untyped-def]
) -> None:
    upgrade_database(settings.database_url)
    research = ResearchStore(EventLedger(settings.database_url).engine)
    snapshots = _seed_snapshots(research, count=40)
    legacy_as_of = snapshots[10].as_of - timedelta(minutes=1)
    legacy = research.record_feature_snapshot(
        snapshots[10].model_copy(
            update={
                "feature_snapshot_id": uuid7(),
                "as_of": legacy_as_of,
                "feature_set_version": "price_event_pit@0.1.0",
                "source_max_available_from": legacy_as_of,
                "data_hash": hashlib.sha256(b"legacy-feature-row").hexdigest(),
                "created_at": legacy_as_of,
            }
        )
    )

    examples = MLDatasetBuilder(research).build(
        symbol="AAPL",
        timeframe="1Day",
        as_of_end=snapshots[-1].as_of,
        horizon_bars=1,
        policy=load_ml_policy(ROOT / "configs/ml_policy.yaml"),
        feature_set_version="price_event_pit@0.2.0",
    )

    assert examples
    assert legacy.feature_snapshot_id not in {
        item.feature_snapshot_id for item in examples
    }


def test_training_excludes_snapshots_and_labels_before_verified_history_start(
    settings,  # type: ignore[no-untyped-def]
) -> None:
    upgrade_database(settings.database_url)
    research = ResearchStore(EventLedger(settings.database_url).engine)
    snapshots = _seed_snapshots(research, count=50)
    verified_start = snapshots[25].as_of

    examples = MLDatasetBuilder(research).build(
        symbol="AAPL",
        timeframe="1Day",
        as_of_start=verified_start,
        as_of_end=snapshots[-1].as_of,
        horizon_bars=1,
        policy=load_ml_policy(ROOT / "configs/ml_policy.yaml"),
    )

    assert examples
    assert all(item.as_of >= verified_start for item in examples)
    assert {item.feature_snapshot_id for item in examples}.isdisjoint(
        {item.feature_snapshot_id for item in snapshots[:25]}
    )


def test_coordinator_retrains_when_ml_policy_contract_changes(
    settings,  # type: ignore[no-untyped-def]
) -> None:
    upgrade_database(settings.database_url)
    ledger = EventLedger(settings.database_url)
    research = ResearchStore(ledger.engine)
    legacy = _seed_snapshots(research, count=128)
    snapshots = tuple(
        research.record_feature_snapshot(
            item.model_copy(
                update={
                    "feature_snapshot_id": uuid7(),
                    "feature_set_version": FEATURE_SET_VERSION,
                }
            )
        )
        for item in legacy
    )
    configured_policy = load_ml_policy(ROOT / "configs/ml_policy.yaml")
    base_policy = configured_policy.model_copy(
        update={
            "validation": configured_policy.validation.model_copy(
                update={"minimum_samples": 30}
            )
        }
    )
    handler = ResearchCoordinatorHandler.__new__(ResearchCoordinatorHandler)
    handler.settings = settings
    handler.research = research
    handler.ml = MLStore(ledger.engine, ledger)
    handler.ml_policy = base_policy
    context = {"feature_snapshot_id": snapshots[-1].feature_snapshot_id}

    first = asyncio.run(handler._train_ml(context))
    handler.ml_policy = base_policy.model_copy(
        update={
            "version": "ml_policy@0.2.0",
            "validation": base_policy.validation.model_copy(
                update={"embargo_bars": base_policy.validation.embargo_bars + 1}
            ),
        }
    )
    second = asyncio.run(handler._train_ml(context))

    assert first["outcome"] == "COMPLETED"
    assert second["outcome"] == "COMPLETED"
    assert second["training_run_id"] != first["training_run_id"]
    runs = handler.ml.recent_training_runs(limit=2)
    assert runs[0]["training_contract_sha256"] != runs[1][
        "training_contract_sha256"
    ]

    handler.ml_policy = handler.ml_policy.model_copy(
        update={"version": "ml_policy@0.3.0"}
    )
    with patch(
        "agentic_quant.coordinator_runtime.WalkForwardMLTrainer.train",
        side_effect=MLTrainingRequirementsError(
            "ML OOS partitions are too small after label-availability purging"
        ),
    ):
        waiting = asyncio.run(handler._train_ml(context))
    assert waiting["outcome"] == "WAITING_ML_TRAINING_REQUIREMENTS"
    assert waiting["sample_count"] > 0

    handler.ml_policy = handler.ml_policy.model_copy(
        update={"version": "ml_policy@0.4.0"}
    )
    with patch(
        "agentic_quant.coordinator_runtime.WalkForwardMLTrainer.train",
        side_effect=ValueError("unexpected model implementation defect"),
    ), pytest.raises(ValueError, match="unexpected model implementation defect"):
        asyncio.run(handler._train_ml(context))


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

    decision_time = snapshots[-1].as_of + timedelta(hours=12)
    research.record_feature_snapshot(
        snapshots[-1].model_copy(
            update={
                "feature_snapshot_id": uuid7(),
                "as_of": decision_time,
                "data_hash": hashlib.sha256(b"decision-time-snapshot").hexdigest(),
                "created_at": decision_time,
            }
        )
    )
    with_decision_snapshot = MLDatasetBuilder(research).build(
        symbol="AAPL",
        timeframe="1Day",
        as_of_end=decision_time,
        horizon_bars=1,
        policy=policy,
    )
    assert with_decision_snapshot == dense

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
    entry_open = builder.session_clock.daily_bar_session_open(bars[1].event_time)

    class PreEntrySplitStore:
        @staticmethod
        def corporate_actions_effective_between(**_kwargs):  # type: ignore[no-untyped-def]
            return (
                SimpleNamespace(
                    action_type=CorporateActionType.SPLIT,
                    split_ratio=Decimal("2"),
                    cash_amount=None,
                    effective_at=entry_open - timedelta(minutes=1),
                ),
            )

    builder.reference_data = PreEntrySplitStore()  # type: ignore[assignment]
    examples = builder.build(
        symbol="AAPL",
        timeframe="1Day",
        as_of_end=snapshots[-1].as_of,
        horizon_bars=2,
        policy=load_ml_policy(ROOT / "configs/ml_policy.yaml"),
    )
    assert examples[0].forward_return == pytest.approx(
        float(bars[2].close / bars[1].open - Decimal("1"))
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


def test_annual_ml_requires_independent_calibration_selection_and_holdout() -> None:
    policy = load_ml_policy(ROOT / "configs/ml_policy.yaml")
    trainer = WalkForwardMLTrainer.__new__(WalkForwardMLTrainer)
    trainer.policy = policy

    folds = trainer._folds(1_092, embargo_bars=252)

    assert len(folds) == 3
    assert {test_end - test_start for _, test_start, test_end in folds} == {272}
    start = datetime(2020, 1, 1, tzinfo=UTC)
    examples = tuple(
        MLTrainingExample(
            feature_snapshot_id=f"feature-{index}",
            as_of=start + timedelta(days=index),
            label_available_from=start + timedelta(days=index + 252),
            values=(float(index),),
            forward_return=0.01,
            label=1,
        )
        for index in range(1_092)
    )
    oos = tuple(
        item
        for _, test_start, test_end in folds
        for item in examples[test_start:test_end]
    )
    calibration_end, selection_start, selection_end, evaluation_start = (
        trainer._purged_oos_partitions(oos)
    )
    assert calibration_end >= 20
    assert selection_start == 272
    assert selection_end - selection_start >= 20
    assert len(oos) - evaluation_start == 272
    with pytest.raises(
        MLTrainingRequirementsError,
        match="requires at least 1092 labeled samples",
    ):
        trainer._folds(1_091, embargo_bars=252)


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
