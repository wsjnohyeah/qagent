# ADR 0049: Accelerate Shadow evidence without weakening deterministic rejection

- Status: accepted
- Date: 2026-09-23

## Context

An isolated Shadow sandbox is the forward evidence layer for one immutable strategy, not a
portfolio simulator. The original system had a permanent failure rule at `$8,800`, but no
positive graduation label. Event Alpha also required a complete later-event holdout before it
could collect any forward Shadow evidence. Production accumulated discovery-qualified Event
Playbooks whose later holdout was merely too small, while explicitly rejected Playbooks and
sample-starved Playbooks were presented too similarly.

The operator approved a faster, horizon-aware graduation policy and an exploratory Event tier.
The purpose is to gather real forward evidence sooner without converting an LLM opinion,
historical replay, or a failed deterministic test into broker authority.

## Decision

1. Shadow graduation is a versioned, deterministic label derived from the append-only virtual
   event journal. It does not change the deployment's execution status, stop the sandbox, or
   erase later evidence.
2. Early-graduation minimums are:
   - 1 session: five closed trades and seven completed market sessions;
   - 2 sessions: four closed trades and ten completed market sessions;
   - 5 sessions: three closed trades and fifteen completed market sessions;
   - 10 sessions: three closed trades and twenty-five completed market sessions; and
   - 20 sessions: two closed trades and forty completed market sessions.
3. Every early graduation also requires at least two winners, positive realized net P&L,
   profit factor of at least 1.05, maximum marked-equity drawdown no greater than 10%, a current
   exact execution contract, and P&L no worse than -0.5% of initial sandbox equity after
   removing the single best trade. Strategies with 63/126/252-session horizons remain in
   long-horizon observation rather than receiving an accelerated label.
4. Graduation is evidence for later review only. It does not bypass the Qualified admission
   tier, the compatible Paper execution profile, the explicit Paper enrollment confirmation,
   or any live-money boundary.
5. An Event Playbook whose discovery gate passed but whose latest chronological validation is
   `INSUFFICIENT_HOLDOUT` may match a later `FORWARD_FIRST_SEEN` bullish news Card and enter an
   isolated Candidate Shadow sandbox. It is labeled `EVENT_EXPLORATORY_FORWARD`.
6. An Event Playbook whose latest validation is `REJECTED` remains blocked. Historical provider
   replay, stale validations, wrong event type, tag similarity below 0.65, and expired events
   remain blocked.
7. One forward news Card may start at most one Event sandbox. Fully held-out Playbooks are
   preferred over exploratory Playbooks. Materially identical new hypotheses reuse an existing
   Playbook family instead of multiplying near-duplicate Playbooks.
8. Event forward graduation is evaluated across a Playbook's future one-shot sandboxes. It
   requires at least three closed forward trades across two symbols, two winners, positive net
   P&L, profit factor of at least 1.05, and no worse than a `$50` loss after removing the best
   trade. The label remains Candidate-Shadow evidence and is never Paper eligible.

## Consequences

- Short-horizon strategies can receive a readable success label sooner while their sandboxes
  keep running and accumulating evidence.
- Event Alpha can use the forward layer for what it is intended to measure instead of waiting
  indefinitely for three historical holdouts.
- The exploratory tier is intentionally weaker than a held-out certificate and is displayed as
  such. A future deterministic rejection invalidates its exact certificate before new exposure.
- The LLM remains a semantic hypothesis generator. Code still controls chronology, similarity,
  entry, sizing, stop/target geometry, restricted securities, idempotency, and circuit breaking.
- No broker or live-money authority changes. `LIVE_TRADING_ENABLED=false` remains mandatory.
