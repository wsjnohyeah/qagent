# Control Center and System Steward runbook

## Operating contract

Phase 6 exposes one administrator and one user-facing System Steward. Internal data, list,
strategy, shadow, model, pipeline, and code-change services are tools behind that identity;
they are not separate agents the operator must manage.

Only `/`, `/health/live`, `/health/ready`, `/v1/auth/login`, and `/v1/auth/session` are public.
When `AUTH_REQUIRED=true`, every other route requires the opaque `aq_admin_session` cookie.
Every mutating route also requires the matching `aq_csrf` cookie value in
`X-CSRF-Token`. Session tokens are stored only as SHA-256 hashes, expire on a sliding window,
and are invalidated when the configured administrator credential changes.

There is no registration, password reset, role matrix, or second account. A production
deployment must use `ADMIN_PASSWORD_HASH`; plaintext `ADMIN_PASSWORD` is development-only.

## Local login

On a new checkout:

```sh
make bootstrap
cat work/initial-admin-password.txt
make run
```

Open `http://127.0.0.1:8000` and sign in as `admin`. The password file and `.env` are ignored
by Git. Move the password into an approved password manager and remove the local copy when it
is no longer needed.

For an existing checkout, preserve the existing `.env` and add:

```dotenv
AUTH_REQUIRED=true
ADMIN_USERNAME=admin
ADMIN_PASSWORD=LOCAL_ONLY_PASSWORD
SESSION_SECRET=AT_LEAST_32_RANDOM_CHARACTERS
SESSION_MAX_AGE_DAYS=90
```

## Object explorer

Lists, raw objects, strategies, and shadow deployments have stable IDs and discussion
threads. List changes append a numbered revision; restriction-list edits remain policy-file
changes and cannot be made in the UI. Raw previews are size-bounded and may read only the
configured archive root/bucket.

The initial lists are:

- `trading-universe`: governed scan scope;
- `focus-watchlist`: administrator attention list;
- `candidate-list`: scanner-produced shortlist;
- `shadow-active`: runtime-maintained active symbols;
- `benchmarks`: context-only instruments;
- `restricted`: read-only policy identifiers.

## Confirmation protocol

The System Steward and UI may create only allowlisted `PENDING_CONFIRMATION` actions. A
proposal stores target, parameters, reason, and an immutable before/after preview. It expires
after 15 minutes. Execution requires a separate request containing `CONFIRM <last-six-ID>`.
The claim is single-use; a repeated confirmation is rejected.

This protocol covers list revisions, global pause/resume, pipeline controls, model routing,
strategy adoption/retirement, shadow deployments/ticks, and code-change sessions. A failed
handler is retained as `FAILED` with a bounded error code. Strategy deletion means retirement,
not record removal.

## System Steward

The Steward is the default top-level page and uses the full central workspace. Its sidebar
contains persistent conversation history; assistant messages are rendered as escaped,
locally parsed Markdown with headings, lists, tables, quotations, links, and code blocks.
Raw model HTML is never trusted or rendered. `Auto route`, OpenAI, and Meta remain selectable
per request. Use Command/Control+Enter to send.

Each request supplies a bounded, freshly queried snapshot of counts, lists, dataset coverage,
strategies, validations, research analyses, models, ingestions, quality reports, workflow jobs,
shadow deployments, pipeline controls, and pending actions. Current page context is separate
from the direct user request. The LLM must return exact citation IDs; unknown citations are
dropped. Stored/user text is explicitly untrusted.

The LLM cannot directly execute a tool. If it returns an allowlisted proposed action, the API
validates and persists that proposal, and the browser presents the same confirmation flow as
a manually initiated action.

## Code changes

The web layer never exposes a shell. A confirmed `code_change.open` request creates a scoped,
audited session and branch name. A trusted out-of-process coding worker may later populate a
diff and test results through the candidate contract. Only a candidate with non-empty,
all-passing tests becomes `READY_FOR_APPROVAL`; a second action can mark it
`APPROVED_FOR_COMMIT`. Push and deployment are separate operator-controlled steps.

## Incident actions

1. Use the top-bar pause control and confirm it to stop new shadow exposure.
2. Pause a specific pipeline from the Pipelines page if its inputs are suspect.
3. Revoke all sessions with `POST /v1/auth/revoke-all` after credential compromise, then
   rotate the password/hash and session secret.
4. Preserve action, auth, shadow, and event-ledger rows for diagnosis.
5. Do not delete database or object-store evidence as an incident response shortcut.
