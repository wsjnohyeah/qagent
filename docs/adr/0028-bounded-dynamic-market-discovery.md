# ADR 0028: Bounded dynamic market discovery without execution authority

- Status: accepted
- Date: 2026-09-07

## Context

A fixed four-symbol universe cannot reliably surface newly active speculative names or early
theme followers. Running the full five-year research DAG over every listed security would be
unbounded, expensive, and operationally noisy. Giving an LLM direct control of the universe
would also make a probabilistic model an implicit risk authority.

## Decision

Add an hourly, versioned market-discovery funnel ahead of the existing per-symbol research
coordinator:

1. merge Alpaca's 100 most-active stocks, 50 gainers and 50 losers, a reviewed theme-seed
   catalog, and the administrator's Focus Watchlist;
2. retrieve current snapshots in bounded batches and deterministically reject restricted,
   benchmark, sub-$3, and sub-$20-million-dollar-volume symbols;
3. rank activity, absolute move, intraday range, liquidity, theme membership, and manual
   focus, retaining at most 40 review candidates;
4. optionally ask the budgeted `routine_pipeline` LLM route to re-rank only that supplied set
   at most once every four hours, accepting strict structured output and falling back to the
   deterministic order for any provider, budget, schema, or invented-symbol failure; and
5. persist immutable raw-provider evidence plus `market.universe.scanned.v1`, revise the
   dynamic Candidate List, and run deep research for at most 20 selected symbols.

The scan ID is carried into every coordinator job. Scanner selection never adds a symbol to
the governed Trading Universe. Even a validated candidate must be separately admitted to
that list and pass the existing human strategy-adoption and Shadow-start confirmations.

## Consequences

- Current movers and explicitly seeded AI-infrastructure/high-beta names, including SNDK,
  can enter the research funnel without maintaining a tiny fixed research list.
- Provider and LLM costs are bounded before the expensive historical research DAG.
- The LLM can contribute qualitative prioritization but cannot invent a symbol, change the
  deterministic eligibility filters, authorize risk, adopt a strategy, or execute a trade.
- The theme catalog is deliberately reviewed configuration, not a claim that every member is
  attractive. It should evolve through code review and observed scan quality.
- Alpaca's screener is a discovery source, not a complete all-listed-equity census. Broader
  licensed reference-universe coverage remains a future enhancement.
