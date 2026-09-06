from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Engine, insert, select

from agentic_quant.config import AppEnvironment
from agentic_quant.database import runtime_controls


class EnvironmentRegistry:
    """Prevent a durable database from silently changing environment identity."""

    CONTROL_KEY = "environment:identity"

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def register(
        self,
        *,
        environment: AppEnvironment,
        environment_id: str,
    ) -> dict[str, Any]:
        normalized = environment_id.strip()
        if not normalized:
            raise ValueError("DEPLOYMENT_ENVIRONMENT_ID is required")
        expected = {
            "environment": environment.value,
            "environment_id": normalized,
        }
        with self.engine.begin() as connection:
            row = connection.execute(
                select(runtime_controls).where(
                    runtime_controls.c.control_key == self.CONTROL_KEY
                )
            ).one_or_none()
            if row is None:
                connection.execute(
                    insert(runtime_controls).values(
                        control_key=self.CONTROL_KEY,
                        state_json=expected,
                        updated_by="environment-bootstrap",
                        updated_at=datetime.now(UTC),
                    )
                )
                return expected
            actual = dict(row.state_json)
        if actual != expected:
            raise RuntimeError(
                "Database environment identity mismatch: "
                f"stored={actual.get('environment')}:"
                f"{actual.get('environment_id')} requested="
                f"{expected['environment']}:{expected['environment_id']}"
            )
        return actual
