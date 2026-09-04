from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Engine, and_, func, insert, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from agentic_quant.database import (
    backtest_trades,
    catalysts,
    corporate_facts,
    evidence_packets,
    experiment_runs,
    feature_snapshots,
    market_bars,
    strategy_specs,
)
from agentic_quant.domain import (
    BacktestResult,
    EvidencePacket,
    EvidenceReference,
    PointInTimeFeatureSnapshot,
    StockBar,
    StrategySpec,
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
                results.append(item)
            return results

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
