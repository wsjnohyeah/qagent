# ADR 0048: News-first Event Playbooks use chronological validation and isolated Shadow

- Status: accepted
- Date: 2026-09-16
- Amended by: ADR 0049 (exploratory forward-only Candidate Shadow for insufficient holdouts)

## Context

Event Alpha V1 mixed news, SEC, and IR catalysts, measured only 1/2/5-session reactions, and
stopped at a research-only Playbook. Production evidence showed that filing headers dominated
the queue without supplying useful event semantics. The product intent is instead to discover
repeatable price responses to material news and observe qualified future matches in Shadow.

## Decision

- Event Alpha accepts normalized news documents only. SEC and IR remain available elsewhere as
  independent evidence but do not create Event Cards or validate news Playbooks.
- Deduplicated catalysts are Event Episodes: substantially similar same-symbol/type articles
  within 36 hours share one bounded evidence packet.
- Event outcomes cover 1, 2, 5, 10, and 20 sessions from the first causally tradable open.
- The LLM creates cited Cards and case-based Playbook hypotheses. Deterministic code calculates
  outcomes, performs discovery gates, and runs a later-event chronological holdout gate.
- Holdout eligibility requires at least three later events across two symbols, at least 50%
  positive outcomes, positive median and mean excluding the best event, profit factor at least
  1.10, and no outcome below -20%.
- The latest append-only validation is authoritative. New evidence may invalidate an older
  eligible certificate.
- Only a bullish `FORWARD_FIRST_SEEN` Card observed after the current eligible certificate can
  trigger. Event type must match and generalized-tag Jaccard similarity must be at least 0.65.
- A trigger compiles one immutable one-shot `event_playbook` StrategySpec and an exact Shadow
  execution certificate. It enters Candidate Shadow only, in a separate `$10,000` sandbox.
- Entry, quantity, restrictions, stop/target, timed exit, and circuit breaking remain deterministic.
  Event stops use the existing volatility-aware geometry capped at 15%, with a 2R target.
- Event strategies are not Paper eligible. Live trading remains structurally impossible.

## Consequences

The system can now test its news hypotheses in forward, broker-free conditions without granting
the LLM risk authority or treating retrospective news as a live signal. Sparse event families
may take time to collect three genuinely later holdouts, and a passing Playbook can become
ineligible as more adverse evidence arrives. This is intentional.
