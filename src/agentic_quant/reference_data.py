from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Engine, and_, func, or_, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from agentic_quant.database import (
    corporate_actions,
    reference_imports,
    universe_memberships,
)
from agentic_quant.domain import (
    CorporateAction,
    CorporateActionType,
    EventEnvelope,
    ReferenceImportResult,
    UniverseMembership,
)
from agentic_quant.ids import uuid7
from agentic_quant.ledger import EventLedger


def corporate_action_fingerprint(
    *,
    symbol: str,
    action_type: str,
    effective_at: datetime,
    split_ratio: str | None = None,
    cash_amount: str | None = None,
    new_symbol: str | None = None,
    source: str,
) -> str:
    material = json.dumps(
        {
            "symbol": symbol.upper(),
            "action_type": action_type,
            "effective_at": effective_at.isoformat(),
            "split_ratio": split_ratio,
            "cash_amount": cash_amount,
            "new_symbol": new_symbol,
            "source": source,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(material).hexdigest()


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class ReferenceDataStore:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def insert_corporate_actions(
        self,
        actions: tuple[CorporateAction, ...],
    ) -> tuple[str, ...]:
        if not actions:
            return ()
        records = [action.model_dump() for action in actions]
        with self.engine.begin() as connection:
            statement = self._insert_ignore(
                corporate_actions,
                records,
                ["action_fingerprint"],
            ).returning(corporate_actions.c.corporate_action_id)
            return tuple(str(value) for value in connection.execute(statement).scalars())

    def insert_universe_memberships(
        self,
        memberships: tuple[UniverseMembership, ...],
    ) -> tuple[str, ...]:
        if not memberships:
            return ()
        records = [membership.model_dump() for membership in memberships]
        identity = ["universe", "symbol", "effective_from", "source", "source_version"]
        with self.engine.begin() as connection:
            statement = self._insert_ignore(
                universe_memberships,
                records,
                identity,
            ).returning(universe_memberships.c.membership_id)
            return tuple(str(value) for value in connection.execute(statement).scalars())

    def corporate_actions_as_of(
        self,
        *,
        symbol: str,
        as_of: datetime,
        effective_from: datetime | None = None,
    ) -> tuple[CorporateAction, ...]:
        conditions = [
            corporate_actions.c.symbol == symbol.upper(),
            corporate_actions.c.effective_at <= as_of,
            corporate_actions.c.available_from <= as_of,
        ]
        if effective_from is not None:
            conditions.append(corporate_actions.c.effective_at >= effective_from)
        statement = (
            select(corporate_actions)
            .where(and_(*conditions))
            .order_by(corporate_actions.c.effective_at.asc())
        )
        with self.engine.connect() as connection:
            rows = connection.execute(statement).all()
        return tuple(self._action_from_row(dict(row._mapping)) for row in rows)

    def corporate_actions_effective_between(
        self,
        *,
        symbol: str,
        start: datetime,
        end: datetime,
    ) -> tuple[CorporateAction, ...]:
        """Return realized accounting events without applying a knowledge-time filter."""
        statement = (
            select(corporate_actions)
            .where(
                and_(
                    corporate_actions.c.symbol == symbol.upper(),
                    corporate_actions.c.effective_at >= start,
                    corporate_actions.c.effective_at <= end,
                )
            )
            .order_by(
                corporate_actions.c.effective_at.asc(),
                corporate_actions.c.corporate_action_id.asc(),
            )
        )
        with self.engine.connect() as connection:
            rows = connection.execute(statement).all()
        return tuple(self._action_from_row(dict(row._mapping)) for row in rows)

    def universe_symbols_as_of(
        self,
        *,
        universe: str,
        as_of: datetime,
    ) -> tuple[str, ...]:
        statement = (
            select(universe_memberships.c.symbol)
            .where(
                and_(
                    universe_memberships.c.universe == universe,
                    universe_memberships.c.effective_from <= as_of,
                    or_(
                        universe_memberships.c.effective_to.is_(None),
                        universe_memberships.c.effective_to > as_of,
                    ),
                    universe_memberships.c.available_from <= as_of,
                )
            )
            .order_by(universe_memberships.c.symbol.asc())
        )
        with self.engine.connect() as connection:
            return tuple(str(value) for value in connection.execute(statement).scalars())

    def health_summary(self) -> dict[str, int]:
        with self.engine.connect() as connection:
            return {
                "corporate_actions": int(
                    connection.execute(
                        select(func.count()).select_from(corporate_actions)
                    ).scalar_one()
                ),
                "universe_memberships": int(
                    connection.execute(
                        select(func.count()).select_from(universe_memberships)
                    ).scalar_one()
                ),
                "reference_imports": int(
                    connection.execute(
                        select(func.count()).select_from(reference_imports)
                    ).scalar_one()
                ),
            }

    def record_reference_import(self, result: ReferenceImportResult) -> bool:
        values = result.model_dump()
        if self.engine.dialect.name == "postgresql":
            statement = (
                postgresql_insert(reference_imports)
                .values(values)
                .on_conflict_do_nothing(
                    index_elements=[
                        "dataset_type",
                        "source",
                        "source_version",
                        "content_sha256",
                    ]
                )
                .returning(reference_imports.c.reference_import_id)
            )
        elif self.engine.dialect.name == "sqlite":
            statement = (
                sqlite_insert(reference_imports)
                .values(values)
                .on_conflict_do_nothing(
                    index_elements=[
                        "dataset_type",
                        "source",
                        "source_version",
                        "content_sha256",
                    ]
                )
                .returning(reference_imports.c.reference_import_id)
            )
        else:
            raise RuntimeError(f"Unsupported SQL dialect: {self.engine.dialect.name}")
        with self.engine.begin() as connection:
            return connection.execute(statement).scalar_one_or_none() is not None

    def reference_import_by_identity(
        self,
        *,
        dataset_type: str,
        source: str,
        source_version: str,
        content_sha256: str,
    ) -> ReferenceImportResult | None:
        statement = select(reference_imports).where(
            and_(
                reference_imports.c.dataset_type == dataset_type,
                reference_imports.c.source == source,
                reference_imports.c.source_version == source_version,
                reference_imports.c.content_sha256 == content_sha256,
            )
        )
        with self.engine.connect() as connection:
            row = connection.execute(statement).one_or_none()
        if row is None:
            return None
        values = dict(row._mapping)
        values["created_at"] = _utc(values["created_at"])
        return ReferenceImportResult.model_validate(values)

    def _insert_ignore(
        self,
        table: Any,
        values: list[dict[str, Any]],
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
    def _action_from_row(row: dict[str, Any]) -> CorporateAction:
        return CorporateAction(
            **{
                **row,
                "effective_at": _utc(row["effective_at"]),
                "available_from": _utc(row["available_from"]),
                "ingested_at": _utc(row["ingested_at"]),
            }
        )


class GovernedReferenceImporter:
    def __init__(
        self,
        store: ReferenceDataStore,
        ledger: EventLedger | None = None,
    ) -> None:
        self.store = store
        self.ledger = ledger

    def import_payload(self, payload: dict[str, Any]) -> ReferenceImportResult:
        dataset_type = str(payload.get("dataset_type") or "")
        source = str(payload.get("source") or "")
        source_version = str(payload.get("source_version") or "")
        records = payload.get("records")
        if dataset_type not in {"corporate_actions", "universe_memberships"}:
            raise ValueError("Unsupported reference dataset_type")
        if not source or not source_version:
            raise ValueError("Reference import requires source and source_version")
        if not isinstance(records, list) or not records:
            raise ValueError("Reference import records must be a nonempty list")
        if len(records) > 100_000:
            raise ValueError("Reference import exceeds the 100000-record safety limit")
        content_sha256 = hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode()
        ).hexdigest()
        existing = self.store.reference_import_by_identity(
            dataset_type=dataset_type,
            source=source,
            source_version=source_version,
            content_sha256=content_sha256,
        )
        if existing is not None:
            return existing.model_copy(update={"records_inserted": 0})
        raw_object_id = f"REFERENCE_IMPORT_{content_sha256}"
        if dataset_type == "corporate_actions":
            values = tuple(
                self._corporate_action(
                    item,
                    source=source,
                    raw_object_id=raw_object_id,
                )
                for item in records
            )
            inserted = len(self.store.insert_corporate_actions(values))
        else:
            memberships = tuple(
                self._universe_membership(
                    item,
                    source=source,
                    source_version=source_version,
                )
                for item in records
            )
            inserted = len(self.store.insert_universe_memberships(memberships))
        result = ReferenceImportResult(
            reference_import_id=uuid7(),
            dataset_type=dataset_type,
            source=source,
            source_version=source_version,
            content_sha256=content_sha256,
            records_received=len(records),
            records_inserted=inserted,
            status="COMPLETED",
            created_at=datetime.now(UTC),
        )
        inserted_import = self.store.record_reference_import(result)
        if not inserted_import:
            existing = self.store.reference_import_by_identity(
                dataset_type=dataset_type,
                source=source,
                source_version=source_version,
                content_sha256=content_sha256,
            )
            if existing is None:
                raise RuntimeError("Reference import audit conflict could not be resolved")
            return existing.model_copy(update={"records_inserted": 0})
        if inserted_import and self.ledger is not None:
            self.ledger.append(
                EventEnvelope(
                    event_id=uuid7(),
                    event_type="reference_data.imported.v1",
                    event_time=result.created_at,
                    emitted_at=datetime.now(UTC),
                    producer="reference-data-importer",
                    correlation_id=result.reference_import_id,
                    payload=result.model_dump(mode="json"),
                )
            )
        return result

    @staticmethod
    def _corporate_action(
        raw: Any,
        *,
        source: str,
        raw_object_id: str,
    ) -> CorporateAction:
        if not isinstance(raw, dict):
            raise ValueError("Corporate-action records must be objects")
        action_type = CorporateActionType(str(raw.get("action_type")))
        effective_at = datetime.fromisoformat(str(raw.get("effective_at")))
        available_from = datetime.fromisoformat(str(raw.get("available_from")))
        if effective_at.tzinfo is None or available_from.tzinfo is None:
            raise ValueError("Reference-data timestamps must include timezones")
        split_ratio = (
            Decimal(str(raw["split_ratio"]))
            if raw.get("split_ratio") is not None
            else None
        )
        cash_amount = (
            Decimal(str(raw["cash_amount"]))
            if raw.get("cash_amount") is not None
            else None
        )
        symbol = str(raw.get("symbol") or "").upper()
        if not symbol:
            raise ValueError("Corporate-action records require symbol")
        fingerprint = corporate_action_fingerprint(
            symbol=symbol,
            action_type=action_type.value,
            effective_at=effective_at,
            split_ratio=str(split_ratio) if split_ratio is not None else None,
            cash_amount=str(cash_amount) if cash_amount is not None else None,
            new_symbol=(str(raw["new_symbol"]) if raw.get("new_symbol") else None),
            source=source,
        )
        return CorporateAction(
            corporate_action_id=uuid7(),
            action_fingerprint=fingerprint,
            symbol=symbol,
            action_type=action_type,
            effective_at=effective_at,
            available_from=available_from,
            split_ratio=split_ratio,
            cash_amount=cash_amount,
            currency=(str(raw["currency"]) if raw.get("currency") else None),
            new_symbol=(str(raw["new_symbol"]) if raw.get("new_symbol") else None),
            source=source,
            raw_object_id=raw_object_id,
            ingested_at=datetime.now(UTC),
        )

    @staticmethod
    def _universe_membership(
        raw: Any,
        *,
        source: str,
        source_version: str,
    ) -> UniverseMembership:
        if not isinstance(raw, dict):
            raise ValueError("Universe-membership records must be objects")
        effective_from = datetime.fromisoformat(str(raw.get("effective_from")))
        effective_to = (
            datetime.fromisoformat(str(raw["effective_to"]))
            if raw.get("effective_to") is not None
            else None
        )
        available_from = datetime.fromisoformat(str(raw.get("available_from")))
        if any(
            value.tzinfo is None
            for value in (effective_from, available_from, effective_to)
            if value is not None
        ):
            raise ValueError("Reference-data timestamps must include timezones")
        universe = str(raw.get("universe") or "")
        symbol = str(raw.get("symbol") or "").upper()
        if not universe or not symbol:
            raise ValueError("Universe-membership records require universe and symbol")
        return UniverseMembership(
            membership_id=uuid7(),
            universe=universe,
            symbol=symbol,
            effective_from=effective_from,
            effective_to=effective_to,
            available_from=available_from,
            source=source,
            source_version=source_version,
            created_at=datetime.now(UTC),
        )
