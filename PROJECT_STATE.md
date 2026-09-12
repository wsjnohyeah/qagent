# Project state

## Current

- Implemented foundations through the corrected Phase 6.1 baseline, the four pre-cloud
  hardening milestones, and a unified Phase 7 Alpaca Paper lifecycle, including
  Phase 1B open-session checks, corrected point-in-time research/ML contracts, constrained
  ML + LLM strategy generation, and the authenticated Control Center/System Steward/shadow
  decision lineage. Phase 5 statistical promotion and Phase 6 continuous-operation exit
  criteria have not passed because current reports are Candidate-Shadow-only rather than
  strictly Qualified and forward observation history remains short.
- The production stack is online in `production` mode at
  `https://qagent.143.110.239.251.sslip.io` on a fresh SFO3 VPS. It runs the immutable verified
  `4fe2d71b372a3731140189a18450a7fa32f74ee6` image, PostgreSQL, Redis, API, Shadow worker,
  and independent research coordinator behind Caddy TLS. Dynamic market scanning, bounded
  Meta re-ranking, audited Scanner Trading Pool admission, paid strategy research, Shadow,
  and Alpaca Paper infrastructure are enabled. The last pre-migration inspection found 106
  active legacy shared-account Shadow deployments. The operator authorized retiring all of
  them and starting the isolated-sandbox policy no earlier than the September 14 open; that
  production rollout is pending exact-SHA CI and guarded deployment. C094 is deployed,
  deterministic automatic Candidate/Qualified Shadow admission is enabled, and the audited
  runtime is resumed. There are no
  Paper enrollments or orders. Live money remains structurally disabled.
- The deployed remediation adds `research_gate@0.4.0`, horizon-specific
  Candidate Shadow activity minima, horizon-specific research-trial accounting, budget-
  independent revalidation of accepted specs, scanner-pool authorization at Shadow start,
  a stricter Research LLM output prompt, and `llm_budget@0.2.0` with a `$40` critical/daily
  ceiling that preserves same-day spend across policy versions, single-attempt invalid LLM
  output handling, and leakage-safe annual ML under a six-year daily-bar target.
- Autonomous research creates and resumes only 5/20/63/126/252-session groups. One-session
  daily-bar research remains an explicit/manual reproducibility path and is not represented as
  a minute-level intraday strategy.
- Production now uses opt-in automatic broker-free Shadow admission: a current exact
  Candidate or Qualified result is idempotently adopted and started by the deterministic
  coordinator, while operator holds, Paper enrollment, Robinhood orders, and live money remain
  outside that automation.
- The latest 20-session cycle completed all stages and produced six Candidate-reviewable
  reports, but its scanner-pool revision became stale before the automatic rollout and was
  correctly refused. The current 5-session cycle completed ML and Research LLM for all 20
  symbols and is advancing through generation/validation. Its first current exact Candidate,
  AAPL, was transition-activated into 5-session Shadow; subsequent current eligible results
  will enter automatically.
- Local-lite uses Python 3.12, a project-local `uv`, SQLite, and filesystem object storage.
- The Control API, single-admin object-centric web console, persistent System Steward,
  append-only event ledger, deterministic risk engine, and shadow runtime exist.
- `live` is not a valid trading mode; `LIVE_TRADING_ENABLED=true` fails configuration validation.
  Paper submission is separately disabled by default and hard-pinned to Alpaca's Paper host.
- A dormant Robinhood MCP bridge is implemented for official-host-pinned OAuth PKCE, encrypted
  refresh tokens, runtime tool discovery, allowlisted reads, and equity-order preview. It has no
  place/cancel API or worker; configuration and production Compose reject order submission while
  `LIVE_TRADING_ENABLED` remains false. Robinhood's shared public MCP client requires its
  loopback OAuth callback for an unknown platform, so a one-shot local relay forwards only the
  authorization result to the VPS HTTPS callback. The verifier and encrypted tokens remain on
  the VPS. The production bridge is authorized and has completed server-side tool discovery plus
  audited, read-only account, portfolio, and watchlist calls; account identifiers stay redacted
  and no order tool has been called. The dedicated Agentic account is the sole account marked
  agent-accessible. Sixteen ordinary equity/crypto watchlists are readable; Robinhood currently
  rejects item expansion for its special empty Options Watchlist, which is isolated as an
  unavailable list rather than failing the complete read.
- Local source now implements ADR 0042: each immutable strategy/symbol Shadow deployment gets
  an isolated `$10,000` sandbox, risks 2% of current marked equity per position, uses
  point-in-time volatility/horizon/strategy stop geometry capped at 15%, and permanently
  retires at a `$8,800` marked-value floor. Explicit commission is zero while spread,
  slippage, impact, liquidity, and gap behavior remain modeled. The legacy migration is
  confirmation-gated and a deployment-time exposure boundary can stage Monday activation.
- Local lint, strict type checking, API readiness, the authenticated HTTP
  vertical slice, and the secret scan pass.
- Docker Desktop 4.89.0 / Engine 29.7.2 is installed on the current Apple Silicon Mac.
- The full Compose stack is healthy: PostgreSQL 17, Redis 8, MinIO, and the API all passed direct checks; the PostgreSQL-backed shadow slice recorded six lineage events.
- `context.md` is the required master record for architecture, discussions, iterations, commit contents, and post-commit global state.
- Alembic migrations now own the ledger and normalized market-data schema.
- The read-only Alpaca adapter supports SIP historical one-minute bars, OPRA option-chain snapshots, and SIP WebSocket authentication/stream parsing.
- The read-only Alpaca adapter also supports bounded most-active, stock-mover, and batched
  snapshot discovery. A versioned scanner merges those feeds with 83 reviewed theme seeds
  (including SNDK) and the Focus Watchlist, then filters and ranks a maximum 20-symbol deep-
  research shortlist.
- The Alpaca Paper account endpoint passed a real read-only probe: account active, USD,
  trading/account blocks false, and zero positions. No order endpoint was called.
- A real AAPL backfill stored 391 unique minute bars; replay inserted zero duplicates. A bounded OPRA request stored 10 unique option snapshots; replay inserted zero duplicates.
- Alpaca historical rows preserve their raw payload, OHLC, volume, and trade count while
  normalizing provider `vw: 0` sentinels to an unavailable VWAP, including the provider's rare
  positive-volume/zero-VWAP rows. Zero volume remains visible as a quality warning; negative
  VWAP still fails closed.
- Raw Alpaca responses are content-addressed in MinIO, normalized rows are stored in PostgreSQL, and new-record events are published to Redis Streams.
- The live collector has bounded reconnects and XNYS-calendar-aware intraday gap detection with automatic REST repair.
- A real open-session SPY run persisted SIP trades, quotes, and minute bars. A controlled
  reconnect detected a missing minute, emitted its gap event, and repaired it through REST.
- Historical windows are strictly half-open even though Alpaca's REST `end` is inclusive;
  out-of-window normalized rows now fail `market_data_quality@0.2.0`.
- Phase 2 stores immutable source-document versions, issuer entities, normalized SEC XBRL facts, and deduplicated catalysts.
- Alpaca News, SEC EDGAR, approved-host IR, and disabled-by-default social aggregate adapters exist.
- A real 10-article Alpaca News page passed MinIO/PostgreSQL/Redis ingestion; replay inserted zero new records or events.
- Ten Apple Newsroom primary-source entries passed the same path; replay inserted zero new records or events.
- Twenty AAPL SEC filing records normalized into 19 deduplicated catalysts; replay inserted zero new records or events.
- A bounded set of 250 AAPL SEC XBRL company facts normalized successfully; replay inserted zero duplicates.
- Immutable evidence packets, point-in-time feature snapshots, strategy specifications,
  experiment runs, backtest trades, corporate actions, historical universe membership,
  feature parity checks, and walk-forward reports are stored through Alembic revision
  `20260911_0037`, including exact validation and ML-training contracts, normalized Paper
  order legs, shadow risk lineage, fenced workflow
  attempts, generation-attempt audit, runtime leases, and the event outbox.
- The Phase 3 runner provides buy-and-hold, long/cash momentum, and long/cash
  mean-reversion baselines with next-bar execution, zero explicit commission, modeled market
  execution costs, metrics, hashes,
  and append-only completion events.
- `make research-smoke` exercises the complete research path in an isolated local database.
- Daily bars become available at the exact XNYS close, including early closes; split-adjusted
  features use only actions known and effective at `as_of`.
- Offline/full-history and online/as-of feature materialization produce persisted comparison
  hashes.
- The Phase 3B portfolio engine persists signal/order/fill/mark/action events, uses exact
  exchange open/close timestamps, models splits and gross cash dividends, and applies
  zero explicit commission, slippage, spread, fixed market impact, and a volume-participation
  cap.
- Phase 3C persists rolling train/embargo/test folds, evaluates every candidate both in and
  out of sample, reports train-to-test degradation and selection failures, and separates
  selected out-of-sample results into up/down/sideways realized regimes.
- Phase 3D adds combinatorial selection-risk/PBO diagnostics, Deflated Sharpe, and a
  versioned minimum-sample/performance gate. It can only grant eligibility for human review;
  bounded local evidence remains rejected or insufficient and never promotes automatically.
- Phase 5A validates bar identity, chronology, OHLC, availability, and exchange intervals
  before research; models half-spread on both fill sides; imports point-in-time reference
  batches with source/version hashes; and resumes idempotent date-partitioned backfills.
- The Phase 4A gateway routes critical research to OpenAI `gpt-5.6-sol` and interactive or
  routine work to Meta `muse-spark-1.3` through versioned configuration. Calls are bounded,
  fail closed without project credentials, and retain immutable hashes, usage, latency,
  status, and output.
- Both Responses API adapters pass mocked contract tests and bounded live probes using
  project-specific credentials in ignored `.env`.
- The local web Control Center is locked behind one persistent administrator session and
  CSRF protection. Its default view is a dedicated full-page System Steward workspace with
  persistent conversations and safely rendered Markdown; the remaining navigation exposes
  overview, lists, market-scanner evidence, data, strategy, shadow, pipeline, model, activity,
  and code-change objects.
- Overview reads the persistent LLM budget ledger and shows current daily/monthly estimated-
  USD consumption, reservations, limits, and provider/workload utilization.
- Each workload's daily estimated-USD limit is editable through an immutable, explicit-
  confirmation revision. There is no operator token ceiling. Changes preserve current-period
  spend and immediately recalculate percentage usage against the new cap; the YAML project
  daily limit remains a hard cap. Reservations abandoned by a process interruption expire
  after the provider's configured timeout plus a five-minute safety margin without erasing
  settled spend.
- One System Steward reads a bounded current-state snapshot, including detailed Paper account,
  position, enrollment, order, and run state; returns validated object citations; persists
  conversations; and can propose allowlisted admin actions. It cannot execute them; a separate
  exact confirmation is required.
- The persistent broker-free shadow runtime admits only the exact static strategy ID and
  execution contract covered by a tier-eligible, human-confirmed validation certificate.
  `CANDIDATE` is observation-only and may collect forward evidence without passing strict
  statistical qualification; `QUALIFIED` passed the strict gate. Candidate deployments are
  deterministically blocked from Alpaca Paper.
  It records candidate → deterministic risk decision → approved plan → execution-price risk
  review → modeled virtual order/fill lineage, cash, and P&L. Actual observation, approval,
  and persistence timestamps are separate from the market-data cutoff; a plan must exist
  before its market session. Its DAY limit is capped at the rounded decision close; a real
  limit touch rechecks reward/risk and quantity, while an untouched order records no fill.
  Missed/late bars never become forward fills. Research-only buy-and-hold cannot be
  shadow-adopted.
- Generated momentum and mean-reversion specifications may use immutable 1, 5, 20, 63, 126,
  or 252-session horizons. The ML label, forecast, LLM proposal, replay, and exact validation
  share that horizon. Multi-session Shadow persists open-position accounting across restarts,
  checks stop/target on every completed bar, handles recorded splits/dividends, and exits at
  the maximum holding session. Global pause blocks new exposure but continues existing exits.
  The scheduler preserves that exit-management contract even when global or pipeline entry pause
  is active. Per-deployment faults produce an audited degraded run while healthy sleeves
  continue; only an all-deployment failure reaches worker restart protection.
- Provider-observed listing/resumption boundaries propagate through market reads, feature
  materialization, ML datasets, LLM evidence, and exact validation. Old raw bars and feature
  snapshots remain auditable but cannot train the current issuer after a ticker-reuse boundary.
- Candidate and Qualified Shadow certificates retain their exact admission tier during every
  runtime recheck. Candidate sleeves are no longer incorrectly quarantined as though they had
  claimed strict qualification.
- New Shadow deployments are isolated strategy/symbol sandboxes with `$10,000` initial
  virtual cash. Each risks 2% of current marked equity per position; another strategy cannot
  consume its capital or capacity. Stop distance uses point-in-time realized volatility,
  holding horizon, and strategy family, clamped to 3%–15%, while the target uses a versioned
  strategy-family R multiple. Explicit commission is zero, but spread, slippage, impact,
  liquidity, and gap risk remain modeled. At `$8,800` total marked value the sandbox blocks
  entries, liquidates an open virtual position causally, and permanently retires that exact
  strategy version. The former shared account is retained only for historical migration.
- Active deployments recheck the exact execution contract on every tick. Engine, cost, risk,
  feature, or restriction changes move stale deployments to `REVALIDATION_REQUIRED` and
  cancel reserved plans without resetting account history.
- A Shadow plan is first persisted as `PENDING_ACTIVATION` and becomes executable only after
  a post-commit clock check proves durability before the next session open. Shadow admission
  explicitly supports `1Day` only; unsupported minute strategies fail before deployment.
- A daily Shadow deployment activated after a completed close but before the next exchange open
  can arm that latest bar exactly once for a causally valid next-open decision. The arm is
  journaled; activation after the open still skips the bar and cannot manufacture a late fill.
- Historical pre-migration evidence: 17 production Candidate deployments used that guarded
  path before the September 9 open.
  The first scheduler tick evaluated 17 bars, produced 15 long candidates, approved nine open
  virtual plans, rejected six when the shared `$780` concurrent-risk ceiling could not support at
  least one share, and left two flat. Post-close processing opened two virtual positions (INTC
  and IREN) before a 252-session calendar-boundary exception blocked the remaining plans; C090
  widens the fixed exchange-calendar range and isolates future per-sleeve faults. No broker order
  was created.
- The deployable `next_session_day_limit_bracket_moc@0.1.0` contract governs one-session
  validation, Forward Shadow, and Alpaca Paper. It includes identical conservative price
  rounding, DAY limit-entry semantics, stop-first ambiguity, and no target credit after an
  ordering-ambiguous intraday fill.
- Multi-session research and Shadow use
  `next_session_day_limit_bracket_timed_exit@0.1.0`. Alpaca Paper intentionally rejects that
  profile until next-session-only entry expiry, durable GTC protection, and timed-exit
  recovery are implemented and separately validated.
- The Phase 7 Alpaca Paper adapter persists deterministic, broker-account-bound intents and
  normalized entry/target/stop/scheduled-close/emergency-exit legs. It performs idempotent
  client-ID recovery, cancels the bracket group twenty minutes before close, submits a
  deterministic MOC exit, and falls back to a separately identified DAY market exit on a
  late/rejected close. Partial fills no longer require an unimplemented manual exit; all new
  exposure remains blocked until broker-flat evidence completes the lifecycle.
- Paper order reconciliation refreshes positions after every observed order transition and
  submission acknowledgement, so a late partial fill cannot be closed using a stale snapshot.
- Old Shadow certificates cannot authorize Paper. Only a freshly generated exact validation
  containing the complete current profile and parameters can enroll. Paper remains off by
  default, account-pinned, single-lifecycle-per-symbol, and hard-pinned to the simulated host;
  the live host remains impossible.
- A persistent hourly coordinator rotates through 5, 20, 63, 126, and 252-session
  research horizons and owns the gap-repaired market-data → source-specific
  document history →
  feature → ML → forecast → Research LLM → constrained strategy → exact-validation →
  shadow-admission DAG. It checks the full configured XNYS window instead of trusting only the
  latest bar, recovers older incomplete hourly groups, records `WAITING_*` business gates,
  and defaults paid research and automatic Shadow admission off. When explicitly enabled, the
  deterministic final stage may adopt and start only an exact current Candidate/Qualified result;
  neither LLM can grant that eligibility or reach Paper/broker execution.
  Incomplete current or backlog cycles are rechecked every minute, so an expired worker lease
  is reclaimed promptly instead of waiting for the next hourly scan cadence.
  The coordinator refreshes its worker heartbeat during long scans/training cycles. Provider 429
  responses become a bounded `WAITING_LLM_PROVIDER_RATE_LIMIT` business state rather than
  exhausting the same job, and all-zero provider volume baselines materialize conservatively.
  A durable current-edge group refreshes the union of the selected research universe and active
  Shadow symbols before older downstream research work, so scanner rotation or a deep LLM/ML
  backlog cannot starve the bars needed for position and plan management.
  Validation reuse is bound to the exact execution/data contracts, current promotion policy,
  and current horizon-specific research-search count. When a new paid generation stage is
  unavailable, validation rotates through accepted immutable specs for the same horizon. ML
  reuse is bound to dataset plus full training contract.
  Exhausted jobs do not starve later groups and require one confirmation-gated retry. Every
  stage honors its persisted subsystem pause control.
  Every stage now reapplies the immutable job's `horizon_bars`; the earlier runtime omission
  caused labeled multi-horizon groups to fall back to one-session ML/validation and is covered
  by regression tests. Production daily-bar lookback is now approximately six calendar years;
  the ML fold allocator reserves a full horizon plus at least 20 independent observations for
  calibration and model selection, while the final holdout retains its separate 60-row
  promotion minimum. This makes 252-session training achievable for mature issuers without
  overlapping-label leakage; short-history issuers wait explicitly.
  One-session daily-bar research remains available only as an explicit/manual experiment and
  is not presented as true intraday research, which requires a separate minute-data contract.
- Production daily bars target 2,192 days, while Alpaca News/SEC evidence target 1,826 days.
  News advances backward
  in bounded partitions (90-day default; current production override 180 days) while its
  current edge is refreshed; SEC filing metadata and
  company facts refresh once per symbol per day. Trades, quotes, and option chains are
  truthfully labeled forward-only, while corporate actions and historical universe
  membership still require reviewed reference data rather than fabricated backfill.
  News progression trusts only explicit successful, untruncated coverage certificates;
  an older article returned because it was recently corrected remains searchable but cannot
  skip an unverified interval. The current-edge refresh is always bounded to one day.
- When enabled, the dynamic scanner runs before that DAG and binds each coordinator job to
  its immutable scan ID. Deterministic price, dollar-volume, restriction, and benchmark
  gates precede a four-hour, USD-budgeted LLM re-rank of at most 40 supplied names. When the
  separate autonomous-pool flag is enabled, only a completed LLM review may refresh the
  bounded Scanner Trading Pool; skipped/failed review holds the previous revision. Each scan
  records admitted, added, and removed symbols plus exact list, scan, and invocation lineage.
  Pool membership permits progression toward Shadow adoption but cannot validate or adopt a
  strategy, start Shadow, enable Paper, or submit an order.
- Newly listed scanner candidates use immutable provider-observed history boundaries after a
  complete leading-window probe and strict validation from the first observed bar forward.
  Lookback expansion forces a new probe; internal/trailing gaps and insufficient ML/validation
  samples still fail closed.
- A distinct post-suspension boundary requires at least 20 missing sessions after 20 explicit
  zero-volume placeholders and a positive-volume resumption. Only the strictly valid resumed
  segment feeds feature construction and every underlying exact-validation backtest; older
  rows remain auditable, while ordinary internal gaps inside the selected segment still fail.
- Later Research LLM calls receive a bounded, content-hashed, point-in-time outcome summary
  derived from backtests, validation reports, Shadow events, and Paper state already known at
  the cutoff. That summary selects decision-useful metrics and falls back to counts/latest
  records instead of exceeding the evidence-item size contract as history accumulates.
  Forecast evidence also carries label semantics, untouched-holdout metrics,
  calibration, drift, model gate, dataset, and training-contract identity. This closes the
  research-feedback wiring without granting the LLM runtime power.
- The global new-exposure pause is enforced inside the shadow tick boundary, so a manually
  confirmed tick cannot create a new plan. Existing multi-session positions continue only
  deterministic risk-reducing mark/exit processing so the kill switch cannot strand risk.
- Tactical risk evaluation now requires explicit, auditable catalyst, restriction-status,
  liquidity, data-health, macro-calendar, and duplicate-order facts. Unknown/unsafe facts
  reject, and `risk_policy@0.4.0` applies a 24-hour major-macro-event blackout unless the
  strategy is separately approved for that event. The policy also owns the baseline
  stop/target geometry used identically by research and shadow. Candidate/snapshot mismatches
  and future signal/feature timestamps also reject.
- GitHub `origin` is `https://github.com/wsjnohyeah/qagent.git`; production currently runs the
  verified immutable functional commit `4fe2d71b372a3731140189a18450a7fa32f74ee6` while the
  isolated-sandbox release awaits CI and guarded deployment.
- GitHub Actions uses the current Node 24-based `actions/checkout@v7.0.1` and
  `astral-sh/setup-uv@v10.0.1` releases. A verified `main` push publishes an immutable GHCR
  commit-SHA image with matching embedded/OCI source provenance; deployment rejects mismatches.
- The source now batches large SEC company-fact writes and resolves their durable IDs in
  bounded queries after a production AAPL response with 5,291 facts exposed PostgreSQL's
  per-statement parameter ceiling. The regression suite contains 188 tests.
- The last complete release audit passed lint, strict typing, the full test suite,
  authenticated local and PostgreSQL/MinIO/Redis doctors, JavaScript parsing, fresh schema
  upgrade and PostgreSQL schema-drift checks. The current source migration head is
  `20260911_0037`; production remains on `20260909_0036` until this rollout completes.
- A local real read-only dynamic scan merged 258 source names, retained 40 review candidates
  and 20 deep-research stocks, included SNDK, and excluded sampled leveraged/single-stock
  ETFs. Its budgeted Meta re-rank cost an estimated `$0.008322` and moved SNDK from
  deterministic rank 8 to final rank 4 without introducing a symbol or execution authority.
  Production independently reproduced the 258 → 40 → 20 funnel. The v0.2.0 scan completed a
  Meta review for estimated cost `$0.004149` and recorded all 20 additions in Scanner Trading
  Pool revision 2, while the administrator's manual Trading Universe remains the separate
  four-symbol revision `[AAPL, IWM, QQQ, SPY]`.
- Production completed two consecutive 20-symbol, 180-stage coordinator cycles after Paper
  activation. The first cycle made 20 `gpt-5.6-sol` research calls: 19 evidence-based
  abstentions and one invalid-structure rejection. No candidate crossed the strategy gate, so
  the system correctly created no validation, Shadow deployment, Paper enrollment, position,
  or order. Total settled project LLM spend at verification was `$1.218124` against the `$20`
  daily cap; one deploy-interrupted `$0.118510` reservation was expired and reserved spend
  returned to zero without altering settled spend.
- The production campaign initially established five-year daily bars or an evidenced
  listing/resumption boundary for 23 symbols, 119,952 SEC facts across the active 20-symbol
  research set, and explicit Alpaca News coverage certificates for every one of those 20
  symbols. News history is still converging backward by durable 180-day partitions and must
  not be described as fully backfilled yet. The hybrid funnel has compiled 12 exact strategy
  specifications from 13 generation attempts. At the 2026-09-08 06:04 UTC checkpoint, the
  first exact validations were all `REJECTED` by the former strict gate. A later audit found
  that roughly 73% of recorded OOS folds had no trade and were incorrectly included in the
  old positive-fold denominator; several strategies had actual trade win rates above 50%.
  `research_gate@0.4.0` separates total/active/no-trade folds and OOS trade count, scales
  Candidate activity minima to the holding horizon, and retains positive exact specs for
  broker-free forward observation. Eight 63-session reports are now Candidate-Shadow eligible;
  six (EOSE, IREN, LITE, NVDA, SMR, TSLA) belong to the current Scanner Trading Pool. A real
  AAPL 252-session production probe repaired the extra year to 1,507 bars, trained on 1,235
  labels under the 252-session embargo, completed ML + Research LLM + generation + exact
  validation, and correctly rejected the negative-net-return strategy. No production adoption,
  Shadow deployment, Paper enrollment, position, or order has yet been created; new exposure
  remains paused pending exact administrator review.
- The production NBIS retry established an evidenced post-suspension boundary at `2024-10-21`.
  Its 470-bar resumed segment passed strict quality with zero missing intervals; the older raw
  and normalized history remains available for audit but is excluded from current research.
  After the validation-fold boundary propagation fix, its previously failing exact validation
  completed normally and returned `REJECTED` on statistical evidence (0.275 positive-OOS fold
  rate and -0.000205 compounded OOS return) rather than a data-quality infrastructure error.
- A real bounded AAPL coordinator run trained 734 point-in-time examples, persisted two ML
  candidates and a one-bar forecast, supplied 14 time-safe feature/forecast/document items to
  `gpt-5.6-sol`, and received a citation-valid `ABSTAIN` at 0.90 confidence. The selected ML
  candidate failed its promotion gates (final-holdout ROC AUC 0.4711, Brier 0.2606), so no
  strategy, adoption, Shadow trade, or Paper order was produced. The final call cost estimate
  was `$0.070045`; the complete audit is in
  `docs/E2E_DEPLOYMENT_READINESS_AUDIT_2026-09-06.md`.
- The independent review of `3b3926e` is dispositioned in
  `docs/REVIEW_REMEDIATION_3B3926E_2026-09-06.md`. Its N01–N07 counterexamples now have
  subject-aware admission, actual-time forward guards, calendar-correct ML labels,
  execution-price revalidation, active-contract quarantine, legacy event reconciliation,
  and search-trial accounting.
- The independent review of `c6a8020` is dispositioned in
  `docs/REVIEW_REMEDIATION_C6A8020_2026-09-06.md`. Its R01–R07 counterexamples now have
  direct fixes or explicit fail-closed scope gates; the report's G01/G02/G04/G05 product and
  production-evidence work remains visible rather than being claimed complete.
- The independent review of `de4c3d0` is dispositioned in
  `docs/REVIEW_REMEDIATION_DE4C3D0_2026-09-07.md`. F01–F06 are fixed or independently
  confirmed as already fixed on the later baseline. The separate Paper execution-policy and
  automatic-exit milestone remains fail-closed rather than being relabeled as complete.
- Phase 4 adds point-in-time document retrieval, `research_analysis@0.2.0`, exact quotation
  validation, deterministic abstention, atomic estimated-USD reservations, and Decision
  Inspector graph `ai_infrastructure_graph@0.1.0`.
- Phase 5 builds point-in-time labels, compares logistic and boosted-stump models with
  embargoed walk-forward splits, separates purged OOS calibration, model-selection, and final
  holdout partitions, measures PSI drift, stores JSON artifacts/forecasts, and enforces human-
  only champion promotion. Labels match the executable next-open-to-future-close contract
  and advance by actual bars rather than sparse snapshot rows.
- A constrained generator combines a matching feature snapshot, linked ML forecast, and
  evidence-bound analysis, then requires adversarial LLM critique before compiling an
  immutable research-only strategy DSL. It cannot emit code, size exposure, adopt a strategy,
  or place an order. A valid cited `ABSTAINED` analyst judgment is advisory and may still feed
  an explicitly exploratory proposal; malformed/rejected output remains blocking, and exact
  deterministic validation remains the empirical gate.
- Bounded local ML evidence remains `CANDIDATE`; no model or strategy has been promoted.
- `.env` explicitly selects `APP_ENV=development`; development daily/news backfills are
  capped at 120 days and one-minute backfills at 7 days by default. The active scope is
  exposed by `/v1/system/status`.
- Production settings fail unless authentication is enabled with a hash-only password, new
  exposure starts paused, and automatic migration is disabled. The production local raw
  archive is mounted on a persistent named volume.
- Production Compose separates the authenticated API from dedicated persistent shadow and
  coordinator workers with SQL heartbeats. Workflow jobs have dependency-aware leases and ownership checks; ledger
  events have a retryable SQL outbox with stable event IDs, payload-redacted dead-letter
  visibility, and confirmation-gated single-event requeue.
- Phase 6.1 data pages are ticker-first and show populated plus missing price, news, filing,
  fact, action, option, stream, feature, ML, and LLM datasets with true event-time coverage,
  separate ingestion time, date grouping, pagination, readable records, and collapsed raw
  payloads. Strategy pages now explicitly
  distinguish deterministic baselines from hybrid candidates and show the full point-in-time
  data → ML → Research LLM → generator → critic → exact spec → validation → shadow chain in
  readable cards; JSON is relegated to Advanced diagnostics. Pipeline jobs and quality
  reports are individually inspectable, and coordinator cycles show all nine stage states.
- The guarded deploy command now migrates once, runs an idempotent production bootstrap,
  registers a non-reusable environment identity, creates account/list defaults, forces the
  global pause, and then verifies API/worker health. Development runtime data is never copied
  as production evidence.
- VPS helpers create and structurally verify checksummed PostgreSQL plus raw-object backups.
  The first production backup passed checksum/catalog verification and an isolated restore at
  schema `20260907_0031`. An automated off-site copy remains required for durable disaster
  recovery.
- Production worker/coordinator container checks allow the measured analytics cold-import
  latency (60-second interval, 20-second timeout) while deploy-time heartbeat gates remain
  immediate and blocking.
- The detailed source/module-to-North-Star disposition is recorded in
  `docs/PRE_DEPLOY_NORTH_STAR_REVIEW_2026-09-06.md` and ADR 0026.

## Next

1. Authorize the dormant Robinhood MCP bridge in Control Center, inspect its authenticated tool
   schemas, and verify read-only plus order-preview behavior. Do not submit an order; idempotency,
   reconciliation, account binding, and a separate live-money decision remain open gates.
2. Replace the temporary `sslip.io` hostname with the operator's permanent domain, select an
   off-site backup target and external alert destination, then automate both retention and
   notification checks.
3. Deploy the reviewed `research_gate@0.4.0` correction, then let horizon-correct cycles
   revalidate both new and previously accepted exact strategies,
   and review any Candidate/Qualified Shadow eligibility without
   manufacturing a pass. Candidate deployments may gather broker-free forward evidence;
   only a Qualified one-session deployment may be separately enrolled in Paper.
4. Continue production-scale history/evidence coverage and collect Phase 5 statistical plus
   continuous-Shadow evidence and ML-only versus ML+LLM ablations.
5. Continue interactive Phase 6.1 UI review with real operator navigation and refine labels;
   the Strategy lineage redesign is implemented locally and awaits operator feedback.
6. Extend fill realism with multi-bar partial fills, order cancellation, quote-derived
   rather than configured spread, and symbol-change/delisting replay.
7. Add an operator-selected provider-lag/sequence-gap notification channel; dead-letter replay
   is now inspectable and confirmation-gated in the Control Center.

## Blocked

- Durable disaster recovery and alerting need operator-selected off-site storage and a
  notification destination. The running stack currently has TLS, host firewalling, a local
  verified backup, and an isolated restore drill.
- Sending the first Paper order remains evidence-blocked until a newly generated strategy
  passes the exact gate and is adopted, started in Shadow, and specifically enrolled. The
  user has authorized and the system has enabled Paper, but no fixture test or deployment
  health check may manufacture a strategy or order.
- Multi-session Alpaca Paper execution is implementation-blocked, not evidence-blocked. The
  safe broker contract must separate a next-session-only DAY entry from durable protective
  exits and recoverable timed liquidation. Multi-session research/backtest/validation and
  broker-free Shadow do not bypass that boundary.

## Decisions

- ADR 0001: Redis Streams for the MVP event bus.
- ADR 0002: PostgreSQL for full environments; SQLite only for local-lite.
- ADR 0003: a no-build Control Center page for Phase 0.
- ADR 0004: Alpaca first, pending entitlement verification.
- ADR 0005: version source evidence and deduplicate catalysts deterministically.
- ADR 0006: persist point-in-time research artifacts and validate with next-bar, cost-aware
  baselines before connecting LLM/ML strategy generation.
- ADR 0007: use exchange-session availability and bitemporal reference data; require
  offline/online feature parity and fail closed where accounting is unsupported.
- ADR 0008: persist an event-driven portfolio ledger and model split/dividend accounting,
  next-open/session-close fills, costs, market impact, and liquidity caps.
- ADR 0009: use non-overlapping rolling out-of-sample windows with an embargo, retain every
  candidate run, and report selection degradation and realized-regime results.
- ADR 0010: use a provider-neutral Responses API gateway with versioned workload routing,
  immutable invocation audits, bounded calls, and no automatic cross-provider fallback.
- ADR 0011: keep the YAML route map as a reviewed base, append development Control Center
  overrides as immutable revisions, and provide bounded Auto/OpenAI/Meta research chat.
- ADR 0012: calculate PBO and Deflated Sharpe on pre-purged OOS folds and apply a versioned,
  fail-closed eligibility gate that cannot promote automatically.
- ADR 0013: require persisted market-data quality checks, explicit spread cost, governed
  reference imports, and durable resumable partitions before Phase 6.
- ADR 0014: bound every LLM call by versioned budgets and accept research analysis only when
  point-in-time evidence, structured output, and exact citations validate.
- ADR 0015: train transparent chronological ML baselines, measure calibration/drift, store
  safe JSON artifacts, and require deterministic eligibility plus a human for champion status.
- ADR 0016: verify real SIP persistence/recovery during an open session and enforce half-open
  historical windows plus production fail-closed startup/persistence invariants.
- ADR 0017: use one authenticated System Steward with object context and explicit,
  expiring, single-use confirmation for sensitive operations.
- ADR 0018: run adopted strategies in a persistent, idempotent, broker-free shadow runtime.
- ADR 0019: require explicit external risk context and enforce pause at the shadow boundary.
- ADR 0020: bind exact research/execution contracts and use recoverable Phase 6 operations.
- ADR 0021: require forward shadow timing, complete execution-contract binding, durable event
  reconciliation, and attempt-level workflow/runtime fencing.
- ADR 0022: originally used one shared virtual account and continues to govern the persistent
  autonomous research DAG plus immutable development/production environment identities; its
  Shadow account topology is superseded by ADR 0042.
- ADR 0023: separate static and selector validation semantics, require actual-time persisted
  plans and next-open risk review, quarantine stale execution contracts, and reconcile legacy
  ingestion IDs without rewriting immutable events.
- ADR 0042: evaluate every immutable strategy in an isolated `$10,000` Shadow sandbox with
  2% current-equity risk, volatility-derived stop/target geometry, a 15% stop ceiling, zero
  explicit commission, and permanent retirement at an `$8,800` marked-value floor.
- ADR 0024: isolate Alpaca Paper behind an exact host, durable idempotent intents, account and
  risk reconciliation, and per-deployment administrator confirmation; retain no live path.
- ADR 0025: reject Shadow certificates at the Paper boundary; require a separately validated
  Paper lifecycle, reauthorize every POST, and keep positions open until broker-flat evidence.
- ADR 0026: continuously repair daily data and refresh news, feed point-in-time outcomes back
  into research, publish commit-addressed images, and distinguish backup integrity from restore.
- ADR 0027: bind validation and ML reuse to current behavior contracts, classify failed LLM
  calls as infrastructure recovery, make workflow exhaustion explicit/fair/retryable, and use
  coherent order-then-position Paper reconciliation.
- ADR 0028: discover a bounded dynamic research universe from market activity, theme seeds,
  and operator focus; permit only budgeted constrained LLM re-ranking; keep all execution
  authority behind the governed Trading Universe and existing deterministic/human gates.
- ADR 0029: distinguish provider-observed listing-era history starts from internal data gaps;
  retain immutable probe evidence and never weaken completeness after the observed boundary.
- ADR 0033: use one deployable DAY-limit/bracket/MOC profile across validation, Shadow, and
  Paper; persist every broker leg and recover deterministic emergency exits.
- ADR 0034: organize data by ticker and true event time, converge source-specific historical
  coverage, permit advisory LLM abstention to reach falsifiable generation, and govern price
  geometry separately from account-dollar risk.
- ADR 0035: bind ML, LLM generation, replay, validation, and Shadow to explicit 1–252-session
  horizons; keep multi-session Paper fail-closed pending a separate broker lifecycle.
- ADR 0036: preserve the planned horizon through every coordinator stage, use active folds for
  sparse-strategy stability, and split broker-free Candidate Shadow from Qualified/Paper.
- ADR 0037: use horizon-aware Candidate activity/search accounting, revalidate accepted specs
  when new paid generation is unavailable, freeze approved search counts for running Shadow,
  and carry LLM spend across budget-policy versions.
- ADR 0041: allow configured deterministic auto-adoption/start for exact current Candidate or
  Qualified results in broker-free Shadow; preserve operator holds and every Paper/live boundary.
