from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Engine, func, insert, select

from agentic_quant.database import llm_invocations
from agentic_quant.domain import LLMInvocation


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class LLMStore:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def record(self, invocation: LLMInvocation) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                insert(llm_invocations).values(
                    invocation_id=invocation.invocation_id,
                    workload=invocation.workload.value,
                    routing_version=invocation.routing_version,
                    routing_sha256=invocation.routing_sha256,
                    code_git_sha=invocation.code_git_sha,
                    provider=invocation.provider.value,
                    model=invocation.model,
                    reasoning_effort=invocation.reasoning_effort,
                    prompt_version=invocation.prompt_version,
                    request_sha256=invocation.request_sha256,
                    input_sha256=invocation.input_sha256,
                    response_id=invocation.response_id,
                    output_text=invocation.output_text,
                    output_sha256=invocation.output_sha256,
                    usage_json=invocation.usage.model_dump(mode="json"),
                    latency_ms=invocation.latency_ms,
                    status=invocation.status.value,
                    error_code=invocation.error_code,
                    created_at=invocation.created_at,
                    completed_at=invocation.completed_at,
                )
            )

    def recent(self, *, limit: int = 50) -> list[dict[str, Any]]:
        statement = (
            select(llm_invocations)
            .order_by(llm_invocations.c.created_at.desc())
            .limit(limit)
        )
        with self.engine.connect() as connection:
            results = []
            for row in connection.execute(statement):
                item = self._normalize(dict(row._mapping))
                output = item.pop("output_text")
                item["output_preview"] = output[:240] if output else None
                results.append(item)
            return results

    def get(self, invocation_id: str) -> dict[str, Any] | None:
        statement = select(llm_invocations).where(
            llm_invocations.c.invocation_id == invocation_id
        )
        with self.engine.connect() as connection:
            row = connection.execute(statement).one_or_none()
        return self._normalize(dict(row._mapping)) if row is not None else None

    def health_summary(self) -> dict[str, int]:
        with self.engine.connect() as connection:
            return {
                "llm_invocations": int(
                    connection.execute(
                        select(func.count()).select_from(llm_invocations)
                    ).scalar_one()
                )
            }

    @staticmethod
    def _normalize(item: dict[str, Any]) -> dict[str, Any]:
        item["created_at"] = _utc(item["created_at"])
        item["completed_at"] = _utc(item["completed_at"])
        item["usage"] = item.pop("usage_json")
        return item
