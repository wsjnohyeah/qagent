from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


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

    @model_validator(mode="after")
    def as_of_is_aware(self) -> Self:
        if self.as_of.tzinfo is None:
            raise ValueError("Feature snapshot as_of must be timezone-aware")
        return self


class RiskEvaluationContext(FrozenModel):
    """Externally resolved facts that the deterministic risk gate must not infer."""

    catalyst_required: bool
    catalyst_verified: bool
    restriction_status_known: bool
    liquidity_confirmed: bool
    market_data_healthy: bool
    macro_calendar_status_known: bool
    nearest_major_macro_event_at: datetime | None = None
    macro_event_strategy_approved: bool = False
    duplicate_order_detected: bool = False

    @model_validator(mode="after")
    def timestamps_are_aware(self) -> Self:
        if (
            self.nearest_major_macro_event_at is not None
            and self.nearest_major_macro_event_at.tzinfo is None
        ):
            raise ValueError("Major macro event timestamp must be timezone-aware")
        return self


class SignalAction(StrEnum):
    LONG = "long"
    SHORT = "short"
    FLAT = "flat"


class ExperimentStatus(StrEnum):
    COMPLETED = "COMPLETED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"


class BacktestEventType(StrEnum):
    SIGNAL = "signal"
    ORDER_SUBMITTED = "order_submitted"
    FILL = "fill"
    SPLIT = "split"
    CASH_DIVIDEND = "cash_dividend"
    MARK = "mark"


class MarketRegime(StrEnum):
    UP = "up"
    DOWN = "down"
    SIDEWAYS = "sideways"


class DataQualityStatus(StrEnum):
    PASSED = "PASSED"
    FAILED = "FAILED"


class WorkflowJobStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class LLMProviderName(StrEnum):
    OPENAI = "openai"
    META = "meta"


class LLMWorkload(StrEnum):
    INTERACTIVE_EXPLANATION = "interactive_explanation"
    ROUTINE_PIPELINE = "routine_pipeline"
    CRITICAL_RESEARCH = "critical_research"
    STRATEGY_GENERATION = "strategy_generation"
    STRATEGY_CRITIQUE = "strategy_critique"


class LLMInvocationStatus(StrEnum):
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class ResearchAnalysisStatus(StrEnum):
    COMPLETED = "COMPLETED"
    ABSTAINED = "ABSTAINED"
    REJECTED = "REJECTED"


class ResearchRecommendation(StrEnum):
    RESEARCH_LONG = "RESEARCH_LONG"
    RESEARCH_SHORT = "RESEARCH_SHORT"
    HOLD = "HOLD"
    ABSTAIN = "ABSTAIN"


class MLModelKind(StrEnum):
    LOGISTIC_REGRESSION = "logistic_regression"
    BOOSTED_STUMPS = "boosted_stumps"


class MLModelStatus(StrEnum):
    CANDIDATE = "CANDIDATE"
    CHALLENGER = "CHALLENGER"
    CHAMPION = "CHAMPION"
    RETIRED = "RETIRED"
    REJECTED = "REJECTED"


class LLMRoutingRevision(FrozenModel):
    routing_revision_id: str
    base_routing_version: str
    base_routing_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    routing_version: str
    routing_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    routes: dict[LLMWorkload, LLMProviderName]
    reason: str = Field(min_length=3, max_length=500)
    created_by: str = Field(min_length=1, max_length=80)
    created_at: datetime

    @model_validator(mode="after")
    def routes_are_complete(self) -> Self:
        missing = set(LLMWorkload) - set(self.routes)
        extra = set(self.routes) - set(LLMWorkload)
        if missing or extra:
            raise ValueError("Routing revisions must define every supported workload")
        return self


class LLMUsage(FrozenModel):
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    reasoning_tokens: int = Field(default=0, ge=0)


class LLMInvocation(FrozenModel):
    invocation_id: str
    workload: LLMWorkload
    routing_version: str
    routing_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    code_git_sha: str
    provider: LLMProviderName
    model: str
    reasoning_effort: str
    prompt_version: str
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    response_id: str | None = None
    output_text: str | None = None
    output_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    usage: LLMUsage = LLMUsage()
    latency_ms: int = Field(ge=0)
    status: LLMInvocationStatus
    error_code: str | None = None
    created_at: datetime
    completed_at: datetime


class ResearchEvidenceItem(FrozenModel):
    citation_id: str = Field(min_length=1, max_length=180)
    evidence_type: str = Field(min_length=1, max_length=60)
    event_time: datetime
    available_from: datetime
    source: str = Field(min_length=1, max_length=240)
    text: str = Field(min_length=1, max_length=4_000)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ResearchEvidenceBundle(FrozenModel):
    symbol: str
    as_of: datetime
    feature_snapshot_id: str
    forecast_id: str | None = None
    items: tuple[ResearchEvidenceItem, ...] = Field(min_length=1, max_length=40)
    evidence_bundle_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def evidence_is_point_in_time_safe_and_unique(self) -> Self:
        if self.as_of.tzinfo is None:
            raise ValueError("Research evidence as_of must be timezone-aware")
        if any(
            item.event_time > self.as_of or item.available_from > self.as_of
            for item in self.items
        ):
            raise ValueError("Research evidence bundle contains future information")
        citation_ids = [item.citation_id for item in self.items]
        if len(citation_ids) != len(set(citation_ids)):
            raise ValueError("Research evidence citation IDs must be unique")
        return self


class AnalystClaim(FrozenModel):
    claim: str = Field(min_length=1, max_length=1_000)
    citations: tuple[str, ...] = Field(min_length=1, max_length=12)


class StructuredResearchAnalysis(FrozenModel):
    schema_version: str = Field(pattern=r"^research_analysis@[0-9]+\.[0-9]+\.[0-9]+$")
    symbol: str
    as_of: datetime
    horizon: str = Field(min_length=1, max_length=40)
    recommendation: ResearchRecommendation
    confidence: Decimal = Field(ge=0, le=1)
    thesis: str = Field(min_length=1, max_length=2_000)
    claims: tuple[AnalystClaim, ...] = Field(default=(), max_length=12)
    risk_factors: tuple[str, ...] = Field(default=(), max_length=12)
    ml_assessment: str | None = Field(default=None, max_length=1_000)
    abstain_reason: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def recommendation_has_required_support(self) -> Self:
        if self.recommendation == ResearchRecommendation.ABSTAIN:
            if not self.abstain_reason:
                raise ValueError("ABSTAIN requires abstain_reason")
        elif not self.claims:
            raise ValueError("Non-abstaining analysis requires cited claims")
        return self


class ResearchAnalysisRecord(FrozenModel):
    analysis_id: str
    symbol: str
    as_of: datetime
    status: ResearchAnalysisStatus
    schema_version: str
    prompt_version: str
    evidence_bundle: ResearchEvidenceBundle
    feature_snapshot_id: str
    forecast_id: str | None = None
    llm_invocation_id: str | None = None
    analysis: StructuredResearchAnalysis | None = None
    citation_validation: dict[str, Any]
    rejection_reason: str | None = None
    code_git_sha: str
    created_at: datetime


class CorporateActionType(StrEnum):
    SPLIT = "split"
    CASH_DIVIDEND = "cash_dividend"
    SYMBOL_CHANGE = "symbol_change"


class CorporateAction(FrozenModel):
    corporate_action_id: str
    action_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    symbol: str
    action_type: CorporateActionType
    effective_at: datetime
    available_from: datetime
    split_ratio: Decimal | None = Field(default=None, gt=0)
    cash_amount: Decimal | None = Field(default=None, ge=0)
    currency: str | None = None
    new_symbol: str | None = None
    source: str
    raw_object_id: str
    ingested_at: datetime

    @model_validator(mode="after")
    def action_payload_matches_type(self) -> Self:
        if self.action_type == CorporateActionType.SPLIT and self.split_ratio is None:
            raise ValueError("Split actions require split_ratio")
        if self.action_type == CorporateActionType.CASH_DIVIDEND and self.cash_amount is None:
            raise ValueError("Cash-dividend actions require cash_amount")
        if self.action_type == CorporateActionType.SYMBOL_CHANGE and not self.new_symbol:
            raise ValueError("Symbol-change actions require new_symbol")
        return self


class UniverseMembership(FrozenModel):
    membership_id: str
    universe: str
    symbol: str
    effective_from: datetime
    effective_to: datetime | None = None
    available_from: datetime
    source: str
    source_version: str
    created_at: datetime

    @model_validator(mode="after")
    def membership_interval_is_valid(self) -> Self:
        if self.effective_to is not None and self.effective_to <= self.effective_from:
            raise ValueError("Universe membership effective_to must follow effective_from")
        return self


class EvidenceReference(FrozenModel):
    evidence_type: str
    evidence_id: str
    event_time: datetime
    available_from: datetime
    source: str


class EvidencePacket(FrozenModel):
    evidence_packet_id: str
    symbol: str
    as_of: datetime
    evidence_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    references: tuple[EvidenceReference, ...]
    source_max_available_from: datetime
    created_at: datetime

    @model_validator(mode="after")
    def evidence_is_point_in_time_safe(self) -> Self:
        if self.source_max_available_from > self.as_of:
            raise ValueError("Evidence packet contains information unavailable at as_of")
        if any(reference.available_from > self.as_of for reference in self.references):
            raise ValueError("Evidence reference is unavailable at packet as_of")
        if any(reference.event_time > self.as_of for reference in self.references):
            raise ValueError("Evidence reference occurs after packet as_of")
        return self


class PointInTimeFeatureSnapshot(FrozenModel):
    feature_snapshot_id: str
    evidence_packet_id: str
    symbol: str
    timeframe: str
    as_of: datetime
    feature_set_version: str
    values: dict[str, Decimal | int | bool | str | None]
    source_max_available_from: datetime
    data_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime

    @model_validator(mode="after")
    def features_are_point_in_time_safe(self) -> Self:
        if self.source_max_available_from > self.as_of:
            raise ValueError("Feature snapshot contains information unavailable at as_of")
        return self


class Forecast(FrozenModel):
    forecast_id: str
    symbol: str
    as_of: datetime
    horizon: str
    expected_return: Decimal
    probability_up: Decimal = Field(ge=0, le=1)
    uncertainty: Decimal = Field(ge=0)
    model_version: str
    training_data_cutoff: datetime
    feature_snapshot_id: str
    created_at: datetime

    @model_validator(mode="after")
    def training_cutoff_is_safe(self) -> Self:
        if self.training_data_cutoff > self.as_of:
            raise ValueError("Forecast training data cutoff cannot be after as_of")
        return self


class MLModelVersion(FrozenModel):
    model_id: str
    training_run_id: str
    model_name: str
    model_version: str
    kind: MLModelKind
    symbol: str
    timeframe: str
    horizon_bars: int = Field(ge=1)
    feature_set_version: str
    feature_names: tuple[str, ...] = Field(min_length=1)
    training_start: datetime
    training_end: datetime
    training_data_cutoff: datetime
    artifact: dict[str, Any]
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    metrics: dict[str, Any]
    calibration: dict[str, Any]
    drift: dict[str, Any]
    promotion_assessment: dict[str, Any]
    status: MLModelStatus
    code_git_sha: str
    created_at: datetime

    @model_validator(mode="after")
    def training_window_is_valid(self) -> Self:
        if self.training_start >= self.training_end:
            raise ValueError("ML training start must precede end")
        if self.training_end > self.training_data_cutoff:
            raise ValueError("ML training end cannot exceed its data cutoff")
        return self


class MLTrainingRun(FrozenModel):
    training_run_id: str
    symbol: str
    timeframe: str
    horizon_bars: int = Field(ge=1)
    feature_set_version: str
    dataset_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    sample_count: int = Field(ge=1)
    fold_count: int = Field(ge=1)
    embargo_bars: int = Field(ge=1)
    model_ids: tuple[str, ...] = Field(min_length=1)
    selected_model_id: str
    selection_metric: str
    status: str
    code_git_sha: str
    started_at: datetime
    finished_at: datetime

    @model_validator(mode="after")
    def run_lineage_is_consistent(self) -> Self:
        if self.selected_model_id not in self.model_ids:
            raise ValueError("Selected ML model must belong to the training run")
        if len(self.model_ids) != len(set(self.model_ids)):
            raise ValueError("ML training run model IDs must be unique")
        if self.finished_at < self.started_at:
            raise ValueError("ML training finish cannot precede start")
        return self


class ModelRegistryEvent(FrozenModel):
    registry_event_id: str
    model_id: str
    previous_status: MLModelStatus
    new_status: MLModelStatus
    training_run_id: str
    approved_by: str
    reason: str
    created_at: datetime


class ResearchSignal(FrozenModel):
    signal_id: str
    symbol: str
    as_of: datetime
    horizon: str
    action: SignalAction
    conviction: Decimal = Field(ge=0, le=1)
    expected_return: Decimal | None = None
    uncertainty: Decimal | None = Field(default=None, ge=0)
    strategy_version: str
    feature_snapshot_id: str
    evidence_ids: tuple[str, ...]


class StrategySpec(FrozenModel):
    strategy_spec_id: str
    name: str
    version: str
    strategy_type: str
    timeframe: str
    feature_set_version: str
    parameters: dict[str, Any]
    data_requirements: dict[str, Any]
    code_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime


class BacktestCostModel(FrozenModel):
    commission_per_share: Decimal = Field(default=Decimal("0.0049"), ge=0)
    minimum_commission_per_order: Decimal = Field(default=Decimal("0.99"), ge=0)
    slippage_bps_per_side: Decimal = Field(default=Decimal("2.0"), ge=0)
    half_spread_bps_per_side: Decimal = Field(default=Decimal("1.0"), ge=0)
    market_impact_bps_per_side: Decimal = Field(default=Decimal("1.0"), ge=0)
    max_volume_participation: Decimal = Field(
        default=Decimal("0.05"),
        gt=0,
        le=1,
    )


class BacktestMetrics(FrozenModel):
    initial_equity: Decimal = Field(gt=0)
    final_equity: Decimal = Field(ge=0)
    net_profit: Decimal
    total_return: Decimal
    annualized_return: Decimal
    sharpe_ratio: Decimal
    sortino_ratio: Decimal
    max_drawdown: Decimal = Field(le=0)
    trade_count: int = Field(ge=0)
    win_rate: Decimal = Field(ge=0, le=1)
    turnover: Decimal = Field(ge=0)
    total_cost: Decimal = Field(ge=0)


class BacktestTrade(FrozenModel):
    trade_id: str
    experiment_run_id: str
    symbol: str
    action: SignalAction
    signal_as_of: datetime
    entry_time: datetime
    exit_time: datetime
    quantity: int = Field(gt=0)
    exit_quantity: Decimal | None = Field(default=None, gt=0)
    entry_price: Decimal = Field(gt=0)
    exit_price: Decimal = Field(gt=0)
    gross_pnl: Decimal
    corporate_action_cash: Decimal = Field(default=Decimal("0"), ge=0)
    transaction_cost: Decimal = Field(ge=0)
    net_pnl: Decimal
    feature_snapshot_id: str
    exit_reason: str

    @model_validator(mode="after")
    def execution_follows_signal(self) -> Self:
        if self.entry_time < self.signal_as_of:
            raise ValueError("Trade entry cannot precede signal availability")
        if self.exit_time < self.entry_time:
            raise ValueError("Trade exit cannot precede entry")
        return self


class BacktestPortfolioEvent(FrozenModel):
    portfolio_event_id: str
    experiment_run_id: str
    sequence: int = Field(ge=1)
    event_type: BacktestEventType
    event_time: datetime
    symbol: str
    cash_balance: Decimal = Field(ge=0)
    position_quantity: Decimal = Field(ge=0)
    cash_delta: Decimal = Decimal("0")
    quantity_delta: Decimal = Decimal("0")
    price: Decimal | None = Field(default=None, gt=0)
    corporate_action_id: str | None = None
    feature_snapshot_id: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class ExperimentRun(FrozenModel):
    experiment_run_id: str
    strategy_spec_id: str
    symbol: str
    timeframe: str
    as_of_start: datetime
    as_of_end: datetime
    dataset_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    code_git_sha: str
    status: ExperimentStatus
    cost_model: BacktestCostModel
    metrics: BacktestMetrics
    feature_snapshot_ids: tuple[str, ...]
    started_at: datetime
    finished_at: datetime

    @model_validator(mode="after")
    def run_interval_is_valid(self) -> Self:
        if self.as_of_start >= self.as_of_end:
            raise ValueError("Experiment as_of_start must be before as_of_end")
        if self.finished_at < self.started_at:
            raise ValueError("Experiment finished_at cannot precede started_at")
        return self


class BacktestResult(FrozenModel):
    experiment: ExperimentRun
    strategy_spec: StrategySpec
    trades: tuple[BacktestTrade, ...]
    portfolio_events: tuple[BacktestPortfolioEvent, ...] = ()


class WalkForwardFold(FrozenModel):
    validation_fold_id: str
    fold_number: int = Field(ge=1)
    train_start: datetime
    train_end: datetime
    test_start: datetime
    test_end: datetime
    selected_strategy: str
    selection_metric: str
    train_experiment_ids: dict[str, str]
    test_experiment_ids: dict[str, str]
    selected_train_metrics: BacktestMetrics
    selected_test_metrics: BacktestMetrics
    selected_test_rank: int = Field(ge=1)
    regime: MarketRegime

    @model_validator(mode="after")
    def windows_are_chronological(self) -> Self:
        if not self.train_start < self.train_end < self.test_start < self.test_end:
            raise ValueError("Walk-forward train/embargo/test windows must be chronological")
        return self


class WalkForwardValidationReport(FrozenModel):
    validation_report_id: str
    symbol: str
    timeframe: str
    strategy_types: tuple[str, ...]
    selection_metric: str
    train_bars: int = Field(ge=22)
    test_bars: int = Field(ge=1)
    step_bars: int = Field(ge=1)
    embargo_bars: int = Field(ge=1)
    folds: tuple[WalkForwardFold, ...] = Field(min_length=1)
    aggregate_metrics: dict[str, Decimal | int | str]
    regime_metrics: dict[str, dict[str, Decimal | int | str]]
    robustness_metrics: dict[str, Any]
    gate_assessment: dict[str, Any]
    report_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    code_git_sha: str
    created_at: datetime


class FeatureParityCheck(FrozenModel):
    parity_check_id: str
    symbol: str
    timeframe: str
    as_of: datetime
    feature_set_version: str
    offline_data_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    online_data_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    matched: bool
    checked_at: datetime

    @model_validator(mode="after")
    def match_flag_agrees_with_hashes(self) -> Self:
        if self.matched != (self.offline_data_hash == self.online_data_hash):
            raise ValueError("Feature parity flag must agree with the compared hashes")
        return self


class DataQualityReport(FrozenModel):
    data_quality_report_id: str
    ruleset_version: str
    dataset_type: str
    symbol: str
    timeframe: str
    window_start: datetime | None = None
    window_end: datetime | None = None
    record_count: int = Field(ge=0)
    data_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    scope_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: DataQualityStatus
    checks: dict[str, bool | int | str]
    issue_counts: dict[str, int]
    code_git_sha: str
    created_at: datetime


class WorkflowJob(FrozenModel):
    workflow_job_id: str
    job_group_id: str
    job_type: str
    partition_key: str
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    payload: dict[str, Any]
    status: WorkflowJobStatus
    attempt_count: int = Field(ge=0)
    max_attempts: int = Field(ge=1)
    cursor: dict[str, Any]
    result: dict[str, Any]
    error_code: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    updated_at: datetime
    completed_at: datetime | None = None


class ReferenceImportResult(FrozenModel):
    reference_import_id: str
    dataset_type: str
    source: str
    source_version: str
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    records_received: int = Field(ge=0)
    records_inserted: int = Field(ge=0)
    status: str
    created_at: datetime


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

    @model_validator(mode="after")
    def timing_is_aware_and_ordered(self) -> Self:
        if self.as_of.tzinfo is None or self.expires_at.tzinfo is None:
            raise ValueError("Signal timestamps must be timezone-aware")
        if self.expires_at <= self.as_of:
            raise ValueError("Signal expiry must follow its as_of timestamp")
        return self


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
