from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, ROUND_FLOOR
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict

from agentic_quant.config import TradingMode
from agentic_quant.domain import (
    AccountState,
    Direction,
    FeatureSnapshot,
    RiskDecision,
    SignalCandidate,
    Verdict,
)
from agentic_quant.ids import uuid7


class RiskPolicy(BaseModel):
    model_config = ConfigDict(frozen=True)

    version: str
    minimum_reward_risk: Decimal
    minimum_relative_volume: Decimal
    maximum_quote_age_seconds: int
    initial_risk_fraction: Decimal
    maximum_trade_risk_usd: Decimal
    maximum_concurrent_risk_usd: Decimal
    daily_loss_stop_usd: Decimal
    account_floor_usd: Decimal
    slippage_buffer_per_share_usd: Decimal
    maximum_equity_quantity: int
    allowed_execution_modes: tuple[TradingMode, ...]

    @classmethod
    def from_yaml(cls, path: Path) -> RiskPolicy:
        return cls.model_validate(yaml.safe_load(path.read_text()))


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
    evaluated_at: datetime,
    new_exposure_paused: bool,
) -> RiskDecision:
    """Deterministically approve or reject a candidate; no LLM input is authoritative here."""
    reasons: list[str] = []
    if new_exposure_paused:
        reasons.append("GLOBAL_NEW_EXPOSURE_PAUSED")
    if mode not in policy.allowed_execution_modes:
        reasons.append("EXECUTION_MODE_NOT_ALLOWED")
    if restrictions.is_restricted(candidate.symbol, evaluated_at.date()):
        reasons.append("SECURITY_RESTRICTED")
    if account.equity <= policy.account_floor_usd:
        reasons.append("ACCOUNT_FLOOR_REACHED")
    if account.daily_pnl <= -policy.daily_loss_stop_usd:
        reasons.append("DAILY_LOSS_HALT")
    if account.concurrent_planned_risk >= policy.maximum_concurrent_risk_usd:
        reasons.append("PORTFOLIO_RISK_LIMIT")
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
        reason_codes = ("CATALYST_VERIFIED", "LIQUIDITY_OK", "PRICE_CONFIRMATION_OK", "RR_OK")
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
