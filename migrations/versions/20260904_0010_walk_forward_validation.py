"""Add immutable walk-forward validation reports."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260904_0010"
down_revision: str | None = "20260904_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "validation_reports",
        sa.Column("validation_report_id", sa.String(length=36), nullable=False),
        sa.Column("symbol", sa.String(length=24), nullable=False),
        sa.Column("timeframe", sa.String(length=16), nullable=False),
        sa.Column("strategy_types", sa.JSON(), nullable=False),
        sa.Column("selection_metric", sa.String(length=40), nullable=False),
        sa.Column("train_bars", sa.Integer(), nullable=False),
        sa.Column("test_bars", sa.Integer(), nullable=False),
        sa.Column("step_bars", sa.Integer(), nullable=False),
        sa.Column("embargo_bars", sa.Integer(), nullable=False),
        sa.Column("aggregate_metrics", sa.JSON(), nullable=False),
        sa.Column("regime_metrics", sa.JSON(), nullable=False),
        sa.Column("report_hash", sa.String(length=64), nullable=False),
        sa.Column("code_git_sha", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("validation_report_id", name="pk_validation_reports"),
    )
    op.create_index("ix_validation_reports_symbol", "validation_reports", ["symbol"])
    op.create_index(
        "ix_validation_reports_report_hash",
        "validation_reports",
        ["report_hash"],
    )
    op.create_index(
        "ix_validation_reports_created_at",
        "validation_reports",
        ["created_at"],
    )

    op.create_table(
        "validation_folds",
        sa.Column("validation_fold_id", sa.String(length=36), nullable=False),
        sa.Column("validation_report_id", sa.String(length=36), nullable=False),
        sa.Column("fold_number", sa.Integer(), nullable=False),
        sa.Column("train_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("train_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("test_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("test_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("selected_strategy", sa.String(length=80), nullable=False),
        sa.Column("selection_metric", sa.String(length=40), nullable=False),
        sa.Column("train_experiment_ids", sa.JSON(), nullable=False),
        sa.Column("test_experiment_ids", sa.JSON(), nullable=False),
        sa.Column("selected_train_metrics", sa.JSON(), nullable=False),
        sa.Column("selected_test_metrics", sa.JSON(), nullable=False),
        sa.Column("selected_test_rank", sa.Integer(), nullable=False),
        sa.Column("regime", sa.String(length=24), nullable=False),
        sa.ForeignKeyConstraint(
            ["validation_report_id"],
            ["validation_reports.validation_report_id"],
            name="fk_validation_folds_report",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("validation_fold_id", name="pk_validation_folds"),
        sa.UniqueConstraint(
            "validation_report_id",
            "fold_number",
            name="uq_validation_folds_report_number",
        ),
    )
    op.create_index(
        "ix_validation_folds_validation_report_id",
        "validation_folds",
        ["validation_report_id"],
    )
    op.create_index("ix_validation_folds_regime", "validation_folds", ["regime"])


def downgrade() -> None:
    op.drop_index("ix_validation_folds_regime", table_name="validation_folds")
    op.drop_index(
        "ix_validation_folds_validation_report_id",
        table_name="validation_folds",
    )
    op.drop_table("validation_folds")
    op.drop_index("ix_validation_reports_created_at", table_name="validation_reports")
    op.drop_index("ix_validation_reports_report_hash", table_name="validation_reports")
    op.drop_index("ix_validation_reports_symbol", table_name="validation_reports")
    op.drop_table("validation_reports")
