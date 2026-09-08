# LLM gateway runbook

## Scope

The gateway connects research-plane workloads to OpenAI GPT-5.6 Sol and Meta Muse Spark
1.3. The constrained generator may use it to propose and critique a research-only strategy
specification. It never receives broker credentials, approves risk, promotes candidates, or
submits orders.

## Configuration

Model allocation is versioned in `configs/model_routing.yaml`. The initial policy is:

| Workload | Provider | Cost tier |
|---|---|---|
| `critical_research` | OpenAI GPT-5.6 Sol | premium |
| `strategy_generation` | OpenAI GPT-5.6 Sol | premium |
| `strategy_critique` | OpenAI GPT-5.6 Sol | premium |
| `interactive_explanation` | Meta Muse Spark 1.3 | value |
| `routine_pipeline` | Meta Muse Spark 1.3 | value |

Add credentials only to the repository's ignored `.env` file. The gateway deliberately uses
project-specific names so it cannot inherit a machine-wide `OPENAI_API_KEY` accidentally:

```dotenv
LLM_OPENAI_API_KEY=...
LLM_META_API_KEY=...
```

The default public endpoints are `https://api.openai.com/v1/responses` and
`https://api.meta.ai/v1/responses`. If Meta assigns a different public model identifier,
change only `providers.meta.model` and bump the routing version.

## Control Center

Open [http://127.0.0.1:8000](http://127.0.0.1:8000) after `make run` or `make docker-up`.
The model panel shows provider readiness, model/cost/reasoning metadata, and the effective
provider for every workload. Saving the complete workload map creates an immutable SQL
routing revision; it never edits the base YAML or exposes a credential.

`Auto` in System Steward follows the effective `interactive_explanation` route. Selecting
OpenAI or Meta overrides the provider for only that invocation. Conversations are durable,
but each request sends only the six most recent prior messages, truncated to 3,000 characters
each, plus a question-relevant system snapshot. The current message is included once. Each
call is capped at 1,200 output tokens and 120 seconds. The invocation audit stores output,
hashes, and a sanitized request envelope for the administrator's prompt inspector.

All paid and write controls require the authenticated administrator session and CSRF token.
Development-only provider probes and bounded ingestion helpers remain disabled outside
`APP_ENV=development`.

The tracked model-routing YAML is the base configuration. Only the newest database revision
whose `base_routing_sha256` matches that YAML is active. A reviewed routing change therefore
retires prior runtime overrides automatically. Inspect the current and historical states with:

```sh
curl -fsS 'http://127.0.0.1:8000/v1/llm/routes'
curl -fsS 'http://127.0.0.1:8000/v1/llm/routes/history?limit=20'
```

## Inspect and probe

Inspect routes and project credential readiness without making a request:

```sh
make llm-routes
```

Run one small development-only request per provider:

```sh
work/tools/uv run quant-llm probe --provider openai
work/tools/uv run quant-llm probe --provider meta
```

Run both with `make llm-probe`. Missing keys fail closed. HTTP 429 and server errors use
bounded retries; authentication and other client errors are not retried.

Inspect audit records:

```sh
work/tools/uv run quant-llm list --limit 20
curl -fsS 'http://127.0.0.1:8000/v1/llm/routes'
curl -fsS 'http://127.0.0.1:8000/v1/llm/invocations?limit=20'
```

The development-only HTTP probes are `POST /v1/llm/probe/openai` and
`POST /v1/llm/probe/meta`.

## Audit and safety invariants

- Every completed or failed attempt is appended to `llm_invocations` and the event ledger.
- Audit records retain source Git SHA, route/prompt version, provider/model, request/routing
  hashes, response ID, token usage, latency, status, and output.
- A sanitized request envelope is retained for administrator inspection. Provider keys,
  authorization headers, cookies, and other credentials are never captured.
- Automatic cross-provider fallback is disabled; changing provider is an explicit route
  decision so behavior and cost cannot drift silently.
- Global route changes are immutable revisions. A chat provider override is explicit in the
  invocation and never changes the global route.
- Provider keys never enter prompts, events, API responses, logs, Git, or model-visible tools.
- The gateway is confined to the research plane and has no path to risk or execution.

## Budget breaker

`configs/llm_budget.yaml` defines versioned project, provider, and workload estimated-USD
ceilings. Before an upstream call, the gateway estimates the maximum cost from a conservative
UTF-8-byte input estimate plus maximum output length and reserves that dollar amount
atomically. A successful call settles actual token telemetry into an estimated USD amount; a
provider failure releases the reservation. There is intentionally no operator token limit.

Inspect the current windows without making a paid call:

```sh
curl -fsS http://127.0.0.1:8000/v1/llm/budget
```

The USD figures are operator planning estimates and must be reviewed against provider
contracts before production. Exhaustion returns HTTP 429 before the provider is called.

The authenticated Overview page can create a complete daily workload-limit revision through
`PUT /v1/llm/budget`. The change remains pending until the administrator confirms its exact
preview. Confirmed revisions are immutable and valid for their matching versioned YAML base;
material YAML changes must bump that version. A same-version removal of the retired token cap
therefore preserves existing USD overrides. Revision activation updates the existing UTC-day
window without resetting consumed or reserved capacity.
Project and provider caps remain the outer hard limits.

## Evidence-bound research analysis

The Phase 4 analyst accepts a persisted point-in-time feature snapshot, retrieves only
document versions available at exactly that cutoff, and sends a bounded evidence bundle to
the configured critical-research model. For example:

```sh
curl -fsS -X POST http://127.0.0.1:8000/v1/intelligence/analyze \
  -H 'content-type: application/json' \
  -d '{"symbol":"AAPL","as_of":"2026-09-03T20:00:00Z","feature_snapshot_id":"REPLACE_ME","horizon":"5 trading days"}'
```

The output must validate against the current `research_analysis` schema. Factual claims may
cite only exact IDs and must reproduce exact source quotations. Unknown citations,
unsupported claims, or malformed JSON produce a durable `REJECTED` record; insufficient
independent evidence produces `ABSTAINED` without an LLM call. A schema-valid, citation-valid
`ABSTAINED` judgment is advisory rather than a veto: the constrained generator may still
propose an explicitly exploratory hypothesis from the linked feature snapshot and ML
forecast. The critic and deterministic validation remain separate, and no LLM status grants
promotion or execution authority.

Inspect recent records and provenance:

```sh
curl -fsS http://127.0.0.1:8000/v1/intelligence/analyses
curl -fsS http://127.0.0.1:8000/v1/decision-inspector/ANALYSIS_ID
```
