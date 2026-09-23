# Event Alpha runbook

## Purpose and authority boundary

Event Alpha is the news-first, LLM-led case-research lane for sparse market events. It runs
beside the ML + LLM technical lane. It creates point-in-time Event Episodes and Cards, measures
their later returns, proposes cross-stock Playbooks, validates those Playbooks on later unseen
events, and can admit an exact future-news match to isolated broker-free Candidate Shadow.

The LLM owns semantic normalization, analogy, and the hypothesis. Deterministic code owns source
selection, time cutoffs, returns, validation, match thresholds, position sizing, restrictions,
stops, targets, idempotency, and the sandbox circuit breaker. Event strategies cannot enter
Paper, Robinhood order submission, or live trading.

## Enablement

Event Alpha is safe-off by default. When enabled, Card extraction and analog synthesis use the
dedicated `event_research` workload, currently routed to Meta Muse Spark with a `$5/day`
workload ceiling. All active LLM routes currently use Meta; workloads compete under the shared
`$10/day` provider/project ceiling. The project monthly ceiling is `$1,400`.

```dotenv
AUTONOMOUS_COORDINATOR_ENABLED=true
COORDINATOR_PAID_RESEARCH_ENABLED=true
COORDINATOR_AUTO_SHADOW_ENABLED=true
EVENT_ALPHA_ENABLED=true
EVENT_ALPHA_MAX_CARDS_PER_CYCLE=2
EVENT_ALPHA_MINIMUM_ANALOGS=5
EVENT_ALPHA_MINIMUM_SYMBOLS=3
```

Settings reject Event Alpha unless the autonomous coordinator and paid research are enabled.
Event Shadow admission also requires the existing automatic broker-free Shadow switch. Keep the
per-cycle Card bound small: a new Card and a viable analog synthesis may each incur a model call.
Unchanged inputs reuse immutable prior results and do not spend again just because time advanced.

## End-to-end pipeline

1. The coordinator incrementally backfills and refreshes Alpaca News for governed scanner
   symbols. SEC and IR remain separate evidence datasets and do not enter Event Alpha.
2. Same-symbol, same deterministic-type news documents with substantially similar headlines are
   deduplicated into a 36-hour Event Episode (`catalyst`). Up to eight point-in-time news versions
   form one bounded evidence packet.
3. Meta extracts a cited `event_card@0.2.0`: event type, direction, causal mechanism, generalized
   tags, and expected 1/2/5/10/20-session horizons. Missing news, corrected backfill that was not
   known at the time, malformed output, and invented quotations fail closed.
4. Deterministic code measures raw next-session-open to 1/2/5/10/20-session-close returns, plus
   favorable and adverse path. Outcomes remain invisible until their exit bar is available.
5. Historical Cards are compared across symbols. With at least five discovery events across
   three symbols, Meta may propose a Playbook or abstain. Median return, mean without the best
   event, profit factor, and worst outcome are code-computed gates.
6. A Playbook is then evaluated only on matching events later than its anchor event. The latest
   append-only certificate is authoritative and replaces any earlier certificate when new
   holdout evidence arrives. Equivalent event type/direction/horizon hypotheses with at least
   0.65 tag similarity reuse the existing Playbook family.
7. A `SHADOW_ELIGIBLE` Playbook, or a discovery-qualified Playbook whose latest status is only
   `INSUFFICIENT_HOLDOUT`, can match a `FORWARD_FIRST_SEEN`, bullish, current-schema news Card
   observed after that certificate. The latter is explicitly `EVENT_EXPLORATORY_FORWARD`.
   `REJECTED` remains blocked. Event type must match and generalized-tag Jaccard similarity must
   be at least 0.65. Historical replay can never trigger, and one Card starts at most one sandbox.
8. A match compiles one immutable `event_playbook` StrategySpec and exact execution certificate,
   then starts one isolated `$10,000` Candidate Shadow sandbox. It submits at most one next-session
   DAY limit plan, applies the existing volatility-aware stop capped at 15%, a 2R target, 2% of
   current-sandbox-equity risk, fixed-session timed exit, and the `$8,800` permanent failure floor.

## Time semantics

- `event_time`: when the news event was published.
- `available_from`: earliest timestamp the stored version may influence a decision.
- `FORWARD_FIRST_SEEN`: the system ingested the news within 24 hours and uses its actual stored
  availability.
- `PROVIDER_PUBLISHED_REPLAY`: the provider supplied older news later. It is research memory only.
- A corrected historical document not observed at the time is excluded rather than backdated.
- Discovery analogs must be available at the assessment cutoff. Validation events must be later
  than the anchor and causally complete at the validation cutoff.
- A future trigger must be available after the current validation certificate was created.

## Deterministic gates

Discovery for the LLM-selected 1, 2, 5, 10, or 20-session horizon requires:

- at least `EVENT_ALPHA_MINIMUM_ANALOGS` completed prior events;
- at least `EVENT_ALPHA_MINIMUM_SYMBOLS` issuers;
- positive median return and positive mean after removing the best event;
- profit factor at least 1.10 and worst return no lower than -20%; and
- a cited LLM `RESEARCH_LONG` hypothesis.

Independent chronological validation then requires at least three later events across two
symbols, at least 50% positive outcomes, positive median, positive mean without the best event,
profit factor at least 1.10, and no outcome below -20%. Passing means Candidate Shadow evidence,
not proven profitability or Paper eligibility. The latest validation always wins; a later
failure makes an earlier eligible certificate stale.

An insufficient holdout is not a failed holdout. If the discovery gate passed, it may collect
exploratory forward evidence from genuinely new news while the complete holdout set grows.
Across future one-shot matches, `EVENT_FORWARD_VALIDATED` requires at least three closed trades
on two symbols, at least two winners, positive net P&L, profit factor at least 1.05, and no worse
than a `$50` loss after removing the best trade. This is still Candidate Shadow evidence and is
never Paper eligible.

## Inspection

Use the Control Center **Event Alpha** page. It shows news Event Cards, discovery assessments,
latest Playbook validation statistics, forward matches, and linked Shadow sandboxes. Authenticated
read endpoints are:

```text
GET /v1/event-alpha/status
GET /v1/event-alpha/cards?symbol=AAPL
GET /v1/event-alpha/cards/{event_card_id}
GET /v1/event-alpha/assessments
GET /v1/event-alpha/playbooks
GET /v1/event-alpha/validations
GET /v1/event-alpha/matches
```

`GET /v1/llm/invocations/{id}` remains the source of exact prompt/output and usage lineage.
Development may trigger one bounded cycle with `POST /v1/event-alpha/run`; production scheduling
belongs to the coordinator.

## Failure and recovery

- Budget exhaustion, provider failure, invalid output, insufficient analogs, or failed validation
  closes only the Event sidecar and cannot stop Technical Alpha or existing Shadow accounting.
- Event Cards, outcomes, assessments, validations, and matches are append-only evidence.
- A current rejected/insufficient certificate supersedes an older eligible certificate.
- Operator-paused or retired strategies cannot be resumed by automatic Event admission.
- Do not manually convert historical replay into a forward trigger or enroll an Event strategy
  in Paper. A separate explicit design and security review is required before that boundary moves.
