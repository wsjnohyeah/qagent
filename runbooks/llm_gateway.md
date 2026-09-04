# LLM gateway runbook

## Scope

The gateway connects research-plane workloads to OpenAI GPT-5.6 Sol and Meta Muse Spark
1.3. It does not generate strategies yet, receive broker credentials, approve risk, promote
candidates, or submit orders.

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

`Auto` in Research Copilot follows the effective `interactive_explanation` route. Selecting
OpenAI or Meta overrides the provider for only that chat invocation. Conversation history is
kept in the current browser session and capped at 20 prior turns/100,000 characters per call.
Each call is capped at 1,200 output tokens and 120 seconds. The invocation audit stores the
output and hashes the input rather than storing raw user messages.

These unauthenticated paid/write controls return HTTP 403 outside `APP_ENV=development`.
Do not expose them remotely until authentication, authorization, rate limits, budgets, CSRF
protection, and session auditing are implemented.

The tracked YAML is the base configuration. Only the newest database revision whose
`base_routing_sha256` matches that YAML is active. A reviewed YAML change therefore retires
prior runtime overrides automatically. Inspect the current and historical states with:

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
- Raw input and instructions are represented by hashes rather than copied into the audit row.
- Automatic cross-provider fallback is disabled; changing provider is an explicit route
  decision so behavior and cost cannot drift silently.
- Global route changes are immutable revisions. A chat provider override is explicit in the
  invocation and never changes the global route.
- Provider keys never enter prompts, events, API responses, logs, Git, or model-visible tools.
- The gateway is confined to the research plane and has no path to risk or execution.
