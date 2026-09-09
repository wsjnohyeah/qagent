# Robinhood MCP bridge runbook

## Current boundary

The bridge is authorization, discovery, read, and order-preview infrastructure. It does not
submit or cancel a Robinhood order. The current release rejects
`ROBINHOOD_ORDER_SUBMISSION_ENABLED=true`, production Compose overrides it to `false`, and
`LIVE_TRADING_ENABLED` remains `false`.

## Safe configuration

Generate the encryption key in the target secret manager; never commit or print its value:

```sh
python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

Configure the protected production environment:

```dotenv
ROBINHOOD_MCP_BRIDGE_ENABLED=true
ROBINHOOD_MCP_SERVER_URL=https://agent.robinhood.com/mcp/trading
ROBINHOOD_OAUTH_REDIRECT_URI=http://127.0.0.1:8765/callback
ROBINHOOD_TOKEN_ENCRYPTION_KEY=SECRET_MANAGER_VALUE
ROBINHOOD_ORDER_SUBMISSION_ENABLED=false
LIVE_TRADING_ENABLED=false
```

Only the API container enables this bridge. Shadow, Paper, and research coordinator containers
override the bridge to disabled.

## Connect and verify

1. On the same desktop that will open Robinhood, start the single-use loopback relay:

   ```sh
   work/tools/uv run quant-robinhood-relay \
     --forward-url https://YOUR_HOST/v1/robinhood/oauth/callback
   ```

2. Open **Robinhood bridge** in the authenticated Control Center.
3. Select **Connect Robinhood**. QAgent creates a single-use PKCE flow and Robinhood redirects to
   the loopback listener. The listener forwards only `code`, `state`, or an OAuth error to the
   HTTPS VPS callback and exits; it never receives the verifier or resulting tokens.
4. Complete Robinhood's desktop consent and dedicated Agentic Account onboarding.
5. Confirm the page reports `Connected`, then inspect the runtime-discovered tool list.
6. Run **Read-only portfolio probe**. Confirm no order tool appears in the MCP call audit.
7. Inspect `/v1/system/status` and `/v1/robinhood/status`; both must report
   `order_submission_enabled=false` and `live_money_enabled=false`.

Do not test connectivity by placing an order. No real order is necessary to verify OAuth,
tool discovery, token refresh, or read calls.

## API surface

```text
GET  /v1/robinhood/status
POST /v1/robinhood/oauth/start
GET  /v1/robinhood/oauth/callback
POST /v1/robinhood/disconnect
GET  /v1/robinhood/tools
POST /v1/robinhood/probe
POST /v1/robinhood/review-equity-order
```

There is deliberately no place-order or cancel-order route.

## Incident response

1. Use **Disconnect** to erase the locally stored access and refresh tokens.
2. Revoke the third-party connection in Robinhood as well; local deletion cannot assert remote
   revocation without an advertised revocation endpoint.
3. Preserve `robinhood_mcp_calls` and the append-only broker connection events. They contain
   hashes and redacted summaries, never tokens or account numbers.
4. If OAuth metadata, the official hostname, or a required tool schema changes, keep the bridge
   fail-closed and review a new code/config revision.

## Requirements before order submission

- Capture the authenticated `review_equity_order`, `place_equity_order`, order-status, and cancel
  schemas and freeze a versioned execution contract.
- Prove recovery from timeout after a possibly accepted order without blind resubmission.
- Persist intent before network I/O and reconcile account, order, fill, position, stop, target,
  and timed-exit state across worker restarts.
- Bind one dedicated Robinhood account to separately confirmed strategy enrollments and risk
  limits. Unknown positions or account changes must block new exposure.
- Complete an explicit live-money security review and a new operator decision. ADR 0040 does not
  authorize that later activation.
