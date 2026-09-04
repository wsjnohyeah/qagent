# ADR 0013: Fail-closed data quality and resumable workflows

- Status: accepted for the pre-Phase 6 reliability layer.
- Decision: validate market datasets before research, model spread explicitly, ingest
  reference data through versioned manifests, and split long backfills into durable jobs.

## Context

Local fixtures prove behavior but production will process multi-year datasets and experience
provider gaps, worker restarts, duplicate pages, and incomplete reference history. The same
workflow must scale without silently accepting malformed data or assuming frictionless fills.

## Consequences

- Market-bar quality checks cover identity consistency, chronological uniqueness, OHLC
  envelopes, availability time, expected XNYS sessions/minutes, and zero-volume warnings.
  Reports are content-addressed and persisted; ingestion and backtests fail closed on fatal
  findings.
- Equity fills now charge a configurable half-spread on both entry and exit in addition to
  commission, slippage, fixed impact, and volume participation. The exact inputs remain in
  every experiment and fill event.
- Corporate actions and historical universe membership enter through governed JSON batches
  containing dataset type, source, source version, availability timestamps, and immutable
  content hashes. Replaying a batch is idempotent.
- Long market backfills are deterministic date partitions stored in `workflow_jobs`.
  Completed partitions are skipped, stale work is requeued after a lease-like timeout,
  attempts are bounded, and start/completion/failure transitions are written to the event
  ledger. Fresh `RUNNING` work is not stolen by a concurrent invoker.
- This establishes correctness and restart semantics, not production throughput. Remote
  concurrency, vendor-specific reference adapters, and capacity calibration remain later
  operational work.
