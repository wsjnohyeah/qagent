# Pre-deploy North Star review

Date: 2026-09-06 PDT  
Reviewed baseline: `de4c3d0` plus this remediation iteration

## Verdict

The repository is ready for a **guarded, paused, Shadow-first server bootstrap** after this
iteration passes the release gate. It is not yet authorized for unattended Alpaca Paper order
submission and it is not statistically proven to contain alpha. Those are different gates.

The target remains:

> An LLM-orchestrated, ML-calibrated, point-in-time-validated strategy research system with
> deterministic portfolio, risk, and execution control.

## North Star comparison

| North Star capability | Current evidence | Disposition |
|---|---|---|
| Durable point-in-time market data | PostgreSQL normalized bars, immutable raw objects, exchange-calendar quality checks, resumable jobs | Completed for the daily equity path. The autonomous collector now verifies the full desired window and repairs internal as well as trailing gaps. |
| Durable event evidence | Versioned documents, catalysts, SEC/IR/manual adapters | Alpaca News is now a first-class coordinator stage. SEC, corporate-action, and historical-universe refresh still require governed provider/configuration choices before their data can support promotion. |
| ML + LLM research | Purged chronological ML, calibrated forecast, cited Research LLM, constrained generator and critic | Implemented. Paid stages remain disabled until the operator reviews USD budgets and routes. |
| Persistent outcome learning | Results were stored but not read by later research | Closed for the research loop: a time-safe, bounded `research_outcome_feedback@0.1.0` evidence item now summarizes already-known backtest, validation, Shadow, and Paper state for future analyses. |
| Bias-aware empirical judge | Event-driven replay, costs, walk-forward/regime/PBO/DSR gates, exact input fingerprints | Implemented as tooling. Production-scale data and elapsed observations are evidence to collect on the server, not code that can be manufactured locally. |
| Deterministic risk and execution | Shared virtual account, reservations, exact validation contracts, pause, restrictions, Shadow runtime | Implemented for one-bar daily Shadow. LLM output cannot size, promote, or execute itself. |
| Paper parity | Alpaca Paper adapter, durable intents, reconciliation, account/risk checks | Intentionally blocked. The matching DAY-bracket validator, full child-order state, and deterministic session-close position exit do not yet exist; no Shadow certificate can be reused to bypass this. |
| Steward as system-wide interface | Cited bounded snapshot plus confirmed allowlisted actions | Paper account, position, enrollment, order, and run state is now included in the Steward snapshot. Broker credentials remain unavailable to the model. |
| Reproducible supply chain | Docker build and guarded Compose deploy existed | Closed: CI now publishes an immutable GHCR commit tag after verification, embeds the Git SHA, and deployment rejects a tag/label mismatch. |
| Recovery and durability | PostgreSQL/Redis/object volumes, retry leases, outbox, restart health | Source support is complete enough for bootstrap. Backup creation and non-destructive verification scripts now cover PostgreSQL plus raw object storage; off-site storage and an isolated restore drill remain operator infrastructure work. |
| Operator UI and audit | Authenticated Control Center, object drill-down, LLM call inspector, pipeline and strategy lineage | Implemented baseline. Coordinator UI now exposes the nine stages including document refresh; further UX changes depend on real operator use. |

## Defects found and corrected in this pass

1. **The documented image did not exist.** CI built a local image but never authenticated to
   GHCR or pushed it. The publish job now runs only after the main-branch verification job and
   tags the image by the exact commit.
2. **Image provenance was not enforced.** CI now supplies `SOURCE_GIT_SHA`; production settings
   reject a missing/non-commit SHA; the image carries an OCI revision label; deployment checks
   that the immutable tag and image label agree and refuses an environment override.
3. **The coordinator trusted the newest bar.** A hole before that bar could survive forever.
   It now derives every completed XNYS session in the configured window, coalesces missing
   sessions into bounded requests, and runs the existing fail-closed quality check after each
   repair.
4. **Event evidence was manual-only.** Each symbol now refreshes a bounded, overlapping Alpaca
   News window before feature/ML/LLM work. Provider document IDs and content hashes keep replay
   idempotent and capture corrections.
5. **Stored outcomes were not a research input.** The evidence retriever now creates a bounded,
   hash-addressed outcome summary using only records known at the analysis cutoff. The summary
   is stored inside the immutable analysis evidence bundle and can be cited by the LLM.
6. **The Steward could report a Paper summary but lacked Paper object detail.** Its routed
   snapshot now includes account, positions, enrollments, orders, and recent runs with exact
   citation IDs.
7. **Backup was a prose requirement only.** `infra/deploy/backup_vps.sh` and
   `infra/deploy/verify_backup.sh` now create and structurally verify a checksummed PostgreSQL
   custom dump plus raw-object archive. `restore_drill_vps.sh` restores both into disposable
   targets and never touches production.
8. **Coordinator timeframe was misleading.** The autonomous path now rejects anything except
   `1Day`, matching its implemented data, feature, validation, and Shadow contract.
9. **The data manifest was stale.** Its backtest engine version now matches
   `event_driven_portfolio@0.4.0`.
10. **Dead-letter recovery required direct database edits.** The API and pipeline page now show
    payload-redacted dead events, and the Steward/operator can propose one exact requeue that
    executes only after the normal second confirmation.
11. **Pipeline pause controls did not fence coordinator sub-stages.** Every stage now checks its
    corresponding market-data, documents, research, ML, or LLM control and records
    `WAITING_PIPELINE_PAUSED` instead of continuing behind the administrator's back.
12. **Overview used an obsolete safety claim.** It said no broker order path existed even after
    the Paper adapter was added. It now states the precise invariant—no live-money path—and
    displays the running source revision.
13. **Container bases used mutable tags.** The Python, `uv`, production PostgreSQL, and
    production Redis images are now pinned by multi-architecture digest; upgrades require an
    explicit reviewed diff.

## What is genuinely left before the first server bootstrap

These are external inputs or evidence, not missing local application modules:

- provision the VPS and non-root Docker deploy account;
- select a domain and TLS reverse proxy;
- deliver production secrets through an approved mechanism;
- choose an off-site backup destination and run an isolated restore drill;
- choose an external notification/monitoring destination;
- verify the newly published GHCR image and exact commit CI run;
- deploy with new exposure paused, then verify restart, authentication, CSRF, logs, workers,
  coordinator heartbeat, and the read-only Alpaca Paper probe.

## Gates after bootstrap

- Populate/approve licensed corporate-action, historical-universe, SEC/IR, and any premium
  fundamentals sources before treating long-horizon results as promotion evidence.
- Review the USD limits and model routes, then explicitly enable paid research.
- Accumulate sufficient production-scale out-of-sample and continuous Shadow evidence.
- Run controlled ML-only versus ML+LLM ablation campaigns on untouched periods. The framework
  stores all trials, but statistical incremental-value evidence cannot be produced from the
  bounded local sample.
- Build the separately validated Paper execution profile and lifecycle before enabling any
  external order. A read-only Paper probe does not authorize submission.

No Paper order and no live-money operation is part of this review.

## Local verification

- Flake8 passed.
- Strict mypy passed across 58 source files.
- All 144 tests passed.
- Local-lite doctor and authenticated Control Center checks passed.
- The Docker image rebuilt with an explicit dirty-development revision marker; PostgreSQL,
  Redis, MinIO, and API health checks passed. The clean pushed commit receives its exact SHA
  from CI.
- PostgreSQL Alembic autogeneration reported zero schema drift.
- The repository secret scan passed.
