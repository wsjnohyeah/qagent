# ADR 0011: Development LLM Control Center

- Status: accepted for the front-loaded Phase 4B development interface.
- Decision: expose workload routing and a bounded research-copilot chat in the local web
  Control Center, while keeping credentials, production writes, and monetary authority out
  of the interface.

## Context

The operator needs to test both configured model tiers and change which provider handles each
workload before the full research orchestrator exists. Editing the tracked YAML for every local
experiment would obscure who changed a route and which configuration an invocation used.

## Consequences

- `configs/model_routing.yaml` remains the reviewed base configuration. A Control Center save
  appends a complete routing revision to SQL; the newest revision matching the current base
  configuration hash becomes effective. Changing the YAML base automatically invalidates older
  runtime overrides rather than silently applying them to a different base.
- Every route revision records its complete workload map, base/effective hashes, reason, actor,
  and timestamp, and emits `llm.routing.activated.v1` to the append-only ledger.
- Chat supports `Auto`, explicit OpenAI, and explicit Meta selection. `Auto` follows the current
  `interactive_explanation` route; explicit selection affects only that invocation and does not
  mutate global routing.
- Browser conversation history is bounded and intentionally session-local. Raw chat input is
  sent to the selected provider but represented only by hashes in durable invocation records;
  model output, usage, latency, model, route, and source-code lineage are retained.
- Route writes and paid chat calls are development-only. Production exposure requires
  authentication, authorization, rate limits, budgets, session audit, CSRF protection, and
  prompt/tool hardening.
- The copilot can explain and brainstorm research. It cannot access broker credentials, call
  tools, approve risk, promote strategies, size positions, or place orders.
