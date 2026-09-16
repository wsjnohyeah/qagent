# ADR 0047: Meta-only active LLM routing and evidence-driven Event reassessment

- Status: accepted
- Date: 2026-09-15

## Context

The operator directed QAgent to stop using GPT/OpenAI for all LLM workloads and use Meta Muse
Spark instead. Production inspection also found that Event Alpha was producing Event Cards but
no Playbooks: 205 cards had completed and 207 assessments existed. Among 46 assessment records
covering substantive bullish, non-`other` events, 35 lacked five completed analogs across three
other issuers and the remaining 11 had adverse or fragile analog returns. The 24 assessments that passed every numeric gate but
still received `ABSTAIN` were all low-confidence `other`/`UNKNOWN` cards derived from
header-only SEC filing records. The model was correctly refusing to infer a directional event
from missing filing content.

The scheduler assessed newly created cards, but did not fairly revisit older actionable cards
after later historical backfill added new eligible analogs. That could leave a formerly
insufficient card stale even after its semantic case set changed.

## Decision

1. Route every active workload—interactive explanation, routine pipeline, technical research,
   Event Alpha, strategy generation, and strategy critique—to Meta `muse-spark-1.3` in
   `llm_routing@0.3.0`. An explicit provider override is accepted only when that provider is
   active in the effective route map, so an ordinary request cannot bypass the Meta-only policy.
2. Preserve the `$80/day` project ceiling and raise the Meta provider ceiling from `$40/day` to
   `$80/day` in `llm_budget@0.4.0`. Existing per-workload limits remain unchanged. This preserves
   the separately approved technical and Event Alpha capacity without increasing total project
   spend.
3. Reject header-only SEC evidence deterministically before an Event Card LLM call when every
   supplied document is an SEC filing with fewer than 30 words. The rejected catalyst remains
   auditable and consumes no LLM budget.
4. Continue assessing newly extracted actionable bullish cards, and also fairly revisit a
   bounded number of older actionable cards whenever their semantic analog-set hash changes.
   Unchanged cases remain idempotent and incur no new provider call.
5. Do not lower the five-analog/three-symbol, median, best-winner sensitivity, profit-factor,
   worst-loss, or LLM recommendation gates. Current production evidence shows a case-quality and
   case-coverage limitation, not an excessively strict deterministic threshold.

## Consequences

- OpenAI remains configured as a dormant provider definition for compatibility and historical
  audit records, but no active workload selects it and explicit calls fail closed while it is
  absent from the effective route map.
- Useful cross-stock case memory should grow and stale actionable cards can become Playbook
  candidates without lowering evidence quality.
- Event Alpha remains research-only and has no Shadow, Paper, Robinhood-order, or live-money
  authority.
