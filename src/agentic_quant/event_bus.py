from __future__ import annotations

import redis


class NullEventPublisher:
    def publish(self, *, event_type: str, event_id: str, envelope_json: str) -> str | None:
        return None

    def health(self) -> bool:
        return True


class RedisStreamPublisher:
    def __init__(self, redis_url: str, stream_name: str) -> None:
        self.client = redis.Redis.from_url(redis_url, decode_responses=True)
        self.stream_name = stream_name

    def publish(self, *, event_type: str, event_id: str, envelope_json: str) -> str:
        return str(
            self.client.xadd(
                self.stream_name,
                {"event_id": event_id, "event_type": event_type, "envelope": envelope_json},
            )
        )

    def health(self) -> bool:
        return bool(self.client.ping())
