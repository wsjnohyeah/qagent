# ADR 0029: Persist provider-observed history boundaries for newly listed symbols

- Status: accepted
- Date: 2026-09-07

## Context

The production market scanner immediately surfaced ALAB, whose public trading history begins
inside the coordinator's configured five-year lookback. The old coordinator interpreted every
pre-listing exchange session as missing data. That made a correct, complete provider response
fail quality validation and would repeatedly request dates on which the security did not
exist.

Blindly accepting the first stored bar is also unsafe: a partial provider response or an
accidentally truncated import must not redefine the expected history window.

## Decision

When repairing a coordinator window, individual provider requests may defer their completeness
check to the final assembled window. If a successfully exhausted request covering the leading
gap returns no earlier data, the coordinator may treat the first observed daily bar as a
provider-observed history boundary only after the remaining window passes strict completeness
validation. It then appends `market.history.boundary.observed.v1` with the requested start,
observed start, provider/feed, observation time, and evidence ingestion-run IDs.

A later cycle may reuse that boundary only when its recorded probe began no later than the new
requested start. Increasing the configured lookback beyond the probed range therefore forces a
new provider query. Internal and trailing gaps after the boundary remain fatal data-quality
errors. A completely empty response becomes `WAITING_MARKET_HISTORY`, not a retry storm.

## Consequences

- Newly listed companies can enter the same scalable workflow without fabricating pre-listing
  bars or weakening internal-gap checks.
- The boundary means “earliest history returned after a complete provider probe,” not a legal
  IPO/listing-date claim and not historical-universe membership evidence.
- Young symbols may still stop at minimum-history, model, or validation gates; the change does
  not lower those requirements or grant any execution permission.
