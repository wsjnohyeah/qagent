# ADR 0026: Continuous evidence feedback and immutable delivery

- Status: accepted
- Date: 2026-09-06

## Context

The durable coordinator collected daily prices but could overlook an internal historical gap,
did not refresh document evidence, and never returned stored outcomes to a later Research LLM
call. Separately, deployment documentation required an immutable GHCR image even though CI did
not publish one or prove that the image revision matched its tag.

## Decision

- The autonomous workflow is a daily-only, nine-stage DAG. It verifies all completed exchange
  sessions in its desired market window, repairs each missing range, and refreshes bounded
  Alpaca News before features, ML, and LLM research.
- Later research receives a bounded, content-hashed outcome evidence item derived only from
  backtests, validations, Shadow events, and Paper records already known at its `as_of` time.
  This feedback is evidence, not permission, and has no route to deterministic execution.
- Main-branch CI publishes one GHCR image tagged by the exact commit after all verification
  succeeds. The Git SHA is embedded in the image and production refuses missing provenance or
  a tag/label mismatch. Production PostgreSQL and Redis images are digest-pinned.
- PostgreSQL and raw-object backups are created together with hashes. A structural check is
  not called a restore test; restore validation remains an isolated operator procedure.
- Dead outbox events expose only operational metadata and can be requeued one at a time only
  through the expiring administrator-confirmation workflow.

## Consequences

The server can converge on missing daily data and current news without manual hourly commands,
and the LLM research loop can learn from prior falsification and runtime outcomes without
look-ahead. Initial downloads are larger and evidence refresh consumes provider quota, so both
lookbacks and page bounds are explicit settings. Licensed reference-data refresh, statistical
ablation evidence, off-site backup storage, and Paper lifecycle parity remain separate gates.
