# ADR 0004: Alpaca as the first market-data and paper-broker adapter

- Status: accepted for Phase 1 market data
- Decision: build the first provider interfaces around Alpaca SIP equities, OPRA options, and paper execution, without leaking provider field names into domain schemas.
- Verification: on 2026-09-03, the configured account successfully accessed SIP historical stock bars, an OPRA option-chain snapshot, and authenticated to the SIP stock WebSocket. Plan naming, long-term retention, licensing, and paper-only broker scope still require review before broader use.
