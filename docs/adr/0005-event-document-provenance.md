# ADR 0005: Versioned source evidence and deterministic catalyst identity

- Status: accepted for Phase 2.
- Decision: archive raw provider responses first, keep stable document identities with
  immutable content versions, and store publication, ingestion, and correction times
  separately.
- Trust: SEC EDGAR and explicitly verified issuer-controlled IR feeds are `primary`;
  news is `secondary`; social data is `aggregate` and disabled until a licensed provider
  is approved.
- Deduplication: link documents to catalysts deterministically using issuer symbol,
  catalyst class, a bounded time window, and headline-token similarity. Prefer a false
  split over a false merge. Measure and version this policy before strategy use.
- Rationale: point-in-time replay requires the exact evidence version visible at the
  decision timestamp, while counting syndicated coverage as multiple independent
  catalysts would distort event features.
- Constraint: an LLM may later extract structured evidence but may not define identity,
  source trust, timestamps, or final risk authority.
