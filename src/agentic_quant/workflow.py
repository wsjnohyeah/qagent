from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Engine, func, or_, select, update
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
                    & or_(
                        workflow_jobs.c.lease_expires_at <= now,
                        (
                            workflow_jobs.c.lease_expires_at.is_(None)
                            & (workflow_jobs.c.updated_at < now - stale_after)
                        ),
                    )
                )
                .values(
                    status=WorkflowJobStatus.PENDING.value,
                    error_code="worker_restarted",
                    lease_owner=None,
                    lease_token=None,
                    lease_expires_at=None,
                    updated_at=now,
                )
            )
            return int(result.rowcount or 0)

    def claim(
        self,
        workflow_job_id: str,
        *,
        worker_id: str = "inline-worker",
        lease_for: timedelta = timedelta(hours=1),
    ) -> str | None:
        if not worker_id.strip():
            raise ValueError("worker_id is required")
        if lease_for <= timedelta(0):
            raise ValueError("lease_for must be positive")
        now = datetime.now(UTC)
        lease_token = uuid7()
        with self.engine.begin() as connection:
            row = connection.execute(
                select(workflow_jobs).where(
                    workflow_jobs.c.workflow_job_id == workflow_job_id
                )
            ).one_or_none()
            if row is None:
                return None
            dependency_ids = tuple(row.dependency_job_ids_json or ())
            if dependency_ids:
                completed_dependencies = int(
                    connection.execute(
                        select(func.count())
                        .select_from(workflow_jobs)
                        .where(
                            workflow_jobs.c.workflow_job_id.in_(dependency_ids),
                            workflow_jobs.c.status
                            == WorkflowJobStatus.COMPLETED.value,
                        )
                    ).scalar_one()
                )
                if completed_dependencies != len(set(dependency_ids)):
                    return None
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
                    lease_owner=worker_id.strip(),
                    lease_token=lease_token,
                    lease_expires_at=now + lease_for,
                    started_at=now,
                    updated_at=now,
                    completed_at=None,
                    error_code=None,
                )
            )
            claimed = bool(result.rowcount)
        if claimed:
            self._emit(workflow_job_id, "workflow.job.started.v1")
        return lease_token if claimed else None

    def heartbeat(
        self,
        workflow_job_id: str,
        *,
        worker_id: str,
        lease_token: str,
        lease_for: timedelta = timedelta(hours=1),
        cursor: dict[str, Any] | None = None,
    ) -> bool:
        if lease_for <= timedelta(0):
            raise ValueError("lease_for must be positive")
        now = datetime.now(UTC)
        values: dict[str, Any] = {
            "lease_expires_at": now + lease_for,
            "updated_at": now,
        }
        if cursor is not None:
            values["cursor_json"] = cursor
        with self.engine.begin() as connection:
            result = connection.execute(
                update(workflow_jobs)
                .where(
                    (workflow_jobs.c.workflow_job_id == workflow_job_id)
                    & (workflow_jobs.c.status == WorkflowJobStatus.RUNNING.value)
                    & (workflow_jobs.c.lease_owner == worker_id)
                    & (workflow_jobs.c.lease_token == lease_token)
                    & (workflow_jobs.c.lease_expires_at > now)
                )
                .values(**values)
            )
        return int(result.rowcount or 0) == 1

    def complete(
        self,
        workflow_job_id: str,
        *,
        result: dict[str, Any],
        worker_id: str,
        lease_token: str,
    ) -> None:
        now = datetime.now(UTC)
        with self.engine.begin() as connection:
            updated = connection.execute(
                update(workflow_jobs)
                .where(
                    (workflow_jobs.c.workflow_job_id == workflow_job_id)
                    & (workflow_jobs.c.status == WorkflowJobStatus.RUNNING.value)
                    & (workflow_jobs.c.lease_owner == worker_id)
                    & (workflow_jobs.c.lease_token == lease_token)
                    & (workflow_jobs.c.lease_expires_at > now)
                )
                .values(
                    status=WorkflowJobStatus.COMPLETED.value,
                    result_json=result,
                    cursor_json={"completed": True},
                    error_code=None,
                    lease_owner=None,
                    lease_token=None,
                    lease_expires_at=None,
                    updated_at=now,
                    completed_at=now,
                )
            )
        if int(updated.rowcount or 0) != 1:
            raise ValueError("Workflow completion requires the active lease owner")
        self._emit(
            workflow_job_id,
            "workflow.job.completed.v1",
            payload={"result_sha256": _canonical_hash(result)},
        )

    def fail(
        self,
        workflow_job_id: str,
        *,
        error_code: str,
        worker_id: str,
        lease_token: str,
    ) -> None:
        now = datetime.now(UTC)
        with self.engine.begin() as connection:
            updated = connection.execute(
                update(workflow_jobs)
                .where(
                    (workflow_jobs.c.workflow_job_id == workflow_job_id)
                    & (workflow_jobs.c.status == WorkflowJobStatus.RUNNING.value)
                    & (workflow_jobs.c.lease_owner == worker_id)
                    & (workflow_jobs.c.lease_token == lease_token)
                    & (workflow_jobs.c.lease_expires_at > now)
                )
                .values(
                    status=WorkflowJobStatus.FAILED.value,
                    error_code=error_code[:120],
                    lease_owner=None,
                    lease_token=None,
                    lease_expires_at=None,
                    updated_at=now,
                )
            )
        if int(updated.rowcount or 0) != 1:
            raise ValueError("Workflow failure requires the active lease owner")
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

    def jobs_by_ids(self, workflow_job_ids: tuple[str, ...]) -> tuple[WorkflowJob, ...]:
        if not workflow_job_ids:
            return ()
        statement = (
            select(workflow_jobs)
            .where(workflow_jobs.c.workflow_job_id.in_(workflow_job_ids))
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
            **job.model_dump(
                exclude={
                    "payload",
                    "cursor",
                    "result",
                    "status",
                    "dependency_job_ids",
                }
            ),
            "dependency_job_ids_json": list(job.dependency_job_ids),
            "payload_json": job.payload,
            "cursor_json": job.cursor,
            "result_json": job.result,
            "status": job.status.value,
        }

    @staticmethod
    def _from_row(row: dict[str, Any]) -> WorkflowJob:
        row["payload"] = row.pop("payload_json")
        row["dependency_job_ids"] = row.pop("dependency_job_ids_json")
        row["cursor"] = row.pop("cursor_json")
        row["result"] = row.pop("result_json")
        row["created_at"] = _utc(row["created_at"])
        row["started_at"] = _utc(row["started_at"])
        row["updated_at"] = _utc(row["updated_at"])
        row["completed_at"] = _utc(row["completed_at"])
        row["lease_expires_at"] = _utc(row["lease_expires_at"])
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
        dataset_contract = {
            key: value
            for key, value in request_material.items()
            if key not in {"start", "end"}
        }
        dataset_contract["partition_days"] = partition_days
        job_group_id = str(
            uuid.uuid5(uuid.NAMESPACE_URL, _canonical_hash(dataset_contract))
        )
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
        return job_group_id, self.jobs.jobs_by_ids(
            tuple(job.workflow_job_id for job in planned)
        )

    async def run(
        self,
        request: StockBarsRequest,
        *,
        partition_days: int,
        max_attempts: int = 3,
        max_partitions: int | None = None,
    ) -> dict[str, Any]:
        job_group_id, planned = self.plan(
            request,
            partition_days=partition_days,
            max_attempts=max_attempts,
        )
        self.jobs.requeue_stale(job_group_id=job_group_id)
        worker_id = f"backfill-{uuid7()}"
        processed = 0
        for job in planned:
            if job.status == WorkflowJobStatus.COMPLETED:
                continue
            if max_partitions is not None and processed >= max_partitions:
                break
            lease_token = self.jobs.claim(
                job.workflow_job_id,
                worker_id=worker_id,
                lease_for=timedelta(minutes=2),
            )
            if lease_token is None:
                continue
            heartbeat = asyncio.create_task(
                self._renew_lease(
                    workflow_job_id=job.workflow_job_id,
                    worker_id=worker_id,
                    lease_token=lease_token,
                )
            )
            try:
                summary = await self.service.ingest_stock_bars(
                    StockBarsRequest.model_validate(job.payload)
                )
            except Exception as exc:
                heartbeat.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat
                self.jobs.fail(
                    job.workflow_job_id,
                    error_code=type(exc).__name__,
                    worker_id=worker_id,
                    lease_token=lease_token,
                )
                raise
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat
            if not self.jobs.heartbeat(
                job.workflow_job_id,
                worker_id=worker_id,
                lease_token=lease_token,
                lease_for=timedelta(minutes=2),
            ):
                raise RuntimeError("Backfill workflow lease was lost before completion")
            self.jobs.complete(
                job.workflow_job_id,
                result=summary.model_dump(mode="json"),
                worker_id=worker_id,
                lease_token=lease_token,
            )
            processed += 1
        jobs = self.jobs.jobs_by_ids(
            tuple(job.workflow_job_id for job in planned)
        )
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

    async def _renew_lease(
        self,
        *,
        workflow_job_id: str,
        worker_id: str,
        lease_token: str,
    ) -> None:
        while True:
            await asyncio.sleep(30)
            renewed = await asyncio.to_thread(
                self.jobs.heartbeat,
                workflow_job_id,
                worker_id=worker_id,
                lease_token=lease_token,
                lease_for=timedelta(minutes=2),
            )
            if not renewed:
                raise RuntimeError("Backfill workflow lease ownership was lost")
