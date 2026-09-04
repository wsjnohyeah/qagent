# ADR 0010: Configurable dual-provider LLM gateway

- Status: accepted for the front-loaded Phase 4A foundation.
- Decision: route named workloads through a provider-neutral Responses API gateway, using
  OpenAI GPT-5.6 Sol as the premium model and Meta Muse Spark 1.3 as the value model.

## Context

The product needs expensive frontier reasoning for important research while using a lower-cost
model for interactive explanation and routine pipeline work. Model allocation must eventually
be controlled from the authenticated Control Center without coupling research logic to one API.

## Consequences

- `configs/model_routing.yaml` owns the initial versioned provider, model, reasoning, token,
  timeout, retry, cost-tier, and workload mapping.
- OpenAI and Meta use separate, project-scoped credentials behind one internal contract.
  Their current public Responses API endpoints remain adapter details.
- Every attempted call creates immutable SQL and event-ledger audit records containing source
  Git SHA, request/input/routing hashes, provider/model, prompt and route versions, usage,
  latency, status, response identity, and output.
- Automatic cross-provider fallback is disabled because it can silently alter behavior,
  reproducibility, and cost. A future authenticated routing change creates a new version.
- API keys are never exposed to the model or included in persisted request payloads.
- This gateway adds model connectivity only. It does not yet implement the strategy-research
  orchestrator and grants no model promotion, risk, portfolio, or broker authority.
