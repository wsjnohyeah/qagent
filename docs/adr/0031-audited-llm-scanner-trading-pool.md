# ADR 0031: Permit audited LLM-reviewed scanner admission to a dynamic trading pool

- Status: accepted
- Date: 2026-09-07

## Context

The market scanner originally updated only a Candidate List. An administrator then had to copy
symbols into the manually governed Trading Universe before an otherwise eligible strategy could
reach the existing Shadow-adoption confirmation. The user authorized the scanner and LLM to
perform that symbol-level admission autonomously as long as the result remains visible and
auditable.

Combining automated membership with the manual Trading Universe would make ownership ambiguous:
the next scan could either accumulate stale names forever or delete administrator-pinned names.
It would also make a stale coordinator job difficult to bind to the exact list decision that
authorized it.

## Decision

Add a separate versioned `scanner-trading-pool` system list. When
`MARKET_SCANNER_AUTO_TRADING_POOL_ENABLED=true`, only a scan with all of these properties may
replace that bounded pool:

1. deterministic asset, restriction, price, and liquidity eligibility passed;
2. the LLM call completed under the existing `routine_pipeline` USD budget;
3. the LLM only re-ranked the supplied deterministic candidates and introduced no symbol; and
4. the admitted set is the configured top-N shortlist.

The scan event records list revision, admitted, added, and removed symbols, scan ID, and LLM
invocation ID. During an LLM interval skip or recoverable provider/model failure, the previous
LLM-reviewed pool is held and no new symbol is admitted.

The final research stage accepts either manual Trading Universe membership or scanner-pool
membership. Scanner authority is valid only when the workflow's immutable scan event names the
symbol and its recorded list revision still equals the current scanner-pool revision. A stale or
manually altered revision fails closed.

## Consequences

- The scanner can autonomously refresh the symbols eligible to progress toward Shadow adoption.
- Manual and scanner-owned membership have independent histories and cannot overwrite each
  other.
- Pool membership still cannot adopt a strategy, start Shadow, enable Paper, submit an order, or
  reach live money. Exact validation and separate human confirmations remain mandatory.
- The feature defaults off and requires both the scanner and scanner LLM flags.
