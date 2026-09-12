# ADR 0044: LLM-led case-based Event Alpha

- Status: accepted
- Date: 2026-09-12

## Context

The technical research path is deliberately built around reproducible price features,
chronological ML, constrained strategy specifications, and exact validation. That architecture
is useful for recurring technical patterns, but it is not sufficient for sparse events such as
a material contract, guidance change, regulatory decision, theme acceleration, or squeeze.
Those events rarely repeat with an identical dense feature vector, and forcing them into a
premature predictive classifier would create false precision.

The project nevertheless needs more than free-form LLM commentary. Event research must retain
the exact evidence available at the decision time, compare the current event with completed
cross-issuer cases, measure the outcomes deterministically, expose losing analogs, and keep the
model outside risk and execution authority.

## Decision

Add Event Alpha as a parallel research sidecar to Technical Alpha.

1. The LLM converts a normalized catalyst and its available document versions into a strict,
   citation-bound Event Card. The card records the generalized mechanism, direction, scores,
   1/2/5-session horizons, exact quotations, input hash, prompt/schema versions, model invocation,
   event time, and evidence availability basis.
2. No trained predictive ML classifier is used in Event Alpha V1. Similarity is a transparent,
   deterministic comparison of event type, direction, and generalized tags. Same-symbol history
   is excluded from the cross-stock analog set.
3. Code, not the LLM, calculates every analog's first session-open strictly after evidence is
   available,
   1/2/5-session close return, favorable/adverse path, sample count, issuer breadth, positive
   rate, median, profit factor, worst case, and mean after deleting the largest winner.
4. The LLM receives only the bounded current Event Card, bounded prior Event Cards, and those
   computed statistics. It may propose a research playbook or abstain. It cannot size a position,
   waive a restriction, promote to Shadow/Paper, or submit an order.
5. A deterministic research-candidate gate requires at least five completed analog events across
   three other symbols, a positive median, positive mean after removing the best event, profit
   factor of at least 1.10, no analog loss below -20%, and an LLM `RESEARCH_LONG` proposal.
   Passing creates an immutable `RESEARCH_CANDIDATE` playbook only.
6. Event Alpha V1 is not Shadow eligible. Event-aware walk-forward replay, a frozen execution
   contract, validation certificate, deterministic entry/exit implementation, and the existing
   risk/restriction/idempotency gates are mandatory before any Event Alpha playbook can enter
   broker-free Shadow. Paper and live authority are unchanged.
7. Forward evidence is labeled `FORWARD_FIRST_SEEN`. Delayed historical evidence may seed a
   clearly labeled `PROVIDER_PUBLISHED_REPLAY` case library, but corrected documents first seen
   only after the historical event fail closed. Historical replay is not represented as a
   forward-validated trading result.
8. The sidecar shares the audited `CRITICAL_RESEARCH` USD budget. An unchanged semantic case set
   is idempotent and must not spend again merely because the coordinator clock advances. Event
   Alpha failure cannot interrupt Technical Alpha or existing Shadow accounting.

## Consequences

- Event hypotheses remain explainable and falsifiable without claiming that an LLM narrative is
  a statistical forecast.
- Sparse event families can accumulate reusable cross-stock memory over time.
- Historical provider-time replay is useful research evidence but visibly weaker than evidence
  actually observed forward.
- The first release builds memory and research playbooks, not an executable event strategy.
- A later ADR must define event replay, exact signal timing, gap/liquidity treatment, validation,
  and Shadow admission before the execution boundary changes.
