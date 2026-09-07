from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta
import hashlib
import json
from typing import Any, Awaitable, Callable

from agentic_quant.domain import WorkflowJob, WorkflowJobStatus
from agentic_quant.ids import stable_uuid, uuid7
from agentic_quant.workflow import WorkflowJobStore


COORDINATOR_STAGES = (
    "collect_market_data",
    "collect_research_evidence",
    "materialize_features",
    "train_ml",
    "forecast_ml",
    "research_llm",
    "generate_strategy",
    "validate_strategy",
    "await_shadow_adoption",
)

CoordinatorHandler = Callable[
    [WorkflowJob, tuple[WorkflowJob, ...]], Awaitable[dict[str, Any]]
]


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str).encode()
    ).hexdigest()


class AutonomousCoordinator:
    """Persistent, resumable DAG runner for the research-to-shadow workflow.

    Business gates are represented as successful WAITING outcomes. Infrastructure
    failures use workflow retries. This distinction prevents missing data, an LLM
    budget stop, or pending human approval from looking like a broken worker.
    """

    def __init__(
        self,
        jobs: WorkflowJobStore,
        *,
        handler: CoordinatorHandler,
        worker_id: str | None = None,
    ) -> None:
        self.jobs = jobs
        self.handler = handler
        self.worker_id = worker_id or f"research-coordinator-{uuid7()}"

    def plan(
        self,
        *,
        symbols: tuple[str, ...],
        as_of: datetime,
        timeframe: str = "1Day",
        max_attempts: int = 5,
        universe_scan_id: str | None = None,
    ) -> tuple[str, tuple[WorkflowJob, ...]]:
        if as_of.tzinfo is None:
            raise ValueError("Coordinator cutoff must be timezone-aware")
        if timeframe != "1Day":
            raise ValueError("Autonomous research currently supports only 1Day bars")
        normalized = tuple(sorted({value.strip().upper() for value in symbols if value.strip()}))
        if not normalized:
            raise ValueError("Coordinator requires at least one governed symbol")
        cycle_key = as_of.astimezone(UTC).strftime("%Y-%m-%dT%H")
        group_id = (
            stable_uuid(
                "autonomous-research",
                timeframe,
                cycle_key,
                universe_scan_id,
            )
            if universe_scan_id is not None
            else stable_uuid("autonomous-research", timeframe, cycle_key)
        )
        now = datetime.now(UTC)
        planned: list[WorkflowJob] = []
        for symbol in normalized:
            dependency_ids: tuple[str, ...] = ()
            for order, stage in enumerate(COORDINATOR_STAGES, 1):
                partition_key = f"{symbol}:{order:02d}:{stage}"
                job_id = stable_uuid("coordinator-job", group_id, partition_key)
                payload = {
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "as_of": as_of.astimezone(UTC).isoformat(),
                    "stage": stage,
                    "cycle_key": cycle_key,
                }
                if universe_scan_id is not None:
                    payload["universe_scan_id"] = universe_scan_id
                planned.append(
                    WorkflowJob(
                        workflow_job_id=job_id,
                        job_group_id=group_id,
                        job_type=f"coordinator.{stage}",
                        partition_key=partition_key,
                        request_sha256=_hash(payload),
                        payload=payload,
                        status=WorkflowJobStatus.PENDING,
                        attempt_count=0,
                        max_attempts=max_attempts,
                        dependency_job_ids=dependency_ids,
                        cursor={},
                        result={},
                        created_at=now,
                        updated_at=now,
                    )
                )
                dependency_ids = (job_id,)
        self.jobs.ensure_jobs(tuple(planned))
        return group_id, self.jobs.jobs_by_ids(
            tuple(item.workflow_job_id for item in planned)
        )

    async def run_once(
        self,
        *,
        symbols: tuple[str, ...],
        as_of: datetime,
        timeframe: str = "1Day",
        max_jobs: int | None = None,
        universe_scan_id: str | None = None,
    ) -> dict[str, Any]:
        group_id, _ = self.plan(
            symbols=symbols,
            as_of=as_of,
            timeframe=timeframe,
            universe_scan_id=universe_scan_id,
        )
        backlog_group_ids = self.jobs.incomplete_group_ids(
            job_type_prefix="coordinator.",
            exclude_group_id=group_id,
        )
        backlog_summaries = []
        processed_total = 0
        for backlog_group_id in backlog_group_ids:
            remaining = (
                None
                if max_jobs is None
                else max(0, max_jobs - processed_total)
            )
            if remaining == 0:
                break
            summary = await self._run_group(
                backlog_group_id,
                max_jobs=remaining,
            )
            backlog_summaries.append(summary)
            processed_total += int(summary["processed_this_run"])
        remaining = (
            None if max_jobs is None else max(0, max_jobs - processed_total)
        )
        current = await self._run_group(group_id, max_jobs=remaining)
        current["processed_this_run"] = (
            int(current["processed_this_run"]) + processed_total
        )
        current["backlog_groups"] = backlog_summaries
        return current

    async def _run_group(
        self,
        group_id: str,
        *,
        max_jobs: int | None,
    ) -> dict[str, Any]:
        recovered = self.jobs.requeue_stale(
            job_group_id=group_id,
            stale_after=timedelta(minutes=10),
        )
        processed = 0
        # Re-read after every pass so newly-completed parents unlock their child
        # jobs in the same cycle. A bounded loop cannot spin indefinitely.
        for _ in range(max(1, len(self.jobs.jobs(job_group_id=group_id)))):
            progressed = False
            current = self.jobs.jobs(job_group_id=group_id)
            by_id = {item.workflow_job_id: item for item in current}
            for job in current:
                if job.status == WorkflowJobStatus.COMPLETED:
                    continue
                if max_jobs is not None and processed >= max_jobs:
                    return self._summary(group_id, recovered, processed)
                dependencies = tuple(
                    by_id[item]
                    for item in job.dependency_job_ids
                    if item in by_id
                )
                if any(
                    item.status != WorkflowJobStatus.COMPLETED
                    for item in dependencies
                ):
                    continue
                lease_token = self.jobs.claim(
                    job.workflow_job_id,
                    worker_id=self.worker_id,
                    lease_for=timedelta(minutes=15),
                )
                if lease_token is None:
                    continue
                heartbeat = asyncio.create_task(
                    self._heartbeat(job.workflow_job_id, lease_token)
                )
                try:
                    result = await self.handler(job, dependencies)
                except Exception as exc:
                    heartbeat.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await heartbeat
                    self.jobs.fail(
                        job.workflow_job_id,
                        error_code=type(exc).__name__,
                        worker_id=self.worker_id,
                        lease_token=lease_token,
                    )
                    processed += 1
                    # Retry on the next scheduler poll, not in a hot loop against
                    # a failing provider.
                    return self._summary(group_id, recovered, processed)
                heartbeat.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat
                self.jobs.complete(
                    job.workflow_job_id,
                    result=result,
                    worker_id=self.worker_id,
                    lease_token=lease_token,
                )
                processed += 1
                progressed = True
            if not progressed:
                break
        return self._summary(group_id, recovered, processed)

    def status(self, *, limit: int = 500) -> dict[str, Any]:
        recent = [
            item
            for item in self.jobs.recent(limit=limit)
            if str(item["job_type"]).startswith("coordinator.")
        ]
        groups: dict[str, list[dict[str, Any]]] = {}
        for item in recent:
            groups.setdefault(str(item["job_group_id"]), []).append(item)
        return {
            "stages": list(COORDINATOR_STAGES),
            "cycles": [
                {
                    "job_group_id": group_id,
                    "status_counts": {
                        status.value: sum(
                            item["status"] == status.value for item in items
                        )
                        for status in WorkflowJobStatus
                    },
                    "jobs": sorted(items, key=lambda item: item["partition_key"]),
                }
                for group_id, items in groups.items()
            ],
        }

    async def _heartbeat(self, job_id: str, lease_token: str) -> None:
        while True:
            await asyncio.sleep(60)
            renewed = await asyncio.to_thread(
                self.jobs.heartbeat,
                job_id,
                worker_id=self.worker_id,
                lease_token=lease_token,
                lease_for=timedelta(minutes=15),
            )
            if not renewed:
                raise RuntimeError("Coordinator workflow lease was lost")

    def _summary(
        self,
        group_id: str,
        recovered: int,
        processed: int,
    ) -> dict[str, Any]:
        jobs = self.jobs.jobs(job_group_id=group_id)
        counts = {
            status.value: sum(item.status == status for item in jobs)
            for status in WorkflowJobStatus
        }
        waiting = sum(
            str(item.result.get("outcome", "")).startswith("WAITING")
            for item in jobs
            if item.status == WorkflowJobStatus.COMPLETED
        )
        return {
            "job_group_id": group_id,
            "job_count": len(jobs),
            "processed_this_run": processed,
            "recovered_stale_jobs": recovered,
            "status_counts": counts,
            "business_waiting_count": waiting,
            "completed": counts[WorkflowJobStatus.COMPLETED.value] == len(jobs),
        }
