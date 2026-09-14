from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, insert

from agentic_quant.config import Settings
from agentic_quant.database import runtime_controls, runtime_leases
from agentic_quant.ids import uuid7
from agentic_quant.migrations import upgrade_database
from agentic_quant.worker import worker_is_healthy


def test_shadow_health_accepts_a_fresh_fenced_tick_lease(settings: Settings) -> None:
    upgrade_database(settings.database_url)
    now = datetime.now(UTC)
    engine = create_engine(settings.database_url)
    with engine.begin() as connection:
        connection.execute(
            insert(runtime_controls).values(
                control_key="worker:shadow",
                state_json={"status": "WAITING", "detail": "prior completed tick"},
                updated_by="runtime-worker",
                updated_at=now - timedelta(minutes=10),
            )
        )
        connection.execute(
            insert(runtime_leases).values(
                lease_key="shadow:portfolio-execution",
                lease_owner="scheduler:test",
                lease_token=uuid7(),
                lease_expires_at=now + timedelta(seconds=60),
                updated_at=now,
            )
        )

    assert worker_is_healthy(settings, pipeline="shadow") is True
    assert worker_is_healthy(settings, pipeline="coordinator") is False


def test_shadow_health_rejects_an_expired_tick_lease(settings: Settings) -> None:
    upgrade_database(settings.database_url)
    now = datetime.now(UTC)
    engine = create_engine(settings.database_url)
    with engine.begin() as connection:
        connection.execute(
            insert(runtime_controls).values(
                control_key="worker:shadow",
                state_json={"status": "WAITING", "detail": "stale completed tick"},
                updated_by="runtime-worker",
                updated_at=now - timedelta(minutes=10),
            )
        )
        connection.execute(
            insert(runtime_leases).values(
                lease_key="shadow:portfolio-execution",
                lease_owner="scheduler:test",
                lease_token=uuid7(),
                lease_expires_at=now - timedelta(seconds=1),
                updated_at=now - timedelta(minutes=2),
            )
        )

    assert worker_is_healthy(settings, pipeline="shadow") is False
