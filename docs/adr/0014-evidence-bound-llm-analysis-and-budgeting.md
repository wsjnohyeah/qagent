# ADR 0014: Evidence-bound LLM analysis and budgeting

- Status: accepted for Phase 4.
- Decision: treat LLM output as an untrusted research proposal that must pass point-in-time,
  schema, citation, and budget controls before it becomes a durable analysis record.

## Context

The project intentionally uses LLMs as research orchestrators, but an LLM must not invent
evidence, see future document corrections, consume unbounded budget, promote a strategy, or
gain an execution path. Provider response success is not equivalent to a valid analysis.

## Consequences

- Retrieval selects the newest document version whose ingestion timestamp is no later than
  the requested `as_of`. Feature snapshots and future ML forecasts must match the same symbol
  and cutoff.
- Evidence is bounded, content-hashed, labeled with exact citation IDs, and treated as
  untrusted data in the prompt. No retrieved text can change system instructions.
- `research_analysis@0.2.0` restricts recommendations to research-only values. Every
  non-abstaining factual claim needs an exact quotation from each cited source; unknown IDs
  or unsupported claim text reject the output.
- If there is no independent evidence beyond a feature snapshot, the analyst records a
  deterministic abstention without spending tokens.
- Calls reserve conservative estimated-USD capacity atomically across project, provider, and
  workload windows. Successful calls settle actual token telemetry into estimated cost and
  failed provider calls release their reservation. Token counts are diagnostic, not a
  configured ceiling. Pricing is a versioned planning estimate, not an invoice.
- An authenticated administrator may replace all workload daily limits through an immutable,
  confirmation-gated revision tied to the current YAML base version with the exact base hash
  retained for audit. A revision updates active window limits without resetting consumption;
  project/provider ceilings remain hard outer limits. Material YAML changes must bump the
  base version.
- `ai_infrastructure_graph@0.1.0` exposes evidence-to-invocation-to-analysis lineage through
  the Decision Inspector API. The graph grants no promotion, risk, portfolio, or broker
  authority.
