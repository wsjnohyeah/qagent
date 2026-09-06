# Review remediation — 2026-09-06

The source review `qagent_review_56bb979_2026-09-06.md` was treated as evidence to verify,
not as executable instructions. All ten concrete C-findings were reproduced by inspection or
an adverse test and repaired. No paid model call, broker order, or live-money path was used.

| Finding | Result |
|---|---|
| C01 | Research and shadow share `risk_policy@0.3.0`, capital/cost/restriction contract binding, quantity limits, and the same conservative stop-first one-bar bracket exit. Shadow data-health input comes from a persisted quality assessment. |
| C02 | Labels apply splits/dividends only after the actual entry event and through the exit event. A pre-entry split regression test returns the unadjusted economic result. |
| C03 | Validation chains every selected OOS equity observation across folds. Intrafold and cross-fold drawdown are retained; PBO N/A is an evidence shortfall and unrelated historical specs cannot satisfy candidate count. |
| C04 | Production worker boot persists a pause epoch. API-only restart preserves SQL state; worker restart requires a new human resume. |
| C05 | Stable business/event IDs plus atomic batch ledger/outbox creation reconcile interruptions after business writes. Network failure cannot prevent later batch intents from being created. |
| C06 | The complete worker iteration is retried with bounded backoff, five consecutive failures terminate the supervised worker task, and a SQL portfolio lease prevents concurrent API/worker ticks. |
| C07 | Claims return a unique lease token. Heartbeat/complete/fail require the token and an unexpired lease; long backfills renew periodically. |
| C08 | Every generation attempt is append-only audited, including rejected/failed output. Model invocation IDs no longer mutate canonical executable specs, so identical proposals are idempotent. |
| C09 | Generated proposal and critique schemas forbid unknown fields; unsupported execution semantics fail before compilation and remain in raw invocation/attempt audit. |
| C10 | Backfill group and fixed-partition identities exclude the enclosing moving date range, so extending a range reuses completed partitions. |

The G04 exact-generated-spec path is now available through `quant-research validate
--strategy-spec-id ...` and `POST /v1/research/validations`. G02 is also closed for the
broker-free boundary: plans are durable before any later bar can fill them, and downtime bars
are marked/skipped rather than backfilled as forward trades.

G01 remains intentionally open: the production worker operates shadow execution and the
outbox, but collection and paid research schedules are still operator-defined. G03 is made
internally consistent as isolated candidate accounting; a shared main-account/sleeve model
needs an explicit allocation policy before it can be implemented safely.

Implementation verification is not strategy-performance evidence. Bounded fixtures establish
software behavior only; statistical eligibility still requires adequate untouched data and
forward observation.
