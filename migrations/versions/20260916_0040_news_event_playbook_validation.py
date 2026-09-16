"""Add news-first Event Alpha validation and match lineage."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260916_0040"
down_revision: str | None = "20260913_0039"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "shadow_signal_candidates",
        sa.Column(
            "catalyst_id",
            sa.String(length=64),
            nullable=False,
            server_default="NOT_APPLICABLE_BASELINE",
        ),
    )
    op.create_table(
        "event_alpha_validations",
        sa.Column("event_validation_id", sa.String(length=36), primary_key=True),
        sa.Column(
            "event_playbook_id",
            sa.String(length=36),
            sa.ForeignKey("event_alpha_playbooks.event_playbook_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("schema_version", sa.String(length=80), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("holdout_card_ids_json", sa.JSON(), nullable=False),
        sa.Column("holdout_statistics_json", sa.JSON(), nullable=False),
        sa.Column("gate_assessment_json", sa.JSON(), nullable=False),
        sa.Column("input_sha256", sa.String(length=64), nullable=False),
        sa.Column("code_git_sha", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "event_playbook_id",
            "schema_version",
            "input_sha256",
            name="uq_event_alpha_validations_input",
        ),
    )
    for column in (
        "event_playbook_id",
        "as_of",
        "status",
        "input_sha256",
        "created_at",
    ):
        op.create_index(
            f"ix_event_alpha_validations_{column}",
            "event_alpha_validations",
            [column],
        )

    op.create_table(
        "event_alpha_matches",
        sa.Column("event_match_id", sa.String(length=36), primary_key=True),
        sa.Column(
            "event_playbook_id",
            sa.String(length=36),
            sa.ForeignKey("event_alpha_playbooks.event_playbook_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "event_validation_id",
            sa.String(length=36),
            sa.ForeignKey("event_alpha_validations.event_validation_id"),
            nullable=False,
        ),
        sa.Column(
            "event_card_id",
            sa.String(length=36),
            sa.ForeignKey("event_alpha_cards.event_card_id"),
            nullable=False,
        ),
        sa.Column("symbol", sa.String(length=24), nullable=False),
        sa.Column("similarity_score", sa.Numeric(8, 6), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("reason", sa.String(length=240), nullable=False),
        sa.Column("strategy_spec_id", sa.String(length=36)),
        sa.Column("shadow_deployment_id", sa.String(length=36)),
        sa.Column("input_sha256", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "event_playbook_id",
            "event_validation_id",
            "event_card_id",
            name="uq_event_alpha_matches_case",
        ),
    )
    for column in (
        "event_playbook_id",
        "event_validation_id",
        "event_card_id",
        "symbol",
        "status",
        "strategy_spec_id",
        "shadow_deployment_id",
        "input_sha256",
        "created_at",
    ):
        op.create_index(
            f"ix_event_alpha_matches_{column}",
            "event_alpha_matches",
            [column],
        )


def downgrade() -> None:
    op.drop_table("event_alpha_matches")
    op.drop_table("event_alpha_validations")
    op.drop_column("shadow_signal_candidates", "catalyst_id")
