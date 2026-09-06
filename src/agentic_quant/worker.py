from __future__ import annotations

import argparse
import asyncio
import contextlib
from datetime import UTC, datetime, timedelta
import signal

from sqlalchemy import create_engine, select

from agentic_quant.api import create_app
from agentic_quant.config import Settings
from agentic_quant.database import runtime_controls
from agentic_quant.migrations import prepare_database


async def run_shadow_worker(settings: Settings) -> None:
    """Run the Phase 6 scheduler without exposing an HTTP listener."""
    if not settings.shadow_runtime_enabled:
        raise RuntimeError("Shadow worker requires SHADOW_RUNTIME_ENABLED=true")
    application = create_app(settings, process_role="worker")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for name in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(name, stop.set)
    async with application.router.lifespan_context(application):
        runtime_task = application.state.shadow_task
        if runtime_task is None:
            raise RuntimeError("Shadow runtime task was not started")
        stop_task = asyncio.create_task(stop.wait(), name="worker-stop-signal")
        done, _ = await asyncio.wait(
            (stop_task, runtime_task),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if runtime_task in done:
            stop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stop_task
            await runtime_task
            raise RuntimeError("Shadow runtime exited unexpectedly")


def worker_is_healthy(settings: Settings) -> bool:
    prepare_database(settings)
    engine = create_engine(settings.database_url, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            row = connection.execute(
                select(runtime_controls).where(
                    runtime_controls.c.control_key == "worker:shadow"
                )
            ).one_or_none()
        if row is None:
            return False
        updated_at = row.updated_at
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=UTC)
        status = str(dict(row.state_json).get("status", "UNKNOWN"))
        maximum_age = timedelta(seconds=max(30, settings.shadow_poll_seconds * 3))
        return datetime.now(UTC) - updated_at <= maximum_age and status != "FAILED"
    finally:
        engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Agentic Quant persistent worker")
    parser.add_argument("--healthcheck", action="store_true")
    args = parser.parse_args()
    settings = Settings()
    if args.healthcheck:
        raise SystemExit(0 if worker_is_healthy(settings) else 1)
    asyncio.run(run_shadow_worker(settings))


if __name__ == "__main__":
    main()
