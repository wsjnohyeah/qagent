# ADR 0018: Persistent broker-free shadow runtime

- Status: accepted for Phase 6.
- Decision: admit only deterministically gate-eligible, human-confirmed strategies to a
  persistent shadow runtime that reuses point-in-time features and portfolio cost semantics
  while having no broker order path.

## Context

Phase 3 replay established research accounting, but it did not continuously track an adopted
strategy as new stored data arrived. Phase 6 needs operational state visible to the System
Steward and object explorer without confusing local workflow validation with live trading or
requiring a multi-year development dataset.

## Consequences

- Strategy adoption checks the immutable validation report's exact static strategy ID,
  timeframe, feature version, engine version, cost model, and
  `eligible_for_human_review` gate before recording administrator approval. Selector reports
  and research-only buy-and-hold benchmarks cannot authorize a shadow deployment.
- A deployment owns virtual cash/P&L, status, last processed bar, and an ordered virtual
  event journal. Unique deployment/bar/type and sequence constraints make replay observable
  and idempotent.
- Automated and manual ticks run through one lock and one deterministic implementation.
  Global pause and the shadow pipeline control stop scheduled processing.
- The runtime reads the same normalized store and builds the same point-in-time feature
  snapshots as research. It uses the event-driven portfolio engine's commission, spread,
  slippage, impact, and participation limits.
- Each attempted long exposure persists a signal candidate, deterministic risk decision,
  optional approved trade plan, and linked virtual events. The entry participation cap uses
  completed decision-bar liquidity, never the future execution bar's completed volume.
- Daily/weekly reports and deduplicated alerts are derived from the durable event and risk
  rows rather than transient process memory.
- The `shadow-active` system list is synchronized with active deployment symbols.
- No broker dependency, credential, account read, order transport, or live-money mode is
  introduced. Production scale changes storage/scheduling capacity, not these contracts.
- Multi-session holdings, partial-fill carryover, cancellation, dynamic quote spread,
  delisting, and symbol-change behavior remain future execution-realism work.
