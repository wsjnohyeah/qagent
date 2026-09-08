# ADR 0038: Align annual ML evaluation with production history

- Status: accepted for production research remediation.
- Date: 2026-09-08.

## Context

The coordinator advertised a 252-session research horizon while requesting only five calendar
years of daily bars. The ML trainer correctly applied a 252-session embargo and purged labels
that crossed the calibration/selection and selection/final-test boundaries. However, its
15%-per-fold allocation left each OOS partition shorter than the purge itself. As a result,
annual jobs for mature issuers exhausted retries with no possible path to a valid model.

Treating those failures as ordinary sparse-history waits fixes their operational classification
but does not make the declared annual workflow achievable. Reducing the embargo or overlapping
labels would instead weaken the point-in-time contract.

## Decision

1. Keep the full holding-period embargo and the separate calibration, model-selection, and
   final-test partitions.
2. Require each test fold to contain the full effective embargo plus the configured minimum
   calibration/selection sample count after purging. The final holdout retains its separate
   promotion minimum. Under the default policy, a 252-session model therefore requires at
   least 1,092 labeled examples.
3. Increase the production daily-bar request from 1,826 to 2,192 calendar days (approximately
   six years). News and SEC evidence retain their separate five-year targets.
4. Bump the ML training contract to `walk_forward_ml_trainer@0.3.0`; cached models from the old
   split behavior cannot be silently reused.
5. A symbol listed too recently to satisfy the horizon-specific requirement remains an explicit
   `WAITING_ML_TRAINING_REQUIREMENTS` state. No synthetic history, overlapping outcome, or
   relaxed promotion threshold is permitted.

## Consequences

- Annual research is reachable for sufficiently mature issuers and remains leakage-resistant.
- The initial market-data backfill is modestly larger, while document history is unchanged.
- All horizons may retrain under the new contract as their next coordinator cycles run.
- This change enables evidence production; it does not assert predictive alpha, promote a
  model, adopt a strategy, start Shadow, or authorize Paper.
