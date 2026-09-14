# ADR 0045: Dedicated Meta budget for Event Alpha operation

- Status: accepted
- Date: 2026-09-13

## Context

Event Alpha V1 was deployed safe-off and originally shared the premium
`critical_research` workload. The operator approved continuous Event Card, outcome, analog,
assessment, and Playbook research, requested Meta Muse Spark for its lower estimated cost, and
authorized a dedicated `$40` daily Event Alpha ceiling.

## Decision

1. Add a distinct `event_research` LLM workload and route it only to Meta
   `muse-spark-1.3`. Technical critical research remains on OpenAI.
2. Set the Event Alpha workload and Meta provider daily estimated-cost ceilings to `$40`.
   Raise the project daily ceiling from `$40` to `$80` so the new allowance does not silently
   consume the already-approved technical research allowance. Retain the existing `$200`
   monthly project ceiling.
3. Keep Event Alpha bounded to two new cards per coordinator cycle. Semantic input hashes make
   unchanged card/analog sets idempotent for spend.
4. Enable the full V1 research-memory pipeline in production: citation-bound Event Cards,
   deterministic 1/2/5-session outcomes, cross-symbol analog retrieval, LLM assessment, and the
   deterministic Playbook-candidate gate.
5. Preserve ADR 0044's execution boundary. An Event Playbook is a research hypothesis, not an
   executable strategy. It has no Shadow, Paper, Robinhood-order, or live authority until a
   later reviewed implementation adds event-aware walk-forward replay, a frozen signal and
   entry/exit contract, exact validation, and deterministic risk integration.

## Consequences

- Event Alpha spend is visible and independently capped without changing technical-research
  model quality.
- The first cards may appear after one completed coordinator cycle. A Playbook requires enough
  completed, similar cross-stock cases and is therefore possible but not guaranteed within 24
  hours.
- The LLM can choose a 1-, 2-, or 5-session hypothesis and bounded entry confirmations, but it
  cannot size or execute a position.
