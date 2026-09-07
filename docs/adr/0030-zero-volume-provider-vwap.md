# ADR 0030: Treat zero-volume provider VWAP zero as unavailable

- Status: accepted
- Date: 2026-09-07

## Context

Alpaca can emit daily placeholder bars during an extended trading suspension. These records
carry unchanged positive OHLC prices, zero volume, zero trades, and `vw: 0`. The project domain
correctly requires every present VWAP to be a positive price, so the literal provider value
caused normalization to fail before the raw response or a data-quality warning could be
recorded. NBIS exposed this behavior during the first broad production research cycle.

## Decision

The Alpaca adapter maps `vw: 0` to a missing VWAP only when the same bar has zero volume. It
preserves the bar, its zero volume, zero trade count, OHLC values, and the untouched raw payload.
A non-positive VWAP on a bar with positive volume continues to fail closed through `StockBar`
validation.

Zero volume remains an explicit data-quality warning under `market_data_quality@0.2.0`; this
adapter rule does not reclassify a placeholder as traded activity, waive completeness checks,
or grant execution authority.

## Consequences

- Extended suspension placeholders can be archived and audited without representing zero as a
  real price.
- Downstream feature, ML, and validation stages receive the existing zero-volume warning and
  retain their independent evidence and promotion gates.
- Unexpected invalid prices on traded bars remain hard failures.
