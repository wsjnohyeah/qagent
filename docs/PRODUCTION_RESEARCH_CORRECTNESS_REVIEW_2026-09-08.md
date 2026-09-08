# Production research correctness review — 2026-09-08

## Outcome

The production system was healthy, but “no eligible strategy” was not explained by one cause.
Market discovery and daily-bar backfill were operating correctly; current ML forecasts were
mostly weak, while several workflow and admission interactions also suppressed otherwise
valid Candidate Shadow evaluation. ADR 0037 records the corrections.

## 1. Stock selection

- The scanner merged 256 source names and retained 40 deterministic review candidates plus a
  20-symbol deep-research pool.
- The current pool included speculative and AI-infrastructure names such as CRWV, EOSE, IREN,
  NBIS, NVDA, RGTI, SMR, TSLA, and WULF. SNDK remains in the reviewed theme catalog and can
  re-enter when its current activity/liquidity score warrants it.
- The LLM can only re-rank the deterministic eligible set; it cannot invent a ticker or bypass
  price, liquidity, restriction, benchmark, or tradability filters.
- Confirmed defect: the final coordinator accepted scanner-pool authority, but Shadow start
  still required manual Trading Universe membership. The adoption/start boundary now verifies
  the exact validation job's scan ID and the current scanner-pool revision.

## 2. Data backfill

- Daily bars covered five years or the provider-observed listing/resumption boundary for all
  20 current pool symbols. The audit found that this was insufficient for leakage-safe
  252-session ML evaluation. The production target is now approximately six calendar years;
  mature issuers will backfill incrementally while later listings retain their truthful shorter
  boundaries.
- Newly listed names correctly have shorter histories; they do not fabricate pre-listing gaps.
- Alpaca News and SEC evidence are source-dependent and continue through durable partitions.
  Foreign issuers and newly listed companies may legitimately have no SEC company-fact rows.
- Trades, quotes, and option chains remain forward streams/snapshots by design; they are not
  falsely labeled as five-year datasets.
- Thousands of current quality reports passed. An older NBIS failure was superseded by the
  verified post-suspension-boundary correction; no general daily-backfill defect was found.

## 3. ML + LLM strategy construction

- Real production model runs exist for 1, 5, 20, 63, and 126 sessions. Their aggregate
  holdout AUC values were approximately 0.47–0.50, near random ranking, and only a handful of
  runs met the existing model-quality gate. This is weak predictive evidence, not a reason to
  label the pipeline broken or lower the ML gate.
- The Research LLM reads point-in-time source evidence, model/holdout/calibration/drift data,
  and prior outcomes. A generator proposes a bounded strategy and a separate critic reviews
  it. Deterministic exact-spec validation, not either LLM, decides eligibility.
- The generator accepted most schema-valid proposals. The Research LLM stage was the larger
  loss point: many provider outputs violated underspecified JSON field types. Prompt version
  `evidence_bound_analyst@0.3.0` now states exact types and cardinalities while preserving exact
  citation checks.
- The first live 63-session cycle exposed a second output-contract defect: the generation
  prompt stated threshold direction but omitted the schema's `[-0.25, 0.25]` magnitude bound.
  BIAF and BNC repeatedly returned values such as `0.3`, `0.5`, or `1.0`, causing five paid
  retries for the same invalid answer. Prompt version `hybrid_strategy_generation@0.3.0` now
  states the exact decimal-return range. Invalid model output remains rejected, but is recorded
  as `WAITING_VALID_STRATEGY_OUTPUT` rather than misclassified as an infrastructure failure and
  automatically retried five times.
- Search-trial correction now separates holding horizons. Candidate Shadow activity minima are
  horizon-aware under `research_gate@0.4.0`; strict qualification remains unchanged.
- Existing accepted specs are now revalidated when a current paid generation stage is blocked,
  so paid research is not discarded merely because a later cycle reaches its USD ceiling.
- The former 15%-per-fold allocator made 252-session training impossible inside the prior
  five-year window after full-horizon purging. Training contract 0.3 sizes each test fold to
  retain at least 20 independent rows in calibration and model selection after purging; the
  final evaluation retains its separate 60-row promotion minimum. The daily-bar target is
  extended to six years. This fixes reachability without reducing the embargo or weakening ML
  promotion thresholds.

## 4. Shadow readiness

- Before this correction, production had zero adoptions, deployments, Shadow plans, positions,
  or Paper orders. Global new exposure remained paused.
- A real 63-session HPE report had seven total folds, five active folds, five OOS trades, 80%
  positive active folds, approximately `+0.4168%` cost-adjusted compounded OOS return, and
  bounded drawdown. It failed the uniform 10-trade Candidate minimum, which was inappropriate
  for that horizon. It must be revalidated under policy 0.4 before any operator decision.
- Candidate Shadow remains broker-free and observation-only. A report becoming eligible does
  not auto-adopt it, auto-start Shadow, resume new exposure, or enroll Paper.
- A next-session plan must be durable before that session opens. A strategy approved after the
  open correctly waits for the next eligible session rather than fabricating a same-day fill.

## Remaining scientific limitation

The implemented strategy family is deliberately bounded to long-only momentum and
mean-reversion entry rules, even when the position is held for 63–252 sessions. The platform
can correctly test and operate those horizons, but current production ML evidence does not yet
demonstrate predictive alpha. Broader factors, cross-sectional models, regime-specific rules,
and ML-only versus ML+LLM ablations are research improvements—not reasons to weaken safety or
statistical gates today.

## Post-remediation production evidence

- GitHub CI and immutable-image publication passed for functional SHA
  `ddaba5152f59e4ae278ef1e221d02494dbfbf502`; API, worker, coordinator, PostgreSQL, and Redis
  are healthy on that exact image. Live trading is disabled and new exposure is paused.
- A real AAPL 252-session probe repaired 251 leading bars, producing 1,507 daily bars from
  2020-09-08 through 2026-09-08. ML trained on 1,235 point-in-time labels with a 252-session
  embargo, 21 calibration rows, 21 model-selection rows, and 272 untouched final-holdout rows.
- The annual model's final ROC AUC was about 0.552, but its calibration error failed the ML
  promotion gate. The Research LLM and generator still produced an exploratory exact strategy;
  deterministic validation rejected it for negative modeled net return. This demonstrates the
  entire long-horizon workflow without claiming alpha.
- Current 63-session research produced eight Candidate-Shadow-reviewable reports. Six belong
  to the current Scanner Trading Pool: EOSE, IREN, LITE, NVDA, SMR, and TSLA. None is strictly
  Qualified, adopted, deployed, or enrolled in Paper. Exact administrator confirmation and the
  global exposure control remain intentionally separate.
