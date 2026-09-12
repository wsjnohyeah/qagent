# ADR 0039 — Prioritize multi-session autonomous research

- Status: accepted
- Date: 2026-09-08 PDT

The production scheduling decision below is superseded by ADR 0043. One-session daily-bar
research is now part of the short-horizon autonomous core, while remaining distinct from a
minute-data intraday strategy.

## Context

The one-session strategy contract forms its signal from completed daily bars, enters at the
next session open, and exits no later than that close. It is not a minute-data intraday
strategy. Production currently has no long-history, continuously operated one-minute research
pipeline, so spending autonomous research and LLM budget on this horizon can be confused with
genuine intraday analysis and displace the more suitable 5- and 20-session work.

The preceding 5- and 20-session production cycles trained ML successfully for most symbols but
then reached the former OpenAI daily ceiling before Research LLM and strategy generation. Their
lack of strategy specifications was therefore a budget-era workflow outcome, not evidence that
every short swing hypothesis failed deterministic validation.

## Decision

1. Keep one session in the explicitly supported research horizons so historical evidence and
   manual experiments remain reproducible.
2. Remove one-session research from the production autonomous rotation.
3. Rotate autonomous research over 5, 20, 63, 126, and 252 sessions, allowing the current
   `$40` daily OpenAI/project ceilings to prioritize multi-session work.
4. Do not call the retained one-session daily-bar contract “intraday.” A future intraday mode
   requires a separate minute-data research, validation, execution, and cost-model contract.
5. Preserve every existing one-session job, model, strategy, and validation record as immutable
   audit evidence; do not adopt or start one-session Shadow merely because it already exists.

## Consequences

- The next coordinator cycle after deployment targets 5 sessions rather than creating another
  autonomous one-session group.
- Manual 1-session research remains possible for comparison and no stored lineage is deleted.
- True intraday research remains a future capability and must not be inferred from daily bars.
