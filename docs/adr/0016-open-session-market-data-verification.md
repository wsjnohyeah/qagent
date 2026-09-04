# ADR 0016: Open-session verification and half-open market-data windows

- Status: accepted after Phase 1B verification.
- Decision: treat every historical bar request as `[start, end)`, allow explicit live-stream
  channel subsets for bounded verification, and require real open-session evidence for the
  SIP persistence and gap-repair path.

## Context

WebSocket authentication proved entitlement but not that live trades, quotes, and bars could
cross the raw archive, normalized database, event ledger, and Redis path. Alpaca's REST `end`
parameter is inclusive, while internal store queries, partitions, and quality windows are
half-open. A live gap repair exposed that mismatch by returning both the missing minute and
the already-received boundary minute.

## Consequences

- The Alpaca adapter retains the complete raw response but normalizes only timestamps inside
  `[start, end)`. Quality rule `market_data_quality@0.2.0` rejects any normalized row outside
  the requested window.
- Live gap seeding is explicitly scoped to `1Min`, so a daily bar cannot mask or invent an
  intraday gap.
- `CHANNELS=bars` can validate minute boundaries without filling a development database with
  unnecessary high-frequency records; the default remains trades, quotes, and bars.
- Production must start paused with automatic migrations disabled, and the local production
  object archive is mounted on a persistent named volume.
- The 2026-09-04 SPY open-session run persisted real trades, quotes, and bars. A controlled
  reconnect created a one-minute observable gap, which emitted a gap event and completed a
  REST repair while uniqueness constraints rejected the live boundary duplicate.
- These checks grant no trading authority and make no claim about alpha, throughput, or
  resilience under every network failure mode.
