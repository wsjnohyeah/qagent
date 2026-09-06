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


BrokerFactory = Callable[[], PaperBroker]
_ZERO = Decimal("0")
_TERMINAL_FAILURES = {
    "canceled",
    "expired",
    "rejected",
    "replaced",
    "stopped",
    "suspended",
}


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


def _order_lifecycle_complete(payload: dict[str, Any]) -> bool:
    status = str(payload.get("status") or "unknown").lower()
    if status in _TERMINAL_FAILURES:
        return True
    if status != "filled":
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
            }
        return {
            "provider": "alpaca_paper",
            "configured": self.configured,
            "paper_trading_enabled": self.enabled,
            "trading_mode": self.trading_mode,
            "live_money_possible": False,
            "submission_ready": (
                self.configured and self.enabled and self.trading_mode == "paper"
            ),
            "latest_account": latest,
            "latest_run": recent_runs[0] if recent_runs else None,
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
            )
            .join(
                paper_enrollments,
                paper_enrollments.c.paper_enrollment_id
                == paper_orders.c.paper_enrollment_id,
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
            "summary": f"Cancel Alpaca paper order for {order['symbol']}",
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
                return await self._cancel_order_locked(paper_order_id)
            finally:
                self._release_lease(token)

    async def _cancel_order_locked(self, paper_order_id: str) -> dict[str, Any]:
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
            await broker.cancel_order(str(row.broker_order_id))
            payload = await broker.fetch_order_by_client_id(str(row.client_order_id))
        if payload is not None:
            self._apply_broker_order(str(row.paper_order_id), payload, self._now())
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
                managed_symbols = {
                    str(order["symbol"])
                    for order in all_orders
                    if not bool(order["lifecycle_complete"])
                }
                unmanaged_symbols = sorted(
                    str(position.get("symbol") or "UNKNOWN")
                    for position in positions
                    if str(position.get("symbol") or "UNKNOWN")
                    not in managed_symbols
                )
                if unmanaged_symbols:
                    submission_blockers.append(
                        "UNMANAGED_PAPER_POSITIONS:" + ",".join(unmanaged_symbols)
                    )
                blocked = bool(submission_blockers)
                buying_power = _decimal(account.get("buying_power"))
                open_risk = sum(
                    max(
                        _ZERO,
                        _decimal(order["entry_limit_price"])
                        - _decimal(order["stop_loss_price"]),
                    )
                    * Decimal(int(order["quantity"]))
                    for order in all_orders
                    if not bool(order["lifecycle_complete"])
                )
                for order in all_orders:
                    if bool(order["lifecycle_complete"]):
                        continue
                    payload = await broker.fetch_order_by_client_id(
                        str(order["client_order_id"])
                    )
                    if payload is not None:
                        self._assert_lease(lease_token)
                        self._apply_broker_order(
                            str(order["paper_order_id"]), payload, self._now()
                        )
                        reconciled += 1
                        current = self._order(str(order["paper_order_id"]))
                        expires_at = _utc(current["plan_expires_at"])
                        if (
                            expires_at is not None
                            and expires_at <= self._now()
                            and _decimal(current["filled_quantity"]) == _ZERO
                            and not bool(current["lifecycle_complete"])
                            and current["broker_order_id"] is not None
                        ):
                            await broker.cancel_order(
                                str(current["broker_order_id"])
                            )
                            cancelled = await broker.fetch_order_by_client_id(
                                str(current["client_order_id"])
                            )
                            if cancelled is not None:
                                self._apply_broker_order(
                                    str(current["paper_order_id"]),
                                    cancelled,
                                    self._now(),
                                )
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
                    }
                    if new_exposure_paused or blocked or not retryable:
                        continue
                    required = (
                        _decimal(order["entry_limit_price"])
                        * Decimal(int(order["quantity"]))
                        * Decimal("1.05")
                    )
                    if required > buying_power:
                        continue
                    if await self._submit_intent(broker, order, lease_token):
                        submitted += 1
                        buying_power -= required
                if not new_exposure_paused and not blocked:
                    for plan in self._eligible_plans(self._now()):
                        trade_risk = max(
                            _ZERO,
                            _decimal(plan["limit_price"])
                            - _decimal(plan["invalidation"]),
                        ) * Decimal(int(plan["quantity"]))
                        if trade_risk > _decimal(
                            limits["maximum_trade_risk_usd"]
                        ):
                            continue
                        if open_risk + trade_risk > _decimal(
                            limits["maximum_concurrent_risk_usd"]
                        ):
                            continue
                        required = (
                            _decimal(plan["limit_price"])
                            * Decimal(int(plan["quantity"]))
                            * Decimal("1.05")
                        )
                        if required > buying_power:
                            continue
                        order_id = self._ensure_order_intent(plan)
                        current = self._order(order_id)
                        if bool(current["lifecycle_complete"]):
                            continue
                        if await self._submit_intent(
                            broker, current, lease_token
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
        self._apply_broker_order(order_id, payload, self._now())
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
            eligible.append(value)
        return eligible

    def _ensure_order_intent(self, plan: dict[str, Any]) -> str:
        targets = list(plan["targets_json"] or [])
        if str(plan["direction"]).upper() != "LONG" or not targets:
            raise ValueError("Phase 7 currently supports long bracket plans only")
        plan_id = str(plan["trade_plan_id"])
        client_order_id = "qagent-" + hashlib.sha256(plan_id.encode()).hexdigest()[:32]
        order_id = stable_uuid("paper-order", plan_id)
        now = self._now()
        values = {
            "paper_order_id": order_id,
            "paper_enrollment_id": plan["paper_enrollment_id"],
            "shadow_trade_plan_id": plan_id,
            "client_order_id": client_order_id,
            "broker_order_id": None,
            "symbol": str(plan["symbol"]).upper(),
            "side": "buy",
            "quantity": int(plan["quantity"]),
            "order_type": "limit",
            "time_in_force": "gtc",
            "order_class": "bracket",
            "entry_limit_price": _decimal(plan["limit_price"]),
            "take_profit_price": _decimal(targets[0]),
            "stop_loss_price": _decimal(plan["invalidation"]),
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

    def _order(self, order_id: str) -> dict[str, Any]:
        with self.engine.connect() as connection:
            row = connection.execute(
                select(paper_orders).where(
                    paper_orders.c.paper_order_id == order_id
                )
            ).one()
        return dict(row._mapping)

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
        with self.engine.begin() as connection:
            connection.execute(
                update(paper_orders)
                .where(paper_orders.c.paper_order_id == order_id)
                .values(
                    status="EXPIRED_BEFORE_SUBMISSION",
                    lifecycle_complete=True,
                    error_code="PLAN_EXPIRED",
                    error_message="Approved plan expired before broker acknowledgment",
                    updated_at=now,
                )
            )
        self._append_order_event(
            order_id,
            broker_status="EXPIRED_BEFORE_SUBMISSION",
            filled_quantity=_decimal(order["filled_quantity"]),
            filled_average_price=(
                _decimal(order["filled_average_price"])
                if order["filled_average_price"] is not None
                else None
            ),
            lifecycle_complete=True,
            payload={"reason": "PLAN_EXPIRED"},
            observed_at=now,
        )

    def _apply_broker_order(
        self,
        order_id: str,
        payload: dict[str, Any],
        observed_at: datetime,
    ) -> None:
        safe = _safe_payload(payload)
        status = str(safe.get("status") or "unknown").lower()
        filled_quantity = _decimal(safe.get("filled_qty"))
        filled_average_price = (
            _decimal(safe.get("filled_avg_price"))
            if safe.get("filled_avg_price") not in {None, ""}
            else None
        )
        lifecycle_complete = _order_lifecycle_complete(safe)
        existing = self._order(order_id)
        changed = (
            str(existing["status"]).lower() != status
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
                    status=status,
                    lifecycle_complete=lifecycle_complete,
                    filled_quantity=filled_quantity,
                    filled_average_price=filled_average_price,
                    broker_payload_json=safe,
                    error_code=None,
                    error_message=None,
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
