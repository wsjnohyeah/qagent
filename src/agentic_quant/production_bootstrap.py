from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
import json

from sqlalchemy import create_engine, insert, select, update

from agentic_quant.config import AppEnvironment, Settings
from agentic_quant.control_plane import SystemObjectStore
from agentic_quant.database import runtime_controls
from agentic_quant.environment import EnvironmentRegistry
from agentic_quant.migrations import require_current_database
from agentic_quant.risk import RiskPolicy
from agentic_quant.virtual_account import VirtualAccountStore


def bootstrap_production(settings: Settings) -> dict[str, object]:
    """Idempotently initialize a fresh production control plane, always paused."""
    if settings.app_env != AppEnvironment.PRODUCTION:
        raise ValueError("Production bootstrap requires APP_ENV=production")
    if settings.auto_migrate:
        raise ValueError("Production bootstrap requires AUTO_MIGRATE=false")
    if settings.database_url.startswith("sqlite"):
        raise ValueError("Production bootstrap requires PostgreSQL, not SQLite")
    if not settings.redis_url:
        raise ValueError("Production bootstrap requires REDIS_URL")
    if settings.deployment_environment_id == "local-development":
        raise ValueError(
            "Set a unique DEPLOYMENT_ENVIRONMENT_ID for this production stack"
        )
    require_current_database(settings.database_url)
    engine = create_engine(settings.database_url, pool_pre_ping=True)
    try:
        identity = EnvironmentRegistry(engine).register(
            environment=settings.app_env,
            environment_id=settings.deployment_environment_id,
        )
        objects = SystemObjectStore(engine)
        objects.ensure_defaults()
        account = VirtualAccountStore(engine).ensure_main_account(
            initial_cash=Decimal("100000"),
            risk_policy=RiskPolicy.from_yaml(settings.risk_policy_path),
        )
        now = datetime.now(UTC)
        with engine.begin() as connection:
            existing = connection.execute(
                select(runtime_controls.c.control_key).where(
                    runtime_controls.c.control_key == "new_exposure_paused"
                )
            ).scalar_one_or_none()
            values = {
                "state_json": {"paused": True},
                "updated_by": "production-bootstrap",
                "updated_at": now,
            }
            if existing is None:
                connection.execute(
                    insert(runtime_controls).values(
                        control_key="new_exposure_paused",
                        **values,
                    )
                )
            else:
                connection.execute(
                    update(runtime_controls)
                    .where(runtime_controls.c.control_key == "new_exposure_paused")
                    .values(**values)
                )
        universe = objects.get_list("trading-universe")
        return {
            "status": "ready_paused",
            "environment": identity,
            "schema": "current",
            "virtual_account_id": account["virtual_account_id"],
            "trading_universe_members": len(universe["members"] if universe else []),
            "new_exposure_paused": True,
            "live_trading_enabled": False,
        }
    finally:
        engine.dispose()


def main() -> None:
    print(json.dumps(bootstrap_production(Settings()), sort_keys=True, default=str))


if __name__ == "__main__":
    main()
