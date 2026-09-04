"""Add ML training, model registry, forecasts, and promotion audit."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260904_0018"
down_revision: str | None = "20260904_0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ml_training_runs",
        sa.Column("training_run_id", sa.String(length=36), nullable=False),
        sa.Column("symbol", sa.String(length=24), nullable=False),
        sa.Column("timeframe", sa.String(length=16), nullable=False),
        sa.Column("horizon_bars", sa.Integer(), nullable=False),
        sa.Column("feature_set_version", sa.String(length=80), nullable=False),
        sa.Column("dataset_sha256", sa.String(length=64), nullable=False),
        sa.Column("sample_count", sa.Integer(), nullable=False),
        sa.Column("fold_count", sa.Integer(), nullable=False),
        sa.Column("embargo_bars", sa.Integer(), nullable=False),
        sa.Column("model_ids_json", sa.JSON(), nullable=False),
        sa.Column("selected_model_id", sa.String(length=36), nullable=False),
        sa.Column("selection_metric", sa.String(length=80), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("code_git_sha", sa.String(length=64), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("training_run_id", name="pk_ml_training_runs"),
    )
    for column in ("symbol", "dataset_sha256", "status", "finished_at"):
        op.create_index(f"ix_ml_training_runs_{column}", "ml_training_runs", [column])

    op.create_table(
        "ml_models",
        sa.Column("model_id", sa.String(length=36), nullable=False),
        sa.Column("training_run_id", sa.String(length=36), nullable=False),
        sa.Column("model_name", sa.String(length=120), nullable=False),
        sa.Column("model_version", sa.String(length=160), nullable=False),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("symbol", sa.String(length=24), nullable=False),
        sa.Column("timeframe", sa.String(length=16), nullable=False),
        sa.Column("horizon_bars", sa.Integer(), nullable=False),
        sa.Column("feature_set_version", sa.String(length=80), nullable=False),
        sa.Column("feature_names_json", sa.JSON(), nullable=False),
        sa.Column("training_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("training_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("training_data_cutoff", sa.DateTime(timezone=True), nullable=False),
        sa.Column("artifact_json", sa.JSON(), nullable=False),
        sa.Column("artifact_sha256", sa.String(length=64), nullable=False),
        sa.Column("metrics_json", sa.JSON(), nullable=False),
        sa.Column("calibration_json", sa.JSON(), nullable=False),
        sa.Column("drift_json", sa.JSON(), nullable=False),
        sa.Column("promotion_assessment_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("champion_slot", sa.String(length=180), nullable=True),
        sa.Column("code_git_sha", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["training_run_id"],
            ["ml_training_runs.training_run_id"],
            name="fk_ml_models_training_run_id_ml_training_runs",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("model_id", name="pk_ml_models"),
        sa.UniqueConstraint("champion_slot", name="uq_ml_models_champion_slot"),
        sa.UniqueConstraint("model_version", name="uq_ml_models_model_version"),
    )
    for column in ("training_run_id", "model_name", "symbol", "status", "created_at"):
        op.create_index(f"ix_ml_models_{column}", "ml_models", [column])

    op.create_table(
        "ml_forecasts",
        sa.Column("forecast_id", sa.String(length=36), nullable=False),
        sa.Column("model_id", sa.String(length=36), nullable=False),
        sa.Column("symbol", sa.String(length=24), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("horizon", sa.String(length=40), nullable=False),
        sa.Column("expected_return", sa.Numeric(24, 12), nullable=False),
        sa.Column("probability_up", sa.Numeric(18, 12), nullable=False),
        sa.Column("uncertainty", sa.Numeric(18, 12), nullable=False),
        sa.Column("model_version", sa.String(length=160), nullable=False),
        sa.Column("training_data_cutoff", sa.DateTime(timezone=True), nullable=False),
        sa.Column("feature_snapshot_id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["feature_snapshot_id"],
            ["feature_snapshots.feature_snapshot_id"],
            name="fk_ml_forecasts_feature_snapshot_id_feature_snapshots",
        ),
        sa.ForeignKeyConstraint(
            ["model_id"],
            ["ml_models.model_id"],
            name="fk_ml_forecasts_model_id_ml_models",
        ),
        sa.PrimaryKeyConstraint("forecast_id", name="pk_ml_forecasts"),
        sa.UniqueConstraint(
            "model_id",
            "feature_snapshot_id",
            "horizon",
            name="uq_ml_forecasts_model_feature_horizon",
        ),
    )
    for column in ("model_id", "symbol", "as_of", "feature_snapshot_id"):
        op.create_index(f"ix_ml_forecasts_{column}", "ml_forecasts", [column])

    with op.batch_alter_table("research_analyses") as batch:
        batch.create_foreign_key(
            "fk_research_analyses_forecast_id_ml_forecasts",
            "ml_forecasts",
            ["forecast_id"],
            ["forecast_id"],
        )

    op.create_table(
        "model_registry_events",
        sa.Column("registry_event_id", sa.String(length=36), nullable=False),
        sa.Column("model_id", sa.String(length=36), nullable=False),
        sa.Column("previous_status", sa.String(length=24), nullable=False),
        sa.Column("new_status", sa.String(length=24), nullable=False),
        sa.Column("training_run_id", sa.String(length=36), nullable=False),
        sa.Column("approved_by", sa.String(length=80), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["model_id"],
            ["ml_models.model_id"],
            name="fk_model_registry_events_model_id_ml_models",
        ),
        sa.ForeignKeyConstraint(
            ["training_run_id"],
            ["ml_training_runs.training_run_id"],
            name="fk_model_registry_events_training_run_id_ml_training_runs",
        ),
        sa.PrimaryKeyConstraint("registry_event_id", name="pk_model_registry_events"),
    )
    for column in ("model_id", "new_status", "created_at"):
        op.create_index(
            f"ix_model_registry_events_{column}",
            "model_registry_events",
            [column],
        )


def downgrade() -> None:
    for column in ("created_at", "new_status", "model_id"):
        op.drop_index(
            f"ix_model_registry_events_{column}",
            table_name="model_registry_events",
        )
    op.drop_table("model_registry_events")
    with op.batch_alter_table("research_analyses") as batch:
        batch.drop_constraint(
            "fk_research_analyses_forecast_id_ml_forecasts",
            type_="foreignkey",
        )
    for column in ("feature_snapshot_id", "as_of", "symbol", "model_id"):
        op.drop_index(f"ix_ml_forecasts_{column}", table_name="ml_forecasts")
    op.drop_table("ml_forecasts")
    for column in ("created_at", "status", "symbol", "model_name", "training_run_id"):
        op.drop_index(f"ix_ml_models_{column}", table_name="ml_models")
    op.drop_table("ml_models")
    for column in ("finished_at", "status", "dataset_sha256", "symbol"):
        op.drop_index(f"ix_ml_training_runs_{column}", table_name="ml_training_runs")
    op.drop_table("ml_training_runs")
