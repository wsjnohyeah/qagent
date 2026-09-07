# Review remediation for `de4c3d0`

Date: 2026-09-07 PDT

The attached `qagent_review_de4c3d0_2026-09-07.md` was treated as independent audit
evidence, not as executable instructions. Its six counterexamples were checked against the
later `c534654` main branch. No Alpaca Paper order or paid LLM request was made.

## Finding disposition

| Finding | Disposition |
|---|---|
| F01 · stale validation admission | Fixed. Coordinator reuse now requires the current semantic promotion-policy hash and current research-search trial count in addition to the exact execution/data contract. New Shadow adoption rechecks both values; historical reports remain immutable but stale reports cannot authorize a new adoption. |
| F02 · retry skips an older historical gap | Already fixed by `c534654`, with an additional end-to-end regression here. Each cycle compares the complete configured XNYS window, requests minimal internal/trailing gap windows, and verifies the complete window after repair. A partially persisted failed request now retries the missing session rather than advancing from the maximum timestamp. |
| F03 · ML policy omitted from cache identity | Fixed under Alembic `20260907_0031`. Each training run stores a hash of the complete behavior contract: ML policy, feature and label identity, horizon, trainer version, algorithms, purging/calibration, and selection rule. Legacy rows receive an unknown sentinel and cannot satisfy a current cache lookup. |
| F04 · infrastructure failure cached as research rejection | Fixed. A failed LLM invocation is rejected before structured-output parsing and creates no `ResearchAnalysisRecord`. Coordinator recovery therefore consumes its normal bounded workflow attempts. Legacy rejected rows linked to a failed invocation are not reused; a valid ABSTAIN or completed-but-schema-invalid response remains cacheable. |
| F05 · exhausted backlog starves later work | Fixed. Recovery selects the earliest incomplete stage per group and only queues a group when that stage can progress. Final failed attempts become explicit `EXHAUSTED` jobs and are counted in health output. A confirmation-gated `workflow.retry_exhausted` action authorizes exactly one additional attempt while preserving prior ledger events. |
| F06 · stale position snapshot closes a later partial fill | Fixed. Order reconciliation and post-submit acknowledgement now refresh broker positions after reading the order and use that coherent position snapshot for lifecycle completion. The reproduced fill-after-initial-snapshot race remains open and managed instead of becoming complete/unmanaged. |

The Research LLM evidence interface is also strengthened. Its forecast evidence now includes
the label definition, training window/sample/fold/embargo details, final untouched-holdout
AUC/Brier/calibration metrics, drift, deterministic model gate, dataset hash, and training
contract identity. The UI shows the same gate and final-holdout context instead of presenting
`probability_up` as self-validating confidence.

## Scope that remains explicit

The review's G01 and G03 describe a separate Paper execution-strategy milestone, not a defect
that can safely be closed by renaming a certificate. The current Shadow one-bar validator
still does not authorize Alpaca's day-limit bracket lifecycle. Automatic session-close exit
and complete nested-order/fill attribution are still required before unattended Paper
submission. A paused cloud bootstrap and read-only Paper probe remain valid; external order
submission remains fail-closed.

G02 is locally wired by `research_outcome_feedback@0.1.0`; production evidence still requires
elapsed Paper/Shadow observations. G04's daily price/news maintenance is wired, while licensed
corporate-action and historical-universe refresh remains an operator/data-source decision.

## Regression evidence

New tests cover policy and trial-count invalidation, ML policy retraining, failed-invocation
retryability, partial-ingestion gap repair, 100 exhausted groups ahead of recoverable work,
explicit exhausted retry, coherent Paper order/position observation, and the expanded ML
evidence payload. Full release evidence is recorded in `context.md` with the delivery commit.
