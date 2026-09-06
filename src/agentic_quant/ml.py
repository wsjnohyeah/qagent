from __future__ import annotations

import hashlib
import json
import math
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Self

from pydantic import Field, model_validator
from sqlalchemy import Engine, and_, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
import yaml

from agentic_quant.database import (
    ml_forecasts,
    ml_models,
    ml_training_runs,
    model_registry_events,
)
from agentic_quant.domain import (
    CorporateActionType,
    EventEnvelope,
    Forecast,
    FrozenModel,
    MLModelKind,
    MLModelStatus,
    MLModelVersion,
    MLTrainingRun,
    ModelRegistryEvent,
    PointInTimeFeatureSnapshot,
)
from agentic_quant.ids import uuid7
from agentic_quant.ledger import EventLedger
from agentic_quant.market_calendar import MarketSessionClock
from agentic_quant.reference_data import ReferenceDataStore
from agentic_quant.research_store import ResearchStore


class MLValidationPolicy(FrozenModel):
    minimum_samples: int = Field(ge=30)
    minimum_oos_samples: int = Field(ge=10)
    minimum_folds: int = Field(ge=2)
    embargo_bars: int = Field(ge=1)
    test_fraction: Decimal = Field(gt=0, lt=0.5)


class MLPromotionPolicy(FrozenModel):
    minimum_roc_auc: Decimal = Field(ge=0, le=1)
    maximum_brier_score: Decimal = Field(gt=0, le=1)
    maximum_expected_calibration_error: Decimal = Field(ge=0, le=1)
    maximum_population_stability_index: Decimal = Field(ge=0)
    require_human_approval: bool = True
    downstream_strategy_requires_research_gate: bool = True


class MLLabelPolicy(FrozenModel):
    positive_return_threshold: Decimal


class MLPolicy(FrozenModel):
    version: str = Field(pattern=r"^ml_policy@[0-9]+\.[0-9]+\.[0-9]+$")
    features: tuple[str, ...] = Field(min_length=1)
    label: MLLabelPolicy
    validation: MLValidationPolicy
    promotion: MLPromotionPolicy
    automatic_promotion: bool

    @model_validator(mode="after")
    def promotion_is_never_automatic(self) -> Self:
        if self.automatic_promotion:
            raise ValueError("Automatic ML model promotion is prohibited")
        return self


class MLTrainingExample(FrozenModel):
    feature_snapshot_id: str
    as_of: datetime
    label_available_from: datetime
    values: tuple[float, ...]
    forward_return: float
    label: int = Field(ge=0, le=1)


class MLTrainingResult(FrozenModel):
    run: MLTrainingRun
    models: tuple[MLModelVersion, ...]


def load_ml_policy(path: Path) -> MLPolicy:
    with path.open("r", encoding="utf-8") as handle:
        return MLPolicy.model_validate(yaml.safe_load(handle))


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def ml_dataset_sha256(examples: tuple[MLTrainingExample, ...]) -> str:
    """Return the stable identity used for an exact point-in-time training set."""
    return _canonical_hash([item.model_dump(mode="json") for item in examples])


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _sigmoid(value: float) -> float:
    bounded = max(-35.0, min(35.0, value))
    return 1.0 / (1.0 + math.exp(-bounded))


def _logit(value: float) -> float:
    bounded = max(1e-8, min(1.0 - 1e-8, value))
    return math.log(bounded / (1.0 - bounded))


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _quantile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    position = quantile * (len(ordered) - 1)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


class MLDatasetBuilder:
    def __init__(
        self,
        store: ResearchStore,
        *,
        calendar_name: str = "XNYS",
    ) -> None:
        self.store = store
        self.reference_data = ReferenceDataStore(store.engine)
        self.session_clock = MarketSessionClock(calendar_name)

    def build(
        self,
        *,
        symbol: str,
        timeframe: str,
        as_of_end: datetime,
        horizon_bars: int,
        policy: MLPolicy,
        feature_set_version: str | None = None,
    ) -> tuple[MLTrainingExample, ...]:
        if horizon_bars < 1:
            raise ValueError("ML horizon_bars must be positive")
        snapshots = self.store.feature_snapshots_for_training(
            symbol=symbol,
            timeframe=timeframe,
            as_of_end=as_of_end,
            feature_set_version=feature_set_version,
        )
        if len(snapshots) <= horizon_bars:
            raise ValueError("Not enough feature snapshots to construct ML labels")
        bars = self.store.load_bars(
            symbol=symbol,
            timeframe=timeframe,
            as_of_end=as_of_end,
        )
        if len(bars) <= horizon_bars:
            raise ValueError("Not enough market bars to construct executable ML labels")
        feature_versions = {item.feature_set_version for item in snapshots}
        if len(feature_versions) != 1:
            raise ValueError("ML training requires one feature-set version per run")
        actions = self.reference_data.corporate_actions_effective_between(
            symbol=symbol,
            start=snapshots[0].as_of,
            end=snapshots[-1].as_of,
        )
        # A label follows the executable one-bar contract: decide after a completed
        # bar, enter at the next bar open, and exit at the requested future bar close.
        # Snapshot position is never used as a proxy for elapsed market bars because
        # snapshots may be sparse.
        decision_index_by_as_of = {
            bar.available_from: index for index, bar in enumerate(bars)
        }
        examples = []
        for snapshot in snapshots:
            decision_index = decision_index_by_as_of.get(snapshot.as_of)
            if decision_index is None:
                raise ValueError(
                    "Feature snapshot does not map to an exact completed market bar"
                )
            entry_index = decision_index + 1
            exit_index = decision_index + horizon_bars
            if entry_index >= len(bars) or exit_index >= len(bars):
                continue
            entry = bars[entry_index]
            future = bars[exit_index]
            if future.available_from > as_of_end:
                continue
            entry_open = float(entry.open)
            entry_time = (
                self.session_clock.daily_bar_session_open(entry.event_time)
                if entry.timeframe == "1Day"
                else entry.event_time
            )
            exit_time = future.available_from
            share_quantity = 1.0
            cash_distributions = 0.0
            for action in actions:
                # The position starts at the next bar's open. Actions between the
                # decision close and that open belong to the prior holder; entry
                # prices already reflect them. Only actions effective strictly
                # after entry and no later than the exit event belong in the label.
                if not entry_time < action.effective_at <= exit_time:
                    continue
                if (
                    action.action_type == CorporateActionType.SPLIT
                    and action.split_ratio is not None
                ):
                    share_quantity *= float(action.split_ratio)
                elif (
                    action.action_type == CorporateActionType.CASH_DIVIDEND
                    and action.cash_amount is not None
                ):
                    cash_distributions += (
                        float(action.cash_amount) * share_quantity
                    )
            forward_return = (
                (
                    float(future.close) * share_quantity
                    + cash_distributions
                )
                / entry_open
                - 1.0
            )
            values = tuple(
                self._numeric(snapshot.values.get(name)) for name in policy.features
            )
            examples.append(
                MLTrainingExample(
                    feature_snapshot_id=snapshot.feature_snapshot_id,
                    as_of=snapshot.as_of,
                    label_available_from=future.available_from,
                    values=values,
                    forward_return=forward_return,
                    label=int(
                        forward_return > float(policy.label.positive_return_threshold)
                    ),
                )
            )
        return tuple(examples)

    @staticmethod
    def _numeric(value: Decimal | int | bool | str | None) -> float:
        if value is None:
            return 0.0
        converted = float(value)
        if not math.isfinite(converted):
            raise ValueError("ML feature values must be finite")
        return converted


def _fit_logistic(
    examples: tuple[MLTrainingExample, ...],
    *,
    iterations: int = 400,
    learning_rate: float = 0.08,
    l2: float = 0.002,
) -> dict[str, Any]:
    width = len(examples[0].values)
    means = [_mean([item.values[index] for item in examples]) for index in range(width)]
    scales = []
    for index in range(width):
        variance = _mean(
            [(item.values[index] - means[index]) ** 2 for item in examples]
        )
        scales.append(max(math.sqrt(variance), 1e-9))
    xs = [
        [
            (item.values[index] - means[index]) / scales[index]
            for index in range(width)
        ]
        for item in examples
    ]
    labels = [float(item.label) for item in examples]
    prevalence = max(1e-5, min(1.0 - 1e-5, _mean(labels)))
    bias = _logit(prevalence)
    weights = [0.0] * width
    for _ in range(iterations):
        predictions = [
            _sigmoid(bias + sum(weight * value for weight, value in zip(weights, row)))
            for row in xs
        ]
        errors = [prediction - label for prediction, label in zip(predictions, labels)]
        bias -= learning_rate * _mean(errors)
        for index in range(width):
            gradient = _mean(
                [error * row[index] for error, row in zip(errors, xs)]
            ) + l2 * weights[index]
            weights[index] -= learning_rate * gradient
    return {
        "algorithm": MLModelKind.LOGISTIC_REGRESSION.value,
        "means": means,
        "scales": scales,
        "weights": weights,
        "bias": bias,
        "iterations": iterations,
        "learning_rate": learning_rate,
        "l2": l2,
    }


def _fit_boosted_stumps(
    examples: tuple[MLTrainingExample, ...],
    *,
    estimators: int = 24,
    learning_rate: float = 0.2,
) -> dict[str, Any]:
    labels = [float(item.label) for item in examples]
    prevalence = max(1e-5, min(1.0 - 1e-5, _mean(labels)))
    base_score = _logit(prevalence)
    scores = [base_score for _ in examples]
    stumps: list[dict[str, float | int]] = []
    width = len(examples[0].values)
    for _ in range(estimators):
        residuals = [
            label - _sigmoid(score) for label, score in zip(labels, scores)
        ]
        best: tuple[float, int, float, float, float] | None = None
        for feature_index in range(width):
            values = [item.values[feature_index] for item in examples]
            for quantile in (0.2, 0.4, 0.6, 0.8):
                threshold = _quantile(values, quantile)
                left = [
                    residual
                    for value, residual in zip(values, residuals)
                    if value <= threshold
                ]
                right = [
                    residual
                    for value, residual in zip(values, residuals)
                    if value > threshold
                ]
                if not left or not right:
                    continue
                left_value = _mean(left)
                right_value = _mean(right)
                loss = sum(
                    (
                        residual
                        - (left_value if value <= threshold else right_value)
                    )
                    ** 2
                    for value, residual in zip(values, residuals)
                )
                candidate = (
                    loss,
                    feature_index,
                    threshold,
                    left_value,
                    right_value,
                )
                if best is None or candidate < best:
                    best = candidate
        if best is None:
            break
        _, feature_index, threshold, left_value, right_value = best
        stump = {
            "feature_index": feature_index,
            "threshold": threshold,
            "left_value": left_value,
            "right_value": right_value,
        }
        stumps.append(stump)
        for index, item in enumerate(examples):
            contribution = (
                left_value
                if item.values[feature_index] <= threshold
                else right_value
            )
            scores[index] += learning_rate * contribution
    return {
        "algorithm": MLModelKind.BOOSTED_STUMPS.value,
        "base_score": base_score,
        "learning_rate": learning_rate,
        "stumps": stumps,
    }


def _predict_raw(artifact: dict[str, Any], values: tuple[float, ...]) -> float:
    algorithm = str(artifact["algorithm"])
    if algorithm == MLModelKind.LOGISTIC_REGRESSION.value:
        normalized = [
            (value - float(mean)) / float(scale)
            for value, mean, scale in zip(
                values,
                artifact["means"],
                artifact["scales"],
            )
        ]
        return _sigmoid(
            float(artifact["bias"])
            + sum(
                float(weight) * value
                for weight, value in zip(artifact["weights"], normalized)
            )
        )
    if algorithm == MLModelKind.BOOSTED_STUMPS.value:
        score = float(artifact["base_score"])
        for stump in artifact["stumps"]:
            feature_index = int(stump["feature_index"])
            contribution = (
                float(stump["left_value"])
                if values[feature_index] <= float(stump["threshold"])
                else float(stump["right_value"])
            )
            score += float(artifact["learning_rate"]) * contribution
        return _sigmoid(score)
    raise ValueError(f"Unsupported model artifact algorithm: {algorithm}")


def _fit_platt(probabilities: list[float], labels: list[int]) -> dict[str, float]:
    if not probabilities or len(set(labels)) < 2:
        return {"slope": 1.0, "intercept": 0.0}
    logits = [_logit(value) for value in probabilities]
    slope = 1.0
    intercept = 0.0
    for _ in range(300):
        predicted = [_sigmoid(slope * value + intercept) for value in logits]
        errors = [prediction - label for prediction, label in zip(predicted, labels)]
        slope -= 0.03 * (_mean([error * value for error, value in zip(errors, logits)]))
        intercept -= 0.03 * _mean(errors)
    return {"slope": slope, "intercept": intercept}


def _apply_calibration(probability: float, calibrator: dict[str, Any]) -> float:
    return _sigmoid(
        float(calibrator["slope"]) * _logit(probability)
        + float(calibrator["intercept"])
    )


def _roc_auc(probabilities: list[float], labels: list[int]) -> float:
    positive = [value for value, label in zip(probabilities, labels) if label == 1]
    negative = [value for value, label in zip(probabilities, labels) if label == 0]
    if not positive or not negative:
        return 0.5
    wins = 0.0
    for positive_value in positive:
        for negative_value in negative:
            if positive_value > negative_value:
                wins += 1.0
            elif positive_value == negative_value:
                wins += 0.5
    return wins / (len(positive) * len(negative))


def _probability_metrics(
    probabilities: list[float],
    labels: list[int],
) -> dict[str, float | int]:
    epsilon = 1e-12
    brier = _mean([(probability - label) ** 2 for probability, label in zip(probabilities, labels)])
    log_loss = -_mean(
        [
            label * math.log(max(epsilon, probability))
            + (1 - label) * math.log(max(epsilon, 1.0 - probability))
            for probability, label in zip(probabilities, labels)
        ]
    )
    predicted_labels = [int(value >= 0.5) for value in probabilities]
    accuracy = _mean(
        [float(predicted == actual) for predicted, actual in zip(predicted_labels, labels)]
    )
    expected_calibration_error = 0.0
    for bin_number in range(10):
        lower = bin_number / 10
        upper = (bin_number + 1) / 10
        indexes = [
            index
            for index, probability in enumerate(probabilities)
            if lower <= probability < upper or (bin_number == 9 and probability == 1.0)
        ]
        if not indexes:
            continue
        confidence = _mean([probabilities[index] for index in indexes])
        observed = _mean([float(labels[index]) for index in indexes])
        expected_calibration_error += (
            len(indexes) / len(probabilities)
        ) * abs(confidence - observed)
    return {
        "sample_count": len(labels),
        "positive_rate": _mean([float(value) for value in labels]),
        "roc_auc": _roc_auc(probabilities, labels),
        "brier_score": brier,
        "log_loss": log_loss,
        "accuracy_at_0_5": accuracy,
        "expected_calibration_error": expected_calibration_error,
    }


def _population_stability(
    examples: tuple[MLTrainingExample, ...],
    feature_names: tuple[str, ...],
) -> dict[str, Any]:
    split = max(1, int(len(examples) * 0.7))
    reference = examples[:split]
    current = examples[split:]
    if not current:
        return {"maximum_psi": 0.0, "by_feature": {name: 0.0 for name in feature_names}}
    by_feature: dict[str, float] = {}
    epsilon = 1e-4
    for feature_index, feature_name in enumerate(feature_names):
        reference_values = [item.values[feature_index] for item in reference]
        current_values = [item.values[feature_index] for item in current]
        boundaries = sorted(
            set(_quantile(reference_values, value) for value in (0.2, 0.4, 0.6, 0.8))
        )
        reference_counts = [0] * (len(boundaries) + 1)
        current_counts = [0] * (len(boundaries) + 1)
        for value in reference_values:
            reference_counts[sum(value > boundary for boundary in boundaries)] += 1
        for value in current_values:
            current_counts[sum(value > boundary for boundary in boundaries)] += 1
        psi = 0.0
        for reference_count, current_count in zip(reference_counts, current_counts):
            reference_share = max(epsilon, reference_count / len(reference_values))
            current_share = max(epsilon, current_count / len(current_values))
            psi += (current_share - reference_share) * math.log(
                current_share / reference_share
            )
        by_feature[feature_name] = psi
    return {
        "reference_sample_count": len(reference),
        "current_sample_count": len(current),
        "maximum_psi": max(by_feature.values(), default=0.0),
        "by_feature": by_feature,
    }


class MLStore:
    def __init__(self, engine: Engine, ledger: EventLedger | None = None) -> None:
        self.engine = engine
        self.ledger = ledger

    def record_training(self, result: MLTrainingResult) -> None:
        run = result.run
        with self.engine.begin() as connection:
            connection.execute(
                insert(ml_training_runs).values(
                    **run.model_dump(exclude={"model_ids"}),
                    model_ids_json=list(run.model_ids),
                )
            )
            connection.execute(
                insert(ml_models),
                [
                    {
                        **model.model_dump(
                            exclude={
                                "kind",
                                "feature_names",
                                "artifact",
                                "metrics",
                                "calibration",
                                "drift",
                                "promotion_assessment",
                                "status",
                            }
                        ),
                        "kind": model.kind.value,
                        "feature_names_json": list(model.feature_names),
                        "artifact_json": model.artifact,
                        "metrics_json": model.metrics,
                        "calibration_json": model.calibration,
                        "drift_json": model.drift,
                        "promotion_assessment_json": model.promotion_assessment,
                        "status": model.status.value,
                        "champion_slot": None,
                    }
                    for model in result.models
                ],
            )
        if self.ledger is not None:
            self.ledger.append(
                EventEnvelope(
                    event_id=uuid7(),
                    event_type="ml.training.completed.v1",
                    event_time=run.finished_at,
                    emitted_at=datetime.now(UTC),
                    producer="ml-trainer",
                    correlation_id=run.training_run_id,
                    payload=run.model_dump(mode="json"),
                )
            )

    def model(self, model_id: str) -> MLModelVersion | None:
        with self.engine.connect() as connection:
            row = connection.execute(
                select(ml_models).where(ml_models.c.model_id == model_id)
            ).one_or_none()
        return self._model_from_row(dict(row._mapping)) if row is not None else None

    def recent_models(self, *, limit: int = 50) -> list[dict[str, Any]]:
        statement = ml_models.select().order_by(ml_models.c.created_at.desc()).limit(limit)
        with self.engine.connect() as connection:
            return [
                self._model_from_row(dict(row._mapping)).model_dump(mode="json")
                for row in connection.execute(statement)
            ]

    def recent_training_runs(self, *, limit: int = 50) -> list[dict[str, Any]]:
        statement = (
            ml_training_runs.select()
            .order_by(ml_training_runs.c.finished_at.desc())
            .limit(limit)
        )
        with self.engine.connect() as connection:
            results = []
            for row in connection.execute(statement):
                item = dict(row._mapping)
                item["model_ids"] = item.pop("model_ids_json")
                item["started_at"] = _utc(item["started_at"])
                item["finished_at"] = _utc(item["finished_at"])
                results.append(item)
            return results

    def record_forecast(self, model_id: str, forecast: Forecast) -> Forecast:
        values = {"model_id": model_id, **forecast.model_dump()}
        statement: Any
        if self.engine.dialect.name == "postgresql":
            statement = (
                postgresql_insert(ml_forecasts)
                .values(values)
                .on_conflict_do_nothing(
                    index_elements=["model_id", "feature_snapshot_id", "horizon"]
                )
            )
        elif self.engine.dialect.name == "sqlite":
            statement = (
                sqlite_insert(ml_forecasts)
                .values(values)
                .on_conflict_do_nothing(
                    index_elements=["model_id", "feature_snapshot_id", "horizon"]
                )
            )
        else:
            raise RuntimeError(f"Unsupported SQL dialect: {self.engine.dialect.name}")
        identity = and_(
            ml_forecasts.c.model_id == model_id,
            ml_forecasts.c.feature_snapshot_id == forecast.feature_snapshot_id,
            ml_forecasts.c.horizon == forecast.horizon,
        )
        with self.engine.begin() as connection:
            connection.execute(statement)
            row = connection.execute(select(ml_forecasts).where(identity)).one()
        stored = self._forecast_from_row(dict(row._mapping))
        if self.ledger is not None and stored.forecast_id == forecast.forecast_id:
            self.ledger.append(
                EventEnvelope(
                    event_id=uuid7(),
                    event_type="ml.forecast.created.v1",
                    event_time=forecast.as_of,
                    emitted_at=datetime.now(UTC),
                    producer="ml-inference",
                    correlation_id=forecast.forecast_id,
                    causation_id=model_id,
                    payload={"model_id": model_id, **forecast.model_dump(mode="json")},
                )
            )
        return stored

    def forecast(self, forecast_id: str) -> Forecast | None:
        with self.engine.connect() as connection:
            row = connection.execute(
                select(ml_forecasts).where(ml_forecasts.c.forecast_id == forecast_id)
            ).one_or_none()
        return self._forecast_from_row(dict(row._mapping)) if row is not None else None

    def recent_forecasts(self, *, limit: int = 50) -> list[dict[str, Any]]:
        statement = (
            ml_forecasts.select().order_by(ml_forecasts.c.created_at.desc()).limit(limit)
        )
        with self.engine.connect() as connection:
            return [
                {
                    "model_id": str(row.model_id),
                    **self._forecast_from_row(dict(row._mapping)).model_dump(mode="json"),
                }
                for row in connection.execute(statement)
            ]

    def recent_registry_events(self, *, limit: int = 50) -> list[dict[str, Any]]:
        statement = (
            model_registry_events.select()
            .order_by(model_registry_events.c.created_at.desc())
            .limit(limit)
        )
        with self.engine.connect() as connection:
            results = []
            for row in connection.execute(statement):
                item = dict(row._mapping)
                item["created_at"] = _utc(item["created_at"])
                results.append(item)
            return results

    def promote(
        self,
        *,
        model_id: str,
        approved_by: str,
        reason: str,
    ) -> ModelRegistryEvent:
        if not approved_by.strip() or len(reason.strip()) < 3:
            raise ValueError("Promotion requires an approver and reason")
        now = datetime.now(UTC)
        with self.engine.begin() as connection:
            model_row = connection.execute(
                select(ml_models)
                .where(ml_models.c.model_id == model_id)
                .with_for_update()
            ).one_or_none()
            if model_row is None:
                raise ValueError("ML model not found")
            if str(model_row.status) != MLModelStatus.CHALLENGER.value:
                raise ValueError("Only an eligible challenger can become champion")
            if not bool(model_row.promotion_assessment_json.get("eligible_for_review")):
                raise ValueError("ML model did not pass its deterministic review gate")
            champion_slot = (
                f"{model_row.model_name}:{model_row.symbol}:"
                f"{model_row.timeframe}:{model_row.horizon_bars}"
            )
            champion_rows = connection.execute(
                select(
                    ml_models.c.model_id,
                    ml_models.c.training_run_id,
                )
                .where(ml_models.c.champion_slot == champion_slot)
                .with_for_update()
            ).all()
            connection.execute(
                update(ml_models)
                .where(ml_models.c.champion_slot == champion_slot)
                .values(
                    status=MLModelStatus.RETIRED.value,
                    champion_slot=None,
                )
            )
            for champion_row in champion_rows:
                connection.execute(
                    insert(model_registry_events).values(
                        registry_event_id=uuid7(),
                        model_id=str(champion_row.model_id),
                        previous_status=MLModelStatus.CHAMPION.value,
                        new_status=MLModelStatus.RETIRED.value,
                        training_run_id=str(champion_row.training_run_id),
                        approved_by=approved_by.strip(),
                        reason=f"Superseded: {reason.strip()}",
                        created_at=now,
                    )
                )
            event = ModelRegistryEvent(
                registry_event_id=uuid7(),
                model_id=model_id,
                previous_status=MLModelStatus.CHALLENGER,
                new_status=MLModelStatus.CHAMPION,
                training_run_id=str(model_row.training_run_id),
                approved_by=approved_by.strip(),
                reason=reason.strip(),
                created_at=now,
            )
            connection.execute(
                update(ml_models)
                .where(ml_models.c.model_id == model_id)
                .values(
                    status=MLModelStatus.CHAMPION.value,
                    champion_slot=champion_slot,
                )
            )
            connection.execute(
                insert(model_registry_events).values(
                    **event.model_dump(exclude={"previous_status", "new_status"}),
                    previous_status=event.previous_status.value,
                    new_status=event.new_status.value,
                )
            )
        if self.ledger is not None:
            self.ledger.append(
                EventEnvelope(
                    event_id=uuid7(),
                    event_type="ml.model.promoted.v1",
                    event_time=now,
                    emitted_at=datetime.now(UTC),
                    producer="model-registry",
                    correlation_id=model_id,
                    payload=event.model_dump(mode="json"),
                )
            )
        return event

    def health_summary(self) -> dict[str, int]:
        with self.engine.connect() as connection:
            return {
                "ml_training_runs": int(
                    connection.execute(
                        select(func.count()).select_from(ml_training_runs)
                    ).scalar_one()
                ),
                "ml_models": int(
                    connection.execute(select(func.count()).select_from(ml_models)).scalar_one()
                ),
                "ml_forecasts": int(
                    connection.execute(
                        select(func.count()).select_from(ml_forecasts)
                    ).scalar_one()
                ),
                "model_registry_events": int(
                    connection.execute(
                        select(func.count()).select_from(model_registry_events)
                    ).scalar_one()
                ),
            }

    @staticmethod
    def _model_from_row(row: dict[str, Any]) -> MLModelVersion:
        return MLModelVersion(
            model_id=str(row["model_id"]),
            training_run_id=str(row["training_run_id"]),
            model_name=str(row["model_name"]),
            model_version=str(row["model_version"]),
            kind=MLModelKind(str(row["kind"])),
            symbol=str(row["symbol"]),
            timeframe=str(row["timeframe"]),
            horizon_bars=int(row["horizon_bars"]),
            feature_set_version=str(row["feature_set_version"]),
            feature_names=tuple(row["feature_names_json"]),
            training_start=_utc(row["training_start"]),
            training_end=_utc(row["training_end"]),
            training_data_cutoff=_utc(row["training_data_cutoff"]),
            artifact=row["artifact_json"],
            artifact_sha256=str(row["artifact_sha256"]),
            metrics=row["metrics_json"],
            calibration=row["calibration_json"],
            drift=row["drift_json"],
            promotion_assessment=row["promotion_assessment_json"],
            status=MLModelStatus(str(row["status"])),
            code_git_sha=str(row["code_git_sha"]),
            created_at=_utc(row["created_at"]),
        )

    @staticmethod
    def _forecast_from_row(row: dict[str, Any]) -> Forecast:
        return Forecast(
            forecast_id=str(row["forecast_id"]),
            symbol=str(row["symbol"]),
            as_of=_utc(row["as_of"]),
            horizon=str(row["horizon"]),
            expected_return=Decimal(row["expected_return"]),
            probability_up=Decimal(row["probability_up"]),
            uncertainty=Decimal(row["uncertainty"]),
            model_version=str(row["model_version"]),
            training_data_cutoff=_utc(row["training_data_cutoff"]),
            feature_snapshot_id=str(row["feature_snapshot_id"]),
            created_at=_utc(row["created_at"]),
        )


class WalkForwardMLTrainer:
    def __init__(
        self,
        store: MLStore,
        policy: MLPolicy,
        *,
        code_git_sha: str = "UNAVAILABLE",
    ) -> None:
        self.store = store
        self.policy = policy
        self.code_git_sha = code_git_sha

    def train(
        self,
        examples: tuple[MLTrainingExample, ...],
        *,
        symbol: str,
        timeframe: str,
        horizon_bars: int,
        feature_set_version: str,
    ) -> MLTrainingResult:
        started_at = datetime.now(UTC)
        effective_embargo = max(
            self.policy.validation.embargo_bars,
            horizon_bars,
        )
        folds = self._folds(
            len(examples),
            embargo_bars=effective_embargo,
        )
        dataset_sha256 = ml_dataset_sha256(examples)
        drift = _population_stability(examples, self.policy.features)
        drafts: list[dict[str, Any]] = []
        for kind in (MLModelKind.LOGISTIC_REGRESSION, MLModelKind.BOOSTED_STUMPS):
            raw_probabilities: list[float] = []
            labels: list[int] = []
            oos_examples: list[MLTrainingExample] = []
            fold_metrics = []
            for fold_number, (train_end, test_start, test_end) in enumerate(folds, 1):
                training = examples[:train_end]
                testing = examples[test_start:test_end]
                artifact = self._fit(kind, training)
                fold_probabilities = [
                    _predict_raw(artifact, item.values) for item in testing
                ]
                raw_probabilities.extend(fold_probabilities)
                labels.extend(item.label for item in testing)
                oos_examples.extend(testing)
                fold_metrics.append(
                    {
                        "fold_number": fold_number,
                        "train_start": training[0].as_of.isoformat(),
                        "train_end": training[-1].as_of.isoformat(),
                        "train_label_cutoff": training[
                            -1
                        ].label_available_from.isoformat(),
                        "test_start": testing[0].as_of.isoformat(),
                        "test_end": testing[-1].as_of.isoformat(),
                        "metrics": _probability_metrics(
                            fold_probabilities,
                            [item.label for item in testing],
                        ),
                    }
                )
            calibration_end, selection_start, selection_end, evaluation_start = (
                self._purged_oos_partitions(tuple(oos_examples))
            )
            calibrator = _fit_platt(
                raw_probabilities[:calibration_end],
                labels[:calibration_end],
            )
            calibrated_selection = [
                _apply_calibration(value, calibrator)
                for value in raw_probabilities[selection_start:selection_end]
            ]
            calibrated_evaluation = [
                _apply_calibration(value, calibrator)
                for value in raw_probabilities[evaluation_start:]
            ]
            metrics: dict[str, Any] = {
                "walk_forward_folds": fold_metrics,
                "raw_oos": _probability_metrics(raw_probabilities, labels),
                "calibrated_model_selection": _probability_metrics(
                    calibrated_selection,
                    labels[selection_start:selection_end],
                ),
                "calibrated_final_holdout": _probability_metrics(
                    calibrated_evaluation,
                    labels[evaluation_start:],
                ),
            }
            final_artifact = self._fit(kind, examples)
            final_artifact["calibrator"] = calibrator
            final_artifact["positive_mean_return"] = _mean(
                [item.forward_return for item in examples if item.label == 1]
            )
            final_artifact["negative_mean_return"] = _mean(
                [item.forward_return for item in examples if item.label == 0]
            )
            assessment = self._promotion_assessment(
                sample_count=len(examples),
                oos_sample_count=len(labels) - evaluation_start,
                fold_count=len(folds),
                metrics=metrics["calibrated_final_holdout"],
                drift=drift,
            )
            drafts.append(
                {
                    "kind": kind,
                    "artifact": final_artifact,
                    "metrics": metrics,
                    "calibration": {
                        "method": (
                            "Platt scaling fitted on an early purged OOS partition; model "
                            "selection and final evaluation use separate later partitions"
                        ),
                        "fit_sample_count": calibration_end,
                        "selection_sample_count": selection_end - selection_start,
                        "evaluation_sample_count": len(labels) - evaluation_start,
                        "calibration_end_as_of": oos_examples[
                            calibration_end - 1
                        ].as_of.isoformat(),
                        "calibration_label_cutoff": oos_examples[
                            calibration_end - 1
                        ].label_available_from.isoformat(),
                        "selection_start_as_of": oos_examples[
                            selection_start
                        ].as_of.isoformat(),
                        "selection_label_cutoff": oos_examples[
                            selection_end - 1
                        ].label_available_from.isoformat(),
                        "evaluation_start_as_of": oos_examples[
                            evaluation_start
                        ].as_of.isoformat(),
                        **calibrator,
                        "expected_calibration_error": metrics[
                            "calibrated_final_holdout"
                        ][
                            "expected_calibration_error"
                        ],
                    },
                    "promotion_assessment": assessment,
                }
            )
        selected = min(
            drafts,
            key=lambda item: (
                item["metrics"]["calibrated_model_selection"]["brier_score"],
                -item["metrics"]["calibrated_model_selection"]["roc_auc"],
                item["kind"].value,
            ),
        )
        run_id = uuid7()
        created_at = datetime.now(UTC)
        model_ids = tuple(uuid7() for _ in drafts)
        models = []
        for model_id, draft in zip(model_ids, drafts):
            artifact_sha256 = _canonical_hash(draft["artifact"])
            version_sha256 = _canonical_hash(
                {
                    "artifact_sha256": artifact_sha256,
                    "dataset_sha256": dataset_sha256,
                    "symbol": symbol.upper(),
                    "timeframe": timeframe,
                    "horizon_bars": horizon_bars,
                    "feature_set_version": feature_set_version,
                    "training_run_id": run_id,
                }
            )
            is_selected = draft is selected
            eligible = bool(
                draft["promotion_assessment"]["eligible_for_review"]
            )
            models.append(
                MLModelVersion(
                    model_id=model_id,
                    training_run_id=run_id,
                    model_name=f"equity_direction_{horizon_bars}bar",
                    model_version=(
                        f"{draft['kind'].value}@0.1.0+{version_sha256[:12]}"
                    ),
                    kind=draft["kind"],
                    symbol=symbol.upper(),
                    timeframe=timeframe,
                    horizon_bars=horizon_bars,
                    feature_set_version=feature_set_version,
                    feature_names=self.policy.features,
                    training_start=examples[0].as_of,
                    training_end=examples[-1].as_of,
                    training_data_cutoff=examples[-1].label_available_from,
                    artifact=draft["artifact"],
                    artifact_sha256=artifact_sha256,
                    metrics=draft["metrics"],
                    calibration=draft["calibration"],
                    drift=drift,
                    promotion_assessment=draft["promotion_assessment"],
                    status=(
                        MLModelStatus.CHALLENGER
                        if is_selected and eligible
                        else MLModelStatus.CANDIDATE
                    ),
                    code_git_sha=self.code_git_sha,
                    created_at=created_at,
                )
            )
        selected_index = drafts.index(selected)
        run = MLTrainingRun(
            training_run_id=run_id,
            symbol=symbol.upper(),
            timeframe=timeframe,
            horizon_bars=horizon_bars,
            feature_set_version=feature_set_version,
            dataset_sha256=dataset_sha256,
            sample_count=len(examples),
            fold_count=len(folds),
            embargo_bars=effective_embargo,
            model_ids=model_ids,
            selected_model_id=model_ids[selected_index],
            selection_metric=(
                "minimum_purged_selection_brier_then_maximum_roc_auc; "
                "promotion_evaluated_on_untouched_final_holdout"
            ),
            status="COMPLETED",
            code_git_sha=self.code_git_sha,
            started_at=started_at,
            finished_at=datetime.now(UTC),
        )
        result = MLTrainingResult(run=run, models=tuple(models))
        self.store.record_training(result)
        return result

    @staticmethod
    def _fit(
        kind: MLModelKind,
        examples: tuple[MLTrainingExample, ...],
    ) -> dict[str, Any]:
        if kind == MLModelKind.LOGISTIC_REGRESSION:
            return _fit_logistic(examples)
        return _fit_boosted_stumps(examples)

    def _folds(
        self,
        sample_count: int,
        *,
        embargo_bars: int,
    ) -> tuple[tuple[int, int, int], ...]:
        fold_count = self.policy.validation.minimum_folds
        requested_test_size = max(
            5,
            int(sample_count * float(self.policy.validation.test_fraction)),
        )
        maximum_test_size = (
            sample_count - 24 - embargo_bars
        ) // fold_count
        test_size = min(requested_test_size, maximum_test_size)
        if test_size < 5:
            raise ValueError(
                "ML training requires enough samples for 24 training rows and "
                f"{fold_count} embargoed test folds"
            )
        initial_train = (
            sample_count - fold_count * test_size - embargo_bars
        )
        folds = []
        for fold_index in range(fold_count):
            train_end = initial_train + fold_index * test_size
            test_start = train_end + embargo_bars
            test_end = test_start + test_size
            folds.append((train_end, test_start, test_end))
        return tuple(folds)

    @staticmethod
    def _purged_oos_partitions(
        examples: tuple[MLTrainingExample, ...],
    ) -> tuple[int, int, int, int]:
        """Return non-overlapping calibration, selection, and final-test slices.

        Purging is based on label availability timestamps rather than a nominal row
        count, so multi-bar horizons cannot leak across either boundary.
        """
        if len(examples) < 9:
            raise ValueError("ML OOS evaluation requires at least nine predictions")
        first_boundary = max(1, len(examples) // 3)
        second_boundary = max(first_boundary + 1, (2 * len(examples)) // 3)
        selection_start = first_boundary
        calibration_end = first_boundary
        while (
            calibration_end > 0
            and examples[calibration_end - 1].label_available_from
            > examples[selection_start].as_of
        ):
            calibration_end -= 1
        evaluation_start = second_boundary
        selection_end = second_boundary
        while (
            selection_end > selection_start
            and examples[selection_end - 1].label_available_from
            > examples[evaluation_start].as_of
        ):
            selection_end -= 1
        if calibration_end < 2 or selection_end - selection_start < 2:
            raise ValueError(
                "ML OOS partitions are too small after label-availability purging"
            )
        if len(examples) - evaluation_start < 2:
            raise ValueError("ML final holdout is too small")
        return calibration_end, selection_start, selection_end, evaluation_start

    def _promotion_assessment(
        self,
        *,
        sample_count: int,
        oos_sample_count: int,
        fold_count: int,
        metrics: dict[str, float | int],
        drift: dict[str, Any],
    ) -> dict[str, Any]:
        checks = {
            "minimum_samples": sample_count >= self.policy.validation.minimum_samples,
            "minimum_oos_samples": (
                oos_sample_count >= self.policy.validation.minimum_oos_samples
            ),
            "minimum_folds": fold_count >= self.policy.validation.minimum_folds,
            "minimum_roc_auc": float(metrics["roc_auc"])
            >= float(self.policy.promotion.minimum_roc_auc),
            "maximum_brier_score": float(metrics["brier_score"])
            <= float(self.policy.promotion.maximum_brier_score),
            "maximum_expected_calibration_error": float(
                metrics["expected_calibration_error"]
            )
            <= float(self.policy.promotion.maximum_expected_calibration_error),
            "maximum_population_stability_index": float(drift["maximum_psi"])
            <= float(self.policy.promotion.maximum_population_stability_index),
        }
        return {
            "policy_version": self.policy.version,
            "checks": checks,
            "shortfalls": [name for name, passed in checks.items() if not passed],
            "eligible_for_review": all(checks.values()),
            "automatic_promotion": False,
            "requires_human_approval": self.policy.promotion.require_human_approval,
            "downstream_strategy_requires_research_gate": (
                self.policy.promotion.downstream_strategy_requires_research_gate
            ),
        }


class MLPredictor:
    def __init__(self, store: MLStore) -> None:
        self.store = store

    def predict(
        self,
        *,
        model: MLModelVersion,
        snapshot: PointInTimeFeatureSnapshot,
    ) -> Forecast:
        if model.symbol != snapshot.symbol or model.timeframe != snapshot.timeframe:
            raise ValueError("ML model and feature snapshot identity do not match")
        if model.feature_set_version != snapshot.feature_set_version:
            raise ValueError("ML model and feature-set versions do not match")
        if _canonical_hash(model.artifact) != model.artifact_sha256:
            raise ValueError("ML model artifact hash does not match its registry record")
        if model.training_data_cutoff > snapshot.as_of:
            raise ValueError("ML model was trained with information after forecast as_of")
        values = tuple(
            MLDatasetBuilder._numeric(snapshot.values.get(name))
            for name in model.feature_names
        )
        raw_probability = _predict_raw(model.artifact, values)
        probability = _apply_calibration(
            raw_probability,
            model.artifact["calibrator"],
        )
        positive_mean = float(model.artifact["positive_mean_return"])
        negative_mean = float(model.artifact["negative_mean_return"])
        expected_return = probability * positive_mean + (
            1.0 - probability
        ) * negative_mean
        forecast = Forecast(
            forecast_id=uuid7(),
            symbol=snapshot.symbol,
            as_of=snapshot.as_of,
            horizon=(
                "1 bar"
                if model.horizon_bars == 1
                else f"{model.horizon_bars} bars"
            ),
            expected_return=Decimal(str(expected_return)),
            probability_up=Decimal(str(probability)),
            uncertainty=Decimal(str(1.0 - abs(probability - 0.5) * 2.0)),
            model_version=model.model_version,
            training_data_cutoff=model.training_data_cutoff,
            feature_snapshot_id=snapshot.feature_snapshot_id,
            created_at=datetime.now(UTC),
        )
        return self.store.record_forecast(model.model_id, forecast)


__all__ = [
    "MLDatasetBuilder",
    "MLPolicy",
    "MLPredictor",
    "MLStore",
    "MLTrainingExample",
    "MLTrainingResult",
    "WalkForwardMLTrainer",
    "load_ml_policy",
    "ml_dataset_sha256",
]
