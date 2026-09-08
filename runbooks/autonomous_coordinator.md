# Autonomous research coordinator runbook

## Purpose

The coordinator turns the existing research tools into one restart-safe workflow without
giving an LLM execution authority. With the market scanner disabled, one hourly cycle is
created for each symbol in the governed `trading-universe` list. With it enabled, the cycle is
bound to an immutable scan ID and runs the deep-research DAG over the scanner's bounded
Candidate List.

## Market-discovery funnel

`configs/market_scanner.yaml` defines a reviewed, versioned policy. The scanner merges Alpaca
most-active/mover responses, theme seeds, and Focus Watchlist members; archives every raw
response; fetches snapshots in batches; and applies deterministic price, dollar-volume,
restriction, and benchmark filters. It retains 40 review candidates and selects at most 20.

When `MARKET_SCANNER_LLM_ENABLED=true`, the `routine_pipeline` model may re-rank only those 40
symbols, at most once every four hours. It cannot invent symbols. Budget exhaustion, incomplete
provider output, invalid JSON, or an invented symbol yields `FAILED_FALLBACK` and preserves
the deterministic result. When `MARKET_SCANNER_AUTO_TRADING_POOL_ENABLED=true`, a completed
LLM-reviewed scan also refreshes the bounded `scanner-trading-pool`; an interval skip or failed
review holds the previous pool and admits nothing new. Every pool revision records additions,
removals, scan ID, and LLM invocation. Exact validation and explicit strategy-adoption/Shadow
confirmations still apply, and neither candidate nor pool membership can submit an order.

## Stages

Each hourly group is partitioned by an approved 1, 5, 20, 63, 126, or 252-session
`horizon_bars`. Every stage reconstructs that value from the immutable job payload; do not
infer it from a dependency result or a one-session default. The ML label, forecast, Research
LLM context, generated holding period, replay, validation window, and Shadow contract must
all agree.

1. `collect_market_data`: incrementally ingest Alpaca `1Day` bars. A fresh production store
   starts with `COORDINATOR_INITIAL_LOOKBACK_DAYS` (default 1,826); development is still
   capped by its bounded-data setting. For a newly listed symbol, a fully exhausted leading-
   window probe may persist `market.history.boundary.observed.v1` and validate from the first
   observed bar forward. A post-suspension boundary additionally requires at least 20 missing
   sessions, 20 immediately preceding explicit zero-volume bars, a positive-volume resumption,
   and a fully valid resumed segment. Other internal or trailing gaps still fail.
2. `collect_research_evidence`: advance Alpaca News backward toward the five-year production
   target in bounded 90-day partitions while also refreshing the newest day, so provider
   corrections are captured idempotently. With `SEC_USER_AGENT` configured, the same stage
   refreshes five-year SEC filing metadata and company facts once per symbol per day. A
   completed empty partition is recorded as coverage rather than retried forever.
3. `materialize_features`: create idempotent point-in-time snapshots for completed bars.
4. `train_ml`: train chronological candidates after the configured minimum sample count;
   reuse requires the exact dataset and complete versioned training contract.
5. `forecast_ml`: persist a forecast bound to the selected model and latest snapshot.
6. `research_llm`: retrieve dated evidence and run the budgeted analyst only when
   `COORDINATOR_PAID_RESEARCH_ENABLED=true`.
7. `generate_strategy`: run constrained proposal plus adversarial critique; no model code or
   sizing is accepted.
8. `validate_strategy`: run exact-spec walk-forward validation under the current shared
   account risk contract. Cached admission also requires the current promotion policy and
   current horizon-specific search-trial count. If the paid LLM/generation stage cannot
   produce a new spec in that cycle, rotate through previously accepted immutable specs for
   the same symbol/timeframe/horizon and revalidate them against current data and policy.
9. `await_shadow_adoption`: report the strategy/report IDs and available Candidate/Qualified
   admission tiers, then wait for the administrator's separate `strategy.adopt` and
   `shadow.start` confirmations. Candidate is broker-free observation only and cannot enter
   Alpaca Paper.

## Configuration

```dotenv
AUTONOMOUS_COORDINATOR_ENABLED=true
COORDINATOR_POLL_SECONDS=3600
COORDINATOR_INITIAL_LOOKBACK_DAYS=1826
COORDINATOR_DOCUMENT_LOOKBACK_DAYS=1826
COORDINATOR_DOCUMENT_PARTITION_DAYS=90
COORDINATOR_PAID_RESEARCH_ENABLED=false
MARKET_SCANNER_ENABLED=false
MARKET_SCANNER_LLM_ENABLED=false
MARKET_SCANNER_AUTO_TRADING_POOL_ENABLED=false
MARKET_SCANNER_POLICY_PATH=./configs/market_scanner.yaml
```

Leave paid research false during initial bootstrap. After data coverage, model readiness,
provider routes, and USD budgets have been inspected, changing it to true and restarting the
worker permits the two LLM stages. The LLM still cannot adopt or execute a strategy.

## Inspection and recovery

Use `GET /v1/market-scanner/status`, the Market Scanner page, and the Scanner Trading Pool's
revision history to inspect each admitted/added/removed symbol and its basis invocation. Also use
`GET /v1/coordinator/status`, `GET /v1/workflow-jobs`, and the Pipelines page. Each job
has dependency IDs, attempt count, owner/token lease, result, and error code. A process crash
leaves a lease that is reclaimed after ten minutes. A provider exception marks only that
stage failed and is retried on the next poll; completed parents are never repeated. A final
failed attempt becomes `EXHAUSTED`, is skipped fairly so later groups can recover, and is
retried only after confirmation of `workflow.retry_exhausted` in the Pipelines page.

`WAITING_*` is a healthy business state. Common examples are insufficient history, missing
credentials, paid research disabled, no accepted strategy proposal, insufficient validation
evidence, or pending human confirmation. Do not convert these into silent success or bypass
their gate.

`WAITING_MARKET_HISTORY` means the provider returned no daily history for the requested
symbol. A provider-observed boundary is not an IPO-date fact; inspect its cited ingestion runs
before using it for broader historical-universe claims. Increasing the coordinator lookback
beyond the recorded probe automatically requires a new leading-window query.

Pause `coordinator` in Pipeline Controls before maintenance. The shadow runtime has a
separate control and global new-exposure switch.
