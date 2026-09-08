from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import hashlib
import json
from typing import Any, Protocol

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine

from agentic_quant.database import (
    paper_account_snapshots,
    paper_enrollments,
    paper_order_legs,
    paper_order_events,
    paper_orders,
    paper_position_snapshots,
    paper_runs,
    runtime_leases,
    shadow_deployments,
    shadow_signal_candidates,
    shadow_trade_plans,
    strategy_specs,
)
from agentic_quant.ids import stable_uuid, uuid7
from agentic_quant.providers.alpaca_paper import (
    AlpacaPaperResponseError,
    normalize_long_bracket_prices,
)
from agentic_quant.risk import (
    BASELINE_EXECUTION_PROFILE_VERSION,
    MULTI_SESSION_EXECUTION_PROFILE_VERSION,
)
from agentic_quant.shadow import ShadowRuntime


class PaperBroker(Protocol):
    async def __aenter__(self) -> PaperBroker: ...

    async def __aexit__(self, *_args: object) -> None: ...

    async def fetch_account(self) -> dict[str, Any]: ...

    async def fetch_positions(self) -> tuple[dict[str, Any], ...]: ...

    async def fetch_order_by_client_id(
        self, client_order_id: str
    ) -> dict[str, Any] | None: ...

    async def submit_bracket_order(
        self,
        *,
        client_order_id: str,
        symbol: str,
        quantity: int,
        entry_limit_price: Decimal,
        take_profit_price: Decimal,
        stop_loss_price: Decimal,
    ) -> dict[str, Any]: ...

    async def cancel_order(self, broker_order_id: str) -> None: ...

    async def submit_market_exit_order(
        self,
        *,
        client_order_id: str,
        symbol: str,
        quantity: Decimal,
        time_in_force: str,
    ) -> dict[str, Any]: ...


BrokerFactory = Callable[[], PaperBroker]
_ZERO = Decimal("0")
PAPER_EXECUTION_PROFILE_VERSION = BASELINE_EXECUTION_PROFILE_VERSION
_SESSION_CLOSE_LEAD = timedelta(minutes=20)
_TERMINAL_FAILURES = {
    "canceled",
    "expired",
    "rejected",
    "replaced",
    "stopped",
    "suspended",
}
_TERMINAL_STATUSES = _TERMINAL_FAILURES | {"filled"}


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _decimal(value: object, default: str = "0") -> Decimal:
    if value in {None, ""}:
        return Decimal(default)
    return Decimal(str(value))


def _safe_payload(payload: dict[str, Any]) -> dict[str, Any]:
    blocked = {"account_number"}

    def scrub(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                str(key): scrub(item)
                for key, item in value.items()
                if str(key).lower() not in blocked
            }
        if isinstance(value, list):
            return [scrub(item) for item in value]
        return value

    result = scrub(payload)
    assert isinstance(result, dict)
    return result


def _order_lifecycle_complete(
    payload: dict[str, Any],
    *,
    position_quantity: Decimal = _ZERO,
) -> bool:
    status = str(payload.get("status") or "unknown").lower()
    if status in _TERMINAL_FAILURES:
        if position_quantity != _ZERO:
            return False
        failure_legs = payload.get("legs")
        if not isinstance(failure_legs, list) or not failure_legs:
            return True
        failure_leg_statuses = {
            str(leg.get("status") or "unknown").lower()
            for leg in failure_legs
            if isinstance(leg, dict)
        }
        return bool(failure_leg_statuses) and failure_leg_statuses <= (
            _TERMINAL_FAILURES | {"filled"}
        )
    if status != "filled":
        return False
    if position_quantity != _ZERO:
        return False
    legs = payload.get("legs")
    if not isinstance(legs, list) or not legs:
        return False
    leg_statuses = {
        str(leg.get("status") or "unknown").lower()
        for leg in legs
        if isinstance(leg, dict)
    }
    return bool(leg_statuses) and leg_statuses <= (_TERMINAL_FAILURES | {"filled"})


class PaperTradingRuntime:
    """Mirror approved forward plans to a hard-pinned Alpaca paper account."""

    def __init__(
        self,
        engine: Engine,
        shadow: ShadowRuntime,
        *,
        broker_factory: BrokerFactory | None,
        enabled: bool,
        trading_mode: str,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self.engine = engine
        self.shadow = shadow
        self.broker_factory = broker_factory
        self.enabled = enabled
        self.trading_mode = trading_mode
        self._now_provider = now_provider or (lambda: datetime.now(UTC))
        self._tick_lock = asyncio.Lock()

    def _now(self) -> datetime:
        value = self._now_provider()
        if value.tzinfo is None:
            raise ValueError("Paper runtime clock must be timezone-aware")
        return value.astimezone(UTC)

    @property
    def configured(self) -> bool:
        return self.broker_factory is not None

    def status(self) -> dict[str, Any]:
        latest = self.latest_account()
        recent_runs = self.runs(limit=1)
        latest_summary = (
            {
                key: latest.get(key)
                for key in (
                    "broker_account_id",
                    "status",
                    "currency",
                    "cash",
                    "buying_power",
                    "equity",
                    "portfolio_value",
                    "last_equity",
                    "pattern_day_trader",
                    "trading_blocked",
                    "transfers_blocked",
                    "account_blocked",
                    "observed_at",
                )
            }
            if latest is not None
            else None
        )
        latest_run = (
            {
                key: recent_runs[0].get(key)
                for key in (
                    "paper_run_id",
                    "trigger",
                    "status",
                    "enrollment_count",
                    "orders_submitted",
                    "orders_reconciled",
                    "started_at",
                    "finished_at",
                    "error_code",
                )
            }
            if recent_runs
            else None
        )
        with self.engine.connect() as connection:
            counts = {
                "enrollments": int(
                    connection.execute(
                        select(func.count()).select_from(paper_enrollments)
                    ).scalar_one()
                ),
                "active_enrollments": int(
                    connection.execute(
                        select(func.count())
                        .select_from(paper_enrollments)
                        .where(paper_enrollments.c.status == "ACTIVE")
                    ).scalar_one()
                ),
                "orders": int(
                    connection.execute(
                        select(func.count()).select_from(paper_orders)
                    ).scalar_one()
                ),
                "open_order_lifecycles": int(
                    connection.execute(
                        select(func.count())
                        .select_from(paper_orders)
                        .where(paper_orders.c.lifecycle_complete.is_(False))
                    ).scalar_one()
                ),
                "broker_order_legs": int(
                    connection.execute(
                        select(func.count()).select_from(paper_order_legs)
                    ).scalar_one()
                ),
            }
        active_enrollments = [
            item
            for item in self.enrollments(limit=1_000)
            if item["status"] == "ACTIVE"
        ]
        compatible_active_enrollments = 0
        for enrollment in active_enrollments:
            try:
                deployment = self.shadow.deployment(
                    str(enrollment["shadow_deployment_id"])
                )
            except ValueError:
                continue
            contract = dict(deployment.get("execution_contract_json") or {})
            if (
                deployment.get("status") == "ACTIVE"
                and deployment.get("contract_status") == "CURRENT"
                and contract.get("execution_profile")
                == PAPER_EXECUTION_PROFILE_VERSION
            ):
                compatible_active_enrollments += 1
        infrastructure_ready = (
            self.configured and self.enabled and self.trading_mode == "paper"
        )
        return {
            "provider": "alpaca_paper",
            "configured": self.configured,
            "paper_trading_enabled": self.enabled,
            "trading_mode": self.trading_mode,
            "live_money_possible": False,
            "infrastructure_ready": infrastructure_ready,
            "submission_ready": (
                infrastructure_ready and compatible_active_enrollments > 0
            ),
            "compatible_active_enrollments": compatible_active_enrollments,
            "required_execution_profile": PAPER_EXECUTION_PROFILE_VERSION,
            "execution_policy": "shared_validated_deployable_profile_only",
            "automatic_session_close_enabled": True,
            "latest_account": latest_summary,
            "latest_run": latest_run,
            **counts,
        }

    async def probe(self) -> dict[str, Any]:
        if self.broker_factory is None:
            raise ValueError("Alpaca paper credentials are not configured")
        async with self.broker_factory() as broker:
            account = _safe_payload(await broker.fetch_account())
            positions = await broker.fetch_positions()
        return {
            "provider": "alpaca_paper",
            "broker_account_id": account.get("id"),
            "status": account.get("status"),
            "currency": account.get("currency"),
            "cash": account.get("cash"),
            "buying_power": account.get("buying_power"),
            "equity": account.get("equity"),
            "trading_blocked": bool(account.get("trading_blocked")),
            "account_blocked": bool(account.get("account_blocked")),
            "position_count": len(positions),
            "live_money_possible": False,
        }

    def enrollment_preview(self, deployment_id: str) -> dict[str, Any]:
        deployment = self.shadow.deployment(deployment_id)
        if deployment["status"] != "ACTIVE":
            raise ValueError("Only an active shadow deployment can enter paper trading")
        if deployment.get("contract_status") != "CURRENT":
            raise ValueError("The deployment requires exact-contract revalidation")
        if deployment.get("admission_tier") != "QUALIFIED":
            raise ValueError(
                "Candidate Shadow is observation-only and cannot enter Alpaca Paper"
            )
        execution_contract = dict(deployment.get("execution_contract_json") or {})
        execution_profile = execution_contract.get("execution_profile")
        if execution_profile != PAPER_EXECUTION_PROFILE_VERSION:
            if execution_profile == MULTI_SESSION_EXECUTION_PROFILE_VERSION:
                raise ValueError(
                    "This multi-session deployment is not compatible with the "
                    "one-session Alpaca Paper lifecycle. Its broker entry-expiry "
                    "and GTC-exit contract must be implemented and separately validated"
                )
            raise ValueError(
                "This deployment uses an obsolete execution profile; Alpaca Paper "
                "requires a current unified deployable validation"
            )
        with self.engine.connect() as connection:
            existing = connection.execute(
                select(paper_enrollments).where(
                    paper_enrollments.c.shadow_deployment_id == deployment_id
                )
            ).one_or_none()
        if existing is not None and existing.status == "RETIRED":
            raise ValueError("A retired paper enrollment cannot be reused")
        return {
            "summary": (
                f"Enroll {deployment['strategy_name']} / {deployment['symbol']} "
                "for Alpaca paper execution"
            ),
            "shadow_deployment_id": deployment_id,
            "strategy_spec_id": deployment["strategy_spec_id"],
            "symbol": deployment["symbol"],
            "broker": "Alpaca paper only",
            "paper_submission_enabled": self.enabled,
            "trading_mode": self.trading_mode,
            "historical_plans_eligible": False,
            "live_broker_effect": True,
            "live_money_possible": False,
        }

    def enroll(
        self,
        *,
        deployment_id: str,
        reason: str,
        created_by: str,
    ) -> dict[str, Any]:
        self.enrollment_preview(deployment_id)
        now = self._now()
        with self.engine.begin() as connection:
            existing = connection.execute(
                select(paper_enrollments).where(
                    paper_enrollments.c.shadow_deployment_id == deployment_id
                )
            ).one_or_none()
            if existing is None:
                enrollment_id = uuid7()
                connection.execute(
                    insert(paper_enrollments).values(
                        paper_enrollment_id=enrollment_id,
                        shadow_deployment_id=deployment_id,
                        broker_account_id=None,
                        status="ACTIVE",
                        created_by=created_by,
                        reason=reason,
                        created_at=now,
                        updated_at=now,
                    )
                )
            else:
                enrollment_id = str(existing.paper_enrollment_id)
                connection.execute(
                    update(paper_enrollments)
                    .where(
                        paper_enrollments.c.paper_enrollment_id == enrollment_id
                    )
                    .values(
                        status="ACTIVE",
                        created_by=created_by,
                        reason=reason,
                        updated_at=now,
                    )
                )
        return self.enrollment(enrollment_id)

    def set_enrollment_status(
        self,
        *,
        enrollment_id: str,
        status: str,
        reason: str,
        updated_by: str,
    ) -> dict[str, Any]:
        if status not in {"ACTIVE", "PAUSED", "RETIRED"}:
            raise ValueError("Unsupported paper enrollment status")
        token = self._acquire_lease(owner=f"enrollment-state:{uuid7()}")
        if token is None:
            raise ValueError("Paper runtime is submitting; retry the enrollment change")
        try:
            with self.engine.begin() as connection:
                row = connection.execute(
                    select(paper_enrollments).where(
                        paper_enrollments.c.paper_enrollment_id == enrollment_id
                    )
                ).one_or_none()
                if row is None:
                    raise ValueError("Paper enrollment not found")
                if row.status == "RETIRED":
                    raise ValueError("Retired paper enrollments cannot be changed")
                if status == "ACTIVE":
                    self.enrollment_preview(str(row.shadow_deployment_id))
                values: dict[str, Any] = {
                    "status": status,
                    "reason": reason,
                    "created_by": updated_by,
                    "updated_at": self._now(),
                }
                if status == "ACTIVE" and row.status == "ACCOUNT_MISMATCH":
                    values["broker_account_id"] = None
                connection.execute(
                    update(paper_enrollments)
                    .where(paper_enrollments.c.paper_enrollment_id == enrollment_id)
                    .values(**values)
                )
            return self.enrollment(enrollment_id)
        finally:
            self._release_lease(token)

    def enrollment(self, enrollment_id: str) -> dict[str, Any]:
        values = [
            item
            for item in self.enrollments(limit=1_000)
            if item["paper_enrollment_id"] == enrollment_id
        ]
        if not values:
            raise ValueError("Paper enrollment not found")
        return values[0]

    def enrollments(self, *, limit: int = 200) -> list[dict[str, Any]]:
        statement = (
            select(
                paper_enrollments,
                shadow_deployments.c.strategy_spec_id,
                shadow_deployments.c.symbol,
                shadow_deployments.c.status.label("shadow_status"),
                strategy_specs.c.name.label("strategy_name"),
                strategy_specs.c.version.label("strategy_version"),
            )
            .join(
                shadow_deployments,
                shadow_deployments.c.shadow_deployment_id
                == paper_enrollments.c.shadow_deployment_id,
            )
            .join(
                strategy_specs,
                strategy_specs.c.strategy_spec_id
                == shadow_deployments.c.strategy_spec_id,
            )
            .order_by(paper_enrollments.c.updated_at.desc())
            .limit(limit)
        )
        with self.engine.connect() as connection:
            return [dict(row._mapping) for row in connection.execute(statement)]

    def orders(self, *, limit: int = 200) -> list[dict[str, Any]]:
        statement = (
            select(
                paper_orders,
                paper_enrollments.c.shadow_deployment_id,
                paper_enrollments.c.status.label("enrollment_status"),
                paper_enrollments.c.broker_account_id.label(
                    "enrollment_broker_account_id"
                ),
                shadow_trade_plans.c.status.label("plan_status"),
                shadow_signal_candidates.c.shadow_deployment_id.label(
                    "plan_shadow_deployment_id"
                ),
                shadow_deployments.c.status.label("shadow_status"),
            )
            .join(
                paper_enrollments,
                paper_enrollments.c.paper_enrollment_id
                == paper_orders.c.paper_enrollment_id,
            )
            .join(
                shadow_trade_plans,
                shadow_trade_plans.c.trade_plan_id
                == paper_orders.c.shadow_trade_plan_id,
            )
            .join(
                shadow_signal_candidates,
                shadow_signal_candidates.c.candidate_id
                == shadow_trade_plans.c.candidate_id,
            )
            .join(
                shadow_deployments,
                shadow_deployments.c.shadow_deployment_id
                == paper_enrollments.c.shadow_deployment_id,
            )
            .order_by(paper_orders.c.updated_at.desc())
            .limit(limit)
        )
        with self.engine.connect() as connection:
            return [dict(row._mapping) for row in connection.execute(statement)]

    def order_preview(self, paper_order_id: str) -> dict[str, Any]:
        order = self._order(paper_order_id)
        if order["broker_order_id"] is None:
            raise ValueError("Paper order has not been acknowledged by Alpaca")
        if bool(order["lifecycle_complete"]):
            raise ValueError("Paper order lifecycle is already complete")
        return {
            "summary": f"Cancel and flatten Alpaca paper lifecycle for {order['symbol']}",
            "paper_order_id": paper_order_id,
            "broker_order_id": order["broker_order_id"],
            "client_order_id": order["client_order_id"],
            "status": order["status"],
            "quantity": str(order["quantity"]),
            "live_broker_effect": True,
            "live_money_possible": False,
        }

    def events(self, *, limit: int = 500) -> list[dict[str, Any]]:
        statement = (
            select(paper_order_events)
            .order_by(paper_order_events.c.observed_at.desc())
            .limit(limit)
        )
        with self.engine.connect() as connection:
            return [dict(row._mapping) for row in connection.execute(statement)]

    def order_legs(self, *, limit: int = 500) -> list[dict[str, Any]]:
        statement = (
            select(paper_order_legs)
            .order_by(paper_order_legs.c.updated_at.desc())
            .limit(limit)
        )
        with self.engine.connect() as connection:
            return [dict(row._mapping) for row in connection.execute(statement)]

    def runs(self, *, limit: int = 100) -> list[dict[str, Any]]:
        statement = paper_runs.select().order_by(
            paper_runs.c.finished_at.desc()
        ).limit(limit)
        with self.engine.connect() as connection:
            return [dict(row._mapping) for row in connection.execute(statement)]

    def latest_account(self) -> dict[str, Any] | None:
        with self.engine.connect() as connection:
            row = connection.execute(
                select(paper_account_snapshots)
                .order_by(paper_account_snapshots.c.observed_at.desc())
                .limit(1)
            ).one_or_none()
        return dict(row._mapping) if row is not None else None

    def latest_positions(self) -> list[dict[str, Any]]:
        with self.engine.connect() as connection:
            snapshot_id = connection.execute(
                select(paper_account_snapshots.c.paper_account_snapshot_id)
                .order_by(paper_account_snapshots.c.observed_at.desc())
                .limit(1)
            ).scalar_one_or_none()
            if snapshot_id is None:
                return []
            return [
                dict(row._mapping)
                for row in connection.execute(
                    select(paper_position_snapshots)
                    .where(
                        paper_position_snapshots.c.paper_account_snapshot_id
                        == snapshot_id
                    )
                    .order_by(paper_position_snapshots.c.symbol.asc())
                )
            ]

    async def cancel_order(self, paper_order_id: str) -> dict[str, Any]:
        if self.broker_factory is None:
            raise ValueError("Alpaca paper credentials are not configured")
        async with self._tick_lock:
            token = self._acquire_lease(owner=f"cancel:{uuid7()}")
            if token is None:
                raise ValueError("Paper runtime is reconciling broker state; retry cancel")
            try:
                return await self._cancel_order_locked(paper_order_id, token)
            finally:
                self._release_lease(token)

    async def _cancel_order_locked(
        self,
        paper_order_id: str,
        lease_token: str,
    ) -> dict[str, Any]:
        assert self.broker_factory is not None
        with self.engine.connect() as connection:
            row = connection.execute(
                select(paper_orders).where(
                    paper_orders.c.paper_order_id == paper_order_id
                )
            ).one_or_none()
        if row is None:
            raise ValueError("Paper order not found")
        if row.broker_order_id is None:
            raise ValueError("Paper order has not been acknowledged by Alpaca")
        if bool(row.lifecycle_complete):
            raise ValueError("Paper order lifecycle is already complete")
        requested_at = self._now()
        with self.engine.begin() as connection:
            connection.execute(
                update(paper_orders)
                .where(paper_orders.c.paper_order_id == paper_order_id)
                .values(status="CANCEL_REQUESTED", updated_at=requested_at)
            )
        self._append_order_event(
            paper_order_id,
            broker_status="CANCEL_REQUESTED",
            filled_quantity=_decimal(row.filled_quantity),
            filled_average_price=(
                _decimal(row.filled_average_price)
                if row.filled_average_price is not None
                else None
            ),
            lifecycle_complete=False,
            payload={"broker_order_id": str(row.broker_order_id)},
            observed_at=requested_at,
        )
        async with self.broker_factory() as broker:
            account = await broker.fetch_account()
            broker_account_id = str(account.get("id") or "")
            if not broker_account_id:
                raise RuntimeError("Alpaca paper account response has no account ID")
            self._pin_account(broker_account_id)
            if str(row.broker_account_id or "") != broker_account_id:
                raise RuntimeError("Paper order is bound to a different broker account")
            original = await broker.fetch_order_by_client_id(str(row.client_order_id))
            if original is None:
                original = {"id": str(row.broker_order_id), "status": row.status, "legs": []}
            await self._cancel_entry_group(broker, original)
            payload = await broker.fetch_order_by_client_id(str(row.client_order_id))
            positions = await broker.fetch_positions()
        position_quantity = self._position_quantity(
            positions, symbol=str(row.symbol)
        )
        if payload is not None:
            self._apply_broker_order(
                str(row.paper_order_id),
                payload,
                self._now(),
                position_quantity=position_quantity,
            )
            if position_quantity != _ZERO:
                if not self._entry_group_quiescent(payload):
                    self._mark_submission_blocked(
                        str(row.paper_order_id),
                        ("MANUAL_CANCEL_PENDING",),
                    )
                else:
                    async with self.broker_factory() as exit_broker:
                        await self._submit_or_reconcile_exit(
                            exit_broker,
                            self._order(str(row.paper_order_id)),
                            position_quantity=position_quantity,
                            emergency=True,
                            lease_token=lease_token,
                        )
        elif position_quantity != _ZERO:
            async with self.broker_factory() as exit_broker:
                await self._submit_or_reconcile_exit(
                    exit_broker,
                    self._order(str(row.paper_order_id)),
                    position_quantity=position_quantity,
                    emergency=True,
                    lease_token=lease_token,
                )
        else:
            self._complete_cancelled_flat_order(str(row.paper_order_id))
        return next(
            item for item in self.orders(limit=1_000)
            if item["paper_order_id"] == paper_order_id
        )

    async def tick(
        self,
        *,
        trigger: str,
        new_exposure_paused: bool,
    ) -> dict[str, Any]:
        if not self.enabled or self.trading_mode != "paper":
            raise ValueError("Paper submission is not enabled in paper mode")
        if self.broker_factory is None:
            raise ValueError("Alpaca paper credentials are not configured")
        async with self._tick_lock:
            token = self._acquire_lease(owner=f"{trigger}:{uuid7()}")
            if token is None:
                raise ValueError("Another paper tick owns the broker execution lease")
            try:
                return await self._tick_locked(
                    trigger=trigger,
                    new_exposure_paused=new_exposure_paused,
                    lease_token=token,
                )
            finally:
                self._release_lease(token)

    async def _tick_locked(
        self,
        *,
        trigger: str,
        new_exposure_paused: bool,
        lease_token: str,
    ) -> dict[str, Any]:
        started_at = self._now()
        run_id = uuid7()
        submitted = 0
        reconciled = 0
        status = "SUCCEEDED"
        error_code: str | None = None
        submission_blockers: list[str] = []
        enrollment_count = len(
            [item for item in self.enrollments(limit=1_000) if item["status"] == "ACTIVE"]
        )
        try:
            assert self.broker_factory is not None
            async with self.broker_factory() as broker:
                account = await broker.fetch_account()
                positions = await broker.fetch_positions()
                self._assert_lease(lease_token)
                self._capture_account(account, positions, observed_at=self._now())
                broker_account_id = str(account.get("id") or "")
                if not broker_account_id:
                    raise RuntimeError("Alpaca paper account response has no account ID")
                self._pin_account(broker_account_id)
                all_orders = self.orders(limit=10_000)
                position_quantity_by_symbol = {
                    str(position.get("symbol") or "UNKNOWN").upper(): _decimal(
                        position.get("qty")
                    )
                    for position in positions
                }
                limits = self.shadow.virtual_account()
                paper_equity = _decimal(account.get("equity"))
                paper_daily_pnl = paper_equity - _decimal(
                    account.get("last_equity")
                )
                if str(account.get("status") or "").upper() != "ACTIVE":
                    submission_blockers.append("BROKER_ACCOUNT_NOT_ACTIVE")
                if bool(account.get("trading_blocked")):
                    submission_blockers.append("BROKER_TRADING_BLOCKED")
                if bool(account.get("account_blocked")):
                    submission_blockers.append("BROKER_ACCOUNT_BLOCKED")
                if bool(account.get("trade_suspended_by_user")):
                    submission_blockers.append("BROKER_TRADING_SUSPENDED_BY_USER")
                if paper_equity <= _decimal(limits["account_floor_usd"]):
                    submission_blockers.append("PAPER_ACCOUNT_FLOOR_REACHED")
                if paper_daily_pnl <= -_decimal(limits["daily_loss_stop_usd"]):
                    submission_blockers.append("PAPER_DAILY_LOSS_STOP_REACHED")
                expected_positions: dict[str, Decimal] = {}
                for order in all_orders:
                    if bool(order["lifecycle_complete"]):
                        continue
                    symbol = str(order["symbol"]).upper()
                    expected_positions[symbol] = (
                        expected_positions.get(symbol, _ZERO)
                        + _decimal(order["filled_quantity"])
                        - self._exit_filled_quantity(str(order["paper_order_id"]))
                    )
                managed_symbols = set(expected_positions)
                unmanaged_symbols = sorted(
                    str(position.get("symbol") or "UNKNOWN").upper()
                    for position in positions
                    if str(position.get("symbol") or "UNKNOWN").upper()
                    not in managed_symbols
                )
                if unmanaged_symbols:
                    submission_blockers.append(
                        "UNMANAGED_PAPER_POSITIONS:" + ",".join(unmanaged_symbols)
                    )
                for symbol, expected_quantity in expected_positions.items():
                    actual_quantity = position_quantity_by_symbol.get(symbol, _ZERO)
                    if actual_quantity != expected_quantity:
                        submission_blockers.append(
                            "PAPER_POSITION_QUANTITY_MISMATCH:"
                            f"{symbol}:{expected_quantity}:{actual_quantity}"
                        )
                blocked = bool(submission_blockers)
                buying_power = _decimal(account.get("buying_power"))
                open_risk = sum(
                    (
                        max(
                            _ZERO,
                            _decimal(order["entry_limit_price"])
                            - _decimal(order["stop_loss_price"]),
                        )
                        * Decimal(int(order["quantity"]))
                        for order in all_orders
                        if not bool(order["lifecycle_complete"])
                    ),
                    _ZERO,
                )
                for order in all_orders:
                    if bool(order["lifecycle_complete"]):
                        continue
                    order_id = str(order["paper_order_id"])
                    exit_leg = self._leg(
                        order_id,
                        "EMERGENCY_EXIT",
                        "SESSION_CLOSE",
                    )
                    if exit_leg is not None:
                        exit_payload = await broker.fetch_order_by_client_id(
                            str(exit_leg["client_order_id"])
                        )
                        positions = await broker.fetch_positions()
                        position_quantity = self._position_quantity(
                            positions,
                            symbol=str(order["symbol"]),
                        )
                        if exit_payload is not None:
                            self._assert_lease(lease_token)
                            self._apply_exit_order(
                                order,
                                exit_leg,
                                exit_payload,
                                position_quantity=position_quantity,
                                observed_at=self._now(),
                            )
                            reconciled += 1
                        elif (
                            position_quantity != _ZERO
                            and str(exit_leg["status"]) not in _TERMINAL_FAILURES
                        ):
                            await self._submit_or_reconcile_exit(
                                broker,
                                order,
                                position_quantity=position_quantity,
                                emergency=str(exit_leg["leg_role"]) == "EMERGENCY_EXIT",
                                lease_token=lease_token,
                            )
                            submitted += 1
                        if position_quantity != _ZERO:
                            refreshed_exit = self._leg(
                                order_id,
                                "EMERGENCY_EXIT",
                                "SESSION_CLOSE",
                            )
                            if (
                                refreshed_exit is not None
                                and str(refreshed_exit["status"]).lower()
                                in _TERMINAL_FAILURES
                                and str(refreshed_exit["leg_role"]) == "SESSION_CLOSE"
                            ):
                                await self._submit_or_reconcile_exit(
                                    broker,
                                    order,
                                    position_quantity=position_quantity,
                                    emergency=True,
                                    lease_token=lease_token,
                                )
                                submitted += 1
                            blocker = "PAPER_EXIT_IN_PROGRESS:" + str(order["symbol"])
                            submission_blockers.append(blocker)
                            blocked = True
                        elif exit_payload is None:
                            blocker = (
                                "EXIT_ORDER_NOT_FOUND_BUT_ACCOUNT_FLAT:"
                                + str(order["symbol"])
                            )
                            submission_blockers.append(blocker)
                            blocked = True
                        continue
                    payload = await broker.fetch_order_by_client_id(
                        str(order["client_order_id"])
                    )
                    if payload is not None:
                        # Order and position snapshots must describe the same side of
                        # the observation boundary. A fill can arrive after the
                        # account-wide snapshot at the start of this tick.
                        positions = await broker.fetch_positions()
                        symbol = str(order["symbol"]).upper()
                        position_quantity = self._position_quantity(
                            positions,
                            symbol=symbol,
                        )
                        position_quantity_by_symbol[symbol] = position_quantity
                        self._assert_lease(lease_token)
                        self._apply_broker_order(
                            str(order["paper_order_id"]),
                            payload,
                            self._now(),
                            position_quantity=position_quantity,
                        )
                        reconciled += 1
                        current = self._order(str(order["paper_order_id"]))
                        expires_at = _utc(current["plan_expires_at"])
                        if (
                            expires_at is not None
                            and expires_at - _SESSION_CLOSE_LEAD <= self._now()
                            and not bool(current["lifecycle_complete"])
                        ):
                            await self._cancel_entry_group(broker, payload)
                            cancelled = await broker.fetch_order_by_client_id(
                                str(current["client_order_id"])
                            )
                            positions = await broker.fetch_positions()
                            position_quantity = self._position_quantity(
                                positions, symbol=str(current["symbol"])
                            )
                            if cancelled is not None:
                                self._apply_broker_order(
                                    str(current["paper_order_id"]),
                                    cancelled,
                                    self._now(),
                                    position_quantity=position_quantity,
                                )
                            if cancelled is None or not self._entry_group_quiescent(
                                cancelled
                            ):
                                blocker = (
                                    "SESSION_CLOSE_CANCELLATION_PENDING:"
                                    + str(current["symbol"])
                                )
                                submission_blockers.append(blocker)
                                blocked = True
                                self._mark_submission_blocked(
                                    str(current["paper_order_id"]),
                                    (blocker,),
                                )
                            elif position_quantity != _ZERO:
                                emergency = self._now() >= expires_at - timedelta(
                                    minutes=10
                                )
                                exit_ready = await self._submit_or_reconcile_exit(
                                    broker,
                                    current,
                                    position_quantity=position_quantity,
                                    emergency=emergency,
                                    lease_token=lease_token,
                                )
                                submitted += 1
                                if not exit_ready and not emergency:
                                    await self._submit_or_reconcile_exit(
                                        broker,
                                        current,
                                        position_quantity=position_quantity,
                                        emergency=True,
                                        lease_token=lease_token,
                                    )
                                    submitted += 1
                                blocker = "PAPER_EXIT_IN_PROGRESS:" + str(
                                    current["symbol"]
                                )
                                submission_blockers.append(blocker)
                                blocked = True
                        continue
                    if order["broker_order_id"] is not None:
                        submission_blockers.append(
                            "ACKNOWLEDGED_BROKER_ORDER_NOT_FOUND:"
                            + str(order["client_order_id"])
                        )
                        blocked = True
                        continue
                    expires_at = _utc(order["plan_expires_at"])
                    if expires_at is not None and expires_at <= self._now():
                        self._expire_unsubmitted(str(order["paper_order_id"]))
                        continue
                    retryable = str(order["status"]) in {
                        "PENDING_SUBMISSION",
                        "SUBMITTING",
                        "SUBMISSION_UNKNOWN",
                        "SUBMISSION_BLOCKED",
                    }
                    if not retryable:
                        continue
                    current_blockers = self._submission_blockers(
                        order,
                        broker_account_id=broker_account_id,
                        buying_power=buying_power,
                        open_risk=open_risk,
                        now=self._now(),
                    )
                    if new_exposure_paused:
                        current_blockers.insert(0, "GLOBAL_NEW_EXPOSURE_PAUSED")
                    if blocked:
                        current_blockers.extend(submission_blockers)
                    if current_blockers:
                        self._mark_submission_blocked(
                            str(order["paper_order_id"]),
                            tuple(dict.fromkeys(current_blockers)),
                        )
                        continue
                    required = (
                        _decimal(order["entry_limit_price"])
                        * Decimal(int(order["quantity"]))
                        * Decimal("1.05")
                    )
                    if required > buying_power:
                        continue
                    if await self._submit_intent(
                        broker,
                        order,
                        lease_token,
                    ):
                        submitted += 1
                        buying_power -= required
                if not new_exposure_paused and not blocked:
                    for plan in self._eligible_plans(self._now()):
                        entry, target, stop = self._normalized_plan_prices(plan)
                        quantity = Decimal(int(plan["quantity"]))
                        trade_risk = (entry - stop) * quantity
                        if trade_risk > _decimal(
                            limits["maximum_trade_risk_usd"]
                        ):
                            continue
                        if open_risk + trade_risk > _decimal(
                            limits["maximum_concurrent_risk_usd"]
                        ):
                            continue
                        required = (
                            entry * quantity * Decimal("1.05")
                        )
                        if required > buying_power:
                            continue
                        order_id = self._ensure_order_intent(
                            plan,
                            broker_account_id=broker_account_id,
                            normalized_prices=(entry, target, stop),
                        )
                        current = self._order_context(order_id)
                        if bool(current["lifecycle_complete"]):
                            continue
                        current_blockers = self._submission_blockers(
                            current,
                            broker_account_id=broker_account_id,
                            buying_power=buying_power,
                            open_risk=open_risk + trade_risk,
                            now=self._now(),
                        )
                        if current_blockers:
                            self._mark_submission_blocked(
                                order_id,
                                tuple(dict.fromkeys(current_blockers)),
                            )
                            continue
                        if await self._submit_intent(
                            broker,
                            current,
                            lease_token,
                        ):
                            submitted += 1
                            buying_power -= required
                            open_risk += trade_risk
        except Exception as exc:
            status = "FAILED"
            error_code = type(exc).__name__
            raise
        finally:
            finished_at = self._now()
            with self.engine.begin() as connection:
                connection.execute(
                    insert(paper_runs).values(
                        paper_run_id=run_id,
                        trigger=trigger[:80],
                        status=status,
                        enrollment_count=enrollment_count,
                        orders_submitted=submitted,
                        orders_reconciled=reconciled,
                        started_at=started_at,
                        finished_at=finished_at,
                        error_code=error_code,
                        detail_json={
                            "submission_blockers": submission_blockers,
                            "new_exposure_paused": new_exposure_paused,
                        },
                    )
                )
        return {
            "paper_run_id": run_id,
            "status": status,
            "enrollment_count": enrollment_count,
            "orders_submitted": submitted,
            "orders_reconciled": reconciled,
            "new_exposure_paused": new_exposure_paused,
            "submission_blockers": submission_blockers,
            "started_at": started_at,
            "finished_at": self._now(),
        }

    async def _submit_intent(
        self,
        broker: PaperBroker,
        order: dict[str, Any],
        lease_token: str,
    ) -> bool:
        order_id = str(order["paper_order_id"])
        self._increment_submission_attempt(order_id)
        try:
            payload = await broker.submit_bracket_order(
                client_order_id=str(order["client_order_id"]),
                symbol=str(order["symbol"]),
                quantity=int(order["quantity"]),
                entry_limit_price=_decimal(order["entry_limit_price"]),
                take_profit_price=_decimal(order["take_profit_price"]),
                stop_loss_price=_decimal(order["stop_loss_price"]),
            )
        except AlpacaPaperResponseError as exc:
            terminal = 400 <= exc.status_code < 500
            self._record_submission_error(
                order_id,
                error=exc,
                terminal=terminal,
            )
            if terminal:
                return False
            raise
        self._assert_lease(lease_token)
        positions = await broker.fetch_positions()
        position_quantity = self._position_quantity(
            positions,
            symbol=str(order["symbol"]),
        )
        self._assert_lease(lease_token)
        self._apply_broker_order(
            order_id,
            payload,
            self._now(),
            position_quantity=position_quantity,
        )
        return True

    def _eligible_plans(self, now: datetime) -> list[dict[str, Any]]:
        statement = (
            select(
                shadow_trade_plans,
                paper_enrollments.c.paper_enrollment_id,
                paper_enrollments.c.created_at.label("enrolled_at"),
                shadow_signal_candidates.c.shadow_deployment_id,
            )
            .join(
                shadow_signal_candidates,
                shadow_signal_candidates.c.candidate_id
                == shadow_trade_plans.c.candidate_id,
            )
            .join(
                paper_enrollments,
                paper_enrollments.c.shadow_deployment_id
                == shadow_signal_candidates.c.shadow_deployment_id,
            )
            .join(
                shadow_deployments,
                shadow_deployments.c.shadow_deployment_id
                == paper_enrollments.c.shadow_deployment_id,
            )
            .outerjoin(
                paper_orders,
                paper_orders.c.shadow_trade_plan_id
                == shadow_trade_plans.c.trade_plan_id,
            )
            .where(
                (paper_enrollments.c.status == "ACTIVE")
                & (shadow_deployments.c.status == "ACTIVE")
                & (shadow_trade_plans.c.status == "OPEN")
                & (shadow_trade_plans.c.expires_at > now)
                & (
                    shadow_trade_plans.c.created_at
                    >= paper_enrollments.c.created_at
                )
                & paper_orders.c.paper_order_id.is_(None)
            )
            .order_by(shadow_trade_plans.c.created_at.asc())
        )
        with self.engine.connect() as connection:
            values = [dict(row._mapping) for row in connection.execute(statement)]
        eligible: list[dict[str, Any]] = []
        for value in values:
            deployment = self.shadow.deployment(
                str(value["shadow_deployment_id"])
            )
            if deployment.get("contract_status") != "CURRENT":
                with self.engine.begin() as connection:
                    connection.execute(
                        update(paper_enrollments)
                        .where(
                            paper_enrollments.c.paper_enrollment_id
                            == value["paper_enrollment_id"]
                        )
                        .values(status="REVALIDATION_REQUIRED", updated_at=now)
                    )
                continue
            execution_contract = dict(
                deployment.get("execution_contract_json") or {}
            )
            if (
                execution_contract.get("execution_profile")
                != PAPER_EXECUTION_PROFILE_VERSION
            ):
                with self.engine.begin() as connection:
                    connection.execute(
                        update(paper_enrollments)
                        .where(
                            paper_enrollments.c.paper_enrollment_id
                            == value["paper_enrollment_id"]
                        )
                        .values(status="REVALIDATION_REQUIRED", updated_at=now)
                    )
                continue
            eligible.append(value)
        return eligible

    def _submission_blockers(
        self,
        order: dict[str, Any],
        *,
        broker_account_id: str,
        buying_power: Decimal,
        open_risk: Decimal,
        now: datetime,
    ) -> list[str]:
        blockers: list[str] = []
        if str(order.get("enrollment_status")) != "ACTIVE":
            blockers.append("PAPER_ENROLLMENT_NOT_ACTIVE")
        if str(order.get("shadow_status")) != "ACTIVE":
            blockers.append("SHADOW_DEPLOYMENT_NOT_ACTIVE")
        if str(order.get("plan_shadow_deployment_id")) != str(
            order.get("shadow_deployment_id")
        ):
            blockers.append("PLAN_ENROLLMENT_MISMATCH")
        if str(order.get("plan_status")) != "OPEN":
            blockers.append("SHADOW_PLAN_NOT_OPEN")
        bound_account = str(order.get("broker_account_id") or "")
        enrollment_account = str(
            order.get("enrollment_broker_account_id") or ""
        )
        if not bound_account:
            blockers.append("ORDER_ACCOUNT_NOT_PINNED")
        elif bound_account != broker_account_id:
            blockers.append("ORDER_ACCOUNT_MISMATCH")
        if not enrollment_account:
            blockers.append("ENROLLMENT_ACCOUNT_NOT_PINNED")
        elif enrollment_account != broker_account_id:
            blockers.append("ENROLLMENT_ACCOUNT_MISMATCH")
        elif bound_account and bound_account != enrollment_account:
            blockers.append("ORDER_ENROLLMENT_ACCOUNT_MISMATCH")
        expires_at = _utc(order.get("plan_expires_at"))
        if expires_at is None or expires_at <= now:
            blockers.append("SHADOW_PLAN_EXPIRED")
        try:
            deployment = self.shadow.deployment(
                str(order["shadow_deployment_id"])
            )
        except ValueError:
            blockers.append("SHADOW_DEPLOYMENT_NOT_FOUND")
        else:
            if deployment.get("contract_status") != "CURRENT":
                blockers.append("SHADOW_CONTRACT_NOT_CURRENT")
            contract = dict(deployment.get("execution_contract_json") or {})
            if contract.get("execution_profile") != PAPER_EXECUTION_PROFILE_VERSION:
                blockers.append("PAPER_EXECUTION_CONTRACT_NOT_VALIDATED")
        symbol = str(order["symbol"]).upper()
        if self.shadow.restrictions.is_restricted(symbol, now.date()):
            blockers.append("SECURITY_RESTRICTED")
        limits = self.shadow.virtual_account()
        trade_risk = max(
            _ZERO,
            _decimal(order["entry_limit_price"])
            - _decimal(order["stop_loss_price"]),
        ) * Decimal(int(order["quantity"]))
        if trade_risk > _decimal(limits["maximum_trade_risk_usd"]):
            blockers.append("PAPER_TRADE_RISK_LIMIT")
        if open_risk > _decimal(limits["maximum_concurrent_risk_usd"]):
            blockers.append("PAPER_CONCURRENT_RISK_LIMIT")
        required = (
            _decimal(order["entry_limit_price"])
            * Decimal(int(order["quantity"]))
            * Decimal("1.05")
        )
        if required > buying_power:
            blockers.append("PAPER_BUYING_POWER")
        with self.engine.connect() as connection:
            conflicting = connection.execute(
                select(func.count())
                .select_from(paper_orders)
                .where(
                    (paper_orders.c.symbol == symbol)
                    & (paper_orders.c.lifecycle_complete.is_(False))
                    & (
                        paper_orders.c.paper_order_id
                        != str(order["paper_order_id"])
                    )
                )
            ).scalar_one()
        if int(conflicting):
            blockers.append("PAPER_SYMBOL_LIFECYCLE_CONFLICT")
        return blockers

    def _mark_submission_blocked(
        self,
        order_id: str,
        blockers: tuple[str, ...],
    ) -> None:
        if not blockers:
            return
        order = self._order(order_id)
        message = ";".join(blockers)
        if (
            str(order["status"]) == "SUBMISSION_BLOCKED"
            and str(order.get("error_message") or "") == message
        ):
            return
        now = self._now()
        with self.engine.begin() as connection:
            connection.execute(
                update(paper_orders)
                .where(paper_orders.c.paper_order_id == order_id)
                .values(
                    status="SUBMISSION_BLOCKED",
                    lifecycle_complete=False,
                    error_code=blockers[0][:120],
                    error_message=message[:2_000],
                    updated_at=now,
                )
            )
        self._append_order_event(
            order_id,
            broker_status="SUBMISSION_BLOCKED",
            filled_quantity=_decimal(order["filled_quantity"]),
            filled_average_price=(
                _decimal(order["filled_average_price"])
                if order["filled_average_price"] is not None
                else None
            ),
            lifecycle_complete=False,
            payload={"reason_codes": list(blockers)},
            observed_at=now,
        )

    @staticmethod
    def _position_quantity(
        positions: tuple[dict[str, Any], ...],
        *,
        symbol: str,
    ) -> Decimal:
        normalized = symbol.upper()
        return sum(
            (
                _decimal(item.get("qty"))
                for item in positions
                if str(item.get("symbol") or "").upper() == normalized
            ),
            _ZERO,
        )

    def _ensure_order_intent(
        self,
        plan: dict[str, Any],
        *,
        broker_account_id: str,
        normalized_prices: tuple[Decimal, Decimal, Decimal] | None = None,
    ) -> str:
        entry, target, stop = (
            normalized_prices
            if normalized_prices is not None
            else self._normalized_plan_prices(plan)
        )
        plan_id = str(plan["trade_plan_id"])
        client_order_id = "qagent-" + hashlib.sha256(plan_id.encode()).hexdigest()[:32]
        order_id = stable_uuid("paper-order", plan_id)
        now = self._now()
        risk_per_share = entry - stop
        reward_risk = (target - entry) / risk_per_share
        if reward_risk < self.shadow.effective_risk_policy().minimum_reward_risk:
            raise ValueError(
                "Rounded Alpaca bracket no longer satisfies minimum reward/risk"
            )
        values = {
            "paper_order_id": order_id,
            "paper_enrollment_id": plan["paper_enrollment_id"],
            "shadow_trade_plan_id": plan_id,
            "client_order_id": client_order_id,
            "broker_order_id": None,
            "broker_account_id": broker_account_id,
            "symbol": str(plan["symbol"]).upper(),
            "side": "buy",
            "quantity": int(plan["quantity"]),
            "order_type": "limit",
            "time_in_force": "day",
            "order_class": "bracket",
            "entry_limit_price": entry,
            "take_profit_price": target,
            "stop_loss_price": stop,
            "plan_expires_at": plan["expires_at"],
            "status": "PENDING_SUBMISSION",
            "lifecycle_complete": False,
            "submission_attempts": 0,
            "filled_quantity": _ZERO,
            "filled_average_price": None,
            "broker_payload_json": {},
            "error_code": None,
            "error_message": None,
            "submitted_at": None,
            "last_reconciled_at": None,
            "created_at": now,
            "updated_at": now,
        }
        dialect_insert: Any
        if self.engine.dialect.name == "postgresql":
            dialect_insert = postgresql_insert
        elif self.engine.dialect.name == "sqlite":
            dialect_insert = sqlite_insert
        else:
            raise RuntimeError(f"Unsupported SQL dialect: {self.engine.dialect.name}")
        with self.engine.begin() as connection:
            connection.execute(
                dialect_insert(paper_orders)
                .values(**values)
                .on_conflict_do_nothing(index_elements=["shadow_trade_plan_id"])
            )
            existing = connection.execute(
                select(paper_orders.c.paper_order_id).where(
                    paper_orders.c.shadow_trade_plan_id == plan_id
                )
            ).scalar_one()
        return str(existing)

    @staticmethod
    def _normalized_plan_prices(
        plan: dict[str, Any],
    ) -> tuple[Decimal, Decimal, Decimal]:
        targets = list(plan["targets_json"] or [])
        if str(plan["direction"]).upper() != "LONG" or not targets:
            raise ValueError("Phase 7 currently supports long bracket plans only")
        return normalize_long_bracket_prices(
            entry_limit_price=_decimal(plan["limit_price"]),
            take_profit_price=_decimal(targets[0]),
            stop_loss_price=_decimal(plan["invalidation"]),
        )

    def _order(self, order_id: str) -> dict[str, Any]:
        with self.engine.connect() as connection:
            row = connection.execute(
                select(paper_orders).where(
                    paper_orders.c.paper_order_id == order_id
                )
            ).one()
        return dict(row._mapping)

    def _order_context(self, order_id: str) -> dict[str, Any]:
        for order in self.orders(limit=10_000):
            if str(order["paper_order_id"]) == order_id:
                return order
        return self._order(order_id)

    def _increment_submission_attempt(self, order_id: str) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                update(paper_orders)
                .where(paper_orders.c.paper_order_id == order_id)
                .values(
                    submission_attempts=paper_orders.c.submission_attempts + 1,
                    status="SUBMITTING",
                    updated_at=self._now(),
                )
            )

    def _record_submission_error(
        self,
        order_id: str,
        *,
        error: AlpacaPaperResponseError,
        terminal: bool,
    ) -> None:
        now = self._now()
        with self.engine.begin() as connection:
            connection.execute(
                update(paper_orders)
                .where(paper_orders.c.paper_order_id == order_id)
                .values(
                    status="REJECTED" if terminal else "SUBMISSION_UNKNOWN",
                    lifecycle_complete=terminal,
                    error_code=f"HTTP_{error.status_code}",
                    error_message=error.detail[:2_000],
                    updated_at=now,
                )
            )
        self._append_order_event(
            order_id,
            broker_status="REJECTED" if terminal else "SUBMISSION_UNKNOWN",
            filled_quantity=_ZERO,
            filled_average_price=None,
            lifecycle_complete=terminal,
            payload={"error_code": f"HTTP_{error.status_code}"},
            observed_at=now,
        )

    def _expire_unsubmitted(self, order_id: str) -> None:
        now = self._now()
        order = self._order(order_id)
        filled_quantity = _decimal(order["filled_quantity"])
        lifecycle_complete = filled_quantity == _ZERO
        status = (
            "EXPIRED_BEFORE_SUBMISSION"
            if lifecycle_complete
            else "POSITION_OPEN_REQUIRES_EXIT"
        )
        with self.engine.begin() as connection:
            connection.execute(
                update(paper_orders)
                .where(paper_orders.c.paper_order_id == order_id)
                .values(
                    status=status,
                    lifecycle_complete=lifecycle_complete,
                    error_code=(
                        "PLAN_EXPIRED"
                        if lifecycle_complete
                        else "POSITION_OPEN_REQUIRES_EXIT"
                    ),
                    error_message=(
                        "Approved plan expired before broker acknowledgment"
                        if lifecycle_complete
                        else "Plan expired with a nonzero recorded fill"
                    ),
                    updated_at=now,
                )
            )
        self._append_order_event(
            order_id,
            broker_status=status,
            filled_quantity=filled_quantity,
            filled_average_price=(
                _decimal(order["filled_average_price"])
                if order["filled_average_price"] is not None
                else None
            ),
            lifecycle_complete=lifecycle_complete,
            payload={"reason": "PLAN_EXPIRED"},
            observed_at=now,
        )

    def _complete_cancelled_flat_order(self, order_id: str) -> None:
        now = self._now()
        order = self._order(order_id)
        with self.engine.begin() as connection:
            connection.execute(
                update(paper_orders)
                .where(paper_orders.c.paper_order_id == order_id)
                .values(
                    status="CANCELED_FLAT",
                    lifecycle_complete=True,
                    error_code=None,
                    error_message=None,
                    last_reconciled_at=now,
                    updated_at=now,
                )
            )
        self._append_order_event(
            order_id,
            broker_status="CANCELED_FLAT",
            filled_quantity=_decimal(order["filled_quantity"]),
            filled_average_price=(
                _decimal(order["filled_average_price"])
                if order["filled_average_price"] is not None
                else None
            ),
            lifecycle_complete=True,
            payload={"reason": "CANCEL_ACKNOWLEDGED_AND_ACCOUNT_FLAT"},
            observed_at=now,
        )

    def _apply_broker_order(
        self,
        order_id: str,
        payload: dict[str, Any],
        observed_at: datetime,
        *,
        position_quantity: Decimal = _ZERO,
    ) -> None:
        safe = _safe_payload(payload)
        self._sync_broker_order_legs(order_id, safe, observed_at)
        status = str(safe.get("status") or "unknown").lower()
        filled_quantity = _decimal(safe.get("filled_qty"))
        filled_average_price = (
            _decimal(safe.get("filled_avg_price"))
            if safe.get("filled_avg_price") not in {None, ""}
            else None
        )
        lifecycle_complete = _order_lifecycle_complete(
            safe,
            position_quantity=position_quantity,
        )
        existing = self._order(order_id)
        preserve_open_position_block = (
            str(existing["status"]) == "SUBMISSION_BLOCKED"
            and "POSITION_OPEN_REQUIRES_EXIT"
            in str(existing.get("error_message") or "")
            and position_quantity != _ZERO
            and (status in _TERMINAL_FAILURES or status == "filled")
        )
        persisted_status = (
            str(existing["status"]) if preserve_open_position_block else status
        )
        changed = (
            str(existing["status"]).lower() != persisted_status.lower()
            or _decimal(existing["filled_quantity"]) != filled_quantity
            or bool(existing["lifecycle_complete"]) != lifecycle_complete
            or dict(existing["broker_payload_json"] or {}) != safe
        )
        with self.engine.begin() as connection:
            connection.execute(
                update(paper_orders)
                .where(paper_orders.c.paper_order_id == order_id)
                .values(
                    broker_order_id=str(safe.get("id") or "") or None,
                    status=persisted_status,
                    lifecycle_complete=lifecycle_complete,
                    filled_quantity=filled_quantity,
                    filled_average_price=filled_average_price,
                    broker_payload_json=safe,
                    error_code=(
                        existing["error_code"]
                        if preserve_open_position_block
                        else None
                    ),
                    error_message=(
                        existing["error_message"]
                        if preserve_open_position_block
                        else None
                    ),
                    submitted_at=existing["submitted_at"] or observed_at,
                    last_reconciled_at=observed_at,
                    updated_at=observed_at,
                )
            )
        if changed:
            self._append_order_event(
                order_id,
                broker_status=status,
                filled_quantity=filled_quantity,
                filled_average_price=filled_average_price,
                lifecycle_complete=lifecycle_complete,
                payload=safe,
                observed_at=observed_at,
            )

    def _sync_broker_order_legs(
        self,
        order_id: str,
        payload: dict[str, Any],
        observed_at: datetime,
    ) -> None:
        order = self._order(order_id)
        parent_id = str(payload.get("id") or "") or None
        values = [
            self._broker_leg_values(
                order=order,
                payload=payload,
                role="ENTRY",
                parent_broker_order_id=None,
                observed_at=observed_at,
            )
        ]
        for child in payload.get("legs") or []:
            if not isinstance(child, dict):
                continue
            role = "STOP_LOSS" if child.get("stop_price") is not None else "TAKE_PROFIT"
            values.append(
                self._broker_leg_values(
                    order=order,
                    payload=_safe_payload(child),
                    role=role,
                    parent_broker_order_id=parent_id,
                    observed_at=observed_at,
                )
            )
        for value in values:
            self._upsert_leg(value)

    def _broker_leg_values(
        self,
        *,
        order: dict[str, Any],
        payload: dict[str, Any],
        role: str,
        parent_broker_order_id: str | None,
        observed_at: datetime,
    ) -> dict[str, Any]:
        status = str(payload.get("status") or "unknown").lower()
        broker_order_id = str(payload.get("id") or "") or None
        client_order_id = str(payload.get("client_order_id") or "") or None
        return {
            "paper_order_leg_id": stable_uuid("paper-order-leg", order["paper_order_id"], role),
            "paper_order_id": order["paper_order_id"],
            "leg_role": role,
            "client_order_id": client_order_id,
            "broker_order_id": broker_order_id,
            "parent_broker_order_id": parent_broker_order_id,
            "symbol": str(payload.get("symbol") or order["symbol"]).upper(),
            "side": str(payload.get("side") or ("buy" if role == "ENTRY" else "sell")),
            "order_type": str(
                payload.get("type")
                or ("limit" if role in {"ENTRY", "TAKE_PROFIT"} else "stop")
            ),
            "time_in_force": str(payload.get("time_in_force") or "day"),
            "quantity": _decimal(payload.get("qty"), str(order["quantity"])),
            "filled_quantity": _decimal(payload.get("filled_qty")),
            "filled_average_price": (
                _decimal(payload.get("filled_avg_price"))
                if payload.get("filled_avg_price") not in {None, ""}
                else None
            ),
            "limit_price": (
                _decimal(payload.get("limit_price"))
                if payload.get("limit_price") not in {None, ""}
                else None
            ),
            "stop_price": (
                _decimal(payload.get("stop_price"))
                if payload.get("stop_price") not in {None, ""}
                else None
            ),
            "status": status,
            "lifecycle_complete": status in _TERMINAL_STATUSES,
            "submission_attempts": 0,
            "broker_payload_json": payload,
            "error_code": None,
            "error_message": None,
            "submitted_at": _utc(order.get("submitted_at")) or observed_at,
            "last_reconciled_at": observed_at,
            "created_at": observed_at,
            "updated_at": observed_at,
        }

    def _upsert_leg(self, values: dict[str, Any]) -> None:
        dialect_insert: Any
        if self.engine.dialect.name == "postgresql":
            dialect_insert = postgresql_insert
        elif self.engine.dialect.name == "sqlite":
            dialect_insert = sqlite_insert
        else:
            raise RuntimeError(f"Unsupported SQL dialect: {self.engine.dialect.name}")
        mutable = {
            key: value
            for key, value in values.items()
            if key not in {"paper_order_leg_id", "paper_order_id", "leg_role", "created_at"}
        }
        with self.engine.begin() as connection:
            connection.execute(
                dialect_insert(paper_order_legs)
                .values(**values)
                .on_conflict_do_update(
                    index_elements=["paper_order_id", "leg_role"],
                    set_=mutable,
                )
            )

    def _leg(self, order_id: str, *roles: str) -> dict[str, Any] | None:
        with self.engine.connect() as connection:
            row = connection.execute(
                select(paper_order_legs)
                .where(
                    (paper_order_legs.c.paper_order_id == order_id)
                    & (paper_order_legs.c.leg_role.in_(roles))
                )
                .order_by(paper_order_legs.c.created_at.desc())
                .limit(1)
            ).one_or_none()
        return dict(row._mapping) if row is not None else None

    def _exit_filled_quantity(self, order_id: str) -> Decimal:
        with self.engine.connect() as connection:
            value = connection.execute(
                select(func.sum(paper_order_legs.c.filled_quantity)).where(
                    (paper_order_legs.c.paper_order_id == order_id)
                    & paper_order_legs.c.leg_role.in_(
                        ("SESSION_CLOSE", "EMERGENCY_EXIT")
                    )
                )
            ).scalar_one_or_none()
        return _decimal(value)

    @staticmethod
    def _entry_group_quiescent(payload: dict[str, Any]) -> bool:
        if str(payload.get("status") or "unknown").lower() not in _TERMINAL_STATUSES:
            return False
        return all(
            str(child.get("status") or "unknown").lower() in _TERMINAL_STATUSES
            for child in payload.get("legs") or []
            if isinstance(child, dict)
        )

    async def _cancel_entry_group(
        self,
        broker: PaperBroker,
        payload: dict[str, Any],
    ) -> None:
        active_ids: list[str] = []
        if str(payload.get("status") or "unknown").lower() not in _TERMINAL_STATUSES:
            if payload.get("id"):
                active_ids.append(str(payload["id"]))
        for child in payload.get("legs") or []:
            if (
                isinstance(child, dict)
                and str(child.get("status") or "unknown").lower()
                not in _TERMINAL_STATUSES
                and child.get("id")
            ):
                active_ids.append(str(child["id"]))
        for broker_order_id in dict.fromkeys(active_ids):
            await broker.cancel_order(broker_order_id)

    def _ensure_exit_intent(
        self,
        order: dict[str, Any],
        *,
        quantity: Decimal,
        emergency: bool,
    ) -> dict[str, Any]:
        role = "EMERGENCY_EXIT" if emergency else "SESSION_CLOSE"
        existing = self._leg(str(order["paper_order_id"]), role)
        if existing is not None:
            if _decimal(existing["quantity"]) != quantity:
                raise RuntimeError("Paper exit intent quantity no longer matches position")
            return existing
        now = self._now()
        suffix = "emergency" if emergency else "moc"
        client_order_id = "qagent-x-" + hashlib.sha256(
            f"{order['paper_order_id']}:{suffix}".encode()
        ).hexdigest()[:30]
        values = {
            "paper_order_leg_id": stable_uuid(
                "paper-order-leg", order["paper_order_id"], role
            ),
            "paper_order_id": order["paper_order_id"],
            "leg_role": role,
            "client_order_id": client_order_id,
            "broker_order_id": None,
            "parent_broker_order_id": order.get("broker_order_id"),
            "symbol": str(order["symbol"]).upper(),
            "side": "sell",
            "order_type": "market",
            "time_in_force": "day" if emergency else "cls",
            "quantity": quantity,
            "filled_quantity": _ZERO,
            "filled_average_price": None,
            "limit_price": None,
            "stop_price": None,
            "status": "PENDING_SUBMISSION",
            "lifecycle_complete": False,
            "submission_attempts": 0,
            "broker_payload_json": {},
            "error_code": None,
            "error_message": None,
            "submitted_at": None,
            "last_reconciled_at": None,
            "created_at": now,
            "updated_at": now,
        }
        self._upsert_leg(values)
        created = self._leg(str(order["paper_order_id"]), role)
        assert created is not None
        return created

    def _apply_exit_order(
        self,
        order: dict[str, Any],
        leg: dict[str, Any],
        payload: dict[str, Any],
        *,
        position_quantity: Decimal,
        observed_at: datetime,
    ) -> None:
        safe = _safe_payload(payload)
        status = str(safe.get("status") or "unknown").lower()
        values = {
            **leg,
            "broker_order_id": str(safe.get("id") or "") or leg.get("broker_order_id"),
            "status": status,
            "filled_quantity": _decimal(safe.get("filled_qty")),
            "filled_average_price": (
                _decimal(safe.get("filled_avg_price"))
                if safe.get("filled_avg_price") not in {None, ""}
                else None
            ),
            "lifecycle_complete": status in _TERMINAL_STATUSES,
            "broker_payload_json": safe,
            "error_code": None,
            "error_message": None,
            "submitted_at": _utc(leg.get("submitted_at")) or observed_at,
            "last_reconciled_at": observed_at,
            "updated_at": observed_at,
        }
        self._upsert_leg(values)
        lifecycle_complete = position_quantity == _ZERO and status in _TERMINAL_STATUSES
        overall_status = "CLOSED" if lifecycle_complete else f"{leg['leg_role']}_{status}".upper()
        with self.engine.begin() as connection:
            connection.execute(
                update(paper_orders)
                .where(paper_orders.c.paper_order_id == order["paper_order_id"])
                .values(
                    status=overall_status,
                    lifecycle_complete=lifecycle_complete,
                    error_code=(
                        None
                        if status not in _TERMINAL_FAILURES
                        else "PAPER_EXIT_ORDER_FAILED"
                    ),
                    error_message=(
                        None
                        if status not in _TERMINAL_FAILURES
                        else f"{leg['leg_role']} ended {status} with position {position_quantity}"
                    ),
                    last_reconciled_at=observed_at,
                    updated_at=observed_at,
                )
            )
        self._append_order_event(
            str(order["paper_order_id"]),
            broker_status=overall_status,
            filled_quantity=_decimal(order["filled_quantity"]),
            filled_average_price=(
                _decimal(order["filled_average_price"])
                if order.get("filled_average_price") is not None
                else None
            ),
            lifecycle_complete=lifecycle_complete,
            payload={"leg_role": leg["leg_role"], "broker_order": safe},
            observed_at=observed_at,
        )

    async def _submit_or_reconcile_exit(
        self,
        broker: PaperBroker,
        order: dict[str, Any],
        *,
        position_quantity: Decimal,
        emergency: bool,
        lease_token: str,
    ) -> bool:
        leg = self._ensure_exit_intent(
            order,
            quantity=position_quantity,
            emergency=emergency,
        )
        client_order_id = str(leg["client_order_id"])
        payload = await broker.fetch_order_by_client_id(client_order_id)
        if payload is None:
            with self.engine.begin() as connection:
                connection.execute(
                    update(paper_order_legs)
                    .where(
                        paper_order_legs.c.paper_order_leg_id
                        == leg["paper_order_leg_id"]
                    )
                    .values(
                        status="SUBMITTING",
                        submission_attempts=paper_order_legs.c.submission_attempts + 1,
                        updated_at=self._now(),
                    )
                )
            try:
                payload = await broker.submit_market_exit_order(
                    client_order_id=client_order_id,
                    symbol=str(order["symbol"]),
                    quantity=position_quantity,
                    time_in_force=str(leg["time_in_force"]),
                )
            except AlpacaPaperResponseError as exc:
                terminal = 400 <= exc.status_code < 500
                with self.engine.begin() as connection:
                    connection.execute(
                        update(paper_order_legs)
                        .where(
                            paper_order_legs.c.paper_order_leg_id
                            == leg["paper_order_leg_id"]
                        )
                        .values(
                            status="REJECTED" if terminal else "SUBMISSION_UNKNOWN",
                            lifecycle_complete=terminal,
                            error_code=f"HTTP_{exc.status_code}",
                            error_message=exc.detail[:2_000],
                            updated_at=self._now(),
                        )
                    )
                if terminal:
                    return False
                raise
        self._assert_lease(lease_token)
        positions = await broker.fetch_positions()
        current_quantity = self._position_quantity(
            positions,
            symbol=str(order["symbol"]),
        )
        self._assert_lease(lease_token)
        refreshed_leg = self._leg(str(order["paper_order_id"]), str(leg["leg_role"]))
        assert refreshed_leg is not None
        self._apply_exit_order(
            order,
            refreshed_leg,
            payload,
            position_quantity=current_quantity,
            observed_at=self._now(),
        )
        return True

    def _append_order_event(
        self,
        order_id: str,
        *,
        broker_status: str,
        filled_quantity: Decimal,
        filled_average_price: Decimal | None,
        lifecycle_complete: bool,
        payload: dict[str, Any],
        observed_at: datetime,
    ) -> None:
        with self.engine.begin() as connection:
            sequence = int(
                connection.execute(
                    select(func.count())
                    .select_from(paper_order_events)
                    .where(paper_order_events.c.paper_order_id == order_id)
                ).scalar_one()
            ) + 1
            connection.execute(
                insert(paper_order_events).values(
                    paper_order_event_id=uuid7(),
                    paper_order_id=order_id,
                    sequence=sequence,
                    broker_status=broker_status,
                    filled_quantity=filled_quantity,
                    filled_average_price=filled_average_price,
                    lifecycle_complete=lifecycle_complete,
                    payload_json=payload,
                    observed_at=observed_at,
                    created_at=self._now(),
                )
            )

    def _capture_account(
        self,
        account: dict[str, Any],
        positions: tuple[dict[str, Any], ...],
        *,
        observed_at: datetime,
    ) -> None:
        safe_account = _safe_payload(account)
        safe_positions = tuple(_safe_payload(item) for item in positions)
        signature = hashlib.sha256(
            json.dumps(
                {"account": safe_account, "positions": safe_positions},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        safe_account["_positions_signature"] = signature
        latest = self.latest_account()
        if latest is not None:
            latest_at = _utc(latest["observed_at"])
            if (
                dict(latest["payload_json"] or {}).get("_positions_signature")
                == signature
                and latest_at is not None
                and observed_at - latest_at < timedelta(minutes=5)
            ):
                return
        snapshot_id = uuid7()
        with self.engine.begin() as connection:
            connection.execute(
                insert(paper_account_snapshots).values(
                    paper_account_snapshot_id=snapshot_id,
                    broker_account_id=str(safe_account.get("id") or "UNKNOWN"),
                    status=str(safe_account.get("status") or "UNKNOWN"),
                    currency=str(safe_account.get("currency") or "USD"),
                    cash=_decimal(safe_account.get("cash")),
                    buying_power=_decimal(safe_account.get("buying_power")),
                    equity=_decimal(safe_account.get("equity")),
                    portfolio_value=_decimal(safe_account.get("portfolio_value")),
                    last_equity=_decimal(safe_account.get("last_equity")),
                    pattern_day_trader=bool(safe_account.get("pattern_day_trader")),
                    trading_blocked=bool(safe_account.get("trading_blocked")),
                    transfers_blocked=bool(safe_account.get("transfers_blocked")),
                    account_blocked=bool(safe_account.get("account_blocked")),
                    payload_json=safe_account,
                    observed_at=observed_at,
                )
            )
            if safe_positions:
                connection.execute(
                    insert(paper_position_snapshots),
                    [
                        {
                            "paper_position_snapshot_id": uuid7(),
                            "paper_account_snapshot_id": snapshot_id,
                            "symbol": str(item.get("symbol") or "UNKNOWN"),
                            "side": str(item.get("side") or "long"),
                            "quantity": _decimal(item.get("qty")),
                            "average_entry_price": _decimal(
                                item.get("avg_entry_price")
                            ),
                            "current_price": _decimal(item.get("current_price")),
                            "market_value": _decimal(item.get("market_value")),
                            "cost_basis": _decimal(item.get("cost_basis")),
                            "unrealized_pnl": _decimal(item.get("unrealized_pl")),
                            "unrealized_pnl_fraction": _decimal(
                                item.get("unrealized_plpc")
                            ),
                            "payload_json": item,
                            "observed_at": observed_at,
                        }
                        for item in safe_positions
                    ],
                )

    def _pin_account(self, broker_account_id: str) -> None:
        now = self._now()
        mismatch_detected = False
        with self.engine.begin() as connection:
            rows = connection.execute(
                select(paper_enrollments).where(
                    paper_enrollments.c.status == "ACTIVE"
                )
            ).all()
            mismatched = [
                row
                for row in rows
                if row.broker_account_id not in {None, broker_account_id}
            ]
            if mismatched:
                mismatch_detected = True
                connection.execute(
                    update(paper_enrollments)
                    .where(
                        paper_enrollments.c.paper_enrollment_id.in_(
                            [row.paper_enrollment_id for row in mismatched]
                        )
                    )
                    .values(status="ACCOUNT_MISMATCH", updated_at=now)
                )
            else:
                connection.execute(
                    update(paper_enrollments)
                    .where(
                        (paper_enrollments.c.status == "ACTIVE")
                        & paper_enrollments.c.broker_account_id.is_(None)
                    )
                    .values(broker_account_id=broker_account_id, updated_at=now)
                )
        if mismatch_detected:
            raise RuntimeError(
                "Alpaca paper account changed; affected enrollments were quarantined"
            )

    def _acquire_lease(self, *, owner: str) -> str | None:
        now = self._now()
        token = uuid7()
        values = {
            "lease_key": "paper:broker-execution",
            "lease_owner": owner,
            "lease_token": token,
            "lease_expires_at": now + timedelta(minutes=2),
            "updated_at": now,
        }
        dialect_insert: Any
        if self.engine.dialect.name == "postgresql":
            dialect_insert = postgresql_insert
        elif self.engine.dialect.name == "sqlite":
            dialect_insert = sqlite_insert
        else:
            raise RuntimeError(f"Unsupported SQL dialect: {self.engine.dialect.name}")
        with self.engine.begin() as connection:
            claimed = connection.execute(
                dialect_insert(runtime_leases)
                .values(**values)
                .on_conflict_do_update(
                    index_elements=["lease_key"],
                    set_=values,
                    where=runtime_leases.c.lease_expires_at <= now,
                )
                .returning(runtime_leases.c.lease_token)
            ).scalar_one_or_none()
        return str(claimed) if claimed is not None else None

    def _assert_lease(self, token: str) -> None:
        with self.engine.connect() as connection:
            active = connection.execute(
                select(runtime_leases.c.lease_token).where(
                    (runtime_leases.c.lease_key == "paper:broker-execution")
                    & (runtime_leases.c.lease_token == token)
                    & (runtime_leases.c.lease_expires_at > self._now())
                )
            ).scalar_one_or_none()
        if active is None:
            raise RuntimeError("Paper broker execution lease was lost")

    def _release_lease(self, token: str) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                delete(runtime_leases).where(
                    (runtime_leases.c.lease_key == "paper:broker-execution")
                    & (runtime_leases.c.lease_token == token)
                )
            )
