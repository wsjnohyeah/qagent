from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True)


class Direction(StrEnum):
    LONG = "long"
    SHORT = "short"


class Verdict(StrEnum):
    APPROVE = "APPROVE"
    REJECT = "REJECT"


class EventEnvelope(FrozenModel):
    event_id: str
    event_type: str
    event_time: datetime
    emitted_at: datetime
    producer: str
    correlation_id: str
    causation_id: str | None = None
    schema_version: int = 1
    payload: dict[str, Any]


class FeatureSnapshot(FrozenModel):
    feature_snapshot_id: str
    as_of: datetime
    relative_volume: Decimal
    vwap_confirmed: bool
    opening_range_confirmed: bool
    sector_compatible: bool
    quote_age_seconds: int = Field(ge=0)


class StockBar(FrozenModel):
    bar_id: str
    symbol: str
    timeframe: str
    event_time: datetime
    available_from: datetime
    open: Decimal = Field(gt=0)
    high: Decimal = Field(gt=0)
    low: Decimal = Field(gt=0)
    close: Decimal = Field(gt=0)
    volume: int = Field(ge=0)
    trade_count: int | None = Field(default=None, ge=0)
    vwap: Decimal | None = Field(default=None, gt=0)
    source: str
    feed: str
    raw_object_id: str
    ingested_at: datetime


class StockTrade(FrozenModel):
    trade_id: str
    provider_trade_id: str
    symbol: str
    event_time: datetime
    available_from: datetime
    price: Decimal = Field(gt=0)
    size: int = Field(gt=0)
    exchange: str
    conditions: tuple[str, ...]
    tape: str | None = None
    source: str
    feed: str
    raw_object_id: str
    ingested_at: datetime


class StockQuote(FrozenModel):
    quote_id: str
    quote_fingerprint: str
    symbol: str
    event_time: datetime
    available_from: datetime
    bid_exchange: str
    bid_price: Decimal = Field(ge=0)
    bid_size: int = Field(ge=0)
    ask_exchange: str
    ask_price: Decimal = Field(ge=0)
    ask_size: int = Field(ge=0)
    conditions: tuple[str, ...]
    tape: str | None = None
    source: str
    feed: str
    raw_object_id: str
    ingested_at: datetime


class OptionSnapshot(FrozenModel):
    option_snapshot_id: str
    contract_symbol: str
    underlying_symbol: str
    as_of: datetime
    available_from: datetime
    bid_price: Decimal | None = Field(default=None, ge=0)
    bid_size: int | None = Field(default=None, ge=0)
    ask_price: Decimal | None = Field(default=None, ge=0)
    ask_size: int | None = Field(default=None, ge=0)
    last_trade_price: Decimal | None = Field(default=None, ge=0)
    last_trade_size: int | None = Field(default=None, ge=0)
    implied_volatility: Decimal | None = Field(default=None, ge=0)
    delta: Decimal | None = None
    gamma: Decimal | None = None
    theta: Decimal | None = None
    vega: Decimal | None = None
    rho: Decimal | None = None
    source: str
    feed: str
    raw_object_id: str
    ingested_at: datetime


class SourceTier(StrEnum):
    PRIMARY = "primary"
    SECONDARY = "secondary"
    AGGREGATE = "aggregate"


class SourceDocument(FrozenModel):
    document_id: str
    provider_document_id: str
    provider: str
    canonical_url: str
    source_kind: str
    source_tier: SourceTier
    publisher: str
    title: str
    summary: str | None = None
    body_text: str | None = None
    symbols: tuple[str, ...]
    issuer_name: str | None = None
    cik: str | None = None
    published_at: datetime
    updated_at: datetime | None = None
    ingested_at: datetime
    raw_object_id: str


class CorporateFact(FrozenModel):
    fact_id: str
    fact_fingerprint: str
    symbol: str
    cik: str
    issuer_name: str
    taxonomy: str
    tag: str
    unit: str
    period_start: datetime | None = None
    period_end: datetime
    filed_at: datetime
    accepted_at: datetime | None = None
    fiscal_year: int | None = None
    fiscal_period: str | None = None
    form: str
    accession_number: str | None = None
    numeric_value: Decimal | None = None
    value_text: str
    available_from: datetime
    raw_object_id: str
    ingested_at: datetime


class SignalCandidate(FrozenModel):
    candidate_id: str
    symbol: str
    direction: Direction
    setup_type: str
    strategy_version: str
    as_of: datetime
    feature_snapshot_id: str
    catalyst_id: str
    planned_entry: Decimal = Field(gt=0)
    invalidation: Decimal = Field(gt=0)
    targets: tuple[Decimal, ...] = Field(min_length=1)
    expires_at: datetime


class AccountState(FrozenModel):
    equity: Decimal = Field(gt=0)
    daily_pnl: Decimal
    concurrent_planned_risk: Decimal = Field(ge=0)


class RiskDecision(FrozenModel):
    risk_decision_id: str
    candidate_id: str
    verdict: Verdict
    reason_codes: tuple[str, ...]
    account_equity: Decimal
    risk_budget_usd: Decimal
    max_quantity: int = Field(ge=0)
    planned_entry: Decimal
    invalidation: Decimal
    planned_r_multiple_to_t1: Decimal | None
    portfolio_risk_after_usd: Decimal
    policy_version: str
    evaluated_at: datetime


class TradePlan(FrozenModel):
    trade_plan_id: str
    candidate_id: str
    risk_decision_id: str
    symbol: str
    direction: Direction
    quantity: int = Field(gt=0)
    limit_price: Decimal
    invalidation: Decimal
    targets: tuple[Decimal, ...]
    expires_at: datetime


class ShadowOrder(FrozenModel):
    order_id: str
    trade_plan_id: str
    idempotency_key: str
    status: str
    submitted_at: datetime


class DecisionBundle(FrozenModel):
    correlation_id: str
    candidate: SignalCandidate
    risk_decision: RiskDecision
    trade_plan: TradePlan | None = None
    shadow_order: ShadowOrder | None = None
