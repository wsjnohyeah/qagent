from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Engine, func, insert, select, update

from agentic_quant.backtest_engine import EventDrivenPortfolio
from agentic_quant.control_plane import SystemObjectStore
from agentic_quant.database import (
    shadow_deployments,
    shadow_events,
    shadow_runs,
    strategy_adoptions,
    strategy_specs,
    validation_reports,
)
from agentic_quant.domain import BacktestCostModel, SignalAction
from agentic_quant.ids import uuid7
from agentic_quant.market_calendar import MarketSessionClock
from agentic_quant.research import PointInTimeFeatureBuilder
from agentic_quant.research_store import ResearchStore


_ZERO = Decimal("0")


def _utc(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


class ShadowRuntime:
    """Deterministic, broker-free shadow execution over already-ingested bars."""

    def __init__(
        self,
        engine: Engine,
        research_store: ResearchStore,
        objects: SystemObjectStore,
        *,
        calendar_name: str = "XNYS",
    ) -> None:
        self.engine = engine
        self.research_store = research_store
        self.objects = objects
        self.features = PointInTimeFeatureBuilder(research_store)
        self.session_clock = MarketSessionClock(calendar_name)
        self.costs = BacktestCostModel()
        self._tick_lock = asyncio.Lock()

    def adoption_preview(
        self,
        *,
        strategy_spec_id: str,
        validation_report_id: str,
    ) -> dict[str, Any]:
        with self.engine.connect() as connection:
            spec = connection.execute(
                select(strategy_specs).where(
                    strategy_specs.c.strategy_spec_id == strategy_spec_id
                )
            ).one_or_none()
            if spec is None:
                raise ValueError("Strategy specification not found")
            report = connection.execute(
                select(validation_reports).where(
                    validation_reports.c.validation_report_id == validation_report_id
                )
            ).one_or_none()
        if report is None:
            raise ValueError("Validation report not found")
        gate = dict(report.gate_assessment)
        if gate.get("eligible_for_human_review") is not True:
            raise ValueError(
                "Strategy is not eligible for human review under the deterministic gate"
            )
        if str(spec.strategy_type) not in list(report.strategy_types):
            raise ValueError("Validation report does not cover this strategy type")
        if str(spec.timeframe) != str(report.timeframe):
            raise ValueError("Validation report timeframe does not match the strategy")
        return {
            "summary": f"Adopt {spec.name}@{spec.version} for shadow use",
            "strategy_spec_id": strategy_spec_id,
            "validation_report_id": validation_report_id,
            "gate_assessment": gate,
            "symbol": report.symbol,
            "timeframe": report.timeframe,
            "live_broker_effect": False,
        }

    def adopt_strategy(
        self,
        *,
        strategy_spec_id: str,
        validation_report_id: str,
        reason: str,
        approved_by: str,
    ) -> dict[str, Any]:
        self.adoption_preview(
            strategy_spec_id=strategy_spec_id,
            validation_report_id=validation_report_id,
        )
        now = datetime.now(UTC)
        with self.engine.begin() as connection:
            spec = connection.execute(
                select(strategy_specs).where(
                    strategy_specs.c.strategy_spec_id == strategy_spec_id
                )
            ).one_or_none()
            if spec is None:
                raise ValueError("Strategy specification not found")
            report = connection.execute(
                select(validation_reports).where(
                    validation_reports.c.validation_report_id == validation_report_id
                )
            ).one_or_none()
            if report is None:
                raise ValueError("Validation report not found")
            existing = connection.execute(
                select(strategy_adoptions).where(
                    strategy_adoptions.c.strategy_spec_id == strategy_spec_id
                )
            ).one_or_none()
            values = {
                "status": "ADOPTED_FOR_SHADOW",
                "validation_report_id": validation_report_id,
                "reason": reason,
                "approved_by": approved_by,
                "updated_at": now,
            }
            if existing is None:
                adoption_id = uuid7()
                connection.execute(
                    insert(strategy_adoptions).values(
                        adoption_id=adoption_id,
                        strategy_spec_id=strategy_spec_id,
                        created_at=now,
                        **values,
                    )
                )
            else:
                adoption_id = str(existing.adoption_id)
                connection.execute(
                    update(strategy_adoptions)
                    .where(strategy_adoptions.c.adoption_id == adoption_id)
                    .values(**values)
                )
        self.objects.post(
            object_type="strategy",
            object_id=strategy_spec_id,
            title=str(spec.name),
            author_kind="admin",
            author_name=approved_by,
            body=f"Adopted for shadow operation. {reason}",
            metadata={"validation_report_id": validation_report_id},
        )
        return self.adoption(adoption_id)

    def set_adoption_status(
        self,
        *,
        strategy_spec_id: str,
        status: str,
        reason: str,
        approved_by: str,
    ) -> dict[str, Any]:
        if status not in {"PAUSED", "RETIRED"}:
            raise ValueError("Unsupported adoption status transition")
        now = datetime.now(UTC)
        with self.engine.begin() as connection:
            row = connection.execute(
                select(strategy_adoptions).where(
                    strategy_adoptions.c.strategy_spec_id == strategy_spec_id
                )
            ).one_or_none()
            if row is None:
                raise ValueError("Strategy has not been adopted")
            connection.execute(
                update(strategy_adoptions)
                .where(strategy_adoptions.c.adoption_id == row.adoption_id)
                .values(
                    status=status,
                    reason=reason,
                    approved_by=approved_by,
                    updated_at=now,
                )
            )
            connection.execute(
                update(shadow_deployments)
                .where(shadow_deployments.c.strategy_spec_id == strategy_spec_id)
                .values(status=status, updated_at=now)
            )
        return self.adoption(str(row.adoption_id))

    def adoption(self, adoption_id: str) -> dict[str, Any]:
        with self.engine.connect() as connection:
            row = connection.execute(
                select(strategy_adoptions).where(
                    strategy_adoptions.c.adoption_id == adoption_id
                )
            ).one_or_none()
        if row is None:
            raise ValueError("Strategy adoption not found")
        return dict(row._mapping)

    def start_deployment(
        self,
        *,
        strategy_spec_id: str,
        symbol: str,
        initial_cash: Decimal,
        requested_by: str,
    ) -> dict[str, Any]:
        normalized_symbol = symbol.strip().upper()
        self.deployment_preview(
            strategy_spec_id=strategy_spec_id,
            symbol=normalized_symbol,
            initial_cash=initial_cash,
        )
        with self.engine.connect() as connection:
            timeframe = connection.execute(
                select(strategy_specs.c.timeframe).where(
                    strategy_specs.c.strategy_spec_id == strategy_spec_id
                )
            ).scalar_one()
        existing_bars = self.research_store.load_bars(
            symbol=normalized_symbol,
            timeframe=str(timeframe),
            as_of_end=datetime.now(UTC),
        )
        initial_cursor = (
            existing_bars[-2].event_time if len(existing_bars) >= 2 else None
        )
        now = datetime.now(UTC)
        with self.engine.begin() as connection:
            adoption = connection.execute(
                select(strategy_adoptions).where(
                    strategy_adoptions.c.strategy_spec_id == strategy_spec_id
                )
            ).one_or_none()
            if adoption is None or adoption.status != "ADOPTED_FOR_SHADOW":
                raise ValueError("Strategy must be adopted before shadow deployment")
            existing = connection.execute(
                select(shadow_deployments).where(
                    (shadow_deployments.c.strategy_spec_id == strategy_spec_id)
                    & (shadow_deployments.c.symbol == normalized_symbol)
                )
            ).one_or_none()
            if existing is None:
                deployment_id = uuid7()
                connection.execute(
                    insert(shadow_deployments).values(
                        shadow_deployment_id=deployment_id,
                        strategy_spec_id=strategy_spec_id,
                        adoption_id=adoption.adoption_id,
                        symbol=normalized_symbol,
                        status="ACTIVE",
                        initial_cash=initial_cash,
                        cash_balance=initial_cash,
                        position_quantity=_ZERO,
                        average_entry_price=None,
                        last_price=None,
                        realized_pnl=_ZERO,
                        unrealized_pnl=_ZERO,
                        last_processed_bar_time=initial_cursor,
                        created_at=now,
                        updated_at=now,
                    )
                )
            else:
                if existing.status == "RETIRED":
                    raise ValueError("Retired deployments are immutable; create a new strategy")
                deployment_id = str(existing.shadow_deployment_id)
                connection.execute(
                    update(shadow_deployments)
                    .where(
                        shadow_deployments.c.shadow_deployment_id == deployment_id
                    )
                    .values(status="ACTIVE", updated_at=now)
                )
        self._sync_active_symbols(
            normalized_symbol,
            add=True,
            reason=f"Shadow deployment started by {requested_by}",
        )
        return self.deployment(deployment_id)

    def deployment_preview(
        self,
        *,
        strategy_spec_id: str,
        symbol: str,
        initial_cash: Decimal,
    ) -> dict[str, Any]:
        if initial_cash <= 0:
            raise ValueError("Initial shadow cash must be positive")
        normalized_symbol = symbol.strip().upper()
        if not normalized_symbol or len(normalized_symbol) > 24:
            raise ValueError("Shadow symbol is invalid")
        universe = self.objects.get_list("trading-universe")
        restricted = self.objects.get_list("restricted")
        if universe is None or normalized_symbol not in universe["members"]:
            raise ValueError("Shadow symbol is outside the governed trading universe")
        if restricted is not None and normalized_symbol in restricted["members"]:
            raise ValueError("Restricted symbols cannot enter shadow deployment")
        with self.engine.connect() as connection:
            row = connection.execute(
                select(
                    strategy_adoptions.c.status,
                    strategy_specs.c.name,
                    strategy_specs.c.version,
                    validation_reports.c.symbol.label("validated_symbol"),
                )
                .join(
                    strategy_specs,
                    strategy_specs.c.strategy_spec_id
                    == strategy_adoptions.c.strategy_spec_id,
                )
                .where(
                    strategy_adoptions.c.strategy_spec_id == strategy_spec_id
                )
                .join(
                    validation_reports,
                    validation_reports.c.validation_report_id
                    == strategy_adoptions.c.validation_report_id,
                )
            ).one_or_none()
        if row is None or row.status != "ADOPTED_FOR_SHADOW":
            raise ValueError("Strategy must be adopted before shadow deployment")
        if str(row.validated_symbol) != normalized_symbol:
            raise ValueError("Shadow symbol does not match the reviewed validation report")
        return {
            "summary": f"Start {row.name}@{row.version} on {normalized_symbol}",
            "strategy_spec_id": strategy_spec_id,
            "symbol": normalized_symbol,
            "initial_cash": str(initial_cash),
            "live_broker_effect": False,
        }

    def set_deployment_status(
        self,
        *,
        deployment_id: str,
        status: str,
        requested_by: str,
        reason: str,
    ) -> dict[str, Any]:
        if status not in {"ACTIVE", "PAUSED", "RETIRED"}:
            raise ValueError("Unsupported shadow deployment status")
        with self.engine.begin() as connection:
            row = connection.execute(
                select(shadow_deployments).where(
                    shadow_deployments.c.shadow_deployment_id == deployment_id
                )
            ).one_or_none()
            if row is None:
                raise ValueError("Shadow deployment not found")
            if row.status == "RETIRED":
                raise ValueError("Retired shadow deployments cannot be changed")
            if status == "ACTIVE":
                adoption_status = connection.execute(
                    select(strategy_adoptions.c.status).where(
                        strategy_adoptions.c.strategy_spec_id == row.strategy_spec_id
                    )
                ).scalar_one_or_none()
                if adoption_status != "ADOPTED_FOR_SHADOW":
                    raise ValueError("Strategy adoption is not active")
            connection.execute(
                update(shadow_deployments)
                .where(shadow_deployments.c.shadow_deployment_id == deployment_id)
                .values(status=status, updated_at=datetime.now(UTC))
            )
        self._sync_active_symbols(
            str(row.symbol),
            add=status == "ACTIVE",
            reason=f"Shadow deployment {status.lower()}: {reason} ({requested_by})",
        )
        return self.deployment(deployment_id)

    def deployments(self, *, limit: int = 200) -> list[dict[str, Any]]:
        statement = (
            select(
                shadow_deployments,
                strategy_specs.c.name.label("strategy_name"),
                strategy_specs.c.version.label("strategy_version"),
                strategy_specs.c.strategy_type,
                strategy_specs.c.timeframe,
                strategy_specs.c.parameters_json,
            )
            .join(
                strategy_specs,
                strategy_specs.c.strategy_spec_id
                == shadow_deployments.c.strategy_spec_id,
            )
            .order_by(shadow_deployments.c.updated_at.desc())
            .limit(limit)
        )
        with self.engine.connect() as connection:
            return [dict(row._mapping) for row in connection.execute(statement)]

    def deployment(self, deployment_id: str) -> dict[str, Any]:
        values = [
            item
            for item in self.deployments(limit=1_000)
            if item["shadow_deployment_id"] == deployment_id
        ]
        if not values:
            raise ValueError("Shadow deployment not found")
        item = values[0]
        item["events"] = self.events(deployment_id=deployment_id, limit=250)
        return item

    def events(
        self,
        *,
        deployment_id: str | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        statement = select(shadow_events)
        if deployment_id is not None:
            statement = statement.where(
                shadow_events.c.shadow_deployment_id == deployment_id
            )
        statement = statement.order_by(
            shadow_events.c.created_at.desc(), shadow_events.c.sequence.desc()
        ).limit(limit)
        with self.engine.connect() as connection:
            return [dict(row._mapping) for row in connection.execute(statement)]

    def runs(self, *, limit: int = 100) -> list[dict[str, Any]]:
        statement = shadow_runs.select().order_by(
            shadow_runs.c.finished_at.desc()
        ).limit(limit)
        with self.engine.connect() as connection:
            return [dict(row._mapping) for row in connection.execute(statement)]

    async def tick(
        self,
        *,
        trigger: str = "manual",
        new_exposure_paused: bool,
    ) -> dict[str, Any]:
        if new_exposure_paused:
            raise ValueError("Global new-exposure pause blocks shadow processing")
        async with self._tick_lock:
            return self._tick_locked(trigger=trigger)

    def _tick_locked(self, *, trigger: str) -> dict[str, Any]:
        started_at = datetime.now(UTC)
        run_id = uuid7()
        deployments = [
            item for item in self.deployments(limit=1_000) if item["status"] == "ACTIVE"
        ]
        bars_processed = 0
        events_created = 0
        status = "SUCCEEDED"
        error_code: str | None = None
        try:
            for deployment in deployments:
                processed, created = self._process_deployment(deployment, run_id)
                bars_processed += processed
                events_created += created
        except Exception as exc:
            status = "FAILED"
            error_code = type(exc).__name__
            raise
        finally:
            finished_at = datetime.now(UTC)
            if trigger != "scheduler" or bars_processed > 0 or status == "FAILED":
                with self.engine.begin() as connection:
                    connection.execute(
                        insert(shadow_runs).values(
                            shadow_run_id=run_id,
                            status=status,
                            trigger=trigger[:40],
                            deployment_count=len(deployments),
                            bars_processed=bars_processed,
                            events_created=events_created,
                            started_at=started_at,
                            finished_at=finished_at,
                            error_code=error_code,
                        )
                    )
        return {
            "shadow_run_id": run_id,
            "status": status,
            "deployment_count": len(deployments),
            "bars_processed": bars_processed,
            "events_created": events_created,
            "started_at": started_at,
            "finished_at": finished_at,
        }

    def health_summary(self) -> dict[str, int]:
        with self.engine.connect() as connection:
            return {
                "shadow_deployments": int(
                    connection.execute(
                        select(func.count()).select_from(shadow_deployments)
                    ).scalar_one()
                ),
                "active_shadow_deployments": int(
                    connection.execute(
                        select(func.count())
                        .select_from(shadow_deployments)
                        .where(shadow_deployments.c.status == "ACTIVE")
                    ).scalar_one()
                ),
                "shadow_events": int(
                    connection.execute(
                        select(func.count()).select_from(shadow_events)
                    ).scalar_one()
                ),
                "shadow_runs": int(
                    connection.execute(
                        select(func.count()).select_from(shadow_runs)
                    ).scalar_one()
                ),
            }

    def _process_deployment(
        self,
        deployment: dict[str, Any],
        run_id: str,
    ) -> tuple[int, int]:
        bars = self.research_store.load_bars(
            symbol=str(deployment["symbol"]),
            timeframe=str(deployment["timeframe"]),
            as_of_end=datetime.now(UTC),
        )
        if len(bars) < 22:
            return 0, 0
        last_processed = _utc(deployment["last_processed_bar_time"])
        decisions = [
            index
            for index in range(20, len(bars) - 1)
            if last_processed is None or bars[index].event_time > last_processed
        ]
        processed = 0
        created = 0
        for index in decisions:
            decision_bar = bars[index]
            execution_bar = bars[index + 1]
            snapshot = self.features.build(
                symbol=str(deployment["symbol"]),
                timeframe=str(deployment["timeframe"]),
                as_of=decision_bar.available_from,
                bars=bars[: index + 1],
            )
            action = self._signal_action(deployment, snapshot.values)
            event_values, cash, realized = self._simulate_one_bar(
                deployment=deployment,
                run_id=run_id,
                decision_bar_id=decision_bar.bar_id,
                snapshot_id=snapshot.feature_snapshot_id,
                signal_time=snapshot.as_of,
                entry_time=(
                    self.session_clock.daily_bar_session_open(
                        execution_bar.event_time
                    )
                    if execution_bar.timeframe == "1Day"
                    else execution_bar.event_time
                ),
                exit_time=execution_bar.available_from,
                action=action,
                open_price=execution_bar.open,
                close_price=execution_bar.close,
                volume=execution_bar.volume,
            )
            with self.engine.begin() as connection:
                if event_values:
                    connection.execute(insert(shadow_events), event_values)
                connection.execute(
                    update(shadow_deployments)
                    .where(
                        shadow_deployments.c.shadow_deployment_id
                        == deployment["shadow_deployment_id"]
                    )
                    .values(
                        cash_balance=cash,
                        position_quantity=_ZERO,
                        average_entry_price=None,
                        last_price=execution_bar.close,
                        realized_pnl=realized,
                        unrealized_pnl=_ZERO,
                        last_processed_bar_time=decision_bar.event_time,
                        updated_at=datetime.now(UTC),
                    )
                )
            deployment["cash_balance"] = cash
            deployment["realized_pnl"] = realized
            deployment["last_processed_bar_time"] = decision_bar.event_time
            processed += 1
            created += len(event_values)
        return processed, created

    def _simulate_one_bar(
        self,
        *,
        deployment: dict[str, Any],
        run_id: str,
        decision_bar_id: str,
        snapshot_id: str,
        signal_time: datetime,
        entry_time: datetime,
        exit_time: datetime,
        action: SignalAction,
        open_price: Decimal,
        close_price: Decimal,
        volume: int,
    ) -> tuple[list[dict[str, Any]], Decimal, Decimal]:
        starting_cash = Decimal(str(deployment["cash_balance"]))
        portfolio = EventDrivenPortfolio(
            experiment_run_id=run_id,
            symbol=str(deployment["symbol"]),
            initial_cash=starting_cash,
            cost_model=self.costs,
        )
        snapshot = self.research_store.feature_snapshot(snapshot_id)
        assert snapshot is not None
        portfolio.record_signal(snapshot=snapshot, action=action)
        if action == SignalAction.LONG:
            entered = portfolio.enter_long(
                signal_as_of=signal_time,
                entry_time=entry_time,
                raw_price=open_price,
                available_volume=volume,
                feature_snapshot_id=snapshot_id,
            )
            if entered:
                portfolio.exit_long(
                    exit_time=exit_time,
                    raw_price=close_price,
                    available_volume=volume,
                    exit_reason="shadow_bar_close",
                )
            else:
                portfolio.mark(event_time=exit_time, raw_price=close_price)
        else:
            portfolio.mark(event_time=exit_time, raw_price=close_price)
        next_sequence = self._next_event_sequence(
            str(deployment["shadow_deployment_id"])
        )
        event_counts: dict[str, int] = {}
        values: list[dict[str, Any]] = []
        type_names = {
            "order_submitted": "VIRTUAL_ORDER",
            "fill": "VIRTUAL_FILL",
        }
        for offset, event in enumerate(portfolio.events):
            base_type = type_names.get(event.event_type.value, event.event_type.value)
            count = event_counts.get(base_type, 0) + 1
            event_counts[base_type] = count
            event_type = base_type if count == 1 else f"{base_type}_{count}"
            values.append(
                {
                    "shadow_event_id": uuid7(),
                    "shadow_deployment_id": deployment["shadow_deployment_id"],
                    "shadow_run_id": run_id,
                    "sequence": next_sequence + offset,
                    "event_type": event_type,
                    "event_time": event.event_time,
                    "symbol": deployment["symbol"],
                    "bar_id": decision_bar_id,
                    "cash_balance": event.cash_balance,
                    "position_quantity": event.position_quantity,
                    "price": event.price,
                    "realized_pnl_delta": (
                        portfolio.cash - starting_cash
                        if offset == len(portfolio.events) - 1
                        else _ZERO
                    ),
                    "payload_json": {
                        **event.details,
                        "feature_snapshot_id": event.feature_snapshot_id,
                        "virtual_only": True,
                    },
                    "created_at": datetime.now(UTC),
                }
            )
        pnl_delta = portfolio.cash - starting_cash
        realized = Decimal(str(deployment["realized_pnl"])) + pnl_delta
        return values, portfolio.cash, realized

    @staticmethod
    def _signal_action(
        deployment: dict[str, Any],
        values: dict[str, Any],
    ) -> SignalAction:
        strategy_type = str(deployment["strategy_type"])
        if strategy_type == "buy_and_hold":
            return SignalAction.LONG
        close = Decimal(str(values["close"]))
        sma_20 = Decimal(str(values["sma_20"]))
        return_5 = Decimal(str(values["return_5"]))
        parameters = dict(deployment.get("parameters_json") or {})
        if strategy_type == "momentum":
            threshold = Decimal(str(parameters.get("minimum_return", "0")))
            return (
                SignalAction.LONG
                if return_5 > threshold and close > sma_20
                else SignalAction.FLAT
            )
        if strategy_type == "mean_reversion":
            threshold = Decimal(str(parameters.get("maximum_return", "-0.02")))
            return (
                SignalAction.LONG
                if return_5 < threshold and close < sma_20
                else SignalAction.FLAT
            )
        raise ValueError(f"Unsupported shadow strategy type: {strategy_type}")

    def _next_event_sequence(self, deployment_id: str) -> int:
        with self.engine.connect() as connection:
            current = connection.execute(
                select(func.max(shadow_events.c.sequence)).where(
                    shadow_events.c.shadow_deployment_id == deployment_id
                )
            ).scalar_one_or_none()
        return int(current or 0) + 1

    def _sync_active_symbols(self, symbol: str, *, add: bool, reason: str) -> None:
        current = self.objects.get_list("shadow-active")
        if current is None:
            return
        members = set(current["members"])
        if add:
            members.add(symbol)
        else:
            with self.engine.connect() as connection:
                remaining = connection.execute(
                    select(func.count())
                    .select_from(shadow_deployments)
                    .where(
                        (shadow_deployments.c.symbol == symbol)
                        & (shadow_deployments.c.status == "ACTIVE")
                    )
                ).scalar_one()
            if int(remaining) == 0:
                members.discard(symbol)
        self.objects.replace_list_members(
            slug_or_id="shadow-active",
            members=sorted(members),
            reason=reason,
            created_by="shadow-runtime",
        )
