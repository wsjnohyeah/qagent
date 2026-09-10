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
COORDINATOR_STAGE_ORDER = {
    stage: order for order, stage in enumerate(COORDINATOR_STAGES)
}
COORDINATOR_RESEARCH_HORIZONS = (1, 5, 20, 63, 126, 252)
# One-session strategies remain available for explicit research and historical
# comparison, but production automation prioritizes multi-session horizons until
# a separate minute-data intraday pipeline exists.
AUTONOMOUS_RESEARCH_HORIZONS = (5, 20, 63, 126, 252)

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
        horizon_bars: int = 1,
    ) -> tuple[str, tuple[WorkflowJob, ...]]:
        if as_of.tzinfo is None:
            raise ValueError("Coordinator cutoff must be timezone-aware")
        if timeframe != "1Day":
            raise ValueError("Autonomous research currently supports only 1Day bars")
        if horizon_bars not in COORDINATOR_RESEARCH_HORIZONS:
            raise ValueError("Autonomous research horizon is not approved")
        normalized = tuple(sorted({value.strip().upper() for value in symbols if value.strip()}))
        if not normalized:
            raise ValueError("Coordinator requires at least one governed symbol")
        cycle_key = as_of.astimezone(UTC).strftime("%Y-%m-%dT%H")
        group_id = (
            stable_uuid(
                "autonomous-research-v2",
                timeframe,
                cycle_key,
                horizon_bars,
                universe_scan_id,
            )
            if universe_scan_id is not None
            else stable_uuid(
                "autonomous-research-v2", timeframe, cycle_key, horizon_bars
            )
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
                    "horizon_bars": horizon_bars,
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

    def plan_market_refresh(
        self,
        *,
        symbols: tuple[str, ...],
        as_of: datetime,
        timeframe: str = "1Day",
        max_attempts: int = 5,
    ) -> tuple[str, tuple[WorkflowJob, ...]]:
        """Plan a durable current-edge refresh independent of research membership."""
        if as_of.tzinfo is None:
            raise ValueError("Coordinator cutoff must be timezone-aware")
        if timeframe != "1Day":
            raise ValueError("Autonomous market refresh currently supports only 1Day bars")
        normalized = tuple(
            sorted({value.strip().upper() for value in symbols if value.strip()})
        )
        if not normalized:
            raise ValueError("Market refresh requires at least one governed symbol")
        cycle_key = as_of.astimezone(UTC).strftime("%Y-%m-%dT%H")
        group_id = stable_uuid("autonomous-market-refresh-v1", timeframe, cycle_key)
        now = datetime.now(UTC)
        planned = tuple(
            WorkflowJob(
                workflow_job_id=stable_uuid(
                    "coordinator-market-refresh-job", group_id, symbol
                ),
                job_group_id=group_id,
                job_type="coordinator.collect_market_data",
                partition_key=f"{symbol}:01:collect_market_data",
                request_sha256=_hash(
                    {
                        "symbol": symbol,
                        "timeframe": timeframe,
                        "as_of": as_of.astimezone(UTC).isoformat(),
                        "stage": "collect_market_data",
                        "cycle_key": cycle_key,
                        "horizon_bars": 1,
                        "refresh_only": True,
                    }
                ),
                payload={
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "as_of": as_of.astimezone(UTC).isoformat(),
                    "stage": "collect_market_data",
                    "cycle_key": cycle_key,
                    "horizon_bars": 1,
                    "refresh_only": True,
                },
                status=WorkflowJobStatus.PENDING,
                attempt_count=0,
                max_attempts=max_attempts,
                dependency_job_ids=(),
                cursor={},
                result={},
                created_at=now,
                updated_at=now,
            )
            for symbol in normalized
        )
        self.jobs.ensure_jobs(planned)
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
        horizon_bars: int = 1,
        backlog_horizons: tuple[int, ...] | None = None,
        market_refresh_symbols: tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        group_id, planned = self.plan(
            symbols=symbols,
            as_of=as_of,
            timeframe=timeframe,
            universe_scan_id=universe_scan_id,
            horizon_bars=horizon_bars,
        )
        backlog_group_ids = self.jobs.incomplete_group_ids(
            job_type_prefix="coordinator.",
            exclude_group_id=group_id,
        )
        if backlog_horizons is not None:
            allowed_horizons = set(backlog_horizons)
            backlog_group_ids = tuple(
                backlog_group_id
                for backlog_group_id in backlog_group_ids
                if (
                    (group_jobs := self.jobs.jobs(job_group_id=backlog_group_id))
                    and int(group_jobs[0].payload.get("horizon_bars", 1))
                    in allowed_horizons
                )
            )
        backlog_summaries: list[dict[str, Any]] = []
        # Refresh the current research universe first. Its ordinary first-stage
        # jobs retain the scanner lineage used by downstream strategy review.
        current_market_job_count = sum(
            item.payload.get("stage") == "collect_market_data" for item in planned
        )
        current_priority_budget = current_market_job_count
        if max_jobs is not None:
            current_priority_budget = min(current_priority_budget, max_jobs)
        current_priority = await self._run_group(
            group_id,
            max_jobs=current_priority_budget,
        )
        processed_total = int(current_priority["processed_this_run"])
        if max_jobs is not None and processed_total >= max_jobs:
            current_priority["market_refresh"] = None
            current_priority["backlog_groups"] = backlog_summaries
            return current_priority

        # Active Shadow symbols that rotated out of today's research shortlist
        # get their own market-only durable jobs. They do not inherit a scanner
        # ID that did not select them and do not create unnecessary LLM/ML work.
        research_symbols = {
            item.payload["symbol"]
            for item in planned
            if item.payload.get("stage") == "collect_market_data"
        }
        extra_refresh_symbols = tuple(
            sorted(
                {
                    value.strip().upper()
                    for value in (market_refresh_symbols or ())
                    if value.strip()
                }
                - research_symbols
            )
        )
        market_refresh: dict[str, Any] | None = None
        if extra_refresh_symbols:
            refresh_group_id, refresh_jobs = self.plan_market_refresh(
                symbols=extra_refresh_symbols,
                as_of=as_of,
                timeframe=timeframe,
            )
            remaining = (
                None if max_jobs is None else max(0, max_jobs - processed_total)
            )
            market_refresh = await self._run_group(
                refresh_group_id,
                max_jobs=(
                    len(refresh_jobs)
                    if remaining is None
                    else min(len(refresh_jobs), remaining)
                ),
            )
            processed_total += int(market_refresh["processed_this_run"])
            if max_jobs is not None and processed_total >= max_jobs:
                result = self._summary(
                    group_id,
                    recovered=0,
                    processed=processed_total,
                )
                result["market_refresh"] = market_refresh
                result["backlog_groups"] = backlog_summaries
                return result
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
        current["market_refresh"] = market_refresh
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
            current = tuple(
                sorted(
                    current,
                    key=lambda item: (
                        COORDINATOR_STAGE_ORDER.get(
                            str(item.payload.get("stage")),
                            len(COORDINATOR_STAGES),
                        ),
                        str(item.payload.get("symbol", "")),
                        item.partition_key,
                    ),
                )
            )
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
