from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Engine, and_, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from agentic_quant.database import (
    backtest_portfolio_events,
    backtest_trades,
    catalysts,
    corporate_actions,
    corporate_facts,
    evidence_packets,
    experiment_runs,
    feature_parity_checks,
    feature_snapshots,
    market_bars,
    strategy_specs,
    strategy_generation_attempts,
    validation_folds,
    validation_reports,
)
from agentic_quant.domain import (
    BacktestResult,
    EvidencePacket,
    EvidenceReference,
    FeatureParityCheck,
    PointInTimeFeatureSnapshot,
    StockBar,
    StrategySpec,
    WalkForwardValidationReport,
)
from agentic_quant.ids import uuid7


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


class ResearchStore:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def load_bars(
        self,
        *,
        symbol: str,
        timeframe: str,
        as_of_end: datetime,
    ) -> tuple[StockBar, ...]:
        statement = (
            select(market_bars)
            .where(
                and_(
                    market_bars.c.symbol == symbol.upper(),
                    market_bars.c.timeframe == timeframe,
                    market_bars.c.event_time <= as_of_end,
                    market_bars.c.available_from <= as_of_end,
                )
            )
            .order_by(market_bars.c.event_time.asc())
        )
        with self.engine.connect() as connection:
            rows = connection.execute(statement).all()
        return tuple(
            StockBar(
                **{
                    **dict(row._mapping),
                    "event_time": _utc(row.event_time),
                    "available_from": _utc(row.available_from),
                    "ingested_at": _utc(row.ingested_at),
                }
            )
            for row in rows
        )

    def load_bars_for_parity_audit(
        self,
        *,
        symbol: str,
        timeframe: str,
    ) -> tuple[StockBar, ...]:
        """Load complete stored history only for testing the as-of filter itself."""
        statement = (
            select(market_bars)
            .where(
                and_(
                    market_bars.c.symbol == symbol.upper(),
                    market_bars.c.timeframe == timeframe,
                )
            )
            .order_by(market_bars.c.event_time.asc())
        )
        with self.engine.connect() as connection:
            rows = connection.execute(statement).all()
        return tuple(
            StockBar(
                **{
                    **dict(row._mapping),
                    "event_time": _utc(row.event_time),
                    "available_from": _utc(row.available_from),
                    "ingested_at": _utc(row.ingested_at),
                }
            )
            for row in rows
        )

    def build_evidence_packet(
        self,
        *,
        symbol: str,
        as_of: datetime,
        bars: tuple[StockBar, ...],
        lookback_days: int = 90,
    ) -> EvidencePacket:
        if not bars:
            raise ValueError("Evidence packet requires at least one market bar")
        if any(bar.available_from > as_of or bar.event_time > as_of for bar in bars):
            raise ValueError("Evidence packet received future market data")
        references = [
            EvidenceReference(
                evidence_type="market_bar",
                evidence_id=bar.bar_id,
                event_time=bar.event_time,
                available_from=bar.available_from,
                source=f"{bar.source}:{bar.feed}",
            )
            for bar in bars
        ]
        since = as_of - timedelta(days=lookback_days)
        with self.engine.connect() as connection:
            catalyst_rows = connection.execute(
                select(
                    catalysts.c.catalyst_id,
                    catalysts.c.event_time,
                    catalysts.c.available_from,
                    catalysts.c.catalyst_type,
                )
                .where(
                    and_(
                        catalysts.c.primary_symbol == symbol.upper(),
                        catalysts.c.event_time >= since,
                        catalysts.c.event_time <= as_of,
                        catalysts.c.available_from <= as_of,
                    )
                )
                .order_by(catalysts.c.event_time.asc())
            ).all()
            fact_rows = connection.execute(
                select(
                    corporate_facts.c.fact_id,
                    corporate_facts.c.period_end,
                    corporate_facts.c.available_from,
                    corporate_facts.c.form,
                )
                .where(
                    and_(
                        corporate_facts.c.symbol == symbol.upper(),
                        corporate_facts.c.period_end <= as_of,
                        corporate_facts.c.available_from <= as_of,
                    )
                )
                .order_by(corporate_facts.c.available_from.desc())
                .limit(50)
            ).all()
            action_rows = connection.execute(
                select(
                    corporate_actions.c.corporate_action_id,
                    corporate_actions.c.effective_at,
                    corporate_actions.c.available_from,
                    corporate_actions.c.action_type,
                    corporate_actions.c.source,
                )
                .where(
                    and_(
                        corporate_actions.c.symbol == symbol.upper(),
                        corporate_actions.c.effective_at >= bars[0].event_time,
                        corporate_actions.c.effective_at <= as_of,
                        corporate_actions.c.available_from <= as_of,
                    )
                )
                .order_by(corporate_actions.c.effective_at.asc())
            ).all()
        references.extend(
            EvidenceReference(
                evidence_type="catalyst",
                evidence_id=str(row.catalyst_id),
                event_time=_utc(row.event_time),
                available_from=_utc(row.available_from),
                source=f"catalyst:{row.catalyst_type}",
            )
            for row in catalyst_rows
        )
        references.extend(
            EvidenceReference(
                evidence_type="corporate_fact",
                evidence_id=str(row.fact_id),
                event_time=_utc(row.period_end),
                available_from=_utc(row.available_from),
                source=f"sec:{row.form}",
            )
            for row in fact_rows
        )
        references.extend(
            EvidenceReference(
                evidence_type="corporate_action",
                evidence_id=str(row.corporate_action_id),
                event_time=_utc(row.effective_at),
                available_from=_utc(row.available_from),
                source=f"{row.source}:{row.action_type}",
            )
            for row in action_rows
        )
        ordered = tuple(
            sorted(
                references,
                key=lambda item: (
                    item.available_from,
                    item.event_time,
                    item.evidence_type,
                    item.evidence_id,
                ),
            )
        )
        evidence_hash = _canonical_hash(
            [reference.model_dump(mode="json") for reference in ordered]
        )
        packet = EvidencePacket(
            evidence_packet_id=uuid7(),
            symbol=symbol.upper(),
            as_of=as_of,
            evidence_hash=evidence_hash,
            references=ordered,
            source_max_available_from=max(
                reference.available_from for reference in ordered
            ),
            created_at=datetime.now(UTC),
        )
        return self.record_evidence_packet(packet)

    def record_evidence_packet(self, packet: EvidencePacket) -> EvidencePacket:
        identity = and_(
            evidence_packets.c.symbol == packet.symbol,
            evidence_packets.c.as_of == packet.as_of,
            evidence_packets.c.evidence_hash == packet.evidence_hash,
        )
        values = {
            "evidence_packet_id": packet.evidence_packet_id,
            "symbol": packet.symbol,
            "as_of": packet.as_of,
            "evidence_hash": packet.evidence_hash,
            "references_json": [
                reference.model_dump(mode="json") for reference in packet.references
            ],
            "source_max_available_from": packet.source_max_available_from,
            "created_at": packet.created_at,
        }
        with self.engine.begin() as connection:
            statement = self._insert_ignore(
                evidence_packets,
                values,
                ["symbol", "as_of", "evidence_hash"],
            )
            connection.execute(statement)
            row = connection.execute(select(evidence_packets).where(identity)).one()
        return self._evidence_packet_from_row(dict(row._mapping))

    def record_feature_snapshot(
        self,
        snapshot: PointInTimeFeatureSnapshot,
    ) -> PointInTimeFeatureSnapshot:
        identity = and_(
            feature_snapshots.c.symbol == snapshot.symbol,
            feature_snapshots.c.timeframe == snapshot.timeframe,
            feature_snapshots.c.as_of == snapshot.as_of,
            feature_snapshots.c.feature_set_version == snapshot.feature_set_version,
            feature_snapshots.c.data_hash == snapshot.data_hash,
        )
        values = {
            "feature_snapshot_id": snapshot.feature_snapshot_id,
            "evidence_packet_id": snapshot.evidence_packet_id,
            "symbol": snapshot.symbol,
            "timeframe": snapshot.timeframe,
            "as_of": snapshot.as_of,
            "feature_set_version": snapshot.feature_set_version,
            "feature_values": snapshot.model_dump(mode="json")["values"],
            "source_max_available_from": snapshot.source_max_available_from,
            "data_hash": snapshot.data_hash,
            "created_at": snapshot.created_at,
        }
        with self.engine.begin() as connection:
            statement = self._insert_ignore(
                feature_snapshots,
                values,
                ["symbol", "timeframe", "as_of", "feature_set_version", "data_hash"],
            )
            connection.execute(statement)
            row = connection.execute(select(feature_snapshots).where(identity)).one()
        return self._feature_snapshot_from_row(dict(row._mapping))

    def feature_snapshot(
        self,
        feature_snapshot_id: str,
    ) -> PointInTimeFeatureSnapshot | None:
        statement = select(feature_snapshots).where(
            feature_snapshots.c.feature_snapshot_id == feature_snapshot_id
        )
        with self.engine.connect() as connection:
            row = connection.execute(statement).one_or_none()
        if row is None:
            return None
        return self._feature_snapshot_from_row(dict(row._mapping))

    def feature_snapshots_for_training(
        self,
        *,
        symbol: str,
        timeframe: str,
        as_of_end: datetime,
        feature_set_version: str | None = None,
    ) -> tuple[PointInTimeFeatureSnapshot, ...]:
        predicates = [
            feature_snapshots.c.symbol == symbol.upper(),
            feature_snapshots.c.timeframe == timeframe,
            feature_snapshots.c.as_of <= as_of_end,
            feature_snapshots.c.source_max_available_from <= as_of_end,
        ]
        if feature_set_version is not None:
            predicates.append(
                feature_snapshots.c.feature_set_version == feature_set_version
            )
        statement = (
            select(feature_snapshots)
            .where(and_(*predicates))
            .order_by(
                feature_snapshots.c.as_of.asc(),
                feature_snapshots.c.created_at.asc(),
            )
        )
        with self.engine.connect() as connection:
            rows = connection.execute(statement).all()
        latest_by_as_of: dict[datetime, PointInTimeFeatureSnapshot] = {}
        for row in rows:
            snapshot = self._feature_snapshot_from_row(dict(row._mapping))
            latest_by_as_of[snapshot.as_of] = snapshot
        return tuple(latest_by_as_of[key] for key in sorted(latest_by_as_of))

    def record_strategy_spec(self, spec: StrategySpec) -> StrategySpec:
        identity = and_(
            strategy_specs.c.name == spec.name,
            strategy_specs.c.version == spec.version,
        )
        values = {
            "strategy_spec_id": spec.strategy_spec_id,
            "name": spec.name,
            "version": spec.version,
            "strategy_type": spec.strategy_type,
            "timeframe": spec.timeframe,
            "feature_set_version": spec.feature_set_version,
            "parameters_json": spec.parameters,
            "data_requirements_json": spec.data_requirements,
            "code_sha256": spec.code_sha256,
            "created_at": spec.created_at,
        }
        with self.engine.begin() as connection:
            connection.execute(
                self._insert_ignore(
                    strategy_specs,
                    values,
                    ["name", "version"],
                )
            )
            existing = connection.execute(select(strategy_specs).where(identity)).one()
        stored = self._strategy_spec_from_row(dict(existing._mapping))
        if stored.model_dump(exclude={"strategy_spec_id", "created_at"}) != spec.model_dump(
            exclude={"strategy_spec_id", "created_at"}
        ):
            raise ValueError(
                f"Strategy version {spec.name}@{spec.version} already exists with different content"
            )
        return stored

    def strategy_spec(self, strategy_spec_id: str) -> StrategySpec | None:
        with self.engine.connect() as connection:
            row = connection.execute(
                select(strategy_specs).where(
                    strategy_specs.c.strategy_spec_id == strategy_spec_id
                )
            ).one_or_none()
        return (
            None
            if row is None
            else self._strategy_spec_from_row(dict(row._mapping))
        )

    def create_generation_attempt(
        self,
        *,
        generation_attempt_id: str,
        feature_snapshot_id: str,
        analysis_id: str,
        forecast_id: str,
        provider: str | None,
    ) -> None:
        now = datetime.now(UTC)
        with self.engine.begin() as connection:
            connection.execute(
                insert(strategy_generation_attempts).values(
                    generation_attempt_id=generation_attempt_id,
                    feature_snapshot_id=feature_snapshot_id,
                    analysis_id=analysis_id,
                    forecast_id=forecast_id,
                    status="STARTED",
                    provider=provider,
                    generation_invocation_id=None,
                    critique_invocation_id=None,
                    strategy_spec_id=None,
                    proposal_json=None,
                    critique_json=None,
                    error_code=None,
                    error_message=None,
                    created_at=now,
                    updated_at=now,
                )
            )

    def update_generation_attempt(
        self,
        generation_attempt_id: str,
        *,
        status: str,
        generation_invocation_id: str | None = None,
        critique_invocation_id: str | None = None,
        strategy_spec_id: str | None = None,
        proposal: dict[str, Any] | None = None,
        critique: dict[str, Any] | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        values: dict[str, Any] = {
            "status": status,
            "updated_at": datetime.now(UTC),
            "error_code": error_code,
            "error_message": error_message[:2_000] if error_message else None,
        }
        optional = {
            "generation_invocation_id": generation_invocation_id,
            "critique_invocation_id": critique_invocation_id,
            "strategy_spec_id": strategy_spec_id,
            "proposal_json": proposal,
            "critique_json": critique,
        }
        values.update({key: value for key, value in optional.items() if value is not None})
        with self.engine.begin() as connection:
            result = connection.execute(
                update(strategy_generation_attempts)
                .where(
                    strategy_generation_attempts.c.generation_attempt_id
                    == generation_attempt_id
                )
                .values(**values)
            )
        if int(result.rowcount or 0) != 1:
            raise ValueError("Strategy generation attempt was not found")

    def generation_attempts(self, *, limit: int = 100) -> list[dict[str, Any]]:
        statement = (
            select(
                strategy_generation_attempts,
                feature_snapshots.c.symbol,
                feature_snapshots.c.as_of.label("feature_as_of"),
            )
            .join(
                feature_snapshots,
                feature_snapshots.c.feature_snapshot_id
                == strategy_generation_attempts.c.feature_snapshot_id,
            )
            .order_by(strategy_generation_attempts.c.created_at.desc())
            .limit(limit)
        )
        with self.engine.connect() as connection:
            return [dict(row._mapping) for row in connection.execute(statement)]

    def record_backtest(self, result: BacktestResult) -> None:
        experiment = result.experiment
        with self.engine.begin() as connection:
            connection.execute(
                insert(experiment_runs).values(
                    experiment_run_id=experiment.experiment_run_id,
                    strategy_spec_id=experiment.strategy_spec_id,
                    symbol=experiment.symbol,
                    timeframe=experiment.timeframe,
                    as_of_start=experiment.as_of_start,
                    as_of_end=experiment.as_of_end,
                    dataset_hash=experiment.dataset_hash,
                    code_git_sha=experiment.code_git_sha,
                    status=experiment.status.value,
                    cost_model_json=experiment.cost_model.model_dump(mode="json"),
                    metrics_json=experiment.metrics.model_dump(mode="json"),
                    feature_snapshot_ids=list(experiment.feature_snapshot_ids),
                    started_at=experiment.started_at,
                    finished_at=experiment.finished_at,
                )
            )
            if result.trades:
                connection.execute(
                    insert(backtest_trades),
                    [trade.model_dump() for trade in result.trades],
                )
            if result.portfolio_events:
                connection.execute(
                    insert(backtest_portfolio_events),
                    [
                        {
                            **event.model_dump(exclude={"details"}),
                            "details_json": event.details,
                        }
                        for event in result.portfolio_events
                    ],
                )

    def record_feature_parity_check(self, check: FeatureParityCheck) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                insert(feature_parity_checks).values(**check.model_dump())
            )

    def record_validation_report(self, report: WalkForwardValidationReport) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                insert(validation_reports).values(
                    validation_report_id=report.validation_report_id,
                    symbol=report.symbol,
                    timeframe=report.timeframe,
                    strategy_types=list(report.strategy_types),
                    validation_subject=report.validation_subject,
                    validated_strategy_spec_ids=report.validated_strategy_spec_ids,
                    execution_contract_json=report.execution_contract,
                    execution_contract_sha256=report.execution_contract_sha256,
                    selection_metric=report.selection_metric,
                    train_bars=report.train_bars,
                    test_bars=report.test_bars,
                    step_bars=report.step_bars,
                    embargo_bars=report.embargo_bars,
                    aggregate_metrics=report.model_dump(mode="json")[
                        "aggregate_metrics"
                    ],
                    regime_metrics=report.model_dump(mode="json")["regime_metrics"],
                    robustness_metrics=report.model_dump(mode="json")[
                        "robustness_metrics"
                    ],
                    gate_assessment=report.model_dump(mode="json")[
                        "gate_assessment"
                    ],
                    report_hash=report.report_hash,
                    code_git_sha=report.code_git_sha,
                    created_at=report.created_at,
                )
            )

            connection.execute(
                insert(validation_folds),
                [
                    {
                        "validation_report_id": report.validation_report_id,
                        **fold.model_dump(
                            exclude={
                                "selected_train_metrics",
                                "selected_test_metrics",
                            },
                        ),
                        "selected_train_metrics": fold.selected_train_metrics.model_dump(
                            mode="json"
                        ),
                        "selected_test_metrics": fold.selected_test_metrics.model_dump(
                            mode="json"
                        ),
                    }
                    for fold in report.folds
                ],
            )

    def strategy_trial_count(self, *, symbol: str, timeframe: str) -> int:
        """Count explored specs and rejected/failed hybrid attempts for this contract.

        This is deliberately conservative until first-class research campaigns are
        introduced: every search against the same symbol/timeframe contributes to
        selection-bias correction, while an accepted hybrid attempt is not counted
        twice merely because its compiled spec was subsequently backtested.
        """
        with self.engine.connect() as connection:
            generated_ids = select(
                strategy_generation_attempts.c.strategy_spec_id
            ).where(strategy_generation_attempts.c.strategy_spec_id.is_not(None))
            non_generated_specs = int(
                connection.execute(
                    select(func.count(func.distinct(experiment_runs.c.strategy_spec_id)))
                    .where(experiment_runs.c.symbol == symbol.upper())
                    .where(experiment_runs.c.timeframe == timeframe)
                    .where(experiment_runs.c.strategy_spec_id.not_in(generated_ids))
                ).scalar_one()
            )
            hybrid_attempts = int(
                connection.execute(
                    select(func.count())
                    .select_from(strategy_generation_attempts)
                    .join(
                        feature_snapshots,
                        feature_snapshots.c.feature_snapshot_id
                        == strategy_generation_attempts.c.feature_snapshot_id,
                    )
                    .where(feature_snapshots.c.symbol == symbol.upper())
                    .where(feature_snapshots.c.timeframe == timeframe)
                ).scalar_one()
            )
        return max(1, non_generated_specs + hybrid_attempts)

    def recent_experiments(self, *, limit: int = 50) -> list[dict[str, Any]]:
        statement = (
            select(experiment_runs)
            .order_by(experiment_runs.c.finished_at.desc())
            .limit(limit)
        )
        with self.engine.connect() as connection:
            results = []
            for row in connection.execute(statement):
                item = self._normalize_times(
                    dict(row._mapping),
                    ("as_of_start", "as_of_end", "started_at", "finished_at"),
                )
                snapshot_ids = item.pop("feature_snapshot_ids", [])
                item["feature_snapshot_count"] = len(snapshot_ids)
                item["portfolio_event_count"] = int(
                    connection.execute(
                        select(func.count())
                        .select_from(backtest_portfolio_events)
                        .where(
                            backtest_portfolio_events.c.experiment_run_id
                            == item["experiment_run_id"]
                        )
                    ).scalar_one()
                )
                results.append(item)
            return results

    def portfolio_events(
        self,
        *,
        experiment_run_id: str,
        limit: int = 10_000,
    ) -> list[dict[str, Any]]:
        statement = (
            select(backtest_portfolio_events)
            .where(
                backtest_portfolio_events.c.experiment_run_id == experiment_run_id
            )
            .order_by(backtest_portfolio_events.c.sequence.asc())
            .limit(limit)
        )
        with self.engine.connect() as connection:
            results = []
            for row in connection.execute(statement):
                item = self._normalize_times(dict(row._mapping), ("event_time",))
                item["details"] = item.pop("details_json")
                results.append(item)
            return results

    def recent_validation_reports(self, *, limit: int = 50) -> list[dict[str, Any]]:
        statement = (
            select(validation_reports)
            .order_by(validation_reports.c.created_at.desc())
            .limit(limit)
        )
        with self.engine.connect() as connection:
            results = []
            for row in connection.execute(statement):
                item = self._normalize_times(dict(row._mapping), ("created_at",))
                item["fold_count"] = int(
                    connection.execute(
                        select(func.count())
                        .select_from(validation_folds)
                        .where(
                            validation_folds.c.validation_report_id
                            == item["validation_report_id"]
                        )
                    ).scalar_one()
                )
                results.append(item)
            return results

    def validation_report(self, validation_report_id: str) -> dict[str, Any] | None:
        with self.engine.connect() as connection:
            report_row = connection.execute(
                select(validation_reports).where(
                    validation_reports.c.validation_report_id == validation_report_id
                )
            ).one_or_none()
            if report_row is None:
                return None
            report = self._normalize_times(dict(report_row._mapping), ("created_at",))
            folds = []
            statement = (
                select(validation_folds)
                .where(validation_folds.c.validation_report_id == validation_report_id)
                .order_by(validation_folds.c.fold_number.asc())
            )
            for row in connection.execute(statement):
                item = self._normalize_times(
                    dict(row._mapping),
                    ("train_start", "train_end", "test_start", "test_end"),
                )
                folds.append(item)
            report["folds"] = folds
            return report

    def health_summary(self) -> dict[str, int]:
        with self.engine.connect() as connection:
            return {
                "evidence_packets": int(
                    connection.execute(
                        select(func.count()).select_from(evidence_packets)
                    ).scalar_one()
                ),
                "feature_snapshots": int(
                    connection.execute(
                        select(func.count()).select_from(feature_snapshots)
                    ).scalar_one()
                ),
                "strategy_specs": int(
                    connection.execute(
                        select(func.count()).select_from(strategy_specs)
                    ).scalar_one()
                ),
                "experiment_runs": int(
                    connection.execute(
                        select(func.count()).select_from(experiment_runs)
                    ).scalar_one()
                ),
                "backtest_trades": int(
                    connection.execute(
                        select(func.count()).select_from(backtest_trades)
                    ).scalar_one()
                ),
                "backtest_portfolio_events": int(
                    connection.execute(
                        select(func.count()).select_from(backtest_portfolio_events)
                    ).scalar_one()
                ),
                "feature_parity_checks": int(
                    connection.execute(
                        select(func.count()).select_from(feature_parity_checks)
                    ).scalar_one()
                ),
                "validation_reports": int(
                    connection.execute(
                        select(func.count()).select_from(validation_reports)
                    ).scalar_one()
                ),
                "validation_folds": int(
                    connection.execute(
                        select(func.count()).select_from(validation_folds)
                    ).scalar_one()
                ),
            }

    def _insert_ignore(
        self,
        table: Any,
        values: dict[str, Any],
        identity_columns: list[str],
    ) -> Any:
        if self.engine.dialect.name == "postgresql":
            return postgresql_insert(table).values(values).on_conflict_do_nothing(
                index_elements=identity_columns
            )
        if self.engine.dialect.name == "sqlite":
            return sqlite_insert(table).values(values).on_conflict_do_nothing(
                index_elements=identity_columns
            )
        raise RuntimeError(f"Unsupported SQL dialect: {self.engine.dialect.name}")

    @staticmethod
    def _evidence_packet_from_row(row: dict[str, Any]) -> EvidencePacket:
        return EvidencePacket(
            evidence_packet_id=str(row["evidence_packet_id"]),
            symbol=str(row["symbol"]),
            as_of=_utc(row["as_of"]),
            evidence_hash=str(row["evidence_hash"]),
            references=tuple(
                EvidenceReference.model_validate(item)
                for item in row["references_json"]
            ),
            source_max_available_from=_utc(row["source_max_available_from"]),
            created_at=_utc(row["created_at"]),
        )

    @staticmethod
    def _feature_snapshot_from_row(row: dict[str, Any]) -> PointInTimeFeatureSnapshot:
        return PointInTimeFeatureSnapshot(
            feature_snapshot_id=str(row["feature_snapshot_id"]),
            evidence_packet_id=str(row["evidence_packet_id"]),
            symbol=str(row["symbol"]),
            timeframe=str(row["timeframe"]),
            as_of=_utc(row["as_of"]),
            feature_set_version=str(row["feature_set_version"]),
            values=row["feature_values"],
            source_max_available_from=_utc(row["source_max_available_from"]),
            data_hash=str(row["data_hash"]),
            created_at=_utc(row["created_at"]),
        )

    @staticmethod
    def _strategy_spec_from_row(row: dict[str, Any]) -> StrategySpec:
        return StrategySpec(
            strategy_spec_id=str(row["strategy_spec_id"]),
            name=str(row["name"]),
            version=str(row["version"]),
            strategy_type=str(row["strategy_type"]),
            timeframe=str(row["timeframe"]),
            feature_set_version=str(row["feature_set_version"]),
            parameters=row["parameters_json"],
            data_requirements=row["data_requirements_json"],
            code_sha256=str(row["code_sha256"]),
            created_at=_utc(row["created_at"]),
        )

    @staticmethod
    def _normalize_times(
        row: dict[str, Any],
        fields: tuple[str, ...],
    ) -> dict[str, Any]:
        for field in fields:
            value = row.get(field)
            if isinstance(value, datetime):
                row[field] = _utc(value)
        return row


__all__ = ["ResearchStore", "_canonical_hash"]
