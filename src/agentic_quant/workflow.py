from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Engine, func, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from agentic_quant.database import workflow_jobs
from agentic_quant.domain import EventEnvelope, WorkflowJob, WorkflowJobStatus
from agentic_quant.ids import uuid7
from agentic_quant.ledger import EventLedger
from agentic_quant.market_ingestion import MarketDataIngestionService
from agentic_quant.providers.base import StockBarsRequest


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _utc(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


class WorkflowJobStore:
    def __init__(self, engine: Engine, ledger: EventLedger | None = None) -> None:
        self.engine = engine
        self.ledger = ledger

    def ensure_jobs(self, jobs: tuple[WorkflowJob, ...]) -> None:
        if not jobs:
            return
        records = [self._values(job) for job in jobs]
        statement: Any
        if self.engine.dialect.name == "postgresql":
            statement = (
                postgresql_insert(workflow_jobs)
                .values(records)
                .on_conflict_do_nothing(
                    index_elements=["job_group_id", "partition_key"]
                )
            )
        elif self.engine.dialect.name == "sqlite":
            statement = (
                sqlite_insert(workflow_jobs)
                .values(records)
                .on_conflict_do_nothing(
                    index_elements=["job_group_id", "partition_key"]
                )
            )
        else:
            raise RuntimeError(f"Unsupported SQL dialect: {self.engine.dialect.name}")
        with self.engine.begin() as connection:
            connection.execute(statement)

    def requeue_stale(
        self,
        *,
        job_group_id: str,
        stale_after: timedelta = timedelta(hours=1),
    ) -> int:
        if stale_after <= timedelta(0):
            raise ValueError("stale_after must be positive")
        now = datetime.now(UTC)
        with self.engine.begin() as connection:
            result = connection.execute(
                update(workflow_jobs)
                .where(
                    (workflow_jobs.c.job_group_id == job_group_id)
                    & (workflow_jobs.c.status == WorkflowJobStatus.RUNNING.value)
                    & (workflow_jobs.c.updated_at < now - stale_after)
                )
                .values(
                    status=WorkflowJobStatus.PENDING.value,
                    error_code="worker_restarted",
                    updated_at=now,
                )
            )
            return int(result.rowcount or 0)

    def claim(self, workflow_job_id: str) -> bool:
        now = datetime.now(UTC)
        with self.engine.begin() as connection:
            result = connection.execute(
                update(workflow_jobs)
                .where(
                    (workflow_jobs.c.workflow_job_id == workflow_job_id)
                    & workflow_jobs.c.status.in_(
                        (
                            WorkflowJobStatus.PENDING.value,
                            WorkflowJobStatus.FAILED.value,
                        )
                    )
                    & (workflow_jobs.c.attempt_count < workflow_jobs.c.max_attempts)
                )
                .values(
                    status=WorkflowJobStatus.RUNNING.value,
                    attempt_count=workflow_jobs.c.attempt_count + 1,
                    started_at=now,
                    updated_at=now,
                    completed_at=None,
                    error_code=None,
                )
            )
            claimed = bool(result.rowcount)
        if claimed:
            self._emit(workflow_job_id, "workflow.job.started.v1")
        return claimed

    def complete(self, workflow_job_id: str, *, result: dict[str, Any]) -> None:
        now = datetime.now(UTC)
        with self.engine.begin() as connection:
            connection.execute(
                update(workflow_jobs)
                .where(workflow_jobs.c.workflow_job_id == workflow_job_id)
                .values(
                    status=WorkflowJobStatus.COMPLETED.value,
                    result_json=result,
                    cursor_json={"completed": True},
                    error_code=None,
                    updated_at=now,
                    completed_at=now,
                )
            )
        self._emit(
            workflow_job_id,
            "workflow.job.completed.v1",
            payload={"result_sha256": _canonical_hash(result)},
        )

    def fail(self, workflow_job_id: str, *, error_code: str) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                update(workflow_jobs)
                .where(workflow_jobs.c.workflow_job_id == workflow_job_id)
                .values(
                    status=WorkflowJobStatus.FAILED.value,
                    error_code=error_code[:120],
                    updated_at=datetime.now(UTC),
                )
            )
        self._emit(
            workflow_job_id,
            "workflow.job.failed.v1",
            payload={"error_code": error_code[:120]},
        )

    def jobs(self, *, job_group_id: str) -> tuple[WorkflowJob, ...]:
        statement = (
            select(workflow_jobs)
            .where(workflow_jobs.c.job_group_id == job_group_id)
            .order_by(workflow_jobs.c.partition_key.asc())
        )
        with self.engine.connect() as connection:
            rows = connection.execute(statement).all()
        return tuple(self._from_row(dict(row._mapping)) for row in rows)

    def recent(self, *, limit: int = 100) -> list[dict[str, Any]]:
        statement = (
            select(workflow_jobs)
            .order_by(workflow_jobs.c.updated_at.desc())
            .limit(limit)
        )
        with self.engine.connect() as connection:
            return [
                self._from_row(dict(row._mapping)).model_dump(mode="json")
                for row in connection.execute(statement)
            ]

    def health_summary(self) -> dict[str, int]:
        with self.engine.connect() as connection:
            return {
                "workflow_jobs": int(
                    connection.execute(
                        select(func.count()).select_from(workflow_jobs)
                    ).scalar_one()
                ),
                "failed_workflow_jobs": int(
                    connection.execute(
                        select(func.count())
                        .select_from(workflow_jobs)
                        .where(workflow_jobs.c.status == WorkflowJobStatus.FAILED.value)
                    ).scalar_one()
                ),
            }

    def _emit(
        self,
        workflow_job_id: str,
        event_type: str,
        *,
        payload: dict[str, Any] | None = None,
    ) -> None:
        if self.ledger is None:
            return
        now = datetime.now(UTC)
        self.ledger.append(
            EventEnvelope(
                event_id=uuid7(),
                event_type=event_type,
                event_time=now,
                emitted_at=now,
                producer="workflow-runner",
                correlation_id=workflow_job_id,
                payload=payload or {},
            )
        )

    @staticmethod
    def _values(job: WorkflowJob) -> dict[str, Any]:
        return {
            **job.model_dump(exclude={"payload", "cursor", "result", "status"}),
            "payload_json": job.payload,
            "cursor_json": job.cursor,
            "result_json": job.result,
            "status": job.status.value,
        }

    @staticmethod
    def _from_row(row: dict[str, Any]) -> WorkflowJob:
        row["payload"] = row.pop("payload_json")
        row["cursor"] = row.pop("cursor_json")
        row["result"] = row.pop("result_json")
        row["created_at"] = _utc(row["created_at"])
        row["started_at"] = _utc(row["started_at"])
        row["updated_at"] = _utc(row["updated_at"])
        row["completed_at"] = _utc(row["completed_at"])
        return WorkflowJob.model_validate(row)


class ResumableMarketBackfill:
    def __init__(
        self,
        *,
        service: MarketDataIngestionService,
        jobs: WorkflowJobStore,
    ) -> None:
        self.service = service
        self.jobs = jobs

    def plan(
        self,
        request: StockBarsRequest,
        *,
        partition_days: int,
        max_attempts: int = 3,
    ) -> tuple[str, tuple[WorkflowJob, ...]]:
        if partition_days < 1:
            raise ValueError("partition_days must be positive")
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        request_material = request.model_dump(mode="json")
        request_sha256 = _canonical_hash(
            {**request_material, "partition_days": partition_days}
        )
        job_group_id = str(uuid.uuid5(uuid.NAMESPACE_URL, request_sha256))
        jobs = []
        partition_start = request.start
        now = datetime.now(UTC)
        while partition_start < request.end:
            partition_end = min(
                request.end,
                partition_start + timedelta(days=partition_days),
            )
            partition_key = f"{partition_start.isoformat()}::{partition_end.isoformat()}"
            payload = {
                **request_material,
                "start": partition_start.isoformat(),
                "end": partition_end.isoformat(),
            }
            job_id = str(
                uuid.uuid5(uuid.NAMESPACE_URL, f"{job_group_id}:{partition_key}")
            )
            jobs.append(
                WorkflowJob(
                    workflow_job_id=job_id,
                    job_group_id=job_group_id,
                    job_type="market_bar_backfill",
                    partition_key=partition_key,
                    request_sha256=_canonical_hash(payload),
                    payload=payload,
                    status=WorkflowJobStatus.PENDING,
                    attempt_count=0,
                    max_attempts=max_attempts,
                    cursor={},
                    result={},
                    created_at=now,
                    updated_at=now,
                )
            )
            partition_start = partition_end
        planned = tuple(jobs)
        self.jobs.ensure_jobs(planned)
        return job_group_id, self.jobs.jobs(job_group_id=job_group_id)

    async def run(
        self,
        request: StockBarsRequest,
        *,
        partition_days: int,
        max_attempts: int = 3,
        max_partitions: int | None = None,
    ) -> dict[str, Any]:
        job_group_id, _ = self.plan(
            request,
            partition_days=partition_days,
            max_attempts=max_attempts,
        )
        self.jobs.requeue_stale(job_group_id=job_group_id)
        processed = 0
        for job in self.jobs.jobs(job_group_id=job_group_id):
            if job.status == WorkflowJobStatus.COMPLETED:
                continue
            if max_partitions is not None and processed >= max_partitions:
                break
            if not self.jobs.claim(job.workflow_job_id):
                continue
            try:
                summary = await self.service.ingest_stock_bars(
                    StockBarsRequest.model_validate(job.payload)
                )
            except Exception as exc:
                self.jobs.fail(
                    job.workflow_job_id,
                    error_code=type(exc).__name__,
                )
                raise
            self.jobs.complete(
                job.workflow_job_id,
                result=summary.model_dump(mode="json"),
            )
            processed += 1
        jobs = self.jobs.jobs(job_group_id=job_group_id)
        status_counts = {
            status.value: sum(job.status == status for job in jobs)
            for status in WorkflowJobStatus
        }
        return {
            "job_group_id": job_group_id,
            "partition_count": len(jobs),
            "processed_this_run": processed,
            "status_counts": status_counts,
            "completed": status_counts[WorkflowJobStatus.COMPLETED.value] == len(jobs),
        }
