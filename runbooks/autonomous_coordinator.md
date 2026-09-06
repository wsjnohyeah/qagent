# Autonomous research coordinator runbook

## Purpose

The coordinator turns the existing research tools into one restart-safe workflow without
giving an LLM execution authority. One hourly cycle is identified by UTC hour and one DAG is
created for each symbol in the governed `trading-universe` list.

## Stages

1. `collect_market_data`: incrementally ingest Alpaca `1Day` bars. A fresh production store
   starts with `COORDINATOR_INITIAL_LOOKBACK_DAYS` (default 1,826); development is still
   capped by its bounded-data setting.
2. `materialize_features`: create idempotent point-in-time snapshots for completed bars.
3. `train_ml`: train chronological candidates after the configured minimum sample count.
4. `forecast_ml`: persist a forecast bound to the selected model and latest snapshot.
5. `research_llm`: retrieve dated evidence and run the budgeted analyst only when
   `COORDINATOR_PAID_RESEARCH_ENABLED=true`.
6. `generate_strategy`: run constrained proposal plus adversarial critique; no model code or
   sizing is accepted.
7. `validate_strategy`: run exact-spec walk-forward validation under the current shared
   account risk contract.
8. `await_shadow_adoption`: report the strategy/report IDs and wait for the administrator's
   separate `strategy.adopt` and `shadow.start` confirmations.

## Configuration

```dotenv
AUTONOMOUS_COORDINATOR_ENABLED=true
COORDINATOR_POLL_SECONDS=3600
COORDINATOR_INITIAL_LOOKBACK_DAYS=1826
COORDINATOR_PAID_RESEARCH_ENABLED=false
```

Leave paid research false during initial bootstrap. After data coverage, model readiness,
provider routes, and USD budgets have been inspected, changing it to true and restarting the
worker permits the two LLM stages. The LLM still cannot adopt or execute a strategy.

## Inspection and recovery

Use `GET /v1/coordinator/status`, `GET /v1/workflow-jobs`, and the Pipelines page. Each job
has dependency IDs, attempt count, owner/token lease, result, and error code. A process crash
leaves a lease that is reclaimed after ten minutes. A provider exception marks only that
stage failed and is retried on the next poll; completed parents are never repeated.

`WAITING_*` is a healthy business state. Common examples are insufficient history, missing
credentials, paid research disabled, no accepted strategy proposal, insufficient validation
evidence, or pending human confirmation. Do not convert these into silent success or bypass
their gate.

Pause `coordinator` in Pipeline Controls before maintenance. The shadow runtime has a
separate control and global new-exposure switch.
