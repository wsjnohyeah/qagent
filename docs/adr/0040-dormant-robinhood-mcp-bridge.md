# ADR 0040: Build a dormant Robinhood MCP bridge before any live-money decision

- Status: accepted
- Date: 2026-09-08

## Context

The operator explicitly asked to prepare Robinhood Agentic Trading as a future autonomous
execution option while the system remains in Shadow. Robinhood exposes an official Streamable
HTTP MCP endpoint at `https://agent.robinhood.com/mcp/trading`. Its protected-resource and
authorization-server metadata advertise dynamic client registration, authorization-code PKCE,
refresh tokens, and a dedicated Agentic Account. Current Robinhood documentation exposes
read-only portfolio/market tools, `review_equity_order`, `place_equity_order`, and
`cancel_equity_order`.

Robinhood does not advertise a Paper sandbox for this endpoint. Its public tool descriptions
also do not establish a durable client-order idempotency contract sufficient to recover safely
from an unknown POST outcome. Connecting the Research LLM directly to the remote MCP server
would additionally disclose the broker OAuth token and give nondeterministic model output an
execution path, violating the existing risk-authority boundary.

## Decision

1. QAgent itself is the MCP client. The VPS API performs official-host-pinned OAuth and MCP
   calls; ChatGPT, Codex, or Claude do not need to remain open.
2. OAuth uses dynamic client registration, authorization-code PKCE, a 15-minute single-use
   state, and refresh tokens. Tokens are Fernet-encrypted at rest under a separately supplied
   secret and are never returned by status APIs, stored in audit payloads, or sent to an LLM.
3. The initial bridge allows tool discovery, explicitly allowlisted read tools, and
   `review_equity_order`. Every call stores argument/response hashes and only a redacted summary.
4. The source adapter contains `place_equity_order` and `cancel_equity_order` methods so their
   future boundary can be tested against a mock MCP server. Runtime configuration nevertheless
   rejects `ROBINHOOD_ORDER_SUBMISSION_ENABLED=true`, production Compose pins it to `false`, and
   no HTTP endpoint or worker invokes either method.
5. `LIVE_TRADING_ENABLED` remains `false`; there is still no live trading mode. Connecting an
   Agentic Account establishes read/preview capability only.
6. Before a later ADR can arm submission, the authenticated runtime schemas, dedicated account
   identity, order-preview parity, broker idempotency/recovery behavior, order lifecycle,
   fractional/whole-share semantics, stop/target support, market-calendar behavior, and emergency
   cancellation/exit behavior must be verified. A separately confirmed strategy enrollment,
   deterministic risk recheck, global kill switch, and persistent order intent remain mandatory.

## Consequences

- The bridge can be deployed and authorized without creating an order.
- Robinhood credentials stay outside all OpenAI/Meta request envelopes.
- A future execution worker can use the broker adapter without redesigning research, but cannot
  be enabled merely by changing an environment variable in the current release.
- Authorization grants broad Robinhood read visibility; only the dedicated Agentic Account may
  trade. Operators must treat connection itself as sensitive and disconnect it when not needed.
