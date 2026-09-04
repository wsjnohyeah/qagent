"""Add point-in-time research artifacts and immutable experiment results."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260904_0007"
down_revision: str | None = "20260904_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "evidence_packets",
        sa.Column("evidence_packet_id", sa.String(length=36), nullable=False),
        sa.Column("symbol", sa.String(length=24), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("evidence_hash", sa.String(length=64), nullable=False),
        sa.Column("references_json", sa.JSON(), nullable=False),
        sa.Column(
            "source_max_available_from",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("evidence_packet_id", name="pk_evidence_packets"),
        sa.UniqueConstraint(
            "symbol",
            "as_of",
            "evidence_hash",
            name="uq_evidence_packets_identity",
        ),
    )
    op.create_index("ix_evidence_packets_symbol", "evidence_packets", ["symbol"])
    op.create_index("ix_evidence_packets_as_of", "evidence_packets", ["as_of"])

    op.create_table(
        "feature_snapshots",
        sa.Column("feature_snapshot_id", sa.String(length=36), nullable=False),
        sa.Column("evidence_packet_id", sa.String(length=36), nullable=False),
        sa.Column("symbol", sa.String(length=24), nullable=False),
        sa.Column("timeframe", sa.String(length=16), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("feature_set_version", sa.String(length=80), nullable=False),
        sa.Column("feature_values", sa.JSON(), nullable=False),
        sa.Column(
            "source_max_available_from",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column("data_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["evidence_packet_id"],
            ["evidence_packets.evidence_packet_id"],
            name="fk_feature_snapshots_evidence_packet_id_evidence_packets",
        ),
        sa.PrimaryKeyConstraint("feature_snapshot_id", name="pk_feature_snapshots"),
        sa.UniqueConstraint(
            "symbol",
            "timeframe",
            "as_of",
            "feature_set_version",
            "data_hash",
            name="uq_feature_snapshots_identity",
        ),
    )
    op.create_index(
        "ix_feature_snapshots_evidence_packet_id",
        "feature_snapshots",
        ["evidence_packet_id"],
    )
    op.create_index("ix_feature_snapshots_symbol", "feature_snapshots", ["symbol"])
    op.create_index("ix_feature_snapshots_as_of", "feature_snapshots", ["as_of"])

    op.create_table(
        "strategy_specs",
        sa.Column("strategy_spec_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("version", sa.String(length=120), nullable=False),
        sa.Column("strategy_type", sa.String(length=80), nullable=False),
        sa.Column("timeframe", sa.String(length=16), nullable=False),
        sa.Column("feature_set_version", sa.String(length=80), nullable=False),
        sa.Column("parameters_json", sa.JSON(), nullable=False),
        sa.Column("data_requirements_json", sa.JSON(), nullable=False),
        sa.Column("code_sha256", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("strategy_spec_id", name="pk_strategy_specs"),
        sa.UniqueConstraint(
            "name",
            "version",
            name="uq_strategy_specs_name_version",
        ),
    )

    op.create_table(
        "experiment_runs",
        sa.Column("experiment_run_id", sa.String(length=36), nullable=False),
        sa.Column("strategy_spec_id", sa.String(length=36), nullable=False),
        sa.Column("symbol", sa.String(length=24), nullable=False),
        sa.Column("timeframe", sa.String(length=16), nullable=False),
        sa.Column("as_of_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("as_of_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("dataset_hash", sa.String(length=64), nullable=False),
        sa.Column("code_git_sha", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("cost_model_json", sa.JSON(), nullable=False),
        sa.Column("metrics_json", sa.JSON(), nullable=False),
        sa.Column("feature_snapshot_ids", sa.JSON(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["strategy_spec_id"],
            ["strategy_specs.strategy_spec_id"],
            name="fk_experiment_runs_strategy_spec_id_strategy_specs",
        ),
        sa.PrimaryKeyConstraint("experiment_run_id", name="pk_experiment_runs"),
    )
    op.create_index(
        "ix_experiment_runs_strategy_spec_id",
        "experiment_runs",
        ["strategy_spec_id"],
    )
    op.create_index("ix_experiment_runs_symbol", "experiment_runs", ["symbol"])
    op.create_index(
        "ix_experiment_runs_dataset_hash",
        "experiment_runs",
        ["dataset_hash"],
    )
    op.create_index("ix_experiment_runs_status", "experiment_runs", ["status"])

    op.create_table(
        "backtest_trades",
        sa.Column("trade_id", sa.String(length=36), nullable=False),
        sa.Column("experiment_run_id", sa.String(length=36), nullable=False),
        sa.Column("symbol", sa.String(length=24), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("signal_as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("entry_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("exit_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("quantity", sa.BigInteger(), nullable=False),
        sa.Column("entry_price", sa.Numeric(precision=20, scale=8), nullable=False),
        sa.Column("exit_price", sa.Numeric(precision=20, scale=8), nullable=False),
        sa.Column("gross_pnl", sa.Numeric(precision=24, scale=8), nullable=False),
        sa.Column("transaction_cost", sa.Numeric(precision=24, scale=8), nullable=False),
        sa.Column("net_pnl", sa.Numeric(precision=24, scale=8), nullable=False),
        sa.Column("feature_snapshot_id", sa.String(length=36), nullable=False),
        sa.Column("exit_reason", sa.String(length=80), nullable=False),
        sa.ForeignKeyConstraint(
            ["experiment_run_id"],
            ["experiment_runs.experiment_run_id"],
            name="fk_backtest_trades_experiment_run_id_experiment_runs",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["feature_snapshot_id"],
            ["feature_snapshots.feature_snapshot_id"],
            name="fk_backtest_trades_feature_snapshot_id_feature_snapshots",
        ),
        sa.PrimaryKeyConstraint("trade_id", name="pk_backtest_trades"),
    )
    op.create_index(
        "ix_backtest_trades_experiment_run_id",
        "backtest_trades",
        ["experiment_run_id"],
    )
    op.create_index("ix_backtest_trades_symbol", "backtest_trades", ["symbol"])


def downgrade() -> None:
    op.drop_index("ix_backtest_trades_symbol", table_name="backtest_trades")
    op.drop_index(
        "ix_backtest_trades_experiment_run_id",
        table_name="backtest_trades",
    )
    op.drop_table("backtest_trades")
    op.drop_index("ix_experiment_runs_status", table_name="experiment_runs")
    op.drop_index("ix_experiment_runs_dataset_hash", table_name="experiment_runs")
    op.drop_index("ix_experiment_runs_symbol", table_name="experiment_runs")
    op.drop_index(
        "ix_experiment_runs_strategy_spec_id",
        table_name="experiment_runs",
    )
    op.drop_table("experiment_runs")
    op.drop_table("strategy_specs")
    op.drop_index("ix_feature_snapshots_as_of", table_name="feature_snapshots")
    op.drop_index("ix_feature_snapshots_symbol", table_name="feature_snapshots")
    op.drop_index(
        "ix_feature_snapshots_evidence_packet_id",
        table_name="feature_snapshots",
    )
    op.drop_table("feature_snapshots")
    op.drop_index("ix_evidence_packets_as_of", table_name="evidence_packets")
    op.drop_index("ix_evidence_packets_symbol", table_name="evidence_packets")
    op.drop_table("evidence_packets")
