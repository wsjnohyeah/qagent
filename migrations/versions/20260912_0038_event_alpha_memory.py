"""Add point-in-time Event Alpha memory and playbooks."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260912_0038"
down_revision: str | None = "20260911_0037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "event_alpha_cards",
        sa.Column("event_card_id", sa.String(length=36), primary_key=True),
        sa.Column(
            "catalyst_id",
            sa.String(length=36),
            sa.ForeignKey("catalysts.catalyst_id"),
            nullable=False,
        ),
        sa.Column("symbol", sa.String(length=24), nullable=False),
        sa.Column("event_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("available_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("availability_basis", sa.String(length=40), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("schema_version", sa.String(length=80), nullable=False),
        sa.Column("prompt_version", sa.String(length=80), nullable=False),
        sa.Column("input_sha256", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("event_type", sa.String(length=48)),
        sa.Column("direction", sa.String(length=24)),
        sa.Column("mechanism", sa.String(length=120)),
        sa.Column("novelty_score", sa.Numeric(8, 6)),
        sa.Column("surprise_score", sa.Numeric(8, 6)),
        sa.Column("source_quality_score", sa.Numeric(8, 6)),
        sa.Column("confidence", sa.Numeric(8, 6)),
        sa.Column("generalized_tags_json", sa.JSON(), nullable=False),
        sa.Column("expected_horizons_json", sa.JSON(), nullable=False),
        sa.Column("evidence_json", sa.JSON(), nullable=False),
        sa.Column("card_json", sa.JSON()),
        sa.Column(
            "llm_invocation_id",
            sa.String(length=36),
            sa.ForeignKey("llm_invocations.invocation_id"),
        ),
        sa.Column("rejection_reason", sa.String(length=240)),
        sa.Column("code_git_sha", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "catalyst_id",
            "schema_version",
            "input_sha256",
            name="uq_event_alpha_cards_input",
        ),
    )
    for column in (
        "catalyst_id",
        "symbol",
        "event_time",
        "available_from",
        "as_of",
        "input_sha256",
        "status",
        "event_type",
        "direction",
        "llm_invocation_id",
        "created_at",
    ):
        op.create_index(
            f"ix_event_alpha_cards_{column}",
            "event_alpha_cards",
            [column],
        )

    op.create_table(
        "event_alpha_outcomes",
        sa.Column("event_outcome_id", sa.String(length=36), primary_key=True),
        sa.Column(
            "event_card_id",
            sa.String(length=36),
            sa.ForeignKey("event_alpha_cards.event_card_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("horizon_sessions", sa.Integer(), nullable=False),
        sa.Column("entry_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("exit_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("available_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("entry_price", sa.Numeric(20, 8), nullable=False),
        sa.Column("exit_price", sa.Numeric(20, 8), nullable=False),
        sa.Column("total_return", sa.Numeric(24, 12), nullable=False),
        sa.Column("maximum_favorable_return", sa.Numeric(24, 12), nullable=False),
        sa.Column("maximum_adverse_return", sa.Numeric(24, 12), nullable=False),
        sa.Column("data_sha256", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "event_card_id",
            "horizon_sessions",
            name="uq_event_alpha_outcomes_card_horizon",
        ),
    )
    for column in ("event_card_id", "horizon_sessions", "available_from"):
        op.create_index(
            f"ix_event_alpha_outcomes_{column}",
            "event_alpha_outcomes",
            [column],
        )

    op.create_table(
        "event_alpha_assessments",
        sa.Column("event_assessment_id", sa.String(length=36), primary_key=True),
        sa.Column(
            "event_card_id",
            sa.String(length=36),
            sa.ForeignKey("event_alpha_cards.event_card_id"),
            nullable=False,
        ),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("schema_version", sa.String(length=80), nullable=False),
        sa.Column("prompt_version", sa.String(length=80), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("selected_horizon_sessions", sa.Integer()),
        sa.Column("analog_card_ids_json", sa.JSON(), nullable=False),
        sa.Column("analog_statistics_json", sa.JSON(), nullable=False),
        sa.Column("assessment_json", sa.JSON()),
        sa.Column("gate_assessment_json", sa.JSON(), nullable=False),
        sa.Column("input_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "llm_invocation_id",
            sa.String(length=36),
            sa.ForeignKey("llm_invocations.invocation_id"),
        ),
        sa.Column("rejection_reason", sa.String(length=240)),
        sa.Column("code_git_sha", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "event_card_id",
            "schema_version",
            "input_sha256",
            name="uq_event_alpha_assessments_input",
        ),
    )
    for column in (
        "event_card_id",
        "as_of",
        "status",
        "selected_horizon_sessions",
        "input_sha256",
        "llm_invocation_id",
        "created_at",
    ):
        op.create_index(
            f"ix_event_alpha_assessments_{column}",
            "event_alpha_assessments",
            [column],
        )

    op.create_table(
        "event_alpha_playbooks",
        sa.Column("event_playbook_id", sa.String(length=36), primary_key=True),
        sa.Column(
            "event_assessment_id",
            sa.String(length=36),
            sa.ForeignKey("event_alpha_assessments.event_assessment_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("version", sa.String(length=160), nullable=False, unique=True),
        sa.Column("event_type", sa.String(length=48), nullable=False),
        sa.Column("direction", sa.String(length=24), nullable=False),
        sa.Column("holding_period_sessions", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("playbook_json", sa.JSON(), nullable=False),
        sa.Column("gate_assessment_json", sa.JSON(), nullable=False),
        sa.Column("code_sha256", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    for column in (
        "event_type",
        "holding_period_sessions",
        "status",
        "created_at",
    ):
        op.create_index(
            f"ix_event_alpha_playbooks_{column}",
            "event_alpha_playbooks",
            [column],
        )


def downgrade() -> None:
    op.drop_table("event_alpha_playbooks")
    op.drop_table("event_alpha_assessments")
    op.drop_table("event_alpha_outcomes")
    op.drop_table("event_alpha_cards")
