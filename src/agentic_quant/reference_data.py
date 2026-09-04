from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Engine, and_, func, or_, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from agentic_quant.database import corporate_actions, universe_memberships
from agentic_quant.domain import CorporateAction, UniverseMembership


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
            }

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
