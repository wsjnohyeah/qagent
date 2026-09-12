from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import Engine, delete, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from agentic_quant.backtest_engine import EventDrivenPortfolio
from agentic_quant.config import TradingMode
from agentic_quant.control_plane import SystemObjectStore
from agentic_quant.data_quality import DataQualityError, MarketDataQualityService
from agentic_quant.database import (
    ledger_events,
    shadow_deployments,
    shadow_events,
    shadow_risk_decisions,
    shadow_runs,
    shadow_signal_candidates,
    shadow_trade_plans,
    strategy_adoptions,
    strategy_specs,
    validation_reports,
    workflow_jobs,
    runtime_leases,
    virtual_accounts,
)
from agentic_quant.domain import (
    AccountState,
    BacktestCostModel,
    CorporateActionType,
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
from agentic_quant.market_scanner import AUTO_TRADING_POOL_SLUG, MARKET_SCAN_EVENT
from agentic_quant.research import (
    PointInTimeFeatureBuilder,
    strategy_signal_action,
)
from agentic_quant.research_store import ResearchStore, _canonical_hash
from agentic_quant.reference_data import ReferenceDataStore
from agentic_quant.risk import (
    RestrictionRegistry,
    RiskPolicy,
    baseline_long_exit,
    baseline_long_geometry,
    deployable_long_exit,
    deployable_long_limit_fill,
    evaluate_candidate,
    normalize_deployable_long_prices,
    strategy_execution_profile,
    strategy_holding_period_sessions,
    strategy_signal_risk_policy,
)
from agentic_quant.virtual_account import (
    MAIN_VIRTUAL_ACCOUNT_ID,
    STRATEGY_SANDBOX_ACCOUNT_TYPE,
    STRATEGY_SANDBOX_INITIAL_EQUITY,
    VirtualAccountStore,
)
from agentic_quant.validation import (
    DEFAULT_PROMOTION_GATE_POLICY,
    PromotionGatePolicy,
    load_promotion_gate_policy,
    promotion_policy_sha256,
    validation_execution_contract,
)


_ZERO = Decimal("0")
_ONE = Decimal("1")
_TERMINAL_DEPLOYMENT_STATUSES = {
    "RETIRED",
    "RETIRED_LEGACY",
    "RETIRED_SHADOW_FAILED",
}


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
        now_provider: Callable[[], datetime] | None = None,
        promotion_policy: PromotionGatePolicy | None = None,
        promotion_policy_path: Path | None = None,
        new_exposure_not_before: datetime | None = None,
    ) -> None:
        self.engine = engine
        self.research_store = research_store
        self.objects = objects
        self.risk_policy = risk_policy
        self.restrictions = restrictions
        self.features = PointInTimeFeatureBuilder(research_store)
        self.reference_data = ReferenceDataStore(engine)
        self.session_clock = MarketSessionClock(calendar_name)
        self.data_quality = MarketDataQualityService(
            engine,
            calendar_name=calendar_name,
        )
        # Robinhood equity trading is commission-free. Keep spread, slippage,
        # impact, and liquidity constraints because they remain market costs.
        self.costs = BacktestCostModel(
            commission_per_share=_ZERO,
            minimum_commission_per_order=_ZERO,
        )
        self.accounts = VirtualAccountStore(engine)
        self._now_provider = now_provider or (lambda: datetime.now(UTC))
        self._promotion_policy = promotion_policy or DEFAULT_PROMOTION_GATE_POLICY
        self._promotion_policy_path = promotion_policy_path
        self.new_exposure_not_before = (
            new_exposure_not_before.astimezone(UTC)
            if new_exposure_not_before is not None
            else None
        )
        self._tick_lock = asyncio.Lock()

    def _now(self) -> datetime:
        value = self._now_provider()
        if value.tzinfo is None:
            raise ValueError("Shadow runtime clock must be timezone-aware")
        return value.astimezone(UTC)

    def initialize_virtual_account(
        self,
        *,
        initial_cash: Decimal = Decimal("100000"),
    ) -> dict[str, Any]:
        return self.accounts.ensure_main_account(
            initial_cash=initial_cash,
            risk_policy=self.risk_policy,
        )

    def virtual_account(self) -> dict[str, Any]:
        return self.accounts.account(MAIN_VIRTUAL_ACCOUNT_ID)

    def sandbox_summary(self) -> dict[str, Any]:
        deployments = self.deployments(limit=10_000)
        legacy = self.virtual_account()
        sandboxes = [
            item
            for item in deployments
            if item.get("account_mode") == STRATEGY_SANDBOX_ACCOUNT_TYPE
        ]
        active = [item for item in sandboxes if item["status"] == "ACTIVE"]
        liquidating = [
            item for item in sandboxes if item["status"] == "LIQUIDATION_PENDING"
        ]
        retired_failed = [
            item for item in sandboxes if item["status"] == "RETIRED_SHADOW_FAILED"
        ]
        legacy_nonterminal = [
            item
            for item in deployments
            if item.get("account_mode") != STRATEGY_SANDBOX_ACCOUNT_TYPE
            and item["status"] not in _TERMINAL_DEPLOYMENT_STATUSES
        ]
        return {
            **legacy,
            "account_mode": "ISOLATED_STRATEGY_SANDBOXES",
            "sandbox_initial_equity": str(STRATEGY_SANDBOX_INITIAL_EQUITY),
            "circuit_breaker_fraction": "0.12",
            "circuit_breaker_value": "8800.00",
            "active_sandboxes": len(active),
            "liquidation_pending_sandboxes": len(liquidating),
            "retired_failed_sandboxes": len(retired_failed),
            "total_sandboxes": len(sandboxes),
            "legacy_nonterminal_deployments": len(legacy_nonterminal),
            "total_sandbox_value": str(
                sum(
                    (Decimal(str(item["sandbox_total_value"])) for item in sandboxes),
                    _ZERO,
                )
            ),
            "open_position_count": sum(
                Decimal(str(item["position_quantity"])) != _ZERO
                for item in sandboxes
            ),
            "legacy_shared_account": legacy,
        }

    def effective_risk_policy(
        self,
        account_id: str = MAIN_VIRTUAL_ACCOUNT_ID,
    ) -> RiskPolicy:
        return self.accounts.effective_risk_policy(
            self.risk_policy,
            account_id=account_id,
        )

    def sandbox_risk_policy(self) -> RiskPolicy:
        return self.risk_policy

    def preview_account_risk(self, limits: dict[str, Any]) -> dict[str, Any]:
        return self.accounts.preview_risk_update(
            MAIN_VIRTUAL_ACCOUNT_ID,
            limits,
        )

    def update_account_risk(
        self,
        limits: dict[str, Any],
        *,
        reason: str,
        created_by: str,
    ) -> dict[str, Any]:
        return self.accounts.update_risk(
            MAIN_VIRTUAL_ACCOUNT_ID,
            limits,
            reason=reason,
            created_by=created_by,
        )

    def adoption_preview(
        self,
        *,
        strategy_spec_id: str,
        validation_report_id: str,
        admission_tier: str = "QUALIFIED",
        require_current_search_count: bool = True,
        require_current_universe_authority: bool = True,
    ) -> dict[str, Any]:
        normalized_tier = admission_tier.strip().upper()
        if normalized_tier not in {"CANDIDATE", "QUALIFIED"}:
            raise ValueError("Shadow admission tier must be CANDIDATE or QUALIFIED")
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
        tier_gate = (
            gate
            if normalized_tier == "QUALIFIED"
            else dict(gate.get("candidate_shadow") or {})
        )
        if tier_gate.get("eligible_for_human_review") is not True:
            raise ValueError(
                f"Strategy is not eligible for {normalized_tier.lower()} Shadow "
                "review under the deterministic gate"
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
        if str(spec.timeframe) != "1Day":
            raise ValueError(
                "Forward Shadow currently supports 1Day strategies only"
            )
        if dict(spec.data_requirements_json or {}).get("shadow_deployable") is not True:
            raise ValueError(
                "This strategy is a research benchmark and has no matching shadow "
                "execution contract"
            )
        strategy_spec = self.research_store.strategy_spec(strategy_spec_id)
        if strategy_spec is None:
            raise ValueError("Strategy specification not found")
        expected_contract = validation_execution_contract(
            validation_subject="static_strategy",
            validated_strategy_spec_ids={
                str(strategy_spec.strategy_type): strategy_spec_id
            },
            cost_model=self.costs,
            risk_policy=self.sandbox_risk_policy(),
            restriction_registry_version=self.restrictions.version,
            initial_equity=STRATEGY_SANDBOX_INITIAL_EQUITY,
            strategy_spec=strategy_spec,
        )
        validated_equity = dict(report.execution_contract_json or {}).get(
            "initial_equity"
        )
        if Decimal(str(validated_equity)) != STRATEGY_SANDBOX_INITIAL_EQUITY:
            raise ValueError(
                "Shadow validation must use the isolated $10,000 sandbox capital"
            )
        if dict(report.execution_contract_json or {}) != expected_contract:
            raise ValueError(
                "Validation execution contract does not match the current shadow runtime"
            )
        if str(report.execution_contract_sha256) != _canonical_hash(expected_contract):
            raise ValueError("Validation execution contract hash is invalid")
        current_policy = (
            load_promotion_gate_policy(self._promotion_policy_path)
            if self._promotion_policy_path is not None
            else self._promotion_policy
        )
        if gate.get("policy_sha256") != promotion_policy_sha256(current_policy):
            raise ValueError(
                "Validation admission assessment is stale under the current "
                "promotion policy"
            )
        current_trial_count = self.research_store.strategy_trial_count(
            symbol=str(report.symbol),
            timeframe=str(report.timeframe),
            holding_period_sessions=strategy_holding_period_sessions(
                strategy_spec.data_requirements
            ),
        )
        assessed_trial_count = int(
            dict(report.robustness_metrics or {}).get(
                "selection_search_trial_count",
                0,
            )
        )
        if require_current_search_count and assessed_trial_count != current_trial_count:
            raise ValueError(
                "Validation admission assessment is stale under the current "
                "research search count"
            )
        universe_admission = (
            self._universe_admission(
                symbol=str(report.symbol),
                validation_report_id=validation_report_id,
            )
            if require_current_universe_authority
            else None
        )
        return {
            "summary": (
                f"Adopt {spec.name}@{spec.version} for "
                f"{normalized_tier.lower()} Shadow use"
            ),
            "strategy_spec_id": strategy_spec_id,
            "validation_report_id": validation_report_id,
            "admission_tier": normalized_tier,
            "gate_assessment": gate,
            "symbol": report.symbol,
            "timeframe": report.timeframe,
            "universe_admission": universe_admission,
            "live_broker_effect": False,
        }

    def _universe_admission(
        self,
        *,
        symbol: str,
        validation_report_id: str,
    ) -> dict[str, Any]:
        normalized_symbol = symbol.upper()
        manual = self.objects.get_list("trading-universe")
        if manual is not None and normalized_symbol in {
            str(value).upper() for value in manual["members"]
        }:
            return {
                "authority": "MANUAL_TRADING_UNIVERSE",
                "list_slug": "trading-universe",
                "list_revision": int(manual["current_revision"]),
            }
        with self.engine.connect() as connection:
            jobs = connection.execute(
                select(
                    workflow_jobs.c.payload_json,
                    workflow_jobs.c.result_json,
                )
                .where(workflow_jobs.c.job_type == "coordinator.validate_strategy")
                .where(workflow_jobs.c.status == "COMPLETED")
                .order_by(workflow_jobs.c.updated_at.desc())
            ).all()
        scan_id = next(
            (
                str(dict(row.payload_json).get("universe_scan_id"))
                for row in jobs
                if str(dict(row.result_json or {}).get("validation_report_id"))
                == validation_report_id
                and str(dict(row.payload_json).get("symbol", "")).upper()
                == normalized_symbol
                and dict(row.payload_json).get("universe_scan_id")
            ),
            None,
        )
        if scan_id is None:
            raise ValueError(
                "Shadow symbol is outside the manual universe and the validation "
                "has no scanner-pool lineage"
            )
        with self.engine.connect() as connection:
            event = connection.execute(
                select(ledger_events.c.payload)
                .where(
                    ledger_events.c.event_type == MARKET_SCAN_EVENT,
                    ledger_events.c.correlation_id == scan_id,
                )
                .order_by(ledger_events.c.sequence.desc())
                .limit(1)
            ).one_or_none()
        if event is None:
            raise ValueError("Scanner-pool admission event is missing")
        admission = dict(dict(event.payload).get("trading_pool_admission") or {})
        pool = self.objects.get_list(AUTO_TRADING_POOL_SLUG)
        if (
            admission.get("status") not in {
                "UPDATED",
                "UNCHANGED",
                "HELD_PREVIOUS_LLM_REVIEW",
            }
            or pool is None
            or int(pool["current_revision"])
            != int(admission.get("list_revision", -1))
            or normalized_symbol
            not in {str(value).upper() for value in admission.get("admitted_symbols", [])}
            or normalized_symbol
            not in {str(value).upper() for value in pool["members"]}
        ):
            raise ValueError(
                "Validation scanner-pool admission is no longer current"
            )
        return {
            "authority": "SCANNER_LLM_TRADING_POOL",
            "list_slug": AUTO_TRADING_POOL_SLUG,
            "list_revision": int(pool["current_revision"]),
            "scan_id": scan_id,
            "basis_scan_id": admission.get("basis_scan_id"),
            "basis_llm_invocation_id": admission.get("basis_llm_invocation_id"),
        }

    def adopt_strategy(
        self,
        *,
        strategy_spec_id: str,
        validation_report_id: str,
        reason: str,
        approved_by: str,
        admission_tier: str = "QUALIFIED",
        author_kind: str = "admin",
        allow_operator_override: bool = True,
    ) -> dict[str, Any]:
        if author_kind not in {"admin", "system"}:
            raise ValueError("Strategy adoption author kind is invalid")
        preview = self.adoption_preview(
            strategy_spec_id=strategy_spec_id,
            validation_report_id=validation_report_id,
            admission_tier=admission_tier,
        )
        normalized_tier = str(preview["admission_tier"])
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
            if (
                existing is not None
                and existing.status == "RETIRED"
                and str(existing.reason).startswith("SANDBOX_CIRCUIT_BREAKER:")
            ):
                raise ValueError(
                    "A strategy retired by its Shadow sandbox circuit breaker "
                    "cannot be re-adopted; generate and validate a new strategy version"
                )
            if (
                existing is not None
                and existing.status in {"PAUSED", "RETIRED"}
                and not allow_operator_override
            ):
                raise ValueError(
                    "Automatic Shadow admission cannot override an operator hold"
                )
            values = {
                "status": "ADOPTED_FOR_SHADOW",
                "admission_tier": normalized_tier,
                "validation_report_id": validation_report_id,
                "reason": reason,
                "approved_by": approved_by,
                "updated_at": now,
            }
            changed = existing is None or any(
                (
                    str(existing.status) != values["status"],
                    str(existing.admission_tier) != values["admission_tier"],
                    str(existing.validation_report_id)
                    != values["validation_report_id"],
                    str(existing.reason) != values["reason"],
                    str(existing.approved_by) != values["approved_by"],
                )
            )
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
            elif changed:
                adoption_id = str(existing.adoption_id)
                connection.execute(
                    update(strategy_adoptions)
                    .where(strategy_adoptions.c.adoption_id == adoption_id)
                    .values(**values)
                )
            else:
                adoption_id = str(existing.adoption_id)
        if changed:
            self.objects.post(
                object_type="strategy",
                object_id=strategy_spec_id,
                title=str(spec.name),
                author_kind=author_kind,
                author_name=approved_by,
                body=f"Adopted for shadow operation. {reason}",
                metadata={
                    "validation_report_id": validation_report_id,
                    "admission_tier": normalized_tier,
                },
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
            deployment_ids = tuple(
                str(value)
                for value in connection.execute(
                    select(shadow_deployments.c.shadow_deployment_id).where(
                        shadow_deployments.c.strategy_spec_id == strategy_spec_id
                    )
                ).scalars()
            )
            if deployment_ids:
                open_positions = connection.execute(
                    select(func.count())
                    .select_from(shadow_trade_plans)
                    .join(
                        shadow_signal_candidates,
                        shadow_signal_candidates.c.candidate_id
                        == shadow_trade_plans.c.candidate_id,
                    )
                    .where(
                        shadow_signal_candidates.c.shadow_deployment_id.in_(
                            deployment_ids
                        )
                        & (shadow_trade_plans.c.status == "POSITION_OPEN")
                    )
                ).scalar_one()
                if int(open_positions) > 0:
                    raise ValueError(
                        "Cannot pause or retire a strategy while a Shadow position "
                        "is open; pause new exposure globally and let deterministic "
                        "exits continue"
                    )
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
                .where(
                    (shadow_deployments.c.strategy_spec_id == strategy_spec_id)
                    & shadow_deployments.c.status.not_in(
                        tuple(_TERMINAL_DEPLOYMENT_STATUSES)
                    )
                )
                .values(status=status, updated_at=now)
            )
            self._cancel_open_plans(
                connection,
                deployment_ids=deployment_ids,
                closed_at=now,
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
        allow_operator_resume: bool = True,
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
        now = self._now()
        existing_bars = self.research_store.load_bars(
            symbol=normalized_symbol,
            timeframe=str(timeframe),
            as_of_end=now,
        )
        initial_cursor = (
            existing_bars[-1].event_time if existing_bars else None
        )
        preopen_bar = None
        next_session_open = None
        if str(timeframe) == "1Day" and len(existing_bars) >= 2:
            latest_bar = existing_bars[-1]
            candidate_open = self.session_clock.next_daily_session_open(
                latest_bar.event_time
            )
            if latest_bar.available_from <= now < candidate_open:
                initial_cursor = existing_bars[-2].event_time
                preopen_bar = latest_bar
                next_session_open = candidate_open
        with self.engine.begin() as connection:
            adoption = connection.execute(
                select(strategy_adoptions).where(
                    strategy_adoptions.c.strategy_spec_id == strategy_spec_id
                )
            ).one_or_none()
            if adoption is None or adoption.status != "ADOPTED_FOR_SHADOW":
                raise ValueError("Strategy must be adopted before shadow deployment")
            existing = connection.execute(
                select(shadow_deployments)
                .where(
                    (shadow_deployments.c.strategy_spec_id == strategy_spec_id)
                    & (shadow_deployments.c.symbol == normalized_symbol)
                    & shadow_deployments.c.status.not_in(
                        tuple(_TERMINAL_DEPLOYMENT_STATUSES)
                    )
                )
                .order_by(shadow_deployments.c.created_at.desc())
                .limit(1)
            ).one_or_none()
            if existing is None:
                deployment_id = uuid7()
                account_id = self.accounts.ensure_strategy_sandbox(
                    deployment_id=deployment_id,
                    strategy_spec_id=strategy_spec_id,
                    symbol=normalized_symbol,
                    initial_cash=initial_cash,
                    risk_policy=self.sandbox_risk_policy(),
                    connection=connection,
                )
                sleeve_id = self.accounts.ensure_sleeve(
                    strategy_spec_id=strategy_spec_id,
                    symbol=normalized_symbol,
                    account_id=account_id,
                    connection=connection,
                )
                connection.execute(
                    insert(shadow_deployments).values(
                        shadow_deployment_id=deployment_id,
                        virtual_account_id=account_id,
                        strategy_sleeve_id=sleeve_id,
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
                        liquidation_requested_at=None,
                        retired_reason=None,
                        last_processed_bar_time=initial_cursor,
                        created_at=now,
                        updated_at=now,
                    )
                )
                prime_preopen = preopen_bar is not None
            else:
                if existing.status == "LIQUIDATION_PENDING":
                    raise ValueError(
                        "Prior Shadow deployment is awaiting deterministic liquidation"
                    )
                if existing.status == "PAUSED" and not allow_operator_resume:
                    raise ValueError(
                        "Automatic Shadow admission cannot override an operator hold"
                    )
                deployment_id = str(existing.shadow_deployment_id)
                sleeve_id = self.accounts.ensure_sleeve(
                    strategy_spec_id=strategy_spec_id,
                    symbol=normalized_symbol,
                    account_id=str(existing.virtual_account_id),
                    connection=connection,
                )
                marker_exists = False
                if preopen_bar is not None:
                    marker_exists = bool(
                        connection.execute(
                            select(func.count())
                            .select_from(shadow_events)
                            .where(
                                (
                                    shadow_events.c.shadow_deployment_id
                                    == deployment_id
                                )
                                & (
                                    shadow_events.c.bar_id
                                    == preopen_bar.bar_id
                                )
                                & (
                                    shadow_events.c.event_type
                                    == "PREOPEN_EVALUATION_ARMED"
                                )
                            )
                        ).scalar_one()
                    )
                existing_created_at = _utc(existing.created_at)
                prime_preopen = bool(
                    preopen_bar is not None
                    and not marker_exists
                    and _utc(existing.last_processed_bar_time)
                    == preopen_bar.event_time
                    and existing_created_at is not None
                    and existing_created_at >= preopen_bar.available_from
                    and Decimal(str(existing.position_quantity)) == _ZERO
                )
                connection.execute(
                    update(shadow_deployments)
                    .where(
                        shadow_deployments.c.shadow_deployment_id == deployment_id
                    )
                    .values(
                        status="ACTIVE",
                        strategy_sleeve_id=sleeve_id,
                        liquidation_requested_at=None,
                        retired_reason=None,
                        last_processed_bar_time=(
                            initial_cursor
                            if prime_preopen
                            else existing.last_processed_bar_time
                        ),
                        updated_at=now,
                    )
                )
            if prime_preopen:
                assert preopen_bar is not None
                assert next_session_open is not None
                sequence = int(
                    connection.execute(
                        select(func.max(shadow_events.c.sequence)).where(
                            shadow_events.c.shadow_deployment_id == deployment_id
                        )
                    ).scalar_one_or_none()
                    or 0
                ) + 1
                connection.execute(
                    insert(shadow_events).values(
                        shadow_event_id=uuid7(),
                        shadow_deployment_id=deployment_id,
                        shadow_run_id=None,
                        sequence=sequence,
                        event_type="PREOPEN_EVALUATION_ARMED",
                        event_time=now,
                        symbol=normalized_symbol,
                        bar_id=preopen_bar.bar_id,
                        cash_balance=(
                            initial_cash if existing is None else existing.cash_balance
                        ),
                        position_quantity=(
                            _ZERO if existing is None else existing.position_quantity
                        ),
                        price=preopen_bar.close,
                        realized_pnl_delta=_ZERO,
                        payload_json={
                            "decision_bar_available_from": (
                                preopen_bar.available_from.isoformat()
                            ),
                            "next_session_open": next_session_open.isoformat(),
                            "requested_by": requested_by,
                            "virtual_only": True,
                        },
                        created_at=now,
                    )
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
        if initial_cash != STRATEGY_SANDBOX_INITIAL_EQUITY:
            raise ValueError("Strategy Shadow sandboxes require exactly $10,000")
        normalized_symbol = symbol.strip().upper()
        if not normalized_symbol or len(normalized_symbol) > 24:
            raise ValueError("Shadow symbol is invalid")
        restricted = self.objects.get_list("restricted")
        if restricted is not None and normalized_symbol in restricted["members"]:
            raise ValueError("Restricted symbols cannot enter shadow deployment")
        with self.engine.connect() as connection:
            row = connection.execute(
                select(
                    strategy_adoptions.c.status,
                    strategy_adoptions.c.admission_tier,
                    strategy_adoptions.c.validation_report_id,
                    strategy_specs.c.name,
                    strategy_specs.c.version,
                    validation_reports.c.symbol.label("validated_symbol"),
                    validation_reports.c.execution_contract_json,
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
        adoption_preview = self.adoption_preview(
            strategy_spec_id=strategy_spec_id,
            validation_report_id=str(row.validation_report_id),
            admission_tier=str(row.admission_tier),
            require_current_search_count=False,
        )
        if str(row.validated_symbol) != normalized_symbol:
            raise ValueError("Shadow symbol does not match the reviewed validation report")
        validated_capital = dict(row.execution_contract_json or {}).get(
            "initial_equity"
        )
        if validated_capital is None or Decimal(str(validated_capital)) != initial_cash:
            raise ValueError(
                "Shadow initial cash must match the validated capital assumption"
            )
        return {
            "summary": f"Start {row.name}@{row.version} on {normalized_symbol}",
            "strategy_spec_id": strategy_spec_id,
            "admission_tier": str(row.admission_tier),
            "symbol": normalized_symbol,
            "initial_cash": str(initial_cash),
            "universe_admission": adoption_preview["universe_admission"],
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
            if row.status in _TERMINAL_DEPLOYMENT_STATUSES:
                raise ValueError("Retired shadow deployments cannot be changed")
            if status != "ACTIVE":
                position_open = connection.execute(
                    select(func.count())
                    .select_from(shadow_trade_plans)
                    .join(
                        shadow_signal_candidates,
                        shadow_signal_candidates.c.candidate_id
                        == shadow_trade_plans.c.candidate_id,
                    )
                    .where(
                        (
                            shadow_signal_candidates.c.shadow_deployment_id
                            == deployment_id
                        )
                        & (shadow_trade_plans.c.status == "POSITION_OPEN")
                    )
                ).scalar_one()
                if int(position_open) > 0:
                    raise ValueError(
                        "Cannot pause or retire a Shadow deployment while its position "
                        "is open; use the global new-exposure pause so exits keep running"
                    )
            if status == "ACTIVE":
                adoption_row = connection.execute(
                    select(
                        strategy_adoptions.c.status,
                        strategy_adoptions.c.admission_tier,
                        strategy_adoptions.c.validation_report_id,
                    ).where(
                        strategy_adoptions.c.strategy_spec_id == row.strategy_spec_id
                    )
                ).one_or_none()
                if adoption_row is None or adoption_row.status != "ADOPTED_FOR_SHADOW":
                    raise ValueError("Strategy adoption is not active")
                self.adoption_preview(
                    strategy_spec_id=str(row.strategy_spec_id),
                    validation_report_id=str(adoption_row.validation_report_id),
                    admission_tier=str(adoption_row.admission_tier),
                    require_current_search_count=False,
                )
            connection.execute(
                update(shadow_deployments)
                .where(shadow_deployments.c.shadow_deployment_id == deployment_id)
                .values(status=status, updated_at=datetime.now(UTC))
            )
            if status == "RETIRED":
                connection.execute(
                    update(strategy_adoptions)
                    .where(
                        strategy_adoptions.c.strategy_spec_id
                        == row.strategy_spec_id
                    )
                    .values(
                        status="RETIRED",
                        reason=f"OPERATOR_DEPLOYMENT_RETIREMENT: {reason}",
                        approved_by=requested_by,
                        updated_at=datetime.now(UTC),
                    )
                )
            if status != "ACTIVE":
                self._cancel_open_plans(
                    connection,
                    deployment_ids=(deployment_id,),
                    closed_at=datetime.now(UTC),
                )
        self._sync_active_symbols(
            str(row.symbol),
            add=status == "ACTIVE",
            reason=f"Shadow deployment {status.lower()}: {reason} ({requested_by})",
        )
        return self.deployment(deployment_id)

    def sandbox_migration_preview(self) -> dict[str, Any]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(
                    shadow_deployments.c.shadow_deployment_id,
                    shadow_deployments.c.symbol,
                    shadow_deployments.c.status,
                    shadow_deployments.c.position_quantity,
                )
                .join(
                    virtual_accounts,
                    virtual_accounts.c.virtual_account_id
                    == shadow_deployments.c.virtual_account_id,
                )
                .where(
                    (virtual_accounts.c.account_type != STRATEGY_SANDBOX_ACCOUNT_TYPE)
                    & shadow_deployments.c.status.not_in(
                        tuple(_TERMINAL_DEPLOYMENT_STATUSES)
                    )
                )
            ).all()
        return {
            "summary": "Stop legacy shared-account Shadow and move to isolated sandboxes",
            "legacy_deployment_count": len(rows),
            "open_position_count": sum(
                Decimal(str(row.position_quantity)) != _ZERO for row in rows
            ),
            "flat_retire_count": sum(
                Decimal(str(row.position_quantity)) == _ZERO for row in rows
            ),
            "symbols": sorted({str(row.symbol) for row in rows}),
            "new_sandbox_initial_equity": str(STRATEGY_SANDBOX_INITIAL_EQUITY),
            "live_broker_effect": False,
            "paper_broker_effect": False,
        }

    def begin_sandbox_migration(
        self,
        *,
        reason: str,
        requested_by: str,
    ) -> dict[str, Any]:
        """Stop legacy entries and retire them after deterministic flattening."""
        token = self._acquire_runtime_lease(owner=f"sandbox-migration:{uuid7()}")
        if token is None:
            raise ValueError(
                "Shadow runtime is processing; retry the sandbox migration"
            )
        try:
            return self._begin_sandbox_migration_locked(
                reason=reason,
                requested_by=requested_by,
                lease_token=token,
            )
        finally:
            self._release_runtime_lease(token)

    def _begin_sandbox_migration_locked(
        self,
        *,
        reason: str,
        requested_by: str,
        lease_token: str,
    ) -> dict[str, Any]:
        preview = self.sandbox_migration_preview()
        now = self._now()
        with self.engine.begin() as connection:
            self._assert_runtime_lease(connection, lease_token)
            rows = connection.execute(
                select(shadow_deployments)
                .join(
                    virtual_accounts,
                    virtual_accounts.c.virtual_account_id
                    == shadow_deployments.c.virtual_account_id,
                )
                .where(
                    (virtual_accounts.c.account_type != STRATEGY_SANDBOX_ACCOUNT_TYPE)
                    & shadow_deployments.c.status.not_in(
                        tuple(_TERMINAL_DEPLOYMENT_STATUSES)
                    )
                )
            ).all()
            deployment_ids = tuple(str(row.shadow_deployment_id) for row in rows)
            self._cancel_open_plans(
                connection,
                deployment_ids=deployment_ids,
                closed_at=now,
            )
            liquidating = 0
            retired = 0
            symbols: set[str] = set()
            for row in rows:
                deployment_id = str(row.shadow_deployment_id)
                symbols.add(str(row.symbol))
                position_open = bool(
                    connection.execute(
                        select(func.count())
                        .select_from(shadow_trade_plans)
                        .join(
                            shadow_signal_candidates,
                            shadow_signal_candidates.c.candidate_id
                            == shadow_trade_plans.c.candidate_id,
                        )
                        .where(
                            (
                                shadow_signal_candidates.c.shadow_deployment_id
                                == deployment_id
                            )
                            & (shadow_trade_plans.c.status == "POSITION_OPEN")
                        )
                    ).scalar_one()
                )
                status = "LIQUIDATION_PENDING" if position_open else "RETIRED_LEGACY"
                liquidating += int(position_open)
                retired += int(not position_open)
                connection.execute(
                    update(shadow_deployments)
                    .where(
                        shadow_deployments.c.shadow_deployment_id == deployment_id
                    )
                    .values(
                        status=status,
                        liquidation_requested_at=now if position_open else None,
                        retired_reason="SANDBOX_MIGRATION",
                        updated_at=now,
                    )
                )
                sequence = int(
                    connection.execute(
                        select(func.max(shadow_events.c.sequence)).where(
                            shadow_events.c.shadow_deployment_id == deployment_id
                        )
                    ).scalar_one_or_none()
                    or 0
                ) + 1
                connection.execute(
                    insert(shadow_events).values(
                        shadow_event_id=uuid7(),
                        shadow_deployment_id=deployment_id,
                        shadow_run_id=None,
                        sequence=sequence,
                        event_type=(
                            "LEGACY_LIQUIDATION_REQUESTED"
                            if position_open
                            else "LEGACY_SANDBOX_RETIRED"
                        ),
                        event_time=now,
                        symbol=row.symbol,
                        bar_id=None,
                        cash_balance=row.cash_balance,
                        position_quantity=row.position_quantity,
                        price=row.last_price,
                        realized_pnl_delta=_ZERO,
                        payload_json={
                            "reason": reason,
                            "requested_by": requested_by,
                            "next_account_model": STRATEGY_SANDBOX_ACCOUNT_TYPE,
                            "virtual_only": True,
                        },
                        created_at=now,
                    )
                )
        for symbol in symbols:
            self._sync_active_symbols(
                symbol,
                add=False,
                reason=f"Legacy Shadow migration requested by {requested_by}",
            )
        return {
            **preview,
            "legacy_liquidation_pending": liquidating,
            "legacy_retired": retired,
            "requested_at": now,
        }

    def _cancel_open_plans(
        self,
        connection: Any,
        *,
        deployment_ids: tuple[str, ...],
        closed_at: datetime,
    ) -> None:
        if not deployment_ids:
            return
        plans = connection.execute(
            select(
                shadow_trade_plans.c.trade_plan_id,
                shadow_trade_plans.c.reserved_cash,
                shadow_trade_plans.c.reserved_risk_usd,
                shadow_deployments.c.virtual_account_id,
            )
            .select_from(shadow_trade_plans)
            .join(
                shadow_signal_candidates,
                shadow_signal_candidates.c.candidate_id
                == shadow_trade_plans.c.candidate_id,
            )
            .join(
                shadow_deployments,
                shadow_deployments.c.shadow_deployment_id
                == shadow_signal_candidates.c.shadow_deployment_id,
            )
            .where(
                shadow_trade_plans.c.status.in_(("OPEN", "PENDING_ACTIVATION"))
                & shadow_signal_candidates.c.shadow_deployment_id.in_(deployment_ids)
            )
        ).all()
        for plan in plans:
            changed = connection.execute(
                update(shadow_trade_plans)
                .where(
                    (shadow_trade_plans.c.trade_plan_id == plan.trade_plan_id)
                    & shadow_trade_plans.c.status.in_(
                        ("OPEN", "PENDING_ACTIVATION")
                    )
                )
                .values(status="CANCELLED", closed_at=closed_at)
            )
            if int(changed.rowcount or 0) == 1:
                self.accounts.release_or_settle(
                    account_id=str(plan.virtual_account_id),
                    reserved_cash=Decimal(str(plan.reserved_cash)),
                    reserved_risk_usd=Decimal(str(plan.reserved_risk_usd)),
                    realized_pnl_delta=_ZERO,
                    connection=connection,
                )

    def deployments(self, *, limit: int = 200) -> list[dict[str, Any]]:
        statement = (
            select(
                shadow_deployments,
                strategy_specs.c.name.label("strategy_name"),
                strategy_specs.c.version.label("strategy_version"),
                strategy_specs.c.strategy_type,
                strategy_specs.c.timeframe,
                strategy_specs.c.parameters_json,
                strategy_specs.c.data_requirements_json,
                strategy_adoptions.c.validation_report_id,
                strategy_adoptions.c.admission_tier,
                validation_reports.c.execution_contract_sha256,
                validation_reports.c.execution_contract_json,
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
            values = [dict(row._mapping) for row in connection.execute(statement)]
            account_ids = {
                str(value["virtual_account_id"]) for value in values
            }
            accounts = {
                str(row.virtual_account_id): dict(row._mapping)
                for row in connection.execute(
                    select(virtual_accounts).where(
                        virtual_accounts.c.virtual_account_id.in_(account_ids)
                    )
                )
            }
            unrealized = {
                str(row.virtual_account_id): Decimal(str(row.total))
                for row in connection.execute(
                    select(
                        shadow_deployments.c.virtual_account_id,
                        func.coalesce(
                            func.sum(shadow_deployments.c.unrealized_pnl), 0
                        ).label("total"),
                    )
                    .where(shadow_deployments.c.virtual_account_id.in_(account_ids))
                    .group_by(shadow_deployments.c.virtual_account_id)
                )
            }
        for value in values:
            account_id = str(value["virtual_account_id"])
            account = accounts[account_id]
            account_unrealized = unrealized.get(account_id, _ZERO)
            account_equity = Decimal(str(account["cash_balance"])) + account_unrealized
            value["account_mode"] = str(account["account_type"])
            value["sleeve_cash_balance"] = value["cash_balance"]
            value["cash_balance"] = account["cash_balance"]
            value["sleeve_realized_pnl"] = value["realized_pnl"]
            value["account_cash_balance"] = account["cash_balance"]
            value["account_realized_pnl"] = account["realized_pnl"]
            value["account_unrealized_pnl"] = account_unrealized
            value["sandbox_total_value"] = account_equity
            value["sandbox_floor_value"] = account["account_floor_usd"]
            value["account_reserved_cash"] = account["reserved_cash"]
            value["account_reserved_risk_usd"] = account["reserved_risk_usd"]
            try:
                self.adoption_preview(
                    strategy_spec_id=str(value["strategy_spec_id"]),
                    validation_report_id=str(value["validation_report_id"]),
                    admission_tier=str(value["admission_tier"]),
                    require_current_search_count=False,
                    require_current_universe_authority=False,
                )
            except ValueError as exc:
                value["contract_status"] = "REVALIDATION_REQUIRED"
                value["contract_message"] = str(exc)
            else:
                value["contract_status"] = "CURRENT"
                value["contract_message"] = "Approved execution contract is current"
        return values

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
                shadow_trade_plans.c.created_at.label("plan_persisted_at"),
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
            values = [dict(row._mapping) for row in connection.execute(statement)]
        for value in values:
            for field in (
                "as_of",
                "expires_at",
                "created_at",
                "evaluated_at",
                "plan_persisted_at",
                "closed_at",
            ):
                value[field] = _utc(value.get(field))
        return values

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
            elif row.event_type == "VIRTUAL_FILL_2":
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
                .where(shadow_runs.c.status.in_(("FAILED", "DEGRADED")))
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
                "kind": (
                    "SHADOW_RUN_FAILED"
                    if row.status == "FAILED"
                    else "SHADOW_RUN_DEGRADED"
                ),
                "severity": "ERROR" if row.status == "FAILED" else "WARNING",
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
        if new_exposure_paused and not self.has_open_positions():
            raise ValueError("Global new-exposure pause blocks shadow processing")
        async with self._tick_lock:
            owner = f"{trigger}:{uuid7()}"
            token = self._acquire_runtime_lease(owner=owner)
            if token is None:
                raise ValueError("Another shadow tick owns the portfolio execution lease")
            work = asyncio.create_task(
                asyncio.to_thread(
                    self._tick_locked,
                    trigger=trigger,
                    lease_token=token,
                    new_exposure_paused=new_exposure_paused,
                ),
                name="shadow-tick-work",
            )
            heartbeat = asyncio.create_task(
                self._runtime_lease_heartbeat(token),
                name="shadow-tick-lease-heartbeat",
            )
            try:
                done, _ = await asyncio.wait(
                    (work, heartbeat),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if heartbeat in done:
                    try:
                        await heartbeat
                    finally:
                        await work
                    raise RuntimeError("Shadow execution lease heartbeat stopped")
                return await work
            finally:
                heartbeat.cancel()
                try:
                    await heartbeat
                except asyncio.CancelledError:
                    pass
                self._release_runtime_lease(token)

    def _tick_locked(
        self,
        *,
        trigger: str,
        lease_token: str,
        new_exposure_paused: bool,
    ) -> dict[str, Any]:
        started_at = datetime.now(UTC)
        run_id = uuid7()
        deployments = [
            item
            for item in self.deployments(limit=10_000)
            if item["status"] in {"ACTIVE", "LIQUIDATION_PENDING"}
        ]
        bars_processed = 0
        events_created = 0
        status = "SUCCEEDED"
        error_code: str | None = None
        failures: list[Exception] = []
        for deployment in deployments:
            try:
                processed, created = self._process_deployment(
                    deployment,
                    run_id,
                    lease_token,
                    new_exposure_paused=new_exposure_paused,
                )
                bars_processed += processed
                events_created += created
            except Exception as exc:
                failures.append(exc)
                if self._record_deployment_processing_failure(
                    deployment=deployment,
                    run_id=run_id,
                    lease_token=lease_token,
                    error=exc,
                ):
                    events_created += 1
        if failures:
            status = "FAILED" if len(failures) == len(deployments) else "DEGRADED"
            error_code = ",".join(
                sorted({type(failure).__name__ for failure in failures})
            )[:120]
        finished_at = datetime.now(UTC)
        if trigger != "scheduler" or bars_processed > 0 or status != "SUCCEEDED":
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
        if status == "FAILED":
            raise failures[0]
        return {
            "shadow_run_id": run_id,
            "status": status,
            "deployment_count": len(deployments),
            "deployment_failures": len(failures),
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

    def has_open_positions(self) -> bool:
        with self.engine.connect() as connection:
            count = connection.execute(
                select(func.count())
                .select_from(shadow_trade_plans)
                .where(shadow_trade_plans.c.status == "POSITION_OPEN")
            ).scalar_one()
        return int(count) > 0

    def _enforce_sandbox_circuit_breaker(
        self,
        *,
        deployment_id: str,
        run_id: str,
        lease_token: str,
    ) -> int:
        deployment = self.deployment(deployment_id)
        if deployment.get("account_mode") != STRATEGY_SANDBOX_ACCOUNT_TYPE:
            return 0
        if deployment["status"] in _TERMINAL_DEPLOYMENT_STATUSES:
            return 0
        total_value = Decimal(str(deployment["sandbox_total_value"]))
        floor = Decimal(str(deployment["sandbox_floor_value"]))
        if total_value > floor:
            return 0
        now = self._now()
        position_open = self._position_is_open(deployment_id)
        next_status = (
            "LIQUIDATION_PENDING" if position_open else "RETIRED_SHADOW_FAILED"
        )
        with self.engine.begin() as connection:
            self._assert_runtime_lease(connection, lease_token)
            current = connection.execute(
                select(shadow_deployments.c.status).where(
                    shadow_deployments.c.shadow_deployment_id == deployment_id
                )
            ).scalar_one()
            if str(current) in _TERMINAL_DEPLOYMENT_STATUSES or str(current) == (
                "LIQUIDATION_PENDING"
            ):
                return 0
            connection.execute(
                update(shadow_deployments)
                .where(shadow_deployments.c.shadow_deployment_id == deployment_id)
                .values(
                    status=next_status,
                    liquidation_requested_at=now if position_open else None,
                    retired_reason="SANDBOX_CIRCUIT_BREAKER",
                    updated_at=now,
                )
            )
            connection.execute(
                update(strategy_adoptions)
                .where(
                    strategy_adoptions.c.strategy_spec_id
                    == deployment["strategy_spec_id"]
                )
                .values(
                    status="RETIRED",
                    reason=(
                        "SANDBOX_CIRCUIT_BREAKER: isolated sandbox total value "
                        f"reached {total_value} at or below {floor}"
                    ),
                    approved_by="shadow-runtime",
                    updated_at=now,
                )
            )
            if not position_open:
                connection.execute(
                    update(virtual_accounts)
                    .where(
                        virtual_accounts.c.virtual_account_id
                        == deployment["virtual_account_id"]
                    )
                    .values(status="FAILED", updated_at=now)
                )
            sequence = int(
                connection.execute(
                    select(func.max(shadow_events.c.sequence)).where(
                        shadow_events.c.shadow_deployment_id == deployment_id
                    )
                ).scalar_one_or_none()
                or 0
            ) + 1
            connection.execute(
                insert(shadow_events).values(
                    shadow_event_id=uuid7(),
                    shadow_deployment_id=deployment_id,
                    shadow_run_id=run_id,
                    sequence=sequence,
                    event_type=(
                        "SANDBOX_CIRCUIT_LIQUIDATION_REQUESTED"
                        if position_open
                        else "SANDBOX_CIRCUIT_RETIRED"
                    ),
                    event_time=now,
                    symbol=deployment["symbol"],
                    bar_id=None,
                    cash_balance=deployment["sleeve_cash_balance"],
                    position_quantity=deployment["position_quantity"],
                    price=deployment["last_price"],
                    realized_pnl_delta=_ZERO,
                    payload_json={
                        "sandbox_total_value": str(total_value),
                        "sandbox_floor_value": str(floor),
                        "loss_fraction_from_initial": str(
                            (
                                STRATEGY_SANDBOX_INITIAL_EQUITY - total_value
                            )
                            / STRATEGY_SANDBOX_INITIAL_EQUITY
                        ),
                        "virtual_only": True,
                    },
                    created_at=now,
                )
            )
        if not position_open:
            self._sync_active_symbols(
                str(deployment["symbol"]),
                add=False,
                reason="Strategy sandbox failed its permanent 12% equity floor",
            )
        return 1

    def _record_deployment_processing_failure(
        self,
        *,
        deployment: dict[str, Any],
        run_id: str,
        lease_token: str,
        error: Exception,
    ) -> bool:
        """Persist one isolated deployment fault without blocking healthy peers."""
        error_type = type(error).__name__
        error_detail = str(error)[:500]
        event = self._operational_event(
            deployment=deployment,
            run_id=run_id,
            bar_id=None,
            event_type="DEPLOYMENT_PROCESSING_FAILED",
            event_time=self._now(),
            payload={
                "error_type": error_type,
                "error_detail": error_detail,
                "virtual_only": True,
            },
        )
        event["sequence"] = self._next_event_sequence(
            str(deployment["shadow_deployment_id"])
        )
        try:
            with self.engine.begin() as connection:
                self._assert_runtime_lease(connection, lease_token)
                latest = connection.execute(
                    select(
                        shadow_events.c.event_type,
                        shadow_events.c.payload_json,
                    )
                    .where(
                        shadow_events.c.shadow_deployment_id
                        == deployment["shadow_deployment_id"]
                    )
                    .order_by(shadow_events.c.sequence.desc())
                    .limit(1)
                ).one_or_none()
                if (
                    latest is not None
                    and latest.event_type == "DEPLOYMENT_PROCESSING_FAILED"
                    and dict(latest.payload_json or {}).get("error_type") == error_type
                    and dict(latest.payload_json or {}).get("error_detail") == error_detail
                ):
                    return False
                connection.execute(insert(shadow_events).values(**event))
        except Exception:
            return False
        return True

    def _position_is_open(self, deployment_id: str) -> bool:
        with self.engine.connect() as connection:
            count = connection.execute(
                select(func.count())
                .select_from(shadow_trade_plans)
                .join(
                    shadow_signal_candidates,
                    shadow_signal_candidates.c.candidate_id
                    == shadow_trade_plans.c.candidate_id,
                )
                .where(
                    (shadow_signal_candidates.c.shadow_deployment_id == deployment_id)
                    & (shadow_trade_plans.c.status == "POSITION_OPEN")
                )
            ).scalar_one()
        return int(count) > 0

    def _advance_deployment_cursor(
        self,
        *,
        deployment_id: str,
        bar: Any,
        lease_token: str,
    ) -> None:
        with self.engine.begin() as connection:
            self._assert_runtime_lease(connection, lease_token)
            connection.execute(
                update(shadow_deployments)
                .where(shadow_deployments.c.shadow_deployment_id == deployment_id)
                .values(
                    last_price=bar.close,
                    last_processed_bar_time=bar.event_time,
                    updated_at=self._now(),
                )
            )

    def _process_deployment(
        self,
        deployment: dict[str, Any],
        run_id: str,
        lease_token: str,
        *,
        new_exposure_paused: bool,
    ) -> tuple[int, int]:
        created = 0
        position_open = self._position_is_open(
            str(deployment["shadow_deployment_id"])
        )
        if deployment["status"] == "ACTIVE":
            created += self._enforce_sandbox_circuit_breaker(
                deployment_id=str(deployment["shadow_deployment_id"]),
                run_id=run_id,
                lease_token=lease_token,
            )
            if created:
                deployment = self.deployment(
                    str(deployment["shadow_deployment_id"])
                )
                position_open = self._position_is_open(
                    str(deployment["shadow_deployment_id"])
                )
                if deployment["status"] in _TERMINAL_DEPLOYMENT_STATUSES:
                    return 0, created
        if not position_open and deployment["status"] == "LIQUIDATION_PENDING":
            raise RuntimeError("Liquidation-pending Shadow deployment has no open position")
        if not position_open and deployment["status"] == "ACTIVE":
            try:
                self.adoption_preview(
                    strategy_spec_id=str(deployment["strategy_spec_id"]),
                    validation_report_id=str(deployment["validation_report_id"]),
                    admission_tier=str(deployment["admission_tier"]),
                    require_current_search_count=False,
                    require_current_universe_authority=False,
                )
            except ValueError as exc:
                self._require_revalidation(
                    deployment,
                    reason=str(exc),
                )
                return 0, created
        observed_at = self._now()
        created += self._cancel_interrupted_plan_activations(
            deployment=deployment,
            run_id=run_id,
            lease_token=lease_token,
            cancelled_at=observed_at,
        )
        bars = self.research_store.load_bars(
            symbol=str(deployment["symbol"]),
            timeframe=str(deployment["timeframe"]),
            as_of_end=observed_at,
        )
        if len(bars) < 22:
            return 0, created
        try:
            quality_report = self.data_quality.require_bars(
                bars,
                symbol=str(deployment["symbol"]),
                timeframe=str(deployment["timeframe"]),
                code_git_sha="shadow-runtime",
            )
            market_data_healthy = quality_report.status.value == "PASSED"
        except DataQualityError:
            if not position_open:
                raise
            quality_report = None
            market_data_healthy = False
        last_processed = _utc(deployment["last_processed_bar_time"])
        new_bars = [
            bar
            for bar in bars[20:]
            if last_processed is None or bar.event_time > last_processed
        ]
        if not new_bars:
            return 0, created
        if position_open:
            processed = 0
            for position_bar in new_bars:
                created += self._execute_open_plan(
                    deployment=deployment,
                    execution_bar=position_bar,
                    run_id=run_id,
                    lease_token=lease_token,
                    market_data_healthy=market_data_healthy,
                    missed_bar_count=0,
                )
                processed += 1
                created += self._enforce_sandbox_circuit_breaker(
                    deployment_id=str(deployment["shadow_deployment_id"]),
                    run_id=run_id,
                    lease_token=lease_token,
                )
                if not self._position_is_open(
                    str(deployment["shadow_deployment_id"])
                ):
                    break
            return processed, created
        if new_exposure_paused:
            return 0, created
        # Forward shadow deliberately consumes only the newest completed bar. It
        # never reconstructs hypothetical orders for bars that arrived while the
        # worker was offline.
        decision_bar = new_bars[-1]
        created += self._execute_open_plan(
            deployment=deployment,
            execution_bar=decision_bar,
            run_id=run_id,
            lease_token=lease_token,
            market_data_healthy=market_data_healthy,
            missed_bar_count=len(new_bars) - 1,
        )
        if self._position_is_open(str(deployment["shadow_deployment_id"])):
            self._advance_deployment_cursor(
                deployment_id=str(deployment["shadow_deployment_id"]),
                bar=decision_bar,
                lease_token=lease_token,
            )
            return len(new_bars), created
        refreshed = self.deployment(str(deployment["shadow_deployment_id"]))
        snapshot = self.features.build(
            symbol=str(refreshed["symbol"]),
            timeframe=str(refreshed["timeframe"]),
            as_of=decision_bar.available_from,
            bars=tuple(bar for bar in bars if bar.event_time <= decision_bar.event_time),
        )
        action = self._signal_action(refreshed, snapshot.values)
        decision_completed_at = self._now()
        candidate = decision = plan = evaluation_context = None
        earliest_execution_at: datetime | None = None
        pending_activation: dict[str, Any] | None = None
        lineage_values: list[dict[str, Any]] = []
        if action == SignalAction.LONG:
            earliest_execution_at = (
                self.session_clock.next_daily_session_open(decision_bar.event_time)
                if decision_bar.timeframe == "1Day"
                else decision_bar.available_from
            )
            expiry = (
                self.session_clock.daily_session_close_after(
                    decision_bar.event_time,
                    sessions_ahead=strategy_holding_period_sessions(
                        dict(refreshed.get("data_requirements_json") or {})
                    ),
                )
                if decision_bar.timeframe == "1Day"
                else snapshot.as_of + timedelta(minutes=5)
            )
            if (
                self.new_exposure_not_before is not None
                and earliest_execution_at < self.new_exposure_not_before
            ):
                lineage_values.append(
                    self._operational_event(
                        deployment=refreshed,
                        run_id=run_id,
                        bar_id=decision_bar.bar_id,
                        event_type="SHADOW_ACTIVATION_WINDOW_NOT_REACHED",
                        event_time=decision_completed_at,
                        payload={
                            "earliest_execution_at": earliest_execution_at.isoformat(),
                            "new_exposure_not_before": (
                                self.new_exposure_not_before.isoformat()
                            ),
                            "virtual_only": True,
                        },
                    )
                )
                action = SignalAction.FLAT
        if action == SignalAction.LONG:
            assert earliest_execution_at is not None
            (
                candidate,
                decision,
                plan,
                evaluation_context,
                lineage_values,
            ) = self._risk_lineage(
                deployment=refreshed,
                run_id=run_id,
                decision_bar_id=decision_bar.bar_id,
                snapshot_id=snapshot.feature_snapshot_id,
                snapshot_values=snapshot.values,
                signal_time=snapshot.as_of,
                evaluation_time=decision_completed_at,
                earliest_execution_at=earliest_execution_at,
                exit_time=expiry,
                planned_entry=decision_bar.close,
                known_liquidity_volume=decision_bar.volume,
                market_data_healthy=market_data_healthy,
            )
        if len(new_bars) > 1:
            lineage_values.append(
                self._operational_event(
                    deployment=refreshed,
                    run_id=run_id,
                    bar_id=decision_bar.bar_id,
                    event_type="FORWARD_BARS_SKIPPED",
                    event_time=decision_bar.available_from,
                    payload={"skipped_count": len(new_bars) - 1, "virtual_only": True},
                )
            )
        next_sequence = self._next_event_sequence(
            str(refreshed["shadow_deployment_id"])
        )
        for offset, value in enumerate(lineage_values):
            value["sequence"] = next_sequence + offset
        with self.engine.begin() as connection:
            self._assert_runtime_lease(connection, lease_token)
            if candidate is not None and decision is not None:
                assert evaluation_context is not None
                connection.execute(
                    insert(shadow_signal_candidates).values(
                        candidate_id=candidate.candidate_id,
                        shadow_deployment_id=refreshed["shadow_deployment_id"],
                        shadow_run_id=run_id,
                        strategy_spec_id=refreshed["strategy_spec_id"],
                        feature_snapshot_id=candidate.feature_snapshot_id,
                        decision_bar_id=decision_bar.bar_id,
                        symbol=candidate.symbol,
                        action=action.value,
                        as_of=candidate.as_of,
                        planned_entry=candidate.planned_entry,
                        invalidation=candidate.invalidation,
                        targets_json=[str(value) for value in candidate.targets],
                        expires_at=candidate.expires_at,
                        execution_contract_sha256=refreshed[
                            "execution_contract_sha256"
                        ],
                        created_at=decision_completed_at,
                    )
                )
                account = self._account_state(refreshed, snapshot.as_of)
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
                        planned_r_multiple_to_t1=decision.planned_r_multiple_to_t1,
                        portfolio_risk_after_usd=decision.portfolio_risk_after_usd,
                        policy_version=decision.policy_version,
                        evaluation_context_json={
                            **evaluation_context.model_dump(mode="json"),
                            "data_quality_report_id": (
                                quality_report.data_quality_report_id
                                if quality_report is not None
                                else None
                            ),
                            "liquidity_source_bar_id": decision_bar.bar_id,
                            "liquidity_source_volume": decision_bar.volume,
                            "future_execution_volume_used_for_entry": False,
                        },
                        evaluated_at=decision.evaluated_at,
                    )
                )
                if plan is not None:
                    reserved_cash = (
                        plan.limit_price * Decimal(plan.quantity)
                    ).quantize(Decimal("0.01"))
                    reserved_risk = max(
                        _ZERO,
                        decision.portfolio_risk_after_usd
                        - account.concurrent_planned_risk,
                    ).quantize(Decimal("0.01"))
                    reserved = self.accounts.reserve(
                        account_id=str(refreshed["virtual_account_id"]),
                        cash=reserved_cash,
                        risk_usd=reserved_risk,
                        connection=connection,
                    )
                    if reserved:
                        connection.execute(
                            insert(shadow_trade_plans).values(
                                trade_plan_id=plan.trade_plan_id,
                                candidate_id=plan.candidate_id,
                                risk_decision_id=plan.risk_decision_id,
                                symbol=plan.symbol,
                                direction=plan.direction.value,
                                quantity=plan.quantity,
                                reserved_cash=reserved_cash,
                                reserved_risk_usd=reserved_risk,
                                limit_price=plan.limit_price,
                                invalidation=plan.invalidation,
                                targets_json=[str(value) for value in plan.targets],
                                expires_at=plan.expires_at,
                                status="PENDING_ACTIVATION",
                                created_at=decision_completed_at,
                                closed_at=None,
                            )
                        )
                        pending_activation = {
                            "trade_plan_id": plan.trade_plan_id,
                            "reserved_cash": reserved_cash,
                            "reserved_risk_usd": reserved_risk,
                        }
                    else:
                        lineage_values[:] = [
                            value
                            for value in lineage_values
                            if value["event_type"] != "TRADE_PLAN"
                        ]
                        lineage_values.append(
                            self._operational_event(
                                deployment=refreshed,
                                run_id=run_id,
                                bar_id=decision_bar.bar_id,
                                event_type="TRADE_PLAN_RESERVATION_REJECTED",
                                event_time=snapshot.as_of,
                                payload={
                                    "trade_plan_id": plan.trade_plan_id,
                                    "reason_codes": [
                                        "SHARED_ACCOUNT_CAPACITY_CHANGED"
                                    ],
                                    "virtual_only": True,
                                },
                            )
                        )
                        for offset, value in enumerate(lineage_values):
                            value["sequence"] = next_sequence + offset
            if lineage_values:
                connection.execute(insert(shadow_events), lineage_values)
            connection.execute(
                update(shadow_deployments)
                .where(
                    shadow_deployments.c.shadow_deployment_id
                    == refreshed["shadow_deployment_id"]
                )
                .values(
                    last_price=decision_bar.close,
                    last_processed_bar_time=decision_bar.event_time,
                    updated_at=decision_completed_at,
                )
            )
        if pending_activation is not None:
            assert earliest_execution_at is not None
            durable_ready_at = self._now()
            activated = durable_ready_at < earliest_execution_at
            with self.engine.begin() as connection:
                self._assert_runtime_lease(connection, lease_token)
                changed = connection.execute(
                    update(shadow_trade_plans)
                    .where(
                        (shadow_trade_plans.c.trade_plan_id
                         == pending_activation["trade_plan_id"])
                        & (shadow_trade_plans.c.status == "PENDING_ACTIVATION")
                    )
                    .values(
                        status="OPEN" if activated else "CANCELLED",
                        created_at=durable_ready_at,
                        closed_at=None if activated else durable_ready_at,
                    )
                )
                if int(changed.rowcount or 0) != 1:
                    raise RuntimeError("Shadow plan activation lost its pending state")
                if not activated:
                    self.accounts.release_or_settle(
                        account_id=str(refreshed["virtual_account_id"]),
                        reserved_cash=Decimal(
                            str(pending_activation["reserved_cash"])
                        ),
                        reserved_risk_usd=Decimal(
                            str(pending_activation["reserved_risk_usd"])
                        ),
                        realized_pnl_delta=_ZERO,
                        connection=connection,
                    )
            if not activated:
                late_event = self._operational_event(
                    deployment=refreshed,
                    run_id=run_id,
                    bar_id=decision_bar.bar_id,
                    event_type="TRADE_PLAN_CANCELLED",
                    event_time=durable_ready_at,
                    payload={
                        "trade_plan_id": pending_activation["trade_plan_id"],
                        "reason_codes": ["PLAN_NOT_DURABLE_BEFORE_EXECUTION"],
                        "earliest_execution_at": earliest_execution_at.isoformat(),
                        "virtual_only": True,
                    },
                )
                late_event["sequence"] = self._next_event_sequence(
                    str(refreshed["shadow_deployment_id"])
                )
                with self.engine.begin() as connection:
                    self._assert_runtime_lease(connection, lease_token)
                    connection.execute(insert(shadow_events).values(**late_event))
                created += 1
        created += self._enforce_sandbox_circuit_breaker(
            deployment_id=str(refreshed["shadow_deployment_id"]),
            run_id=run_id,
            lease_token=lease_token,
        )
        return 1, created + len(lineage_values)

    def _cancel_interrupted_plan_activations(
        self,
        *,
        deployment: dict[str, Any],
        run_id: str,
        lease_token: str,
        cancelled_at: datetime,
    ) -> int:
        """Fail closed after a crash between plan persistence and activation."""
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(
                    shadow_trade_plans,
                    shadow_signal_candidates.c.decision_bar_id,
                )
                .join(
                    shadow_signal_candidates,
                    shadow_signal_candidates.c.candidate_id
                    == shadow_trade_plans.c.candidate_id,
                )
                .where(
                    (shadow_signal_candidates.c.shadow_deployment_id
                     == deployment["shadow_deployment_id"])
                    & (shadow_trade_plans.c.status == "PENDING_ACTIVATION")
                )
            ).all()
        if not rows:
            return 0
        next_sequence = self._next_event_sequence(
            str(deployment["shadow_deployment_id"])
        )
        events: list[dict[str, Any]] = []
        with self.engine.begin() as connection:
            self._assert_runtime_lease(connection, lease_token)
            for row in rows:
                changed = connection.execute(
                    update(shadow_trade_plans)
                    .where(
                        (shadow_trade_plans.c.trade_plan_id == row.trade_plan_id)
                        & (shadow_trade_plans.c.status == "PENDING_ACTIVATION")
                    )
                    .values(status="CANCELLED", closed_at=cancelled_at)
                )
                if int(changed.rowcount or 0) != 1:
                    continue
                self.accounts.release_or_settle(
                    account_id=str(deployment["virtual_account_id"]),
                    reserved_cash=Decimal(str(row.reserved_cash)),
                    reserved_risk_usd=Decimal(str(row.reserved_risk_usd)),
                    realized_pnl_delta=_ZERO,
                    connection=connection,
                )
                event = self._operational_event(
                    deployment=deployment,
                    run_id=run_id,
                    bar_id=str(row.decision_bar_id),
                    event_type="TRADE_PLAN_CANCELLED",
                    event_time=cancelled_at,
                    payload={
                        "trade_plan_id": str(row.trade_plan_id),
                        "reason_codes": ["INTERRUPTED_BEFORE_DURABLE_ACTIVATION"],
                        "virtual_only": True,
                    },
                )
                event["sequence"] = next_sequence + len(events)
                events.append(event)
            if events:
                connection.execute(insert(shadow_events), events)
        return len(events)

    def _require_revalidation(
        self,
        deployment: dict[str, Any],
        *,
        reason: str,
    ) -> None:
        """Quarantine an active sleeve when its approved contract is obsolete."""
        now = self._now()
        deployment_id = str(deployment["shadow_deployment_id"])
        with self.engine.begin() as connection:
            changed = connection.execute(
                update(shadow_deployments)
                .where(
                    (shadow_deployments.c.shadow_deployment_id == deployment_id)
                    & (shadow_deployments.c.status == "ACTIVE")
                )
                .values(status="REVALIDATION_REQUIRED", updated_at=now)
            )
            if int(changed.rowcount or 0) == 1:
                self._cancel_open_plans(
                    connection,
                    deployment_ids=(deployment_id,),
                    closed_at=now,
                )
        deployment["status"] = "REVALIDATION_REQUIRED"
        deployment["revalidation_reason"] = reason

    def _execute_open_plan(
        self,
        *,
        deployment: dict[str, Any],
        execution_bar: Any,
        run_id: str,
        lease_token: str,
        market_data_healthy: bool,
        missed_bar_count: int,
    ) -> int:
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(
                    shadow_trade_plans,
                    shadow_signal_candidates.c.feature_snapshot_id,
                    shadow_signal_candidates.c.as_of.label("signal_as_of"),
                    shadow_signal_candidates.c.decision_bar_id,
                    shadow_risk_decisions.c.evaluation_context_json,
                )
                .join(
                    shadow_signal_candidates,
                    shadow_signal_candidates.c.candidate_id
                    == shadow_trade_plans.c.candidate_id,
                )
                .join(
                    shadow_risk_decisions,
                    shadow_risk_decisions.c.risk_decision_id
                    == shadow_trade_plans.c.risk_decision_id,
                )
                .where(
                    (shadow_signal_candidates.c.shadow_deployment_id
                     == deployment["shadow_deployment_id"])
                    & shadow_trade_plans.c.status.in_(("OPEN", "POSITION_OPEN"))
                )
                .order_by(shadow_trade_plans.c.created_at.asc())
            ).all()
        if not rows:
            return 0
        if len(rows) != 1:
            raise RuntimeError("A strategy sleeve has multiple open plans")
        pending = dict(rows[0]._mapping)
        if str(pending["status"]) == "POSITION_OPEN":
            return self._execute_position_bar(
                deployment=deployment,
                pending=pending,
                execution_bar=execution_bar,
                run_id=run_id,
                lease_token=lease_token,
                market_data_healthy=market_data_healthy,
            )
        entry_time = (
            self.session_clock.daily_bar_session_open(execution_bar.event_time)
            if execution_bar.timeframe == "1Day"
            else execution_bar.event_time
        )
        account = self._account_state(deployment, entry_time)
        rejection_reasons: list[str] = []
        expires_at = _utc(pending["expires_at"])
        assert expires_at is not None
        signal_as_of = _utc(pending["signal_as_of"])
        assert signal_as_of is not None
        persisted_at = _utc(pending["created_at"])
        assert persisted_at is not None
        if entry_time < signal_as_of:
            rejection_reasons.append("EXECUTION_PRECEDES_SIGNAL")
        if persisted_at >= entry_time:
            rejection_reasons.append("PLAN_NOT_PERSISTED_BEFORE_EXECUTION")
        if missed_bar_count:
            rejection_reasons.append("MISSED_EARLIEST_FILL_BAR")
        if entry_time > expires_at:
            rejection_reasons.append("PLAN_EXPIRED")
        snapshot = self.research_store.feature_snapshot(
            str(pending["feature_snapshot_id"])
        )
        if snapshot is None:
            raise RuntimeError("Persisted Shadow plan feature snapshot is missing")
        profile_version, _, _ = strategy_execution_profile(
            data_requirements=dict(deployment.get("data_requirements_json") or {}),
            account_policy=self.effective_risk_policy(
                str(deployment["virtual_account_id"])
            ),
            strategy_type=str(deployment["strategy_type"]),
        )
        policy = strategy_signal_risk_policy(
            data_requirements=dict(deployment.get("data_requirements_json") or {}),
            strategy_type=str(deployment["strategy_type"]),
            feature_values=dict(snapshot.values),
            account_policy=self.effective_risk_policy(
                str(deployment["virtual_account_id"])
            ),
        )
        if account.equity <= policy.account_floor_usd:
            rejection_reasons.append("ACCOUNT_FLOOR_REACHED_AT_EXECUTION")
        if (
            policy.daily_loss_limit_enabled
            and account.daily_pnl <= -policy.daily_loss_stop_usd
        ):
            rejection_reasons.append("DAILY_LOSS_HALT_AT_EXECUTION")
        if self.restrictions.is_restricted(
            str(deployment["symbol"]), entry_time.astimezone(UTC).date()
        ):
            rejection_reasons.append("SECURITY_RESTRICTED_AT_EXECUTION")
        if not market_data_healthy:
            rejection_reasons.append("MARKET_DATA_UNHEALTHY_AT_EXECUTION")
        execution_review: RiskDecision | None = None
        execution_quantity = 0
        if not rejection_reasons:
            context = dict(pending["evaluation_context_json"] or {})
            own_risk = Decimal(str(pending["reserved_risk_usd"]))
            review_account = account.model_copy(
                update={
                    "concurrent_planned_risk": max(
                        _ZERO,
                        account.concurrent_planned_risk - own_risk,
                    )
                }
            )
            limit_fill = deployable_long_limit_fill(
                open_price=Decimal(str(execution_bar.open)),
                low_price=Decimal(str(execution_bar.low)),
                limit_price=Decimal(str(pending["limit_price"])),
            )
            execution_candidate = SignalCandidate(
                candidate_id=str(pending["candidate_id"]),
                symbol=str(pending["symbol"]),
                direction=Direction.LONG,
                setup_type="baseline_shadow:execution_revalidation",
                strategy_version=str(deployment["strategy_version"]),
                as_of=signal_as_of,
                feature_snapshot_id=str(pending["feature_snapshot_id"]),
                catalyst_id="NOT_APPLICABLE_BASELINE",
                planned_entry=(
                    limit_fill[0]
                    if limit_fill is not None
                    else Decimal(str(pending["limit_price"]))
                ),
                invalidation=Decimal(str(pending["invalidation"])),
                targets=tuple(
                    Decimal(str(value)) for value in pending["targets_json"]
                ),
                expires_at=expires_at,
            )
            execution_review = evaluate_candidate(
                candidate=execution_candidate,
                features=FeatureSnapshot(
                    feature_snapshot_id=str(pending["feature_snapshot_id"]),
                    as_of=signal_as_of,
                    relative_volume=Decimal("0"),
                    vwap_confirmed=False,
                    opening_range_confirmed=False,
                    sector_compatible=False,
                    quote_age_seconds=0,
                ),
                account=review_account,
                mode=TradingMode.SHADOW,
                policy=policy,
                restrictions=self.restrictions,
                context=RiskEvaluationContext(
                    catalyst_required=False,
                    catalyst_verified=False,
                    restriction_status_known=True,
                    liquidity_confirmed=int(
                        context.get("liquidity_source_volume") or 0
                    )
                    > 0,
                    market_data_healthy=market_data_healthy,
                    macro_calendar_status_known=False,
                    duplicate_order_detected=False,
                    evaluation_profile="baseline_shadow",
                ),
                evaluated_at=entry_time,
                new_exposure_paused=False,
            )
            if limit_fill is None:
                rejection_reasons.append("DAY_LIMIT_NOT_FILLED")
            elif execution_review.verdict != Verdict.APPROVE:
                rejection_reasons.extend(execution_review.reason_codes)
            else:
                cash_limit = int(
                    Decimal(str(pending["reserved_cash"]))
                    // limit_fill[0]
                )
                execution_quantity = min(
                    int(pending["quantity"]),
                    execution_review.max_quantity,
                    cash_limit,
                )
                if execution_quantity < 1:
                    rejection_reasons.append("EXECUTION_SIZE_ROUNDS_TO_ZERO")
        review_event = self._operational_event(
            deployment=deployment,
            run_id=run_id,
            bar_id=execution_bar.bar_id,
            event_type="EXECUTION_RISK_REVIEW",
            event_time=entry_time,
            payload={
                "trade_plan_id": pending["trade_plan_id"],
                "execution_risk_decision_id": (
                    execution_review.risk_decision_id
                    if execution_review is not None
                    else None
                ),
                "reference_price": str(pending["limit_price"]),
                "open_price": str(execution_bar.open),
                "original_quantity": int(pending["quantity"]),
                "approved_quantity": execution_quantity,
                "risk_budget_usd": (
                    str(execution_review.risk_budget_usd)
                    if execution_review is not None
                    else "0"
                ),
                "reward_risk_at_open": (
                    str(execution_review.planned_r_multiple_to_t1)
                    if execution_review is not None
                    and execution_review.planned_r_multiple_to_t1 is not None
                    else None
                ),
                "verdict": (
                    execution_review.verdict.value
                    if execution_review is not None
                    else "REJECT"
                ),
                "reason_codes": (
                    list(rejection_reasons)
                    if rejection_reasons
                    else list(execution_review.reason_codes)
                    if execution_review is not None
                    else []
                ),
                "execution_profile": profile_version,
                "virtual_only": True,
            },
        )
        position_state: dict[str, Any] | None = None
        if rejection_reasons:
            events = [
                review_event,
                self._operational_event(
                    deployment=deployment,
                    run_id=run_id,
                    bar_id=execution_bar.bar_id,
                    event_type="TRADE_PLAN_CANCELLED",
                    event_time=entry_time,
                    payload={
                        "trade_plan_id": pending["trade_plan_id"],
                        "reason_codes": rejection_reasons,
                        "virtual_only": True,
                    },
                )
            ]
            status = "CANCELLED"
            cash = account.equity
            realized = Decimal(str(deployment["realized_pnl"]))
        else:
            context = dict(pending["evaluation_context_json"] or {})
            simulation_deployment = {
                **deployment,
                "cash_balance": account.equity,
            }
            holding_sessions = strategy_holding_period_sessions(
                dict(deployment.get("data_requirements_json") or {})
            )
            simulation_arguments = {
                "deployment": simulation_deployment,
                "run_id": run_id,
                "decision_bar_id": execution_bar.bar_id,
                "snapshot_id": str(pending["feature_snapshot_id"]),
                "signal_time": signal_as_of,
                "entry_time": entry_time,
                "exit_time": execution_bar.available_from,
                "action": SignalAction.LONG,
                "open_price": execution_bar.open,
                "high_price": execution_bar.high,
                "low_price": execution_bar.low,
                "close_price": execution_bar.close,
                "volume": int(context.get("liquidity_source_volume") or 0),
                "exit_volume": execution_bar.volume,
                "quantity_limit": execution_quantity,
                "invalidation": Decimal(str(pending["invalidation"])),
                "target": Decimal(str(list(pending["targets_json"])[0])),
                "entry_limit_price": Decimal(str(pending["limit_price"])),
                "lineage": {
                    "candidate_id": pending["candidate_id"],
                    "risk_decision_id": pending["risk_decision_id"],
                    "trade_plan_id": pending["trade_plan_id"],
                },
            }
            if holding_sessions == 1:
                simulated_events, cash, realized = self._simulate_one_bar(
                    **simulation_arguments,
                )
            else:
                (
                    simulated_events,
                    cash,
                    realized,
                    position_state,
                ) = self._simulate_multi_session_entry(**simulation_arguments)
            events = [review_event, *simulated_events]
            status = "POSITION_OPEN" if position_state is not None else "CLOSED"
        if events:
            next_sequence = self._next_event_sequence(
                str(deployment["shadow_deployment_id"])
            )
            for offset, value in enumerate(events):
                value["sequence"] = next_sequence + offset
        with self.engine.begin() as connection:
            self._assert_runtime_lease(connection, lease_token)
            connection.execute(
                update(shadow_trade_plans)
                .where(
                    (shadow_trade_plans.c.trade_plan_id == pending["trade_plan_id"])
                    & (shadow_trade_plans.c.status == "OPEN")
                )
                .values(
                    status=status,
                    opened_at=(
                        position_state["opened_at"]
                        if position_state is not None
                        else None
                    ),
                    entry_raw_price=(
                        position_state["entry_raw_price"]
                        if position_state is not None
                        else None
                    ),
                    entry_fill_price=(
                        position_state["entry_fill_price"]
                        if position_state is not None
                        else None
                    ),
                    entry_commission=(
                        position_state["entry_commission"]
                        if position_state is not None
                        else None
                    ),
                    filled_quantity=(
                        position_state["filled_quantity"]
                        if position_state is not None
                        else None
                    ),
                    starting_cash=(
                        position_state["starting_cash"]
                        if position_state is not None
                        else None
                    ),
                    corporate_action_cash=(
                        position_state["corporate_action_cash"]
                        if position_state is not None
                        else None
                    ),
                    applied_corporate_action_ids_json=(
                        position_state["applied_corporate_action_ids"]
                        if position_state is not None
                        else None
                    ),
                    closed_at=None if position_state is not None else self._now(),
                )
            )
            realized_delta = cash - account.equity if status == "CLOSED" else _ZERO
            if status != "POSITION_OPEN":
                self.accounts.release_or_settle(
                    account_id=str(deployment["virtual_account_id"]),
                    reserved_cash=Decimal(str(pending["reserved_cash"])),
                    reserved_risk_usd=Decimal(str(pending["reserved_risk_usd"])),
                    realized_pnl_delta=realized_delta,
                    connection=connection,
                )
            if events:
                connection.execute(insert(shadow_events), events)
            connection.execute(
                update(shadow_deployments)
                .where(
                    shadow_deployments.c.shadow_deployment_id
                    == deployment["shadow_deployment_id"]
                )
                .values(
                    cash_balance=cash,
                    position_quantity=(
                        position_state["filled_quantity"]
                        if position_state is not None
                        else _ZERO
                    ),
                    average_entry_price=(
                        position_state["entry_fill_price"]
                        if position_state is not None
                        else None
                    ),
                    last_price=execution_bar.close,
                    realized_pnl=realized,
                    unrealized_pnl=(
                        position_state["unrealized_pnl"]
                        if position_state is not None
                        else _ZERO
                    ),
                    updated_at=datetime.now(UTC),
                )
            )
        deployment["cash_balance"] = cash
        deployment["sleeve_cash_balance"] = cash
        deployment["realized_pnl"] = realized
        return len(events)

    def _simulate_multi_session_entry(
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
        high_price: Decimal,
        low_price: Decimal,
        volume: int,
        exit_volume: int | None = None,
        quantity_limit: int | None = None,
        invalidation: Decimal | None = None,
        target: Decimal | None = None,
        entry_limit_price: Decimal | None = None,
        lineage: dict[str, str | None] | None = None,
    ) -> tuple[
        list[dict[str, Any]],
        Decimal,
        Decimal,
        dict[str, Any] | None,
    ]:
        if action != SignalAction.LONG:
            raise ValueError("Multi-session Shadow supports long positions only")
        if invalidation is None or target is None:
            raise ValueError("Approved shadow entry requires bracket geometry")
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
        limit_fill = deployable_long_limit_fill(
            open_price=open_price,
            low_price=low_price,
            limit_price=entry_limit_price or open_price,
        )
        entered = limit_fill is not None and portfolio.enter_long(
            signal_as_of=signal_time,
            entry_time=entry_time,
            raw_price=limit_fill[0],
            available_volume=exit_volume if exit_volume is not None else volume,
            feature_snapshot_id=snapshot_id,
            quantity_limit=quantity_limit,
        )
        position_state: dict[str, Any] | None = None
        if entered:
            assert limit_fill is not None
            exit_price, exit_reason = deployable_long_exit(
                entry_kind=limit_fill[1],
                open_price=open_price,
                high_price=high_price,
                low_price=low_price,
                close_price=close_price,
                invalidation=invalidation,
                target=target,
            )
            bracket_triggered = exit_reason not in {
                "session_close",
                "market_on_close",
            }
            if bracket_triggered:
                portfolio.exit_long(
                    exit_time=exit_time,
                    raw_price=exit_price,
                    available_volume=(
                        exit_volume if exit_volume is not None else volume
                    ),
                    exit_reason=exit_reason,
                )
            else:
                equity = portfolio.mark(event_time=exit_time, raw_price=close_price)
                fill = next(
                    event
                    for event in portfolio.events
                    if event.event_type.value == "fill"
                    and event.details.get("side") == "buy"
                )
                quantity = portfolio.quantity
                position_state = {
                    "opened_at": entry_time,
                    "entry_raw_price": limit_fill[0],
                    "entry_fill_price": fill.price,
                    "entry_commission": Decimal(str(fill.details["commission"])),
                    "filled_quantity": quantity,
                    "starting_cash": starting_cash,
                    "corporate_action_cash": _ZERO,
                    "applied_corporate_action_ids": [],
                    "unrealized_pnl": equity - starting_cash,
                }
        else:
            portfolio.mark(event_time=exit_time, raw_price=close_price)
        values = self._portfolio_event_values(
            deployment=deployment,
            run_id=run_id,
            decision_bar_id=decision_bar_id,
            events=portfolio.events,
            realized_pnl_delta=(
                portfolio.cash - starting_cash if position_state is None else _ZERO
            ),
            lineage=lineage,
        )
        pnl_delta = portfolio.cash - starting_cash if position_state is None else _ZERO
        realized = Decimal(str(deployment["realized_pnl"])) + pnl_delta
        return values, portfolio.cash, realized, position_state

    def _execute_position_bar(
        self,
        *,
        deployment: dict[str, Any],
        pending: dict[str, Any],
        execution_bar: Any,
        run_id: str,
        lease_token: str,
        market_data_healthy: bool,
    ) -> int:
        required = (
            "opened_at",
            "entry_raw_price",
            "entry_fill_price",
            "entry_commission",
            "filled_quantity",
            "starting_cash",
        )
        if any(pending.get(field) is None for field in required):
            raise RuntimeError("Persisted Shadow position state is incomplete")
        opened_at = _utc(pending["opened_at"])
        expires_at = _utc(pending["expires_at"])
        assert opened_at is not None and expires_at is not None
        bar_close = _utc(execution_bar.available_from)
        assert bar_close is not None
        if execution_bar.event_time < opened_at:
            return 0
        bar_open = (
            self.session_clock.daily_bar_session_open(execution_bar.event_time)
            if execution_bar.timeframe == "1Day"
            else execution_bar.event_time
        )
        liquidation_requested_at = _utc(
            deployment.get("liquidation_requested_at")
        )
        forced_liquidation = bool(
            deployment.get("status") == "LIQUIDATION_PENDING"
            and liquidation_requested_at is not None
            and liquidation_requested_at < bar_open
        )

        quantity = Decimal(str(pending["filled_quantity"]))
        entry_raw = Decimal(str(pending["entry_raw_price"]))
        entry_fill = Decimal(str(pending["entry_fill_price"]))
        entry_commission = Decimal(str(pending["entry_commission"]))
        starting_cash = Decimal(str(pending["starting_cash"]))
        corporate_cash = Decimal(str(pending.get("corporate_action_cash") or 0))
        invalidation = Decimal(str(pending["invalidation"]))
        targets = [Decimal(str(value)) for value in pending["targets_json"]]
        if not targets:
            raise RuntimeError("Persisted Shadow position is missing its target")
        target = targets[0]
        applied_ids = {
            str(value)
            for value in (pending.get("applied_corporate_action_ids_json") or [])
        }
        action_events: list[dict[str, Any]] = []
        actions = self.reference_data.corporate_actions_as_of(
            symbol=str(deployment["symbol"]),
            as_of=bar_close,
            effective_from=opened_at,
        )
        for action in actions:
            if action.corporate_action_id in applied_ids:
                continue
            if action.action_type == CorporateActionType.SYMBOL_CHANGE:
                raise RuntimeError(
                    "An open Shadow position requires manual review after a symbol change"
                )
            if action.action_type == CorporateActionType.SPLIT:
                assert action.split_ratio is not None
                quantity *= action.split_ratio
                entry_raw /= action.split_ratio
                entry_fill /= action.split_ratio
                invalidation /= action.split_ratio
                target /= action.split_ratio
            else:
                assert action.cash_amount is not None
                dividend = quantity * action.cash_amount
                corporate_cash += dividend
            applied_ids.add(action.corporate_action_id)
            action_events.append(
                {
                    "shadow_event_id": uuid7(),
                    "shadow_deployment_id": deployment["shadow_deployment_id"],
                    "shadow_run_id": run_id,
                    "sequence": 0,
                    "event_type": f"CORPORATE_ACTION_{len(action_events) + 1}",
                    "event_time": action.effective_at,
                    "symbol": deployment["symbol"],
                    "bar_id": execution_bar.bar_id,
                    "cash_balance": self._position_cash_after_entry(
                        starting_cash=starting_cash,
                        entry_fill_price=entry_fill,
                        entry_commission=entry_commission,
                        quantity=quantity,
                        corporate_action_cash=corporate_cash,
                    ),
                    "position_quantity": quantity,
                    "price": None,
                    "realized_pnl_delta": _ZERO,
                    "payload_json": {
                        "corporate_action_id": action.corporate_action_id,
                        "action_type": action.action_type.value,
                        "split_ratio": (
                            str(action.split_ratio)
                            if action.split_ratio is not None
                            else None
                        ),
                        "cash_amount": (
                            str(action.cash_amount)
                            if action.cash_amount is not None
                            else None
                        ),
                        "trade_plan_id": pending["trade_plan_id"],
                        "virtual_only": True,
                    },
                    "created_at": self._now(),
                }
            )

        cash_after_entry = self._position_cash_after_entry(
            starting_cash=starting_cash,
            entry_fill_price=entry_fill,
            entry_commission=entry_commission,
            quantity=quantity,
            corporate_action_cash=corporate_cash,
        )
        exit_price, exit_reason = baseline_long_exit(
            open_price=execution_bar.open,
            high_price=execution_bar.high,
            low_price=execution_bar.low,
            close_price=execution_bar.close,
            invalidation=invalidation,
            target=target,
        )
        bracket_triggered = exit_reason != "session_close"
        timed_exit = bar_close >= expires_at
        emergency_exit = not market_data_healthy
        close_position = (
            forced_liquidation or bracket_triggered or timed_exit or emergency_exit
        )
        events = action_events
        final_cash = cash_after_entry
        realized_delta = _ZERO
        unrealized = cash_after_entry + quantity * execution_bar.close - starting_cash
        status = "POSITION_OPEN"
        if close_position:
            raw_exit = (
                execution_bar.open
                if emergency_exit or forced_liquidation
                else exit_price
                if bracket_triggered
                else execution_bar.close
            )
            volume_capacity = (
                Decimal(execution_bar.volume) * self.costs.max_volume_participation
            )
            if quantity > volume_capacity:
                raise ValueError(
                    "Insufficient bar liquidity to close the Shadow position"
                )
            execution_bps = (
                self.costs.slippage_bps_per_side
                + self.costs.half_spread_bps_per_side
                + self.costs.market_impact_bps_per_side
            )
            fill_price = raw_exit * (_ONE - execution_bps / Decimal("10000"))
            exit_commission = EventDrivenPortfolio.commission(quantity, self.costs)
            final_cash = cash_after_entry + fill_price * quantity - exit_commission
            realized_delta = final_cash - starting_cash
            unrealized = _ZERO
            status = "CLOSED"
            events.extend(
                (
                    self._position_event(
                        deployment=deployment,
                        run_id=run_id,
                        bar=execution_bar,
                        event_type="VIRTUAL_ORDER_2",
                        event_time=bar_close,
                        cash_balance=cash_after_entry,
                        position_quantity=quantity,
                        price=None,
                        payload={
                            "side": "sell",
                            "quantity": str(quantity),
                            "trade_plan_id": pending["trade_plan_id"],
                        },
                    ),
                    self._position_event(
                        deployment=deployment,
                        run_id=run_id,
                        bar=execution_bar,
                        event_type="VIRTUAL_FILL_2",
                        event_time=bar_close,
                        cash_balance=final_cash,
                        position_quantity=_ZERO,
                        price=fill_price,
                        realized_pnl_delta=realized_delta,
                        payload={
                            "side": "sell",
                            "quantity": str(quantity),
                            "raw_price": str(raw_exit),
                            "commission": str(exit_commission),
                            "exit_reason": (
                                "sandbox_circuit_forced_exit"
                                if forced_liquidation
                                and deployment.get("retired_reason")
                                == "SANDBOX_CIRCUIT_BREAKER"
                                else "legacy_shadow_migration_forced_exit"
                                if forced_liquidation
                                else "market_data_gap_emergency_exit"
                                if emergency_exit
                                else exit_reason
                                if bracket_triggered
                                else "maximum_holding_period"
                            ),
                            "trade_plan_id": pending["trade_plan_id"],
                        },
                    ),
                )
            )
        else:
            events.append(
                self._position_event(
                    deployment=deployment,
                    run_id=run_id,
                    bar=execution_bar,
                    event_type="mark",
                    event_time=bar_close,
                    cash_balance=cash_after_entry,
                    position_quantity=quantity,
                    price=execution_bar.close,
                    payload={
                        "equity": str(cash_after_entry + quantity * execution_bar.close),
                        "trade_plan_id": pending["trade_plan_id"],
                    },
                )
            )
        next_sequence = self._next_event_sequence(
            str(deployment["shadow_deployment_id"])
        )
        for offset, event in enumerate(events):
            event["sequence"] = next_sequence + offset
        cumulative_realized = Decimal(str(deployment["realized_pnl"]))
        if close_position:
            cumulative_realized += realized_delta
        with self.engine.begin() as connection:
            self._assert_runtime_lease(connection, lease_token)
            changed = connection.execute(
                update(shadow_trade_plans)
                .where(
                    (shadow_trade_plans.c.trade_plan_id == pending["trade_plan_id"])
                    & (shadow_trade_plans.c.status == "POSITION_OPEN")
                )
                .values(
                    status=status,
                    entry_raw_price=entry_raw,
                    entry_fill_price=entry_fill,
                    filled_quantity=quantity,
                    corporate_action_cash=corporate_cash,
                    applied_corporate_action_ids_json=sorted(applied_ids),
                    invalidation=invalidation,
                    targets_json=[str(target)],
                    closed_at=bar_close if close_position else None,
                )
            )
            if int(changed.rowcount or 0) != 1:
                raise RuntimeError("Shadow position lost its persisted open state")
            if close_position:
                self.accounts.release_or_settle(
                    account_id=str(deployment["virtual_account_id"]),
                    reserved_cash=Decimal(str(pending["reserved_cash"])),
                    reserved_risk_usd=Decimal(str(pending["reserved_risk_usd"])),
                    realized_pnl_delta=realized_delta,
                    connection=connection,
                )
            connection.execute(insert(shadow_events), events)
            deployment_values: dict[str, Any] = {
                "cash_balance": final_cash,
                "position_quantity": _ZERO if close_position else quantity,
                "average_entry_price": None if close_position else entry_fill,
                "last_price": execution_bar.close,
                "realized_pnl": cumulative_realized,
                "unrealized_pnl": unrealized,
                "last_processed_bar_time": execution_bar.event_time,
                "updated_at": self._now(),
            }
            if close_position and deployment.get("status") == "LIQUIDATION_PENDING":
                deployment_values["status"] = (
                    "RETIRED_SHADOW_FAILED"
                    if deployment.get("retired_reason")
                    == "SANDBOX_CIRCUIT_BREAKER"
                    else "RETIRED_LEGACY"
                )
            connection.execute(
                update(shadow_deployments)
                .where(
                    shadow_deployments.c.shadow_deployment_id
                    == deployment["shadow_deployment_id"]
                )
                .values(**deployment_values)
            )
            if (
                close_position
                and deployment.get("status") == "LIQUIDATION_PENDING"
                and deployment.get("account_mode") == STRATEGY_SANDBOX_ACCOUNT_TYPE
            ):
                connection.execute(
                    update(virtual_accounts)
                    .where(
                        virtual_accounts.c.virtual_account_id
                        == deployment["virtual_account_id"]
                    )
                    .values(status="FAILED", updated_at=self._now())
                )
        deployment["sleeve_cash_balance"] = final_cash
        deployment["realized_pnl"] = cumulative_realized
        if close_position and deployment.get("status") == "LIQUIDATION_PENDING":
            self._sync_active_symbols(
                str(deployment["symbol"]),
                add=False,
                reason=str(
                    deployment.get("retired_reason")
                    or "Shadow liquidation completed"
                ),
            )
        return len(events)

    @staticmethod
    def _position_cash_after_entry(
        *,
        starting_cash: Decimal,
        entry_fill_price: Decimal,
        entry_commission: Decimal,
        quantity: Decimal,
        corporate_action_cash: Decimal,
    ) -> Decimal:
        return (
            starting_cash
            - entry_fill_price * quantity
            - entry_commission
            + corporate_action_cash
        )

    def _position_event(
        self,
        *,
        deployment: dict[str, Any],
        run_id: str,
        bar: Any,
        event_type: str,
        event_time: datetime,
        cash_balance: Decimal,
        position_quantity: Decimal,
        price: Decimal | None,
        payload: dict[str, Any],
        realized_pnl_delta: Decimal = _ZERO,
    ) -> dict[str, Any]:
        return {
            "shadow_event_id": uuid7(),
            "shadow_deployment_id": deployment["shadow_deployment_id"],
            "shadow_run_id": run_id,
            "sequence": 0,
            "event_type": event_type,
            "event_time": event_time,
            "symbol": deployment["symbol"],
            "bar_id": bar.bar_id,
            "cash_balance": cash_balance,
            "position_quantity": position_quantity,
            "price": price,
            "realized_pnl_delta": realized_pnl_delta,
            "payload_json": {**payload, "virtual_only": True},
            "created_at": self._now(),
        }

    def _portfolio_event_values(
        self,
        *,
        deployment: dict[str, Any],
        run_id: str,
        decision_bar_id: str,
        events: tuple[Any, ...],
        realized_pnl_delta: Decimal,
        lineage: dict[str, str | None] | None,
    ) -> list[dict[str, Any]]:
        event_counts: dict[str, int] = {}
        type_names = {
            "order_submitted": "VIRTUAL_ORDER",
            "fill": "VIRTUAL_FILL",
        }
        values: list[dict[str, Any]] = []
        for offset, event in enumerate(events):
            base_type = type_names.get(event.event_type.value, event.event_type.value)
            count = event_counts.get(base_type, 0) + 1
            event_counts[base_type] = count
            event_type = base_type if count == 1 else f"{base_type}_{count}"
            values.append(
                {
                    "shadow_event_id": uuid7(),
                    "shadow_deployment_id": deployment["shadow_deployment_id"],
                    "shadow_run_id": run_id,
                    "sequence": offset + 1,
                    "event_type": event_type,
                    "event_time": event.event_time,
                    "symbol": deployment["symbol"],
                    "bar_id": decision_bar_id,
                    "cash_balance": event.cash_balance,
                    "position_quantity": event.position_quantity,
                    "price": event.price,
                    "realized_pnl_delta": (
                        realized_pnl_delta if offset == len(events) - 1 else _ZERO
                    ),
                    "payload_json": {
                        **event.details,
                        "feature_snapshot_id": event.feature_snapshot_id,
                        **(lineage or {}),
                        "virtual_only": True,
                    },
                    "created_at": self._now(),
                }
            )
        return values

    @staticmethod
    def _operational_event(
        *,
        deployment: dict[str, Any],
        run_id: str,
        bar_id: str | None,
        event_type: str,
        event_time: datetime,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "shadow_event_id": uuid7(),
            "shadow_deployment_id": deployment["shadow_deployment_id"],
            "shadow_run_id": run_id,
            "sequence": 0,
            "event_type": event_type,
            "event_time": event_time,
            "symbol": deployment["symbol"],
            "bar_id": bar_id,
            "cash_balance": Decimal(str(deployment["cash_balance"])),
            "position_quantity": _ZERO,
            "price": deployment.get("last_price"),
            "realized_pnl_delta": _ZERO,
            "payload_json": payload,
            "created_at": datetime.now(UTC),
        }

    def _acquire_runtime_lease(self, *, owner: str) -> str | None:
        now = datetime.now(UTC)
        token = uuid7()
        values = {
            "lease_key": "shadow:portfolio-execution",
            "lease_owner": owner,
            "lease_token": token,
            "lease_expires_at": now + timedelta(minutes=1),
            "updated_at": now,
        }
        dialect_insert: Any
        if self.engine.dialect.name == "postgresql":
            dialect_insert = postgresql_insert
        elif self.engine.dialect.name == "sqlite":
            dialect_insert = sqlite_insert
        else:
            raise RuntimeError(f"Unsupported SQL dialect: {self.engine.dialect.name}")
        statement = (
            dialect_insert(runtime_leases)
            .values(**values)
            .on_conflict_do_update(
                index_elements=["lease_key"],
                set_=values,
                where=runtime_leases.c.lease_expires_at <= now,
            )
            .returning(runtime_leases.c.lease_token)
        )
        with self.engine.begin() as connection:
            claimed = connection.execute(statement).scalar_one_or_none()
        return str(claimed) if claimed is not None else None

    async def _runtime_lease_heartbeat(self, token: str) -> None:
        while True:
            await asyncio.sleep(15)
            if not await asyncio.to_thread(self._renew_runtime_lease, token):
                raise RuntimeError("Shadow portfolio execution lease was lost")

    def _renew_runtime_lease(self, token: str) -> bool:
        now = datetime.now(UTC)
        with self.engine.begin() as connection:
            result = connection.execute(
                update(runtime_leases)
                .where(
                    (runtime_leases.c.lease_key == "shadow:portfolio-execution")
                    & (runtime_leases.c.lease_token == token)
                    & (runtime_leases.c.lease_expires_at > now)
                )
                .values(
                    lease_expires_at=now + timedelta(minutes=1),
                    updated_at=now,
                )
            )
        return int(result.rowcount or 0) == 1

    def _release_runtime_lease(self, token: str) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                delete(runtime_leases).where(
                    (runtime_leases.c.lease_key == "shadow:portfolio-execution")
                    & (runtime_leases.c.lease_token == token)
                )
            )

    @staticmethod
    def _assert_runtime_lease(connection: Any, token: str) -> None:
        now = datetime.now(UTC)
        active = connection.execute(
            select(runtime_leases.c.lease_token).where(
                (runtime_leases.c.lease_key == "shadow:portfolio-execution")
                & (runtime_leases.c.lease_token == token)
                & (runtime_leases.c.lease_expires_at > now)
            )
        ).scalar_one_or_none()
        if active is None:
            raise RuntimeError("Shadow portfolio execution lease was lost")

    def _risk_lineage(
        self,
        *,
        deployment: dict[str, Any],
        run_id: str,
        decision_bar_id: str,
        snapshot_id: str,
        snapshot_values: dict[str, Any],
        signal_time: datetime,
        evaluation_time: datetime,
        earliest_execution_at: datetime,
        exit_time: datetime,
        planned_entry: Decimal,
        known_liquidity_volume: int,
        market_data_healthy: bool,
    ) -> tuple[
        SignalCandidate,
        RiskDecision,
        TradePlan | None,
        RiskEvaluationContext,
        list[dict[str, Any]],
    ]:
        policy = strategy_signal_risk_policy(
            data_requirements=dict(deployment.get("data_requirements_json") or {}),
            strategy_type=str(deployment["strategy_type"]),
            feature_values=snapshot_values,
            account_policy=self.effective_risk_policy(
                str(deployment["virtual_account_id"])
            ),
        )
        raw_invalidation, raw_target = baseline_long_geometry(
            planned_entry,
            policy,
        )
        planned_entry, target, invalidation = normalize_deployable_long_prices(
            entry_limit_price=planned_entry,
            take_profit_price=raw_target,
            stop_loss_price=raw_invalidation,
        )
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
            expires_at=max(exit_time, signal_time + timedelta(microseconds=1)),
        )
        account = self._account_state(deployment, evaluation_time)
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
            market_data_healthy=market_data_healthy,
            macro_calendar_status_known=False,
            duplicate_order_detected=False,
            decision_before_execution=evaluation_time < earliest_execution_at,
            evaluation_profile="baseline_shadow",
        )
        decision = evaluate_candidate(
            candidate=candidate,
            features=risk_features,
            account=account,
            mode=TradingMode.SHADOW,
            policy=policy,
            restrictions=self.restrictions,
            context=context,
            evaluated_at=evaluation_time,
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
            "created_at": evaluation_time,
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
                "event_time": evaluation_time,
                "payload_json": {
                    **decision.model_dump(mode="json"),
                    "evaluation_context": context.model_dump(mode="json"),
                    "earliest_execution_at": earliest_execution_at.isoformat(),
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
                    "event_time": evaluation_time,
                    "payload_json": {
                        **plan.model_dump(mode="json"),
                        "approved_at": evaluation_time.isoformat(),
                        "persisted_at": evaluation_time.isoformat(),
                        "earliest_execution_at": earliest_execution_at.isoformat(),
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
                .join(
                    shadow_deployments,
                    shadow_deployments.c.shadow_deployment_id
                    == shadow_events.c.shadow_deployment_id,
                )
                .where(
                    shadow_deployments.c.virtual_account_id
                    == deployment["virtual_account_id"]
                )
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
            account = connection.execute(
                select(virtual_accounts).where(
                    virtual_accounts.c.virtual_account_id
                    == deployment["virtual_account_id"]
                )
            ).one()
            unrealized_pnl = connection.execute(
                select(
                    func.coalesce(func.sum(shadow_deployments.c.unrealized_pnl), 0)
                ).where(
                    shadow_deployments.c.virtual_account_id
                    == deployment["virtual_account_id"]
                )
            ).scalar_one()
        return AccountState(
            equity=(
                Decimal(str(account.cash_balance))
                + Decimal(str(unrealized_pnl))
            ),
            daily_pnl=daily_pnl,
            concurrent_planned_risk=Decimal(str(account.reserved_risk_usd)),
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
        high_price: Decimal,
        low_price: Decimal,
        volume: int,
        exit_volume: int | None = None,
        quantity_limit: int | None = None,
        invalidation: Decimal | None = None,
        target: Decimal | None = None,
        entry_limit_price: Decimal | None = None,
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
            limit_fill = deployable_long_limit_fill(
                open_price=open_price,
                low_price=low_price,
                limit_price=entry_limit_price or open_price,
            )
            entered = limit_fill is not None and portfolio.enter_long(
                signal_as_of=signal_time,
                entry_time=entry_time,
                raw_price=limit_fill[0],
                available_volume=exit_volume if exit_volume is not None else volume,
                feature_snapshot_id=snapshot_id,
                quantity_limit=quantity_limit,
            )
            if entered:
                if invalidation is None or target is None:
                    raise ValueError("Approved shadow entry requires bracket geometry")
                assert limit_fill is not None
                exit_price, exit_reason = deployable_long_exit(
                    entry_kind=limit_fill[1],
                    open_price=open_price,
                    high_price=high_price,
                    low_price=low_price,
                    close_price=close_price,
                    invalidation=invalidation,
                    target=target,
                )
                portfolio.exit_long(
                    exit_time=exit_time,
                    raw_price=exit_price,
                    available_volume=exit_volume if exit_volume is not None else volume,
                    exit_reason=exit_reason,
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
                        & shadow_deployments.c.status.in_(
                            ("ACTIVE", "LIQUIDATION_PENDING")
                        )
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
