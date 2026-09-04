"""Add data-quality, resumable-workflow, and reference-import audit tables."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260904_0015"
down_revision: str | None = "20260904_0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "data_quality_reports",
        sa.Column("data_quality_report_id", sa.String(length=36), nullable=False),
        sa.Column("ruleset_version", sa.String(length=80), nullable=False),
        sa.Column("dataset_type", sa.String(length=80), nullable=False),
        sa.Column("symbol", sa.String(length=24), nullable=False),
        sa.Column("timeframe", sa.String(length=16), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("record_count", sa.Integer(), nullable=False),
        sa.Column("data_sha256", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("checks_json", sa.JSON(), nullable=False),
        sa.Column("issue_counts_json", sa.JSON(), nullable=False),
        sa.Column("code_git_sha", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "data_quality_report_id",
            name="pk_data_quality_reports",
        ),
        sa.UniqueConstraint(
            "ruleset_version",
            "dataset_type",
            "symbol",
            "timeframe",
            "data_sha256",
            name="uq_data_quality_reports_dataset_hash",
        ),
    )
    for column in ("dataset_type", "symbol", "data_sha256", "status", "created_at"):
        op.create_index(f"ix_data_quality_reports_{column}", "data_quality_reports", [column])

    op.create_table(
        "workflow_jobs",
        sa.Column("workflow_job_id", sa.String(length=36), nullable=False),
        sa.Column("job_group_id", sa.String(length=36), nullable=False),
        sa.Column("job_type", sa.String(length=80), nullable=False),
        sa.Column("partition_key", sa.String(length=160), nullable=False),
        sa.Column("request_sha256", sa.String(length=64), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("cursor_json", sa.JSON(), nullable=False),
        sa.Column("result_json", sa.JSON(), nullable=False),
        sa.Column("error_code", sa.String(length=120), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("workflow_job_id", name="pk_workflow_jobs"),
        sa.UniqueConstraint(
            "job_group_id",
            "partition_key",
            name="uq_workflow_jobs_group_partition",
        ),
    )
    for column in ("job_group_id", "job_type", "status", "updated_at"):
        op.create_index(f"ix_workflow_jobs_{column}", "workflow_jobs", [column])

    op.create_table(
        "reference_imports",
        sa.Column("reference_import_id", sa.String(length=36), nullable=False),
        sa.Column("dataset_type", sa.String(length=80), nullable=False),
        sa.Column("source", sa.String(length=80), nullable=False),
        sa.Column("source_version", sa.String(length=120), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("records_received", sa.Integer(), nullable=False),
        sa.Column("records_inserted", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("reference_import_id", name="pk_reference_imports"),
        sa.UniqueConstraint(
            "dataset_type",
            "source",
            "source_version",
            "content_sha256",
            name="uq_reference_imports_content",
        ),
    )
    op.create_index("ix_reference_imports_dataset_type", "reference_imports", ["dataset_type"])
    op.create_index("ix_reference_imports_created_at", "reference_imports", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_reference_imports_created_at", table_name="reference_imports")
    op.drop_index("ix_reference_imports_dataset_type", table_name="reference_imports")
    op.drop_table("reference_imports")
    for column in ("updated_at", "status", "job_type", "job_group_id"):
        op.drop_index(f"ix_workflow_jobs_{column}", table_name="workflow_jobs")
    op.drop_table("workflow_jobs")
    for column in ("created_at", "status", "data_sha256", "symbol", "dataset_type"):
        op.drop_index(f"ix_data_quality_reports_{column}", table_name="data_quality_reports")
    op.drop_table("data_quality_reports")
