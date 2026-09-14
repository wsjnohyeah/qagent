# ADR 0046: Fair cross-symbol Event Alpha case scheduling

- Status: accepted
- Date: 2026-09-14

## Context

Event Alpha requires completed analogs across multiple issuers before it may create a research
Playbook. The first production scheduler queried the globally oldest 500 unprocessed catalysts
and then selected an old and a recent row from that truncated result. AAPL alone had more than
17,000 historical catalysts, so the truncated set remained AAPL-heavy even though the active
scanner supplied 20 symbols. After 55 cards, 21 of 27 completed cards belonged to AAPL and only
four symbols had any completed card. That selection bias could starve the cross-symbol gate for
days and made a 24-hour Playbook expectation unrealistic despite a healthy worker and ample LLM
budget.

## Decision

1. Schedule at most one unprocessed catalyst per active scanner symbol in a selection pass.
2. Rank symbols by the number of current-schema cards already attempted, preserving scanner rank
   as the deterministic tie-break. Rejected cards count as attempts so a symbol with unusable
   evidence cannot monopolize paid work.
3. Alternate each symbol between its oldest and newest remaining catalyst according to its
   current-schema attempt count. This retains both historical case-memory growth and timely event
   coverage without a global high-volume-issuer cutoff.
4. Keep the existing two-paid-card-per-cycle limit, Meta route, USD budgets, evidence checks,
   analog thresholds, deterministic Playbook gate, and no-execution boundary unchanged.

## Consequences

- The active scanner universe receives bounded, approximately even Event Card coverage.
- Cross-symbol analog evidence can accumulate at the rate intended by the minimum-issuer gate.
- The scheduler improves the probability of a Playbook candidate within 24 hours but does not
  promise one; evidence and robustness checks continue to fail closed.
- Existing cards, assessments, outcomes, and spend remain immutable and are not rewritten.
