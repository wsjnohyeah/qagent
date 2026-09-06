# ADR 0021: Forward shadow execution and recovery fencing

- Status: superseded in part by ADR 0022 and ADR 0023
- Date: 2026-09-06

## Context

The independent review of `56bb979` found that research and shadow execution did not share
one complete risk/exit contract, a completed execution bar could be observed before its
shadow plan existed, and several retry paths lacked attempt-level fencing or durable event
reconciliation.

## Decision

- Version the baseline risk geometry and one-bar bracket executor. Validation binds the
  complete risk policy, restriction registry, capital, costs, feature version, and engine
  version. Shadow adoption rejects any mismatch.
- A shadow tick first executes only a previously persisted open plan, then evaluates the
  newly completed bar and persists a plan for a future bar. Missed bars cancel the old plan;
  they are never reconstructed as hypothetical forward fills.
- Keep Phase 6 candidate accounts isolated by deployment. Concurrent planned risk is scoped
  to that same account; the UI/API must not present independent candidate capital as one
  portfolio.
- Fence workflow claims and portfolio ticks with unique attempt tokens and expiries.
- Replaying normalized ingestion reconciles deterministic event IDs and missing outbox
  intents. Every batch creates all durable intents before attempting network delivery.
- A production worker boot persistently pauses new exposure. An API-only restart preserves
  the shared SQL control.
- Separate canonical executable strategy identity from append-only generation-attempt
  provenance. Unsupported generated DSL fields fail validation.
- A single-candidate PBO result is an explicit evidence shortfall. Validation drawdown uses
  the complete selected OOS equity path.

## Consequences

Phase 6 now supplies broker-free forward timing and deterministic replay parity for its
supported one-bar long/cash strategies. It still does not model broker acknowledgements,
partial fills, or real paper positions; those remain Phase 7. A full collection/research
coordinator and a separately specified shared-main-account model remain future product work,
not implicit properties of the shadow worker.
