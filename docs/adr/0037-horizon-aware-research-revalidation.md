# ADR 0037: Make research admission horizon-aware and continuously revalidate hypotheses

- Status: accepted for production research remediation.
- Date: 2026-09-08.

## Context

Production completed many research cycles without surfacing a Shadow-reviewable strategy.
The data and horizon propagation were healthy, but four independent control interactions made
the funnel stricter than intended:

1. Candidate Shadow used one activity minimum for every holding horizon, although five years
   can produce many independent one-day windows but only one annual validation window.
2. Deflated-Sharpe search-trial correction pooled hypotheses from every holding horizon for a
   symbol, so unrelated one-day searches penalized quarterly and annual hypotheses.
3. A budget-blocked generation stage left previously accepted immutable strategies
   unreachable by the current validation stage.
4. New research after adoption changed the global trial count and could make a frozen,
   administrator-approved Shadow deployment appear stale even when its execution contract had
   not changed.

Separately, some otherwise usable Research LLM calls were rejected because the provider used
objects instead of strings, qualitative confidence labels, or too many claims. The schema was
correctly fail-closed, but the prompt did not state those types and cardinalities clearly.

## Decision

1. Keep the strict Qualified gate unchanged. Scale only Candidate Shadow activity minima by
   the exact 1/5/20/63/126/252-session holding horizon. Every candidate still needs positive
   cost-adjusted compounded OOS return and drawdown within the same deterministic limit.
2. Count research trials within one symbol/timeframe/holding-horizon family. Rejected and
   failed hybrid attempts still count when their bound forecast identifies that horizon.
3. When a new LLM proposal is unavailable, deterministically rotate through previously
   accepted immutable specs for the same symbol/timeframe/horizon and revalidate them against
   current market data, risk/execution contracts, promotion policy, and trial count.
4. Require the current trial count at the human adoption boundary, then freeze that reviewed
   count into the adopted certificate. Later unrelated research does not invalidate an active
   deployment; policy, risk, cost, feature, restriction, or execution-contract changes still
   do.
5. Accept either the manual Trading Universe or an exact, current, LLM-reviewed scanner-pool
   lineage at adoption and Shadow start. Pool membership alone still cannot adopt or start a
   deployment.
6. Make the Research LLM JSON field types and maximum list sizes explicit. Do not coerce or
   weaken citation validation.
7. Raise the versioned daily project, OpenAI-provider, and critical-research USD ceilings to
   `$40` as explicitly authorized. Rebuild current accounting windows from the durable
   reservation ledger so a policy-version change cannot reset same-day spend.

## Consequences

- A profitable, bounded-risk long-horizon hypothesis can collect broker-free forward evidence
  with the amount of historical activity its horizon can realistically provide, while never
  becoming Paper-eligible through the Candidate tier.
- Weak ML remains visible as weak ML. Model promotion thresholds and the strict Qualified gate
  are not relaxed to manufacture a strategy.
- Previously paid-for hypotheses continue to produce useful deterministic evidence even when
  an LLM budget is temporarily exhausted.
- New research and existing Shadow execution can run concurrently without unrelated search
  activity invalidating an approved certificate.
- All strategy adoption, Shadow start, exposure resume, and Paper enrollment actions remain
  separately human-confirmed. Live-money trading remains unavailable.
