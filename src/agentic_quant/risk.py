from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, ROUND_FLOOR
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from agentic_quant.config import TradingMode
from agentic_quant.domain import (
    AccountState,
    Direction,
    FeatureSnapshot,
    RiskDecision,
    RiskEvaluationContext,
    SignalCandidate,
    Verdict,
)
from agentic_quant.ids import uuid7


_ONE = Decimal("1")


class RiskPolicy(BaseModel):
    model_config = ConfigDict(frozen=True)

    version: str
    baseline_stop_fraction: Decimal = Field(gt=0, lt=1)
    baseline_target_r_multiple: Decimal = Field(gt=0)
    minimum_reward_risk: Decimal = Field(gt=0)
    minimum_relative_volume: Decimal = Field(ge=0)
    maximum_quote_age_seconds: int = Field(ge=0)
    macro_event_blackout_hours: int = Field(ge=0, le=168)
    initial_risk_fraction: Decimal = Field(gt=0, le=1)
    maximum_trade_risk_usd: Decimal = Field(gt=0)
    maximum_concurrent_risk_usd: Decimal = Field(gt=0)
    daily_loss_stop_usd: Decimal = Field(gt=0)
    account_floor_usd: Decimal = Field(gt=0)
    slippage_buffer_per_share_usd: Decimal = Field(ge=0)
    maximum_equity_quantity: int = Field(ge=1)
    allowed_execution_modes: tuple[TradingMode, ...] = Field(min_length=1)

    @classmethod
    def from_yaml(cls, path: Path) -> RiskPolicy:
        return cls.model_validate(yaml.safe_load(path.read_text()))

    @model_validator(mode="after")
    def limits_are_coherent(self) -> RiskPolicy:
        if self.maximum_trade_risk_usd > self.maximum_concurrent_risk_usd:
            raise ValueError("Per-trade risk cannot exceed concurrent portfolio risk")
        return self


BASELINE_EXECUTION_PROFILE_VERSION = "next_open_bracket_one_bar@0.1.0"


def baseline_long_geometry(
    reference_price: Decimal,
    policy: RiskPolicy,
) -> tuple[Decimal, Decimal]:
    invalidation = reference_price * (_ONE - policy.baseline_stop_fraction)
    target = reference_price + (
        reference_price - invalidation
    ) * policy.baseline_target_r_multiple
    return invalidation, target


def baseline_long_exit(
    *,
    open_price: Decimal,
    high_price: Decimal,
    low_price: Decimal,
    close_price: Decimal,
    invalidation: Decimal,
    target: Decimal,
) -> tuple[Decimal, str]:
    """Resolve a one-bar bracket conservatively when intrabar order is unknown."""
    if open_price <= invalidation:
        return open_price, "gap_through_stop"
    if open_price >= target:
        return open_price, "gap_through_target"
    if low_price <= invalidation:
        return invalidation, "protective_stop"
    if high_price >= target:
        return target, "profit_target"
    return close_price, "session_close"


class Restriction(BaseModel):
    symbol: str
    reason: str
    effective_from: date
    effective_to: date | None = None


class RestrictionRegistry(BaseModel):
    version: str
    default_unknown_status: str
    restricted: tuple[Restriction, ...]

    @classmethod
    def from_yaml(cls, path: Path) -> RestrictionRegistry:
        return cls.model_validate(yaml.safe_load(path.read_text()))

    def is_restricted(self, symbol: str, as_of: date) -> bool:
        normalized = symbol.upper()
        return any(
            item.symbol.upper() == normalized
            and item.effective_from <= as_of
            and (item.effective_to is None or as_of <= item.effective_to)
            for item in self.restricted
        )


def _reward_and_risk(candidate: SignalCandidate, slippage: Decimal) -> tuple[Decimal, Decimal]:
    target = candidate.targets[0]
    if candidate.direction == Direction.LONG:
        return (
            target - candidate.planned_entry,
            candidate.planned_entry - candidate.invalidation + slippage,
        )
    return (
        candidate.planned_entry - target,
        candidate.invalidation - candidate.planned_entry + slippage,
    )


def evaluate_candidate(
    *,
    candidate: SignalCandidate,
    features: FeatureSnapshot,
    account: AccountState,
    mode: TradingMode,
    policy: RiskPolicy,
    restrictions: RestrictionRegistry,
    context: RiskEvaluationContext,
    evaluated_at: datetime,
    new_exposure_paused: bool,
) -> RiskDecision:
    """Deterministically approve or reject a candidate; no LLM input is authoritative here."""
    if evaluated_at.tzinfo is None:
        raise ValueError("Risk evaluation timestamp must be timezone-aware")
    reasons: list[str] = []
    if new_exposure_paused:
        reasons.append("GLOBAL_NEW_EXPOSURE_PAUSED")
    if mode not in policy.allowed_execution_modes:
        reasons.append("EXECUTION_MODE_NOT_ALLOWED")
    tactical = context.evaluation_profile == "tactical_intraday"
    if tactical and context.catalyst_required and not context.catalyst_verified:
        reasons.append("CATALYST_UNVERIFIED")
    if not context.restriction_status_known:
        reasons.append("RESTRICTION_STATUS_UNKNOWN")
    if candidate.feature_snapshot_id != features.feature_snapshot_id:
        reasons.append("FEATURE_SNAPSHOT_MISMATCH")
    if candidate.as_of > evaluated_at:
        reasons.append("SIGNAL_FROM_FUTURE")
    if features.as_of > evaluated_at:
        reasons.append("FEATURE_FROM_FUTURE")
    if restrictions.is_restricted(
        candidate.symbol,
        evaluated_at.astimezone(UTC).date(),
    ):
        reasons.append("SECURITY_RESTRICTED")
    if not context.liquidity_confirmed:
        reasons.append("LIQUIDITY_NOT_CONFIRMED")
    if not context.market_data_healthy:
        reasons.append("MARKET_DATA_UNHEALTHY")
    if tactical and not context.macro_calendar_status_known:
        reasons.append("MACRO_CALENDAR_STATUS_UNKNOWN")
    if (
        tactical
        and context.nearest_major_macro_event_at is not None
        and not context.macro_event_strategy_approved
        and abs(context.nearest_major_macro_event_at - evaluated_at)
        <= timedelta(hours=policy.macro_event_blackout_hours)
    ):
        reasons.append("MACRO_EVENT_BLACKOUT")
    if context.duplicate_order_detected:
        reasons.append("DUPLICATE_ORDER")
    if account.equity <= policy.account_floor_usd:
        reasons.append("ACCOUNT_FLOOR_REACHED")
    if account.daily_pnl <= -policy.daily_loss_stop_usd:
        reasons.append("DAILY_LOSS_HALT")
    if account.concurrent_planned_risk >= policy.maximum_concurrent_risk_usd:
        reasons.append("PORTFOLIO_RISK_LIMIT")
    if tactical:
        if features.relative_volume < policy.minimum_relative_volume:
            reasons.append("RELATIVE_VOLUME_TOO_LOW")
        if not features.vwap_confirmed:
            reasons.append("VWAP_NOT_CONFIRMED")
        if not features.opening_range_confirmed:
            reasons.append("OPENING_RANGE_NOT_CONFIRMED")
        if not features.sector_compatible:
            reasons.append("SECTOR_CONFLICT")
        if features.quote_age_seconds > policy.maximum_quote_age_seconds:
            reasons.append("STALE_QUOTE")
    if evaluated_at > candidate.expires_at:
        reasons.append("SIGNAL_EXPIRED")

    reward, per_share_risk = _reward_and_risk(candidate, policy.slippage_buffer_per_share_usd)
    if reward <= 0 or per_share_risk <= 0:
        reasons.append("INVALID_PRICE_GEOMETRY")
        reward_risk = None
    else:
        reward_risk = (reward / per_share_risk).quantize(Decimal("0.001"))
        if reward_risk < policy.minimum_reward_risk:
            reasons.append("REWARD_RISK_TOO_LOW")

    remaining_portfolio_risk = max(
        Decimal("0"), policy.maximum_concurrent_risk_usd - account.concurrent_planned_risk
    )
    risk_budget = min(
        account.equity * policy.initial_risk_fraction,
        policy.maximum_trade_risk_usd,
        remaining_portfolio_risk,
    ).quantize(Decimal("0.01"))
    quantity = 0
    if per_share_risk > 0 and risk_budget > 0:
        quantity = min(
            policy.maximum_equity_quantity,
            int((risk_budget / per_share_risk).to_integral_value(rounding=ROUND_FLOOR)),
        )
    if quantity < 1:
        reasons.append("SIZE_ROUNDS_TO_ZERO")

    verdict = Verdict.REJECT if reasons else Verdict.APPROVE
    reason_codes: tuple[str, ...]
    if verdict == Verdict.APPROVE:
        approved_reasons = [
            "RESTRICTION_STATUS_VERIFIED",
            "LIQUIDITY_OK",
            "MARKET_DATA_HEALTHY",
            "MACRO_CALENDAR_VERIFIED",
            "PRICE_CONFIRMATION_OK",
            "RR_OK",
        ]
        if not tactical:
            approved_reasons = [
                "BASELINE_SHADOW_PROFILE",
                "RESTRICTION_STATUS_VERIFIED",
                "LIQUIDITY_OK",
                "MARKET_DATA_HEALTHY",
                "ACCOUNT_LIMITS_OK",
                "RR_OK",
            ]
        if context.catalyst_required:
            approved_reasons.insert(0, "CATALYST_VERIFIED")
        reason_codes = tuple(approved_reasons)
        approved_risk = (per_share_risk * quantity).quantize(Decimal("0.01"))
    else:
        reason_codes = tuple(dict.fromkeys(reasons))
        quantity = 0
        approved_risk = Decimal("0")

    return RiskDecision(
        risk_decision_id=uuid7(),
        candidate_id=candidate.candidate_id,
        verdict=verdict,
        reason_codes=reason_codes,
        account_equity=account.equity,
        risk_budget_usd=risk_budget,
        max_quantity=quantity,
        planned_entry=candidate.planned_entry,
        invalidation=candidate.invalidation,
        planned_r_multiple_to_t1=reward_risk,
        portfolio_risk_after_usd=(account.concurrent_planned_risk + approved_risk).quantize(
            Decimal("0.01")
        ),
        policy_version=policy.version,
        evaluated_at=evaluated_at,
    )
