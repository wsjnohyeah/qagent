# ADR 0001: Redis Streams for the first event bus

- Status: accepted for the MVP; not yet implemented in Phase 0
- Decision: use Redis Streams with at-least-once delivery, durable consumer offsets, idempotent consumers, and a dead-letter stream.
- Rationale: the initial throughput does not justify Kafka, while Redis is simple to operate on one VPS. Keep event contracts transport-independent so NATS JetStream remains a migration option.

