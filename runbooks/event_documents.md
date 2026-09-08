# Event and document pipeline runbook

This runbook operates the Phase 2 point-in-time event/document pipeline. It is read-only
with respect to providers and has no broker or order capability.

## Source policy

- SEC EDGAR filings and an explicitly approved issuer IR feed are `primary` sources.
- Alpaca News is a `secondary` source.
- Social data is an `aggregate` source and is disabled by default.
- Never upgrade a news article or social aggregate to `primary`.
- Raw responses are content-addressed before normalized records and events are emitted.
- A document update creates an immutable version; it does not overwrite earlier evidence.

## Configuration

Alpaca News reuses the ignored local Alpaca market-data credentials. SEC requires a
descriptive User-Agent with a monitored contact email, per SEC fair-access guidance:

```dotenv
SEC_USER_AGENT=Agentic Quant Research your-email@example.com
```

Do not invent or commit a contact email. Social ingestion remains off unless an approved
licensed vendor, endpoint, and token have been selected:

```dotenv
ENABLE_SOCIAL_AGGREGATES=false
```

## Operations

In production, the autonomous coordinator resolves ticker-to-CIK mappings from the SEC,
archives that mapping, and refreshes the configured five-year filing/fact window at most once
per symbol per day. Large XBRL responses are written and ID-resolved in bounded batches so
PostgreSQL's statement parameter ceiling cannot turn a valid multi-thousand-fact issuer
response into a retry loop. Manual commands below remain useful for bounded diagnosis and
replay.

Start and verify the full stack:

```sh
make docker-up
make docker-doctor
make docker-event-health
```

Ingest one bounded Alpaca News page:

```sh
./scripts/compose.sh exec -T api quant-events news AAPL --limit 10 --max-pages 1
```

Ingest primary SEC filing metadata and normalized company facts after configuring
`SEC_USER_AGENT`:

```sh
./scripts/compose.sh exec -T api quant-events sec-filings AAPL \
  --cik 0000320193 --forms 8-K,10-K,10-Q --limit 50
./scripts/compose.sh exec -T api quant-events sec-facts AAPL \
  --cik 0000320193 --max-facts 1000
```

Ingest an issuer-controlled RSS/Atom feed only after verifying its hostname:

```sh
./scripts/compose.sh exec -T api quant-events ir-feed AAPL \
  --url https://investor.example.com/feed.xml \
  --host investor.example.com --issuer "Example Inc." --limit 50
```

Search normalized documents:

```sh
./scripts/compose.sh exec -T api quant-events search revenue --symbol AAPL --limit 20
```

The equivalent read APIs are `GET /v1/documents/search` and `GET /v1/catalysts`.
Development-only ingestion endpoints are visible in `/docs`.

## Expected invariants

- Replaying an unchanged provider page inserts zero new document versions and events.
- Publication, first-seen/ingestion, and provider correction times remain separate.
- Multiple documents may link to one catalyst; `source_count` reflects distinct documents.
- Primary-source linkage is populated when an SEC filing or approved IR release matches.
- Low-similarity stories remain separate rather than risk a false semantic merge.

The current deterministic deduplicator uses symbol, event class, a 36-hour window, and
headline-token similarity. It is intentionally conservative and must be evaluated on a
labeled cross-provider corpus before any strategy consumes its output.

## Troubleshooting

- `SEC_USER_AGENT ... contact email`: set a real monitored contact identity in ignored
  `.env`, rebuild/restart the API container, and retry.
- `401` or `403` from Alpaca News: rotate/check the ignored Alpaca credentials and account
  entitlement; do not paste them into logs or tracked files.
- An IR hostname mismatch is a safety rejection. Confirm the official issuer domain, then
  pass the exact hostname; do not disable the check.
- A failed ingestion run remains visible in `ingestion_runs` with a non-secret error class.
