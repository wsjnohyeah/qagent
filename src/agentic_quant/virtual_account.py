from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Connection, Engine, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from agentic_quant.database import (
    shadow_deployments,
    strategy_sleeves,
    virtual_account_risk_revisions,
    virtual_accounts,
)
from agentic_quant.ids import stable_uuid, uuid7
from agentic_quant.risk import RiskPolicy


MAIN_VIRTUAL_ACCOUNT_ID = "cffdf661-b2c0-5e58-a49c-13f8a2403d98"
MAIN_VIRTUAL_ACCOUNT_SLUG = "shadow-main"
STRATEGY_SANDBOX_ACCOUNT_TYPE = "STRATEGY_SANDBOX"
STRATEGY_SANDBOX_INITIAL_EQUITY = Decimal("10000")
_ZERO = Decimal("0")


class VirtualAccountStore:
    """Virtual cash/risk ledgers for legacy shared and isolated strategy accounts."""

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
            "baseline_stop_fraction": risk_policy.baseline_stop_fraction,
            "baseline_target_r_multiple": risk_policy.baseline_target_r_multiple,
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
                "baseline_stop_fraction": row.baseline_stop_fraction,
                "baseline_target_r_multiple": row.baseline_target_r_multiple,
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

    def ensure_strategy_sandbox(
        self,
        *,
        deployment_id: str,
        strategy_spec_id: str,
        symbol: str,
        risk_policy: RiskPolicy,
        initial_cash: Decimal = STRATEGY_SANDBOX_INITIAL_EQUITY,
        connection: Connection | None = None,
    ) -> str:
        if initial_cash != STRATEGY_SANDBOX_INITIAL_EQUITY:
            raise ValueError("Strategy Shadow sandboxes require exactly $10,000")
        account_id = stable_uuid("shadow-sandbox-account", deployment_id)
        now = datetime.now(UTC)
        values = {
            "virtual_account_id": account_id,
            "slug": f"shadow-sandbox-{deployment_id}",
            "name": f"{symbol.upper()} strategy sandbox",
            "account_type": STRATEGY_SANDBOX_ACCOUNT_TYPE,
            "currency": "USD",
            "status": "ACTIVE",
            "initial_cash": initial_cash,
            "cash_balance": initial_cash,
            "realized_pnl": _ZERO,
            "reserved_cash": _ZERO,
            "reserved_risk_usd": _ZERO,
            "base_policy_version": risk_policy.version,
            "risk_revision": 1,
            "baseline_stop_fraction": risk_policy.baseline_stop_fraction,
            "baseline_target_r_multiple": risk_policy.baseline_target_r_multiple,
            "initial_risk_fraction": risk_policy.initial_risk_fraction,
            "maximum_trade_risk_usd": initial_cash,
            "maximum_concurrent_risk_usd": initial_cash,
            "daily_loss_stop_usd": initial_cash,
            "account_floor_usd": (
                initial_cash * (Decimal("1") - Decimal("0.12"))
            ),
            "created_at": now,
            "updated_at": now,
        }
        owned = connection is None
        target = connection or self.engine.connect()
        try:
            target.execute(
                self._insert_ignore(
                    virtual_accounts,
                    values,
                    conflict_columns=("virtual_account_id",),
                )
            )
            target.execute(
                self._insert_ignore(
                    virtual_account_risk_revisions,
                    {
                        "risk_revision_id": stable_uuid(
                            "virtual-account-risk", account_id, 1
                        ),
                        "virtual_account_id": account_id,
                        "revision_number": 1,
                        "baseline_stop_fraction": risk_policy.baseline_stop_fraction,
                        "baseline_target_r_multiple": (
                            risk_policy.baseline_target_r_multiple
                        ),
                        "initial_risk_fraction": risk_policy.initial_risk_fraction,
                        "maximum_trade_risk_usd": initial_cash,
                        "maximum_concurrent_risk_usd": initial_cash,
                        "daily_loss_stop_usd": initial_cash,
                        "account_floor_usd": values["account_floor_usd"],
                        "reason": (
                            "Initial isolated strategy Shadow sandbox risk policy"
                        ),
                        "created_by": "shadow-runtime",
                        "created_at": now,
                    },
                    conflict_columns=("virtual_account_id", "revision_number"),
                )
            )
            if owned:
                target.commit()
        finally:
            if owned:
                target.close()
        return account_id

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
            unrealized_pnl = connection.execute(
                select(func.coalesce(func.sum(shadow_deployments.c.unrealized_pnl), 0))
                .where(shadow_deployments.c.virtual_account_id == account_id)
            ).scalar_one()
        value = dict(row._mapping)
        value["unrealized_pnl"] = Decimal(str(unrealized_pnl))
        value["equity"] = (
            Decimal(str(value["cash_balance"])) + value["unrealized_pnl"]
        )
        value["loss_from_initial_fraction"] = (
            max(
                _ZERO,
                Decimal(str(value["initial_cash"])) - value["equity"],
            )
            / Decimal(str(value["initial_cash"]))
        )
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
        version = str(account["base_policy_version"])
        revision = int(account["risk_revision"])
        if revision > 1:
            version = f"{version}+account-r{revision}"
        sandbox = str(account["account_type"]) == STRATEGY_SANDBOX_ACCOUNT_TYPE
        return base_policy.model_copy(
            update={
                "version": version,
                "baseline_stop_fraction": Decimal(
                    str(account["baseline_stop_fraction"])
                ),
                "baseline_target_r_multiple": Decimal(
                    str(account["baseline_target_r_multiple"])
                ),
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
                "position_sizing_mode": (
                    "equity_fraction" if sandbox else "portfolio_caps"
                ),
                "daily_loss_limit_enabled": False if sandbox else True,
                "volatility_geometry_enabled": sandbox,
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
            "baseline_stop_fraction",
            "baseline_target_r_multiple",
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
        if not 0 < proposed["baseline_stop_fraction"] < 1:
            raise ValueError("Baseline stop fraction must be in (0, 1)")
        if proposed["baseline_target_r_multiple"] <= 0:
            raise ValueError("Baseline target R multiple must be positive")
        if proposed["initial_risk_fraction"] <= 0 or proposed[
            "initial_risk_fraction"
        ] > 1:
            raise ValueError("Initial risk fraction must be in (0, 1]")
        for field in fields[3:]:
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
