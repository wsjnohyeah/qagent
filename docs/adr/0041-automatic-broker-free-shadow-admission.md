# ADR 0041: Automatically admit exact eligible strategies to broker-free Shadow

- Status: accepted
- Date: 2026-09-11 PDT

## Context

The autonomous coordinator could complete data, ML, Research LLM, constrained generation, and
exact validation, but every Candidate or Qualified result then stopped for two administrator
confirmations. That made human availability—not evidence or risk—the limiting step in a
broker-free observation environment.

## Decision

1. Add `COORDINATOR_AUTO_SHADOW_ENABLED`, disabled by default and explicitly enabled in the
   reviewed production environment.
2. After exact static-strategy validation, automatically choose `QUALIFIED` when available and
   otherwise `CANDIDATE`, record the adoption, and start the matching strategy/symbol Shadow
   sleeve idempotently.
3. Preserve the exact current validation contract, horizon-specific search count, current
   promotion policy, governed universe/scanner lineage, restriction checks, shared virtual
   account, deterministic sizing, and risk limits.
4. Treat an operator `PAUSED` or `RETIRED` adoption/deployment as an authoritative hold that
   automation cannot reverse.
5. Candidate remains observation-only. Automatic Shadow admission does not enroll Alpaca Paper,
   call Robinhood order tools, enable live money, or grant either LLM risk authority.

## Consequences

- Gate-eligible hypotheses begin collecting forward virtual evidence without daily operator
  intervention.
- The coordinator—not the LLM—owns the state transition after deterministic validation.
- Manual adoption/start remains available for recovery and historical review, while manual
  pause/retirement remains stronger than automation.
- Paper and any future live-money boundary continue to require separate explicit decisions.
