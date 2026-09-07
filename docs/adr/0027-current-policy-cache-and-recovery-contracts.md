# ADR 0027: Bind research caches and recovery to current behavior contracts

- Status: accepted
- Date: 2026-09-07

## Context

Immutable historical reports are necessary for audit, but their historical truth does not
make their admission decision current. Promotion thresholds, research search breadth, and ML
training policy can change without changing the underlying price rows. Separately, exhausted
workflow groups and asynchronously changing broker snapshots can make a healthy worker report
misleading progress.

## Decision

- A validation report may be reused for a current admission decision only when its execution
  and data-window contracts, semantic promotion-policy hash, and search-trial count all match.
  New Shadow adoption independently enforces the current policy and trial count.
- An ML training cache key is the dataset hash plus a versioned training-contract hash that
  includes every behavior-bearing policy, label, feature, horizon, algorithm, calibration,
  purging, and selection input.
- LLM infrastructure failure is workflow failure, not research rejection. It produces an
  invocation audit but no cached analyst judgment.
- Workflow recovery selects groups by their earliest incomplete stage. Final failed attempts
  are explicit `EXHAUSTED` state; one additional attempt requires a separately confirmed,
  ledgered administrator action.
- Paper lifecycle completion uses an order observation followed by a fresh position snapshot.
  A position snapshot taken before an order transition cannot prove the resulting lifecycle
  is flat.

## Consequences

Policy changes can trigger additional compute instead of silently reusing obsolete decisions.
Historical reports and attempts remain visible. Recovery queues stay fair after long failures,
and broker reconciliation performs more read-only requests in exchange for coherent state.
None of these changes weakens the separate Paper execution-profile gate or creates a live-money
path.
