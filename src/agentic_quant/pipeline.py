from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from agentic_quant.config import Settings
from agentic_quant.domain import (
    AccountState,
    DecisionBundle,
    Direction,
    EventEnvelope,
    FeatureSnapshot,
    RiskEvaluationContext,
    ShadowOrder,
    SignalCandidate,
    TradePlan,
    Verdict,
)
from agentic_quant.ids import uuid7
from agentic_quant.ledger import EventLedger
from agentic_quant.risk import RestrictionRegistry, RiskPolicy, evaluate_candidate


def _event(
    *,
    event_type: str,
    event_time: datetime,
    producer: str,
    correlation_id: str,
    payload: dict[str, Any],
    causation_id: str | None = None,
) -> EventEnvelope:
    return EventEnvelope(
        event_id=uuid7(),
        event_type=event_type,
        event_time=event_time,
        emitted_at=event_time,
        producer=producer,
        correlation_id=correlation_id,
        causation_id=causation_id,
        payload=payload,
    )


def run_synthetic_vertical_slice(
    *,
    settings: Settings,
    ledger: EventLedger,
    new_exposure_paused: bool,
    as_of: datetime | None = None,
) -> DecisionBundle:
    """Replay one deterministic, synthetic candidate through the safety boundary."""
    now = (as_of or datetime.now(UTC)).astimezone(UTC)
    correlation_id = uuid7()
    catalyst_id = uuid7()

    catalyst_event = _event(
        event_type="catalyst.normalized.v1",
        event_time=now,
        producer="synthetic-replay",
        correlation_id=correlation_id,
        payload={
            "catalyst_id": catalyst_id,
            "symbol": "DEMO",
            "source": "synthetic://phase-0/catalyst-1",
            "verified": True,
        },
    )
    ledger.append(catalyst_event)

    features = FeatureSnapshot(
        feature_snapshot_id=uuid7(),
        as_of=now,
        relative_volume=Decimal("2.50"),
        vwap_confirmed=True,
        opening_range_confirmed=True,
        sector_compatible=True,
        quote_age_seconds=1,
    )
    feature_event = _event(
        event_type="feature.snapshot.created.v1",
        event_time=now,
        producer="feature-worker",
        correlation_id=correlation_id,
        causation_id=catalyst_event.event_id,
        payload=features.model_dump(mode="json"),
    )
    ledger.append(feature_event)

    candidate = SignalCandidate(
        candidate_id=uuid7(),
        symbol="DEMO",
        direction=Direction.LONG,
        setup_type="event_pullback_continuation",
        strategy_version="event_pullback_continuation@0.1.0",
        as_of=now,
        feature_snapshot_id=features.feature_snapshot_id,
        catalyst_id=catalyst_id,
        planned_entry=Decimal("17.50"),
        invalidation=Decimal("16.65"),
        targets=(Decimal("18.90"), Decimal("20.20")),
        expires_at=now + timedelta(hours=1),
    )
    candidate_event = _event(
        event_type="signal.candidate.created.v1",
        event_time=now,
        producer="strategy-worker",
        correlation_id=correlation_id,
        causation_id=feature_event.event_id,
        payload=candidate.model_dump(mode="json"),
    )
    ledger.append(candidate_event)

    policy = RiskPolicy.from_yaml(settings.risk_policy_path)
    restrictions = RestrictionRegistry.from_yaml(settings.restricted_securities_path)
    risk_context = RiskEvaluationContext(
        catalyst_required=True,
        catalyst_verified=True,
        restriction_status_known=True,
        liquidity_confirmed=True,
        market_data_healthy=True,
        macro_calendar_status_known=True,
    )
    decision = evaluate_candidate(
        candidate=candidate,
        features=features,
        account=AccountState(
            equity=Decimal("52000.00"),
            daily_pnl=Decimal("0.00"),
            concurrent_planned_risk=Decimal("0.00"),
        ),
        mode=settings.trading_mode,
        policy=policy,
        restrictions=restrictions,
        context=risk_context,
        evaluated_at=now,
        new_exposure_paused=new_exposure_paused,
    )
    decision_event = _event(
        event_type="risk.decision.created.v1",
        event_time=now,
        producer="risk-worker",
        correlation_id=correlation_id,
        causation_id=candidate_event.event_id,
        payload={
            **decision.model_dump(mode="json"),
            "evaluation_context": risk_context.model_dump(mode="json"),
        },
    )
    ledger.append(decision_event)

    plan: TradePlan | None = None
    order: ShadowOrder | None = None
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
        plan_event = _event(
            event_type="trade.plan.approved.v1",
            event_time=now,
            producer="risk-worker",
            correlation_id=correlation_id,
            causation_id=decision_event.event_id,
            payload=plan.model_dump(mode="json"),
        )
        ledger.append(plan_event)
        if settings.trading_mode.value == "shadow":
            order = ShadowOrder(
                order_id=uuid7(),
                trade_plan_id=plan.trade_plan_id,
                idempotency_key=f"shadow:local:{plan.trade_plan_id}:entry:1",
                status="RECORDED_NOT_SUBMITTED",
                submitted_at=now,
            )
            ledger.append(
                _event(
                    event_type="order.state.changed.v1",
                    event_time=now,
                    producer="shadow-executor",
                    correlation_id=correlation_id,
                    causation_id=plan_event.event_id,
                    payload=order.model_dump(mode="json"),
                )
            )
    return DecisionBundle(
        correlation_id=correlation_id,
        candidate=candidate,
        risk_decision=decision,
        trade_plan=plan,
        shadow_order=order,
    )
