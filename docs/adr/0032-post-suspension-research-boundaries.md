# ADR 0032: Start research at an evidenced post-suspension segment

- Status: accepted
- Date: 2026-09-07

## Context

NBIS history from Alpaca contains an extended sequence of explicit zero-volume placeholder
bars, then 43 XNYS sessions with no bars, followed by normal traded bars after the security
resumed under its current identity. Treating the gap as ordinary missing vendor data would
retry forever. Treating the entire five-year series as continuous would mix a long non-tradable
period and earlier security identity into current feature, ML, and validation evidence.

## Decision

After the coordinator has completely queried every apparent gap, it may recognize a
post-suspension research boundary only when all of these deterministic conditions hold:

1. one continuous inactive segment spans at least 20 completed exchange sessions;
2. that segment contains both missing sessions and explicit zero-volume provider bars, in
   either order, and follows earlier positive-volume history;
3. a following provider bar exists and has positive volume; and
4. the complete interval after that resumed bar passes the unchanged strict quality rules.

The coordinator records the boundary as `market.history.boundary.observed.v1` with
`PROVIDER_OBSERVED_POST_SUSPENSION_START`, the full probe window, and ingestion-run evidence.
Feature materialization and exact strategy validation use only bars at or after the verified
boundary. Every train/test backtest receives that same history start; it must not reload and
quality-check excluded pre-boundary rows merely because they remain in the audit store. The
record is operational provider evidence, not a legal assertion about listing, corporate
identity, or corporate actions.

## Consequences

- Current research can proceed for securities with a strongly evidenced long suspension and
  resumed active segment without weakening ordinary internal-gap detection.
- Short gaps, gaps without the zero-volume precursor, trailing gaps, and missing bars inside
  the resumed segment still fail closed.
- Earlier raw and normalized observations remain preserved for audit but are excluded from the
  current-segment feature and validation window.
