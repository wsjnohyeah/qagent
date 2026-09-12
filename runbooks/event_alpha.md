# Event Alpha runbook

## Purpose and authority boundary

Event Alpha is the LLM-led, case-based research lane for sparse market events. It runs beside
the existing ML + LLM technical lane. V1 produces Event Cards, realized case outcomes, analog
assessments, and immutable research playbooks. It does not train a predictive event model and
cannot enter Shadow, Paper, Robinhood order submission, or live trading.

The LLM owns semantic normalization and the research hypothesis. Deterministic code owns time
cutoffs, evidence selection, returns, robustness statistics, the research-candidate gate, and
all existing risk boundaries.

## Enablement

Event Alpha is safe-off by default and shares the critical-research USD budget.

```dotenv
AUTONOMOUS_COORDINATOR_ENABLED=true
COORDINATOR_PAID_RESEARCH_ENABLED=true
EVENT_ALPHA_ENABLED=true
EVENT_ALPHA_MAX_CARDS_PER_CYCLE=2
EVENT_ALPHA_MINIMUM_ANALOGS=5
EVENT_ALPHA_MINIMUM_SYMBOLS=3
```

Settings validation rejects Event Alpha unless both the autonomous coordinator and paid research
are enabled. Keep the per-cycle card limit small: one card extraction and one analog synthesis
can each incur a premium-model call. An unchanged case set reuses its prior assessment and does
not spend again because wall-clock time advanced.

## Pipeline

1. Source-specific ingestion archives Alpaca News, SEC, or approved IR evidence and resolves a
   deterministic catalyst.
2. The bounded coordinator selects old cases to grow memory while reserving capacity for the
   newest event.
3. The extraction call creates a strict Event Card with exact source quotations. Invented
   citations, non-verbatim quotes, bad horizons, and malformed output are persisted as rejected.
   Each call is capped at eight source versions, prioritizing primary and recent evidence.
4. When daily bars become causally complete, deterministic code records raw 1/2/5-session price
   reactions from the first session open strictly after the evidence became available. These are
   case-study outcomes, not cost-aware strategy backtests.
5. The current card is compared with prior cards from other symbols whose requested outcome was
   available by the assessment cutoff.
6. If at least five analogs across three symbols exist, a bounded LLM call compares the winning
   and losing cases and may propose a playbook. Deterministic robustness checks then mark it
   `PLAYBOOK_CANDIDATE` or `RESEARCH_ONLY`.

`PLAYBOOK_CANDIDATE` means a hypothesis is worth implementing and replaying. It does not mean
the strategy is validated, Shadow eligible, profitable, or safe to trade.

## Time semantics

- `event_time`: when the underlying event occurred or was published.
- `available_from`: earliest evidence timestamp the Event Alpha decision is allowed to use.
- `FORWARD_FIRST_SEEN`: the system ingested the source within 24 hours and uses the actual stored
  version timestamp.
- `PROVIDER_PUBLISHED_REPLAY`: the provider supplied older evidence later; the case is explicitly
  retrospective and uses the provider publication timestamp only for research replay.
- A corrected historical document that was not observed at the time is excluded rather than
  backdated.
- An analog outcome is invisible until its exit bar is available. Future outcomes cannot enter
  an earlier assessment.

## Deterministic research-candidate gate

For the LLM-selected 1, 2, or 5-session horizon, V1 requires:

- at least `EVENT_ALPHA_MINIMUM_ANALOGS` completed prior events;
- at least `EVENT_ALPHA_MINIMUM_SYMBOLS` other issuers;
- positive median return;
- positive mean after removing the best event;
- profit factor at least 1.10;
- worst analog return no lower than -20%; and
- an LLM `RESEARCH_LONG` recommendation with valid Event Card citations.

A long proposal must cite the current card and at least one supplied analog from its selected
horizon; citing a card visible only in another horizon is rejected.

These are research-memory checks, not a Shadow validation policy. The stored positive rate is
diagnostic evidence rather than a universal win-rate threshold.

## Inspection

Use the Control Center's **Event Alpha** page or these authenticated read endpoints:

```text
GET /v1/event-alpha/status
GET /v1/event-alpha/cards?symbol=AAPL
GET /v1/event-alpha/cards/{event_card_id}
GET /v1/event-alpha/assessments
GET /v1/event-alpha/playbooks
```

The card detail shows the immutable evidence packet and completed outcomes. The assessment shows
the LLM reasoning alongside counts, issuer breadth, median, profit factor, and largest-winner
sensitivity. `GET /v1/llm/invocations/{id}` remains the source of exact prompt/output and usage
lineage.

Development may trigger one bounded cycle with `POST /v1/event-alpha/run`. Production has no
manual mutation endpoint; the dedicated coordinator owns scheduling.

## Failure and recovery

- Budget exhaustion, provider failure, invalid JSON, invalid citations, or insufficient analogs
  fail this sidecar closed without stopping Technical Alpha or existing Shadow accounting.
- Rejected cards are immutable. A materially revised schema/prompt requires a version bump.
- Assessments rerun only when the semantic Event Card/analog set changes.
- Do not manually promote an Event Playbook into a technical `StrategySpec`. Implement the
  event-aware replay and certification boundary first.
