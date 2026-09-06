# End-to-end deployment-readiness audit — 2026-09-06

## Verdict

The repository is **source-ready for a guarded cloud bootstrap** after this audit. It is not
yet an operating production research/trading service: a VPS, TLS, secret delivery, backups,
monitoring, production backfill, and elapsed shadow/Paper evidence are still external gates.

The real local AAPL research path now completes from collection through the Research LLM
gate. It correctly stops before strategy generation because the measured ML evidence is weak
and the evidence-bound LLM chose `ABSTAIN`. That is a successful safety outcome, not evidence
of alpha and not a successful candidate-strategy promotion.

## Defects reproduced and repaired

1. Historical feature versions could be mixed in one ML dataset. Training can now pin the
   exact feature-set version while legacy rows remain queryable.
2. PostgreSQL rejected the descriptive ML selection metric because its 80-character column
   was shorter than the real value. Alembic `20260906_0029` expands it to 160 characters.
3. A second coordinator training cycle read a nonexistent `training_end` key. Exact training
   reuse now uses the persisted dataset hash plus symbol, timeframe, horizon, and feature
   version.
4. High-effort `gpt-5.6-sol` exhausted the former 1,800-token Responses API ceiling before
   emitting JSON. Research, generation, and critique now have the configured 4,096-token
   ceiling.
5. Provider responses marked `incomplete` could consume billable tokens while the local USD
   ledger released the reservation and recorded zero usage. Failed responses now retain the
   provider response ID/usage and settle the budget whenever the provider reports usage.
6. The coordinator asked a one-bar ML forecast to support a five-trading-day LLM conclusion.
   It now aligns the research horizon to the forecast. Forecast wall-clock creation time is
   retained in lineage but excluded from market-evidence text, and cached analysis reuse now
   requires the current prompt version and exact evidence-bundle hash.

## Real bounded ML + LLM experiment

The final run used AAPL daily data through the 2026-09-04 close:

- 755 daily bars and 735 current `price_event_pit@0.3.0` snapshots;
- 734 executable next-open-to-next-close labeled examples and three purged walk-forward
  folds;
- logistic-regression and boosted-stump candidates trained and persisted in PostgreSQL;
- selected logistic candidate: final-holdout ROC AUC `0.4711`, Brier `0.2606`;
- one-bar forecast: expected return `-0.1552%`, probability up `34.77%`, uncertainty `69.54%`;
- the Research LLM received 14 time-safe items: one feature snapshot, one ML forecast, and 12
  source documents;
- `gpt-5.6-sol` returned a schema-valid, citation-valid `ABSTAIN` with confidence `0.90`;
- the final call used 3,845 input and 2,541 output tokens and was estimated at `$0.070045`;
- all three successful audit research calls totaled 16,689 tokens and `$0.193455` under the
  configured estimates.

One earlier `incomplete` response occurred before failed-response usage accounting was fixed.
Its local record contains zero usage, so the provider may have billed an amount that cannot be
reconstructed from the stored record. Future incomplete responses are accounted correctly.

No generated strategy, validation certificate, Shadow adoption, Paper enrollment, or broker
order was created by this experiment. The generator/critic accepted branch remains covered by
deterministic contract tests but was intentionally not forced past the real LLM's abstention.

## Verification

- `make release-check`: Flake8, strict mypy across 58 source files, 124 tests, local doctor,
  authenticated Control Center check, secret scan, Docker rebuild/doctor, and PostgreSQL
  Alembic drift check passed.
- Fresh SQLite migration: base to `20260906_0029`, downgrade to `0028`, and re-upgrade to head
  passed with no generated schema changes.
- Production Compose renders with an explicit environment-file override; the VPS deployment
  script passes POSIX shell syntax validation.
- Local services are healthy. Paper remains disabled and `paper_orders` remains zero.

## Remaining gates

- The local ML candidates failed statistical promotion gates. More data cannot be assumed to
  fix that; production backfill and untouched/forward evidence must measure it.
- A naturally accepted real-provider generator/critic run has not occurred because the real
  analyst rejected the current evidence. It must not be manufactured merely to advance the
  workflow.
- GitHub CI must pass on the audit commit.
- Internet-facing deployment still needs the operator inputs in `docs/DEPLOYMENT.md`.
- The first Paper enrollment/order remains a separate administrator-confirmed decision.
