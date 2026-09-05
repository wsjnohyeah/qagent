# ADR 0017: Authenticated single-steward control plane

- Status: accepted for Phase 6.
- Decision: expose one persistent System Steward and object-centric Control Center behind a
  single administrator session; route sensitive operations through expiring, explicit,
  single-use confirmation requests.

## Context

The operator wants one knowledgeable system manager rather than separate visible research,
operations, and coding agents. The same interface must answer questions about current data,
strategies, pipeline state, and shadow results, then help administer those objects. A normal
multi-user registration/role product is unnecessary, but unauthenticated remote access is
unacceptable. Natural-language content, model output, and stored documents are not reliable
authorization signals.

## Consequences

- One administrator is configured outside Git. Sessions are server-side, revocable, long-
  lived, cookie-based, and protected by a separate CSRF token. Production refuses plaintext
  administrator passwords.
- The no-build UI has one left-side object navigator and a central object workspace. The
  System Steward is a default, full-page conversation surface with persistent history,
  safely rendered Markdown, and carried object context rather than a narrow global sidebar.
- The steward receives a bounded current-state snapshot and may cite only IDs supplied in
  that snapshot. Prompt or document text cannot grant new capability.
- The steward may propose the same allowlisted actions as the UI, but cannot execute them.
  Sensitive operations require an exact second confirmation before a 15-minute expiry and
  are recorded with preview, actor, result, and failure state.
- Strategy “deletion” is retirement. Policy restrictions remain reviewed configuration.
- Code work is represented by scoped change sessions with diff/test/commit approval stages;
  the web process exposes no shell and cannot push or deploy.
- Authentication is intentionally single-user. Multi-tenant authorization, registration,
  password recovery, and delegation are out of scope.
