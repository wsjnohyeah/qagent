# ADR 0034: Make history symbol-centric and LLM judgment advisory

- Status: accepted
- Date: 2026-09-07

## Context

The archive-oriented Data Explorer displayed provider receipt dates as if they were source
history. Production therefore appeared to have only one day of data even when daily bars
covered five years. Conversely, news really covered only about 90 days and SEC filings and
facts were not part of the autonomous coordinator. Snapshot/stream datasets cannot honestly
claim historical coverage merely because the daily-bar store can be backfilled.

The research workflow also treated a valid Research LLM `ABSTAIN` as a hard veto before the
strategy generator. That made the LLM an implicit research authority and produced no
testable strategy candidates even though the intended architecture makes the LLM an idea and
interpretation layer while deterministic validation remains the empirical judge.

## Decision

1. Data Explorer is ticker-first. Each symbol exposes populated and missing data types,
   record counts, true event-time bounds, separate ingestion bounds, date grouping,
   pagination, readable normalized records, and raw-object lineage.
2. Production daily bars, Alpaca News, SEC filing metadata, and SEC company facts target
   1,826 days. Development remains capped by its bounded-data setting. News history advances
   backward in completed 90-day partitions while the newest day is refreshed independently.
   SEC evidence refreshes at most once per symbol per day and archives the SEC ticker map.
3. Trades, quotes, and option chains are labeled forward streams/snapshots. Corporate actions
   and historical universe membership remain governed reference imports until a reviewed,
   licensed provider is connected. The application must show these gaps rather than invent
   five years of data.
4. A current research snapshot may use the latest completed daily price bar plus documents
   that were actually ingested by the later decision cutoff. Such decision-time snapshots are
   valid forecast inputs but are excluded from ML label construction because they do not map
   to a completed decision bar.
5. A schema-valid, citation-valid `ABSTAINED` Research LLM record may feed the constrained
   generator as explicitly advisory context. Malformed/rejected analysis still blocks. The
   generator must disclose the abstention, the independent critic may reject the proposal,
   and only exact deterministic walk-forward validation can make a frozen spec eligible for
   human review.
6. Stop distance and account-dollar risk are separate governed fields. The Control Center
   exposes both under the existing two-step account-risk revision flow. The active one-day
   execution profile still exits remaining exposure at the same session close; changing a
   stop does not silently create a multi-day strategy.

## Consequences

- Operators can answer “what data exists for this ticker, over which dates, and what is
  missing?” without opening archive JSON.
- Historical evidence converges incrementally and restart-safely without making snapshots
  look backfillable.
- LLM uncertainty no longer suppresses hypothesis testing, but the LLM still has no risk,
  adoption, Shadow, Paper, or broker authority.
- Candidate count may increase, so LLM USD budgets and search-trial correction continue to
  bound cost and selection bias.
- A win rate over 50% remains descriptive only. Fold count, regimes, drawdown, positive-OOS
  rate, Deflated Sharpe, exact execution contract, and administrator confirmation remain
  independent gates.
