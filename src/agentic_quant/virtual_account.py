from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Connection, Engine, insert, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from agentic_quant.database import (
    strategy_sleeves,
    virtual_account_risk_revisions,
    virtual_accounts,
)
from agentic_quant.ids import stable_uuid, uuid7
from agentic_quant.risk import RiskPolicy


MAIN_VIRTUAL_ACCOUNT_ID = "cffdf661-b2c0-5e58-a49c-13f8a2403d98"
MAIN_VIRTUAL_ACCOUNT_SLUG = "shadow-main"
_ZERO = Decimal("0")


class VirtualAccountStore:
    """Shared virtual cash and portfolio-risk ledger for all strategy sleeves."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def ensure_main_account(
        self,
        *,
        initial_cash: Decimal,
        risk_policy: RiskPolicy,
    ) -> dict[str, Any]:
        if initial_cash <= 0:
            raise ValueError("Virtual account initial cash must be positive")
        now = datetime.now(UTC)
        account_values = {
            "virtual_account_id": MAIN_VIRTUAL_ACCOUNT_ID,
            "slug": MAIN_VIRTUAL_ACCOUNT_SLUG,
            "name": "Shared Shadow Master",
            "account_type": "SHARED_MASTER",
            "currency": "USD",
            "status": "ACTIVE",
            "initial_cash": initial_cash,
            "cash_balance": initial_cash,
            "realized_pnl": _ZERO,
            "reserved_cash": _ZERO,
            "reserved_risk_usd": _ZERO,
            "base_policy_version": risk_policy.version,
            "risk_revision": 1,
            "initial_risk_fraction": risk_policy.initial_risk_fraction,
            "maximum_trade_risk_usd": risk_policy.maximum_trade_risk_usd,
            "maximum_concurrent_risk_usd": (
                risk_policy.maximum_concurrent_risk_usd
            ),
            "daily_loss_stop_usd": risk_policy.daily_loss_stop_usd,
            "account_floor_usd": risk_policy.account_floor_usd,
            "created_at": now,
            "updated_at": now,
        }
        with self.engine.begin() as connection:
            statement = self._insert_ignore(
                virtual_accounts,
                account_values,
                conflict_columns=("virtual_account_id",),
            )
            connection.execute(statement)
            row = connection.execute(
                select(virtual_accounts).where(
                    virtual_accounts.c.virtual_account_id
                    == MAIN_VIRTUAL_ACCOUNT_ID
                )
            ).one()
            if Decimal(str(row.initial_cash)) != initial_cash:
                raise ValueError(
                    "Configured initial cash does not match the existing virtual account"
                )
            revision_values = {
                "risk_revision_id": stable_uuid(
                    "virtual-account-risk", MAIN_VIRTUAL_ACCOUNT_ID, 1
                ),
                "virtual_account_id": MAIN_VIRTUAL_ACCOUNT_ID,
                "revision_number": 1,
                "initial_risk_fraction": row.initial_risk_fraction,
                "maximum_trade_risk_usd": row.maximum_trade_risk_usd,
                "maximum_concurrent_risk_usd": (
                    row.maximum_concurrent_risk_usd
                ),
                "daily_loss_stop_usd": row.daily_loss_stop_usd,
                "account_floor_usd": row.account_floor_usd,
                "reason": "Initial governed virtual-account risk policy",
                "created_by": "system-bootstrap",
                "created_at": row.created_at,
            }
            connection.execute(
                self._insert_ignore(
                    virtual_account_risk_revisions,
                    revision_values,
                    conflict_columns=("virtual_account_id", "revision_number"),
                )
            )
        return self.account(MAIN_VIRTUAL_ACCOUNT_ID)

    def account(self, account_id: str = MAIN_VIRTUAL_ACCOUNT_ID) -> dict[str, Any]:
        with self.engine.connect() as connection:
            row = connection.execute(
                select(virtual_accounts).where(
                    virtual_accounts.c.virtual_account_id == account_id
                )
            ).one_or_none()
            if row is None:
                raise ValueError("Virtual account not found")
            sleeves = connection.execute(
                select(strategy_sleeves)
                .where(strategy_sleeves.c.virtual_account_id == account_id)
                .order_by(strategy_sleeves.c.created_at.asc())
            ).all()
        value = dict(row._mapping)
        value["available_cash"] = max(
            _ZERO,
            Decimal(str(value["cash_balance"]))
            - Decimal(str(value["reserved_cash"])),
        )
        value["available_risk_usd"] = max(
            _ZERO,
            Decimal(str(value["maximum_concurrent_risk_usd"]))
            - Decimal(str(value["reserved_risk_usd"])),
        )
        value["sleeves"] = [dict(item._mapping) for item in sleeves]
        return value

    def effective_risk_policy(
        self,
        base_policy: RiskPolicy,
        *,
        account_id: str = MAIN_VIRTUAL_ACCOUNT_ID,
    ) -> RiskPolicy:
        account = self.account(account_id)
        revision = int(account["risk_revision"])
        if revision == 1:
            return base_policy
        version = str(account["base_policy_version"])
        version = f"{version}+account-r{revision}"
        return base_policy.model_copy(
            update={
                "version": version,
                "initial_risk_fraction": Decimal(
                    str(account["initial_risk_fraction"])
                ),
                "maximum_trade_risk_usd": Decimal(
                    str(account["maximum_trade_risk_usd"])
                ),
                "maximum_concurrent_risk_usd": Decimal(
                    str(account["maximum_concurrent_risk_usd"])
                ),
                "daily_loss_stop_usd": Decimal(
                    str(account["daily_loss_stop_usd"])
                ),
                "account_floor_usd": Decimal(str(account["account_floor_usd"])),
            }
        )

    def ensure_sleeve(
        self,
        *,
        strategy_spec_id: str,
        symbol: str,
        account_id: str = MAIN_VIRTUAL_ACCOUNT_ID,
        connection: Connection | None = None,
    ) -> str:
        normalized_symbol = symbol.strip().upper()
        sleeve_id = stable_uuid(
            "strategy-sleeve", account_id, strategy_spec_id, normalized_symbol
        )
        values = {
            "strategy_sleeve_id": sleeve_id,
            "virtual_account_id": account_id,
            "strategy_spec_id": strategy_spec_id,
            "symbol": normalized_symbol,
            "status": "ACTIVE",
            "created_at": datetime.now(UTC),
            "updated_at": datetime.now(UTC),
        }
        if connection is not None:
            connection.execute(
                self._insert_ignore(
                    strategy_sleeves,
                    values,
                    conflict_columns=(
                        "virtual_account_id",
                        "strategy_spec_id",
                        "symbol",
                    ),
                )
            )
            return sleeve_id
        with self.engine.begin() as owned_connection:
            owned_connection.execute(
                self._insert_ignore(
                    strategy_sleeves,
                    values,
                    conflict_columns=(
                        "virtual_account_id",
                        "strategy_spec_id",
                        "symbol",
                    ),
                )
            )
        return sleeve_id

    def preview_risk_update(
        self,
        account_id: str,
        raw_limits: dict[str, Any],
    ) -> dict[str, Any]:
        current = self.account(account_id)
        if Decimal(str(current["reserved_risk_usd"])) > 0:
            raise ValueError(
                "Close or cancel open trade plans before revising account risk"
            )
        fields = (
            "initial_risk_fraction",
            "maximum_trade_risk_usd",
            "maximum_concurrent_risk_usd",
            "daily_loss_stop_usd",
            "account_floor_usd",
        )
        proposed = {
            field: Decimal(str(raw_limits.get(field, current[field])))
            for field in fields
        }
        if proposed["initial_risk_fraction"] <= 0 or proposed[
            "initial_risk_fraction"
        ] > 1:
            raise ValueError("Initial risk fraction must be in (0, 1]")
        for field in fields[1:]:
            if proposed[field] <= 0:
                raise ValueError(f"{field} must be positive")
        if (
            proposed["maximum_trade_risk_usd"]
            > proposed["maximum_concurrent_risk_usd"]
        ):
            raise ValueError("Per-trade risk cannot exceed concurrent risk")
        if proposed["account_floor_usd"] >= Decimal(str(current["initial_cash"])):
            raise ValueError("Account floor must be below initial cash")
        return {
            "account_id": account_id,
            "current_revision": int(current["risk_revision"]),
            "next_revision": int(current["risk_revision"]) + 1,
            "current": {field: str(current[field]) for field in fields},
            "proposed": {field: str(proposed[field]) for field in fields},
            "invalidates_existing_validation_contracts": True,
            "live_broker_effect": False,
        }

    def update_risk(
        self,
        account_id: str,
        raw_limits: dict[str, Any],
        *,
        reason: str,
        created_by: str,
    ) -> dict[str, Any]:
        preview = self.preview_risk_update(account_id, raw_limits)
        proposed = preview["proposed"]
        now = datetime.now(UTC)
        revision = int(preview["next_revision"])
        with self.engine.begin() as connection:
            result = connection.execute(
                update(virtual_accounts)
                .where(
                    (virtual_accounts.c.virtual_account_id == account_id)
                    & (
                        virtual_accounts.c.risk_revision
                        == int(preview["current_revision"])
                    )
                )
                .values(
                    **{key: Decimal(value) for key, value in proposed.items()},
                    risk_revision=revision,
                    updated_at=now,
                )
            )
            if int(result.rowcount or 0) != 1:
                raise ValueError("Risk policy changed; preview and confirm again")
            connection.execute(
                insert(virtual_account_risk_revisions).values(
                    risk_revision_id=uuid7(),
                    virtual_account_id=account_id,
                    revision_number=revision,
                    **{key: Decimal(value) for key, value in proposed.items()},
                    reason=reason.strip(),
                    created_by=created_by,
                    created_at=now,
                )
            )
        return self.account(account_id)

    def reserve(
        self,
        *,
        account_id: str,
        cash: Decimal,
        risk_usd: Decimal,
        connection: Connection,
    ) -> bool:
        if cash < 0 or risk_usd <= 0:
            raise ValueError("Reservation cash must be nonnegative and risk positive")
        statement = (
            update(virtual_accounts)
            .where(
                (virtual_accounts.c.virtual_account_id == account_id)
                & (virtual_accounts.c.status == "ACTIVE")
                & (
                    virtual_accounts.c.cash_balance
                    - virtual_accounts.c.reserved_cash
                    >= cash
                )
                & (
                    virtual_accounts.c.reserved_risk_usd + risk_usd
                    <= virtual_accounts.c.maximum_concurrent_risk_usd
                )
            )
            .values(
                reserved_cash=virtual_accounts.c.reserved_cash + cash,
                reserved_risk_usd=virtual_accounts.c.reserved_risk_usd + risk_usd,
                updated_at=datetime.now(UTC),
            )
        )
        return int(connection.execute(statement).rowcount or 0) == 1

    def release_or_settle(
        self,
        *,
        account_id: str,
        reserved_cash: Decimal,
        reserved_risk_usd: Decimal,
        realized_pnl_delta: Decimal,
        connection: Connection,
    ) -> None:
        result = connection.execute(
            update(virtual_accounts)
            .where(
                (virtual_accounts.c.virtual_account_id == account_id)
                & (virtual_accounts.c.reserved_cash >= reserved_cash)
                & (
                    virtual_accounts.c.reserved_risk_usd
                    >= reserved_risk_usd
                )
            )
            .values(
                cash_balance=virtual_accounts.c.cash_balance + realized_pnl_delta,
                realized_pnl=virtual_accounts.c.realized_pnl + realized_pnl_delta,
                reserved_cash=virtual_accounts.c.reserved_cash - reserved_cash,
                reserved_risk_usd=(
                    virtual_accounts.c.reserved_risk_usd - reserved_risk_usd
                ),
                updated_at=datetime.now(UTC),
            )
        )
        if int(result.rowcount or 0) != 1:
            raise RuntimeError("Virtual account reservation settlement failed")

    def _insert_ignore(
        self,
        table: Any,
        values: dict[str, Any],
        *,
        conflict_columns: tuple[str, ...],
    ) -> Any:
        if self.engine.dialect.name == "postgresql":
            return (
                postgresql_insert(table)
                .values(**values)
                .on_conflict_do_nothing(index_elements=list(conflict_columns))
            )
        if self.engine.dialect.name == "sqlite":
            return (
                sqlite_insert(table)
                .values(**values)
                .on_conflict_do_nothing(index_elements=list(conflict_columns))
            )
        raise RuntimeError(f"Unsupported SQL dialect: {self.engine.dialect.name}")
