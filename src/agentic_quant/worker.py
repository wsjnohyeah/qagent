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
    """Run configured persistent schedulers without exposing an HTTP listener."""
    if not (
        settings.shadow_runtime_enabled
        or settings.paper_trading_enabled
        or settings.autonomous_coordinator_enabled
    ):
        raise RuntimeError("Worker requires at least one enabled runtime")
    role = (
        "worker"
        if settings.shadow_runtime_enabled or settings.paper_trading_enabled
        else "coordinator"
    )
    application = create_app(settings, process_role=role)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for name in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(name, stop.set)
    async with application.router.lifespan_context(application):
        runtime_tasks = tuple(
            task
            for task in (
                application.state.shadow_task,
                application.state.paper_task,
                application.state.coordinator_task,
            )
            if task is not None
        )
        if not runtime_tasks:
            raise RuntimeError("No persistent worker runtime was started")
        stop_task = asyncio.create_task(stop.wait(), name="worker-stop-signal")
        done, _ = await asyncio.wait(
            (stop_task, *runtime_tasks),
            return_when=asyncio.FIRST_COMPLETED,
        )
        failed = next((task for task in runtime_tasks if task in done), None)
        if failed is not None:
            stop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stop_task
            await failed
            raise RuntimeError("Persistent worker runtime exited unexpectedly")


def worker_is_healthy(settings: Settings, *, pipeline: str = "shadow") -> bool:
    prepare_database(settings)
    engine = create_engine(settings.database_url, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            row = connection.execute(
                select(runtime_controls).where(
                    runtime_controls.c.control_key == f"worker:{pipeline}"
                )
            ).one_or_none()
        if row is None:
            return False
        updated_at = row.updated_at
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=UTC)
        status = str(dict(row.state_json).get("status", "UNKNOWN"))
        poll_seconds = (
            settings.coordinator_poll_seconds
            if pipeline == "coordinator"
            else settings.paper_poll_seconds
            if pipeline == "paper"
            else settings.shadow_poll_seconds
        )
        maximum_age = timedelta(seconds=max(30, poll_seconds * 3))
        return datetime.now(UTC) - updated_at <= maximum_age and status != "FAILED"
    finally:
        engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Agentic Quant persistent worker")
    parser.add_argument("--healthcheck", action="store_true")
    parser.add_argument(
        "--pipeline",
        choices=("shadow", "paper", "coordinator"),
        default="shadow",
    )
    args = parser.parse_args()
    settings = Settings()
    if args.healthcheck:
        raise SystemExit(
            0 if worker_is_healthy(settings, pipeline=args.pipeline) else 1
        )
    asyncio.run(run_shadow_worker(settings))


if __name__ == "__main__":
    main()
