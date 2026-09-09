"""Add encrypted Robinhood MCP authorization and call audit state."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260909_0036"
down_revision: str | None = "20260908_0035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "robinhood_oauth_flows",
        sa.Column("oauth_flow_id", sa.String(length=36), primary_key=True),
        sa.Column("state_sha256", sa.String(length=64), nullable=False, unique=True),
        sa.Column("code_verifier_ciphertext", sa.Text(), nullable=False),
        sa.Column("client_id", sa.String(length=240), nullable=False),
        sa.Column("redirect_uri", sa.Text(), nullable=False),
        sa.Column("requested_by", sa.String(length=80), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_robinhood_oauth_flows_expires_at",
        "robinhood_oauth_flows",
        ["expires_at"],
    )
    op.create_table(
        "robinhood_mcp_connections",
        sa.Column("connection_id", sa.String(length=36), primary_key=True),
        sa.Column("provider", sa.String(length=40), nullable=False, unique=True),
        sa.Column("client_id", sa.String(length=240), nullable=False),
        sa.Column("redirect_uri", sa.Text(), nullable=False),
        sa.Column("access_token_ciphertext", sa.Text(), nullable=False),
        sa.Column("refresh_token_ciphertext", sa.Text(), nullable=True),
        sa.Column("token_type", sa.String(length=40), nullable=False),
        sa.Column("scope", sa.String(length=240), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("connected_by", sa.String(length=80), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("disconnected_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_robinhood_mcp_connections_status",
        "robinhood_mcp_connections",
        ["status"],
    )
    op.create_index(
        "ix_robinhood_mcp_connections_updated_at",
        "robinhood_mcp_connections",
        ["updated_at"],
    )
    op.create_table(
        "robinhood_mcp_calls",
        sa.Column("mcp_call_id", sa.String(length=36), primary_key=True),
        sa.Column("connection_id", sa.String(length=36), nullable=True),
        sa.Column("tool_name", sa.String(length=120), nullable=False),
        sa.Column("arguments_sha256", sa.String(length=64), nullable=False),
        sa.Column("arguments_json", sa.JSON(), nullable=False),
        sa.Column("response_sha256", sa.String(length=64), nullable=True),
        sa.Column("response_json", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("error_code", sa.String(length=120), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_robinhood_mcp_calls_connection_id",
        "robinhood_mcp_calls",
        ["connection_id"],
    )
    op.create_index(
        "ix_robinhood_mcp_calls_tool_name",
        "robinhood_mcp_calls",
        ["tool_name"],
    )
    op.create_index(
        "ix_robinhood_mcp_calls_status",
        "robinhood_mcp_calls",
        ["status"],
    )
    op.create_index(
        "ix_robinhood_mcp_calls_finished_at",
        "robinhood_mcp_calls",
        ["finished_at"],
    )


def downgrade() -> None:
    op.drop_table("robinhood_mcp_calls")
    op.drop_table("robinhood_mcp_connections")
    op.drop_table("robinhood_oauth_flows")
