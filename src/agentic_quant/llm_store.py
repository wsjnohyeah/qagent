from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Engine, func, insert, select

from agentic_quant.database import (
    llm_budget_reservations,
    llm_invocations,
    llm_routing_revisions,
)
from agentic_quant.domain import LLMInvocation, LLMRoutingRevision


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
                    request_envelope_json=invocation.request_envelope,
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
            select(
                llm_invocations,
                llm_budget_reservations.c.actual_cost_microusd,
                llm_budget_reservations.c.reserved_cost_microusd,
            )
            .outerjoin(
                llm_budget_reservations,
                llm_budget_reservations.c.invocation_id
                == llm_invocations.c.invocation_id,
            )
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
        statement = (
            select(
                llm_invocations,
                llm_budget_reservations.c.actual_cost_microusd,
                llm_budget_reservations.c.reserved_cost_microusd,
            )
            .outerjoin(
                llm_budget_reservations,
                llm_budget_reservations.c.invocation_id
                == llm_invocations.c.invocation_id,
            )
            .where(llm_invocations.c.invocation_id == invocation_id)
        )
        with self.engine.connect() as connection:
            row = connection.execute(statement).one_or_none()
        return self._normalize(dict(row._mapping)) if row is not None else None

    def record_routing_revision(self, revision: LLMRoutingRevision) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                insert(llm_routing_revisions).values(
                    routing_revision_id=revision.routing_revision_id,
                    base_routing_version=revision.base_routing_version,
                    base_routing_sha256=revision.base_routing_sha256,
                    routing_version=revision.routing_version,
                    routing_sha256=revision.routing_sha256,
                    routes_json={
                        workload.value: provider.value
                        for workload, provider in revision.routes.items()
                    },
                    reason=revision.reason,
                    created_by=revision.created_by,
                    created_at=revision.created_at,
                )
            )

    def latest_routing_revision(
        self,
        *,
        base_routing_sha256: str,
    ) -> LLMRoutingRevision | None:
        statement = (
            select(llm_routing_revisions)
            .where(
                llm_routing_revisions.c.base_routing_sha256 == base_routing_sha256
            )
            .order_by(
                llm_routing_revisions.c.created_at.desc(),
                llm_routing_revisions.c.routing_revision_id.desc(),
            )
            .limit(1)
        )
        with self.engine.connect() as connection:
            row = connection.execute(statement).one_or_none()
        if row is None:
            return None
        item = dict(row._mapping)
        item["routes"] = item.pop("routes_json")
        item["created_at"] = _utc(item["created_at"])
        return LLMRoutingRevision.model_validate(item)

    def recent_routing_revisions(self, *, limit: int = 50) -> list[dict[str, Any]]:
        statement = (
            select(llm_routing_revisions)
            .order_by(
                llm_routing_revisions.c.created_at.desc(),
                llm_routing_revisions.c.routing_revision_id.desc(),
            )
            .limit(limit)
        )
        with self.engine.connect() as connection:
            results = []
            for row in connection.execute(statement):
                item = dict(row._mapping)
                item["routes"] = item.pop("routes_json")
                item["created_at"] = _utc(item["created_at"])
                results.append(item)
            return results

    def health_summary(self) -> dict[str, int]:
        with self.engine.connect() as connection:
            return {
                "llm_invocations": int(
                    connection.execute(
                        select(func.count()).select_from(llm_invocations)
                    ).scalar_one()
                ),
                "llm_routing_revisions": int(
                    connection.execute(
                        select(func.count()).select_from(llm_routing_revisions)
                    ).scalar_one()
                ),
            }

    @staticmethod
    def _normalize(item: dict[str, Any]) -> dict[str, Any]:
        item["created_at"] = _utc(item["created_at"])
        item["completed_at"] = _utc(item["completed_at"])
        item["usage"] = item.pop("usage_json")
        item["request_envelope"] = item.pop("request_envelope_json", None)
        actual_cost = item.pop("actual_cost_microusd", None)
        reserved_cost = item.pop("reserved_cost_microusd", None)
        microusd = actual_cost if actual_cost is not None else reserved_cost
        item["estimated_cost_usd"] = (
            str(Decimal(int(microusd)) / Decimal("1000000"))
            if microusd is not None
            else None
        )
        return item
