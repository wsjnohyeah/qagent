from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import Engine, func, insert, select, update

from agentic_quant.backtest_engine import EventDrivenPortfolio
from agentic_quant.config import TradingMode
from agentic_quant.control_plane import SystemObjectStore
from agentic_quant.database import (
    shadow_deployments,
    shadow_events,
    shadow_risk_decisions,
    shadow_runs,
    shadow_signal_candidates,
    shadow_trade_plans,
    strategy_adoptions,
    strategy_specs,
    validation_reports,
)
from agentic_quant.domain import (
    AccountState,
    BacktestCostModel,
    Direction,
    FeatureSnapshot,
    RiskEvaluationContext,
    RiskDecision,
    SignalAction,
    SignalCandidate,
    TradePlan,
    Verdict,
)
from agentic_quant.ids import uuid7
from agentic_quant.market_calendar import MarketSessionClock
from agentic_quant.research import (
    BACKTEST_ENGINE_VERSION,
    FEATURE_SET_VERSION,
    PointInTimeFeatureBuilder,
    strategy_signal_action,
)
from agentic_quant.research_store import ResearchStore, _canonical_hash
from agentic_quant.risk import (
    RestrictionRegistry,
    RiskPolicy,
    evaluate_candidate,
)


_ZERO = Decimal("0")
_ONE = Decimal("1")


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
        risk_policy: RiskPolicy,
        restrictions: RestrictionRegistry,
        calendar_name: str = "XNYS",
    ) -> None:
        self.engine = engine
        self.research_store = research_store
        self.objects = objects
        self.risk_policy = risk_policy
        self.restrictions = restrictions
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
        if str(report.validation_subject) != "static_strategy":
            raise ValueError(
                "An adaptive-selector report cannot authorize one static strategy"
            )
        validated_ids = dict(report.validated_strategy_spec_ids or {})
        if validated_ids.get(str(spec.strategy_type)) != strategy_spec_id:
            raise ValueError(
                "Validation report is not bound to this exact strategy specification"
            )
        if str(spec.timeframe) != str(report.timeframe):
            raise ValueError("Validation report timeframe does not match the strategy")
        if dict(spec.data_requirements_json or {}).get("shadow_deployable") is not True:
            raise ValueError(
                "This strategy is a research benchmark and has no matching shadow "
                "execution contract"
            )
        expected_contract = {
            "subject": "static_strategy",
            "strategy_spec_ids": {str(spec.strategy_type): strategy_spec_id},
            "feature_set_version": FEATURE_SET_VERSION,
            "backtest_engine_version": BACKTEST_ENGINE_VERSION,
            "cost_model": self.costs.model_dump(mode="json"),
        }
        if dict(report.execution_contract_json or {}) != expected_contract:
            raise ValueError(
                "Validation execution contract does not match the current shadow runtime"
            )
        if str(report.execution_contract_sha256) != _canonical_hash(expected_contract):
            raise ValueError("Validation execution contract hash is invalid")
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
                strategy_adoptions.c.validation_report_id,
                validation_reports.c.execution_contract_sha256,
            )
            .join(
                strategy_specs,
                strategy_specs.c.strategy_spec_id
                == shadow_deployments.c.strategy_spec_id,
            )
            .join(
                strategy_adoptions,
                strategy_adoptions.c.adoption_id == shadow_deployments.c.adoption_id,
            )
            .join(
                validation_reports,
                validation_reports.c.validation_report_id
                == strategy_adoptions.c.validation_report_id,
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
        item["decision_lineage"] = self.decision_lineage(
            deployment_id=deployment_id,
            limit=100,
        )
        return item

    def decision_lineage(
        self,
        *,
        deployment_id: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        statement = (
            select(
                shadow_signal_candidates,
                shadow_risk_decisions.c.risk_decision_id,
                shadow_risk_decisions.c.verdict,
                shadow_risk_decisions.c.reason_codes_json,
                shadow_risk_decisions.c.account_equity,
                shadow_risk_decisions.c.daily_pnl,
                shadow_risk_decisions.c.concurrent_planned_risk,
                shadow_risk_decisions.c.risk_budget_usd,
                shadow_risk_decisions.c.max_quantity,
                shadow_risk_decisions.c.policy_version,
                shadow_risk_decisions.c.evaluation_context_json,
                shadow_risk_decisions.c.evaluated_at,
                shadow_trade_plans.c.trade_plan_id,
                shadow_trade_plans.c.quantity.label("planned_quantity"),
                shadow_trade_plans.c.status.label("plan_status"),
                shadow_trade_plans.c.closed_at,
            )
            .join(
                shadow_risk_decisions,
                shadow_risk_decisions.c.candidate_id
                == shadow_signal_candidates.c.candidate_id,
            )
            .outerjoin(
                shadow_trade_plans,
                shadow_trade_plans.c.risk_decision_id
                == shadow_risk_decisions.c.risk_decision_id,
            )
        )
        if deployment_id is not None:
            statement = statement.where(
                shadow_signal_candidates.c.shadow_deployment_id == deployment_id
            )
        statement = statement.order_by(
            shadow_signal_candidates.c.created_at.desc()
        ).limit(limit)
        with self.engine.connect() as connection:
            return [dict(row._mapping) for row in connection.execute(statement)]

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

    def performance_reports(
        self,
        *,
        period: str = "daily",
        limit: int = 30,
    ) -> list[dict[str, Any]]:
        if period not in {"daily", "weekly"}:
            raise ValueError("Shadow report period must be daily or weekly")
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(shadow_events)
                .order_by(shadow_events.c.event_time.desc())
                .limit(20_000)
            ).all()
        grouped: dict[date, dict[str, Any]] = {}
        symbols: dict[date, set[str]] = defaultdict(set)
        deployments: dict[date, set[str]] = defaultdict(set)
        for row in rows:
            event_time = _utc(row.event_time)
            assert event_time is not None
            event_day = event_time.date()
            bucket = (
                event_day
                if period == "daily"
                else event_day - timedelta(days=event_day.weekday())
            )
            item = grouped.setdefault(
                bucket,
                {
                    "period": period,
                    "period_start": bucket.isoformat(),
                    "candidate_count": 0,
                    "approved_count": 0,
                    "rejected_count": 0,
                    "round_trip_count": 0,
                    "realized_pnl": _ZERO,
                },
            )
            symbols[bucket].add(str(row.symbol))
            deployments[bucket].add(str(row.shadow_deployment_id))
            if row.event_type == "SIGNAL_CANDIDATE":
                item["candidate_count"] += 1
            elif row.event_type == "RISK_DECISION":
                if str(row.payload_json.get("verdict")) == Verdict.APPROVE.value:
                    item["approved_count"] += 1
                else:
                    item["rejected_count"] += 1
            elif row.event_type == "VIRTUAL_FILL":
                item["round_trip_count"] += 1
            item["realized_pnl"] += Decimal(str(row.realized_pnl_delta))
        reports = []
        for bucket, item in sorted(grouped.items(), reverse=True)[:limit]:
            reports.append(
                {
                    **item,
                    "realized_pnl": str(item["realized_pnl"]),
                    "symbols": sorted(symbols[bucket]),
                    "deployment_count": len(deployments[bucket]),
                }
            )
        return reports

    def alerts(self, *, limit: int = 100) -> list[dict[str, Any]]:
        """Return deduplicated operational alerts derived from durable runtime facts."""
        with self.engine.connect() as connection:
            decisions = connection.execute(
                select(
                    shadow_signal_candidates.c.shadow_deployment_id,
                    shadow_signal_candidates.c.symbol,
                    shadow_risk_decisions.c.reason_codes_json,
                    shadow_risk_decisions.c.evaluated_at,
                )
                .join(
                    shadow_risk_decisions,
                    shadow_risk_decisions.c.candidate_id
                    == shadow_signal_candidates.c.candidate_id,
                )
                .where(shadow_risk_decisions.c.verdict == Verdict.REJECT.value)
                .order_by(shadow_risk_decisions.c.evaluated_at.desc())
                .limit(5_000)
            ).all()
            failures = connection.execute(
                select(shadow_runs)
                .where(shadow_runs.c.status == "FAILED")
                .order_by(shadow_runs.c.finished_at.desc())
                .limit(1_000)
            ).all()
        grouped: dict[tuple[str, str, date], dict[str, Any]] = {}
        for row in decisions:
            evaluated_at = _utc(row.evaluated_at)
            assert evaluated_at is not None
            reasons = ",".join(sorted(str(value) for value in row.reason_codes_json))
            key = (str(row.shadow_deployment_id), reasons, evaluated_at.date())
            current = grouped.get(key)
            if current is None:
                grouped[key] = {
                    "alert_key": f"risk:{key[0]}:{key[2].isoformat()}:{reasons}",
                    "kind": "RISK_REJECTION",
                    "severity": "WARNING",
                    "symbol": str(row.symbol),
                    "reason_codes": list(row.reason_codes_json),
                    "occurrence_count": 1,
                    "first_seen_at": evaluated_at,
                    "last_seen_at": evaluated_at,
                }
            else:
                current["occurrence_count"] += 1
                current["first_seen_at"] = min(
                    current["first_seen_at"], evaluated_at
                )
                current["last_seen_at"] = max(current["last_seen_at"], evaluated_at)
        alerts = list(grouped.values())
        alerts.extend(
            {
                "alert_key": f"shadow-run:{row.shadow_run_id}",
                "kind": "SHADOW_RUN_FAILED",
                "severity": "ERROR",
                "symbol": None,
                "reason_codes": [str(row.error_code or "UNKNOWN")],
                "occurrence_count": 1,
                "first_seen_at": _utc(row.finished_at),
                "last_seen_at": _utc(row.finished_at),
            }
            for row in failures
        )
        return sorted(
            alerts,
            key=lambda item: item["last_seen_at"],
            reverse=True,
        )[:limit]

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
                "shadow_candidates": int(
                    connection.execute(
                        select(func.count()).select_from(shadow_signal_candidates)
                    ).scalar_one()
                ),
                "shadow_risk_decisions": int(
                    connection.execute(
                        select(func.count()).select_from(shadow_risk_decisions)
                    ).scalar_one()
                ),
                "shadow_trade_plans": int(
                    connection.execute(
                        select(func.count()).select_from(shadow_trade_plans)
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
            entry_time = (
                self.session_clock.daily_bar_session_open(execution_bar.event_time)
                if execution_bar.timeframe == "1Day"
                else execution_bar.event_time
            )
            candidate = None
            decision = None
            plan = None
            evaluation_context = None
            lineage_values: list[dict[str, Any]] = []
            if action == SignalAction.LONG:
                (
                    candidate,
                    decision,
                    plan,
                    evaluation_context,
                    lineage_values,
                ) = self._risk_lineage(
                    deployment=deployment,
                    run_id=run_id,
                    decision_bar_id=decision_bar.bar_id,
                    snapshot_id=snapshot.feature_snapshot_id,
                    snapshot_values=snapshot.values,
                    signal_time=snapshot.as_of,
                    entry_time=entry_time,
                    exit_time=execution_bar.available_from,
                    planned_entry=execution_bar.open,
                    known_liquidity_volume=decision_bar.volume,
                )
            if decision is not None and decision.verdict != Verdict.APPROVE:
                event_values = lineage_values
                cash = Decimal(str(deployment["cash_balance"]))
                realized = Decimal(str(deployment["realized_pnl"]))
            else:
                simulated, cash, realized = self._simulate_one_bar(
                    deployment=deployment,
                    run_id=run_id,
                    decision_bar_id=decision_bar.bar_id,
                    snapshot_id=snapshot.feature_snapshot_id,
                    signal_time=snapshot.as_of,
                    entry_time=entry_time,
                    exit_time=execution_bar.available_from,
                    action=action,
                    open_price=execution_bar.open,
                    close_price=execution_bar.close,
                    # Opening capacity uses only the completed decision bar.
                    volume=decision_bar.volume,
                    exit_volume=execution_bar.volume,
                    quantity_limit=(plan.quantity if plan is not None else None),
                    lineage={
                        "candidate_id": candidate.candidate_id if candidate else None,
                        "risk_decision_id": (
                            decision.risk_decision_id if decision else None
                        ),
                        "trade_plan_id": plan.trade_plan_id if plan else None,
                    },
                )
                event_values = lineage_values + simulated
            next_sequence = self._next_event_sequence(
                str(deployment["shadow_deployment_id"])
            )
            for offset, value in enumerate(event_values):
                value["sequence"] = next_sequence + offset
            with self.engine.begin() as connection:
                if candidate is not None and decision is not None:
                    assert evaluation_context is not None
                    connection.execute(
                        insert(shadow_signal_candidates).values(
                            candidate_id=candidate.candidate_id,
                            shadow_deployment_id=deployment["shadow_deployment_id"],
                            shadow_run_id=run_id,
                            strategy_spec_id=deployment["strategy_spec_id"],
                            feature_snapshot_id=candidate.feature_snapshot_id,
                            decision_bar_id=decision_bar.bar_id,
                            symbol=candidate.symbol,
                            action=action.value,
                            as_of=candidate.as_of,
                            planned_entry=candidate.planned_entry,
                            invalidation=candidate.invalidation,
                            targets_json=[str(value) for value in candidate.targets],
                            expires_at=candidate.expires_at,
                            execution_contract_sha256=deployment[
                                "execution_contract_sha256"
                            ],
                            created_at=datetime.now(UTC),
                        )
                    )
                    account = self._account_state(deployment, entry_time)
                    connection.execute(
                        insert(shadow_risk_decisions).values(
                            risk_decision_id=decision.risk_decision_id,
                            candidate_id=candidate.candidate_id,
                            verdict=decision.verdict.value,
                            reason_codes_json=list(decision.reason_codes),
                            account_equity=decision.account_equity,
                            daily_pnl=account.daily_pnl,
                            concurrent_planned_risk=account.concurrent_planned_risk,
                            risk_budget_usd=decision.risk_budget_usd,
                            max_quantity=decision.max_quantity,
                            planned_entry=decision.planned_entry,
                            invalidation=decision.invalidation,
                            planned_r_multiple_to_t1=(
                                decision.planned_r_multiple_to_t1
                            ),
                            portfolio_risk_after_usd=decision.portfolio_risk_after_usd,
                            policy_version=decision.policy_version,
                            evaluation_context_json={
                                **evaluation_context.model_dump(mode="json"),
                                "liquidity_source_bar_id": decision_bar.bar_id,
                                "liquidity_source_volume": decision_bar.volume,
                                "future_execution_volume_used_for_entry": False,
                            },
                            evaluated_at=decision.evaluated_at,
                        )
                    )
                    if plan is not None:
                        connection.execute(
                            insert(shadow_trade_plans).values(
                                trade_plan_id=plan.trade_plan_id,
                                candidate_id=plan.candidate_id,
                                risk_decision_id=plan.risk_decision_id,
                                symbol=plan.symbol,
                                direction=plan.direction.value,
                                quantity=plan.quantity,
                                limit_price=plan.limit_price,
                                invalidation=plan.invalidation,
                                targets_json=[str(value) for value in plan.targets],
                                expires_at=plan.expires_at,
                                status="CLOSED",
                                created_at=entry_time,
                                closed_at=execution_bar.available_from,
                            )
                        )
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

    def _risk_lineage(
        self,
        *,
        deployment: dict[str, Any],
        run_id: str,
        decision_bar_id: str,
        snapshot_id: str,
        snapshot_values: dict[str, Any],
        signal_time: datetime,
        entry_time: datetime,
        exit_time: datetime,
        planned_entry: Decimal,
        known_liquidity_volume: int,
    ) -> tuple[
        SignalCandidate,
        RiskDecision,
        TradePlan | None,
        RiskEvaluationContext,
        list[dict[str, Any]],
    ]:
        stop_fraction = Decimal("0.02")
        reward_multiple = Decimal("2")
        invalidation = planned_entry * (_ONE - stop_fraction)
        target = planned_entry + (planned_entry - invalidation) * reward_multiple
        candidate = SignalCandidate(
            candidate_id=uuid7(),
            symbol=str(deployment["symbol"]),
            direction=Direction.LONG,
            setup_type=f"baseline_shadow:{deployment['strategy_type']}",
            strategy_version=str(deployment["strategy_version"]),
            as_of=signal_time,
            feature_snapshot_id=snapshot_id,
            catalyst_id="NOT_APPLICABLE_BASELINE",
            planned_entry=planned_entry,
            invalidation=invalidation,
            targets=(target,),
            expires_at=max(exit_time, entry_time + timedelta(microseconds=1)),
        )
        account = self._account_state(deployment, entry_time)
        risk_features = FeatureSnapshot(
            feature_snapshot_id=snapshot_id,
            as_of=signal_time,
            relative_volume=Decimal(
                str(snapshot_values.get("volume_ratio_20") or "0")
            ),
            vwap_confirmed=False,
            opening_range_confirmed=False,
            sector_compatible=False,
            quote_age_seconds=0,
        )
        context = RiskEvaluationContext(
            catalyst_required=False,
            catalyst_verified=False,
            restriction_status_known=True,
            liquidity_confirmed=known_liquidity_volume > 0,
            market_data_healthy=True,
            macro_calendar_status_known=False,
            duplicate_order_detected=False,
            evaluation_profile="baseline_shadow",
        )
        decision = evaluate_candidate(
            candidate=candidate,
            features=risk_features,
            account=account,
            mode=TradingMode.SHADOW,
            policy=self.risk_policy,
            restrictions=self.restrictions,
            context=context,
            evaluated_at=entry_time,
            new_exposure_paused=False,
        )
        plan = None
        if decision.verdict == Verdict.APPROVE:
            plan = TradePlan(
                trade_plan_id=uuid7(),
                candidate_id=candidate.candidate_id,
                risk_decision_id=decision.risk_decision_id,
                symbol=candidate.symbol,
                direction=candidate.direction,
                quantity=decision.max_quantity,
                limit_price=candidate.planned_entry,
                invalidation=candidate.invalidation,
                targets=candidate.targets,
                expires_at=candidate.expires_at,
            )
        common = {
            "shadow_deployment_id": deployment["shadow_deployment_id"],
            "shadow_run_id": run_id,
            "symbol": deployment["symbol"],
            "bar_id": decision_bar_id,
            "cash_balance": account.equity,
            "position_quantity": _ZERO,
            "price": planned_entry,
            "realized_pnl_delta": _ZERO,
            "created_at": datetime.now(UTC),
        }
        events = [
            {
                **common,
                "shadow_event_id": uuid7(),
                "sequence": 0,
                "event_type": "SIGNAL_CANDIDATE",
                "event_time": signal_time,
                "payload_json": {
                    **candidate.model_dump(mode="json"),
                    "virtual_only": True,
                },
            },
            {
                **common,
                "shadow_event_id": uuid7(),
                "sequence": 0,
                "event_type": "RISK_DECISION",
                "event_time": entry_time,
                "payload_json": {
                    **decision.model_dump(mode="json"),
                    "evaluation_context": context.model_dump(mode="json"),
                    "virtual_only": True,
                },
            },
        ]
        if plan is not None:
            events.append(
                {
                    **common,
                    "shadow_event_id": uuid7(),
                    "sequence": 0,
                    "event_type": "TRADE_PLAN",
                    "event_time": entry_time,
                    "payload_json": {
                        **plan.model_dump(mode="json"),
                        "virtual_only": True,
                    },
                }
            )
        return candidate, decision, plan, context, events

    def _account_state(
        self,
        deployment: dict[str, Any],
        evaluated_at: datetime,
    ) -> AccountState:
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(shadow_events.c.event_time, shadow_events.c.realized_pnl_delta)
                .where(shadow_events.c.shadow_deployment_id == deployment["shadow_deployment_id"])
            )
            day = evaluated_at.astimezone(UTC).date()
            daily_pnl = sum(
                (
                    Decimal(str(row.realized_pnl_delta))
                    for row in rows
                    if (_utc(row.event_time) or evaluated_at).date() == day
                ),
                _ZERO,
            )
            concurrent = connection.execute(
                select(
                    func.coalesce(func.sum(shadow_risk_decisions.c.risk_budget_usd), 0)
                )
                .select_from(shadow_trade_plans)
                .join(
                    shadow_risk_decisions,
                    shadow_risk_decisions.c.risk_decision_id
                    == shadow_trade_plans.c.risk_decision_id,
                )
                .where(shadow_trade_plans.c.status == "OPEN")
            ).scalar_one()
        return AccountState(
            equity=Decimal(str(deployment["cash_balance"])),
            daily_pnl=daily_pnl,
            concurrent_planned_risk=Decimal(str(concurrent)),
        )

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
        exit_volume: int | None = None,
        quantity_limit: int | None = None,
        lineage: dict[str, str | None] | None = None,
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
                quantity_limit=quantity_limit,
            )
            if entered:
                portfolio.exit_long(
                    exit_time=exit_time,
                    raw_price=close_price,
                    available_volume=exit_volume if exit_volume is not None else volume,
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
                        **(lineage or {}),
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
        return strategy_signal_action(
            strategy_type=str(deployment["strategy_type"]),
            parameters=dict(deployment.get("parameters_json") or {}),
            values=dict(values),
        )

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
