# Review remediation — `3b3926e` follow-up

The external review `qagent_review_3b3926e_2026-09-06.md` was treated as
evidence to verify against the newer repository state, not as executable
instructions. The review correctly identified seven defects in `3b3926e`.
The later `c221891` commit had already implemented its two largest product
gaps—an autonomous research coordinator and a continuous shared virtual
account—but had not changed the N01–N07 code paths.

## Finding disposition

| Finding | Current result |
|---|---|
| N01 | Fixed. `static_strategy` validation has a subject-specific gate: candidate breadth and PBO are explicitly not applicable, while fold, regime, drawdown, positive-fold and Deflated Sharpe checks still apply. A real validator → eligible report → adoption → shadow-start integration test replaces the previous fixture-only proof. |
| N02 | Fixed for the broker-free bar simulator. Candidate observation, risk approval and plan persistence use actual runtime time. An execution is rejected unless the plan was durably persisted before that market open; expired and missed opportunities remain cancelled. Historical backfill therefore cannot enter forward P&L. |
| N03 | Fixed. Daily labels use the exchange-calendar session open and the future bar availability/close boundary. Splits change held share count and later dividends are multiplied by that count. The pre-open, horizon-two split counterexample now produces the unadjusted economic return. |
| N04 | Fixed. The declared profile is now `next_open_market_revalidated_bracket_one_bar@0.2.0`. Both backtest and shadow preserve the precommitted bracket but re-evaluate reward/risk and quantity against the actual next open. Shadow emits `EXECUTION_RISK_REVIEW`; an adverse open is cancelled or downsized and cannot exceed its cash/risk reservation. |
| N05 | Fixed. Every active deployment rechecks its exact validation/execution contract before processing. A mismatch transitions it to `REVALIDATION_REQUIRED`, cancels open plans without erasing history, and blocks resume until a current exact report is adopted. The engine version is now `event_driven_portfolio@0.4.0`, intentionally invalidating older certificates. |
| N06 | Fixed on replay without rewriting immutable history. After natural-key upsert, ingestion resolves the actual persisted bar/trade/quote/option ID before creating an event. The ledger recognizes a legacy event by its persisted business-object ID, repairs a missing outbox row, and does not create a second logical event. |
| N07 | Fixed conservatively. Deflated Sharpe now receives the recorded market-contract search-trial count instead of the submitted candidate-list length. Failed/rejected hybrid attempts count, accepted generated specs are not double-counted after backtesting, and the exact count is stored as `selection_search_trial_count`. The scope is deliberately the whole symbol/timeframe contract until campaign isolation is introduced, which may over-penalize but cannot manufacture eligibility. |

## Product gaps from the review

- G01 is superseded by the persistent eight-stage coordinator implemented in
  `c221891`. Paid LLM stages remain disabled by default and cannot stop shadow
  accounting.
- G03 is superseded by the shared virtual master account and strategy sleeves
  implemented in `c221891`. Cash, risk reservations and realized P&L persist
  across strategy versions.
- Phase 7 remains intentionally open. There is still no Alpaca order-submission
  or reconciliation path, and no live-money path.
- Production-scale elapsed evidence, deployment soak testing, TLS, backups and
  monitoring cannot be proven by local fixtures.

## Phase 6 explainability update

The Strategy registry now labels every version as either deterministic baseline
or ML + LLM. A strategy detail page presents the creation chain in plain
language: point-in-time snapshot, ML forecast and model, cited Research LLM
comment, generator proposal, independent critique, frozen executable spec,
validation evidence, historical trades and forward shadow observations. Raw
IDs/hashes remain available only under Advanced diagnostics. LLM invocation
inspectors link to the existing sanitized prompt/usage/cost view.

The Shadow page now surfaces the shared master account, validation-contract
currency, actual approval/persistence times, and execution-time risk-review
events. The Pipelines page presents the coordinator stages and per-stage waiting,
failure and completion state instead of only aggregate counts.

These corrections establish software invariants; they do not establish that a
strategy is profitable.
