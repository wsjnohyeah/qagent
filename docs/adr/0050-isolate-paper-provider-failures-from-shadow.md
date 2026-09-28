# ADR 0050: Isolate Paper provider failures from Shadow continuity

- Status: accepted
- Date: 2026-09-28

## Context

Production enables the Alpaca Paper foundation and the broker-free Shadow runtime in one
worker process. On 2026-09-26 the Paper loop contacted Alpaca even though there were no active
Paper enrollments or incomplete Paper orders. Five consecutive `/v2/account` timeouts ended
the Paper task, which ended the shared worker process. Docker correctly restarted it, and the
required production boot gate correctly restored the global new-exposure pause. Existing
Shadow positions remained exit-manageable, but unrelated new Shadow exposure stopped until an
administrator noticed and resumed it.

Paper provider availability is not evidence about a broker-free Shadow strategy. A transient
Paper outage must not terminate Shadow, while an unresolved broker lifecycle must still be
reconciled and reported as failed.

## Decision

1. A scheduled Paper tick with zero active enrollments and zero incomplete Paper orders is a
   successful broker-idle tick. It records a durable run but performs no Alpaca request.
   Explicit read-only account inspection remains available through the authenticated probe.
2. When an active Paper lifecycle does require the broker and a provider call fails, the Paper
   heartbeat remains `FAILED` and retries with bounded exponential backoff. Repeated Paper
   failures do not terminate the shared worker process or interrupt Shadow.
3. Incomplete Paper orders always require reconciliation even if their enrollment was paused
   or retired. The broker-idle shortcut cannot bypass an outstanding entry, exit, unknown
   submission, or broker-flat check.
4. Shadow failure handling, execution fencing, and the mandatory production worker boot pause
   are unchanged. A genuine Shadow worker restart still requires an audited resume before new
   exposure.
5. Paper enrollment, order submission, Robinhood mutation, and live-money authority are
   unchanged. `LIVE_TRADING_ENABLED=false` remains mandatory.

## Consequences

- An unused Paper integration no longer creates external traffic or availability coupling.
- A real Paper incident remains visible and continuously retryable without sacrificing
  broker-free Shadow exit management.
- Docker may still restart the worker for a genuine process or Shadow-runtime failure; the
  production boot pause continues to fail closed in that case.
- Operators must inspect the Paper heartbeat before enrolling a strategy. A healthy Shadow
  heartbeat does not imply that Alpaca Paper is reachable.
