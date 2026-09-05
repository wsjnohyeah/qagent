"""Add the authenticated Phase 6 control plane and shadow runtime."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260905_0019"
down_revision: str | None = "20260904_0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _index(table: str, *columns: str) -> None:
    for column in columns:
        op.create_index(f"ix_{table}_{column}", table, [column])


def upgrade() -> None:
    op.create_table(
        "admin_sessions",
        sa.Column("session_id", sa.String(length=36), nullable=False),
        sa.Column("token_sha256", sa.String(length=64), nullable=False),
        sa.Column("username", sa.String(length=80), nullable=False),
        sa.Column("credential_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("user_agent_sha256", sa.String(length=64), nullable=True),
        sa.Column("client_ip_sha256", sa.String(length=64), nullable=True),
        sa.PrimaryKeyConstraint("session_id", name="pk_admin_sessions"),
        sa.UniqueConstraint("token_sha256", name="uq_admin_sessions_token_sha256"),
    )
    _index("admin_sessions", "username", "last_seen_at", "expires_at")

    op.create_table(
        "admin_auth_events",
        sa.Column("auth_event_id", sa.String(length=36), nullable=False),
        sa.Column("event_type", sa.String(length=40), nullable=False),
        sa.Column("username", sa.String(length=80), nullable=False),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.Column("detail", sa.String(length=160), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("auth_event_id", name="pk_admin_auth_events"),
    )
    _index("admin_auth_events", "event_type", "username", "success", "created_at")

    op.create_table(
        "system_lists",
        sa.Column("list_id", sa.String(length=36), nullable=False),
        sa.Column("slug", sa.String(length=80), nullable=False),
        sa.Column("list_type", sa.String(length=40), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("mode", sa.String(length=24), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("current_revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("list_id", name="pk_system_lists"),
        sa.UniqueConstraint("slug", name="uq_system_lists_slug"),
    )
    _index("system_lists", "list_type", "status", "updated_at")

    op.create_table(
        "system_list_revisions",
        sa.Column("list_revision_id", sa.String(length=36), nullable=False),
        sa.Column("list_id", sa.String(length=36), nullable=False),
        sa.Column("revision_number", sa.Integer(), nullable=False),
        sa.Column("members_json", sa.JSON(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_by", sa.String(length=80), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["list_id"],
            ["system_lists.list_id"],
            name="fk_system_list_revisions_list_id_system_lists",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "list_revision_id", name="pk_system_list_revisions"
        ),
        sa.UniqueConstraint(
            "list_id",
            "revision_number",
            name="uq_system_list_revisions_number",
        ),
    )
    _index("system_list_revisions", "list_id", "created_at")

    op.create_table(
        "object_threads",
        sa.Column("thread_id", sa.String(length=36), nullable=False),
        sa.Column("object_type", sa.String(length=60), nullable=False),
        sa.Column("object_id", sa.String(length=160), nullable=False),
        sa.Column("title", sa.String(length=240), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("thread_id", name="pk_object_threads"),
        sa.UniqueConstraint(
            "object_type", "object_id", name="uq_object_threads_object"
        ),
    )
    _index("object_threads", "object_type", "object_id", "updated_at")

    op.create_table(
        "thread_posts",
        sa.Column("post_id", sa.String(length=36), nullable=False),
        sa.Column("thread_id", sa.String(length=36), nullable=False),
        sa.Column("author_kind", sa.String(length=24), nullable=False),
        sa.Column("author_name", sa.String(length=80), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["thread_id"],
            ["object_threads.thread_id"],
            name="fk_thread_posts_thread_id_object_threads",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("post_id", name="pk_thread_posts"),
    )
    _index("thread_posts", "thread_id", "created_at")

    op.create_table(
        "admin_action_requests",
        sa.Column("action_request_id", sa.String(length=36), nullable=False),
        sa.Column("action_type", sa.String(length=80), nullable=False),
        sa.Column("target_type", sa.String(length=60), nullable=False),
        sa.Column("target_id", sa.String(length=160), nullable=False),
        sa.Column("parameters_json", sa.JSON(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("preview_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("requested_by", sa.String(length=80), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result_json", sa.JSON(), nullable=False),
        sa.Column("error_code", sa.String(length=120), nullable=True),
        sa.PrimaryKeyConstraint(
            "action_request_id", name="pk_admin_action_requests"
        ),
    )
    _index(
        "admin_action_requests",
        "action_type",
        "target_type",
        "target_id",
        "status",
        "created_at",
    )

    op.create_table(
        "steward_conversations",
        sa.Column("conversation_id", sa.String(length=36), nullable=False),
        sa.Column("title", sa.String(length=160), nullable=False),
        sa.Column("created_by", sa.String(length=80), nullable=False),
        sa.Column("context_object_type", sa.String(length=60), nullable=True),
        sa.Column("context_object_id", sa.String(length=160), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "conversation_id", name="pk_steward_conversations"
        ),
    )
    _index("steward_conversations", "updated_at")

    op.create_table(
        "steward_messages",
        sa.Column("message_id", sa.String(length=36), nullable=False),
        sa.Column("conversation_id", sa.String(length=36), nullable=False),
        sa.Column("role", sa.String(length=24), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("citations_json", sa.JSON(), nullable=False),
        sa.Column("action_request_id", sa.String(length=36), nullable=True),
        sa.Column("llm_invocation_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["steward_conversations.conversation_id"],
            name="fk_steward_messages_conversation_id_steward_conversations",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["action_request_id"],
            ["admin_action_requests.action_request_id"],
            name=(
                "fk_steward_messages_action_request_id_"
                "admin_action_requests"
            ),
        ),
        sa.ForeignKeyConstraint(
            ["llm_invocation_id"],
            ["llm_invocations.invocation_id"],
            name="fk_steward_messages_llm_invocation_id_llm_invocations",
        ),
        sa.PrimaryKeyConstraint("message_id", name="pk_steward_messages"),
    )
    _index(
        "steward_messages",
        "conversation_id",
        "action_request_id",
        "llm_invocation_id",
        "created_at",
    )

    op.create_table(
        "strategy_adoptions",
        sa.Column("adoption_id", sa.String(length=36), nullable=False),
        sa.Column("strategy_spec_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("validation_report_id", sa.String(length=36), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("approved_by", sa.String(length=80), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["strategy_spec_id"],
            ["strategy_specs.strategy_spec_id"],
            name="fk_strategy_adoptions_strategy_spec_id_strategy_specs",
        ),
        sa.ForeignKeyConstraint(
            ["validation_report_id"],
            ["validation_reports.validation_report_id"],
            name=(
                "fk_strategy_adoptions_validation_report_id_"
                "validation_reports"
            ),
        ),
        sa.PrimaryKeyConstraint("adoption_id", name="pk_strategy_adoptions"),
        sa.UniqueConstraint(
            "strategy_spec_id", name="uq_strategy_adoptions_strategy_spec_id"
        ),
    )
    _index(
        "strategy_adoptions",
        "strategy_spec_id",
        "status",
        "validation_report_id",
        "updated_at",
    )

    op.create_table(
        "shadow_deployments",
        sa.Column("shadow_deployment_id", sa.String(length=36), nullable=False),
        sa.Column("strategy_spec_id", sa.String(length=36), nullable=False),
        sa.Column("adoption_id", sa.String(length=36), nullable=True),
        sa.Column("symbol", sa.String(length=24), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("initial_cash", sa.Numeric(24, 8), nullable=False),
        sa.Column("cash_balance", sa.Numeric(24, 8), nullable=False),
        sa.Column("position_quantity", sa.Numeric(24, 10), nullable=False),
        sa.Column("average_entry_price", sa.Numeric(20, 8), nullable=True),
        sa.Column("last_price", sa.Numeric(20, 8), nullable=True),
        sa.Column("realized_pnl", sa.Numeric(24, 8), nullable=False),
        sa.Column("unrealized_pnl", sa.Numeric(24, 8), nullable=False),
        sa.Column("last_processed_bar_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["adoption_id"],
            ["strategy_adoptions.adoption_id"],
            name="fk_shadow_deployments_adoption_id_strategy_adoptions",
        ),
        sa.ForeignKeyConstraint(
            ["strategy_spec_id"],
            ["strategy_specs.strategy_spec_id"],
            name="fk_shadow_deployments_strategy_spec_id_strategy_specs",
        ),
        sa.PrimaryKeyConstraint(
            "shadow_deployment_id", name="pk_shadow_deployments"
        ),
        sa.UniqueConstraint(
            "strategy_spec_id",
            "symbol",
            name="uq_shadow_deployments_strategy_symbol",
        ),
    )
    _index(
        "shadow_deployments",
        "strategy_spec_id",
        "adoption_id",
        "symbol",
        "status",
        "updated_at",
    )

    op.create_table(
        "shadow_runs",
        sa.Column("shadow_run_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("trigger", sa.String(length=40), nullable=False),
        sa.Column("deployment_count", sa.Integer(), nullable=False),
        sa.Column("bars_processed", sa.Integer(), nullable=False),
        sa.Column("events_created", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("error_code", sa.String(length=120), nullable=True),
        sa.PrimaryKeyConstraint("shadow_run_id", name="pk_shadow_runs"),
    )
    _index("shadow_runs", "status", "finished_at")

    op.create_table(
        "shadow_events",
        sa.Column("shadow_event_id", sa.String(length=36), nullable=False),
        sa.Column("shadow_deployment_id", sa.String(length=36), nullable=False),
        sa.Column("shadow_run_id", sa.String(length=36), nullable=True),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=40), nullable=False),
        sa.Column("event_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("symbol", sa.String(length=24), nullable=False),
        sa.Column("bar_id", sa.String(length=36), nullable=True),
        sa.Column("cash_balance", sa.Numeric(24, 8), nullable=False),
        sa.Column("position_quantity", sa.Numeric(24, 10), nullable=False),
        sa.Column("price", sa.Numeric(20, 8), nullable=True),
        sa.Column("realized_pnl_delta", sa.Numeric(24, 8), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["shadow_deployment_id"],
            ["shadow_deployments.shadow_deployment_id"],
            name="fk_shadow_events_shadow_deployment_id_shadow_deployments",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("shadow_event_id", name="pk_shadow_events"),
        sa.UniqueConstraint(
            "shadow_deployment_id",
            "sequence",
            name="uq_shadow_events_deployment_sequence",
        ),
        sa.UniqueConstraint(
            "shadow_deployment_id",
            "bar_id",
            "event_type",
            name="uq_shadow_events_bar_type",
        ),
    )
    _index(
        "shadow_events",
        "shadow_deployment_id",
        "shadow_run_id",
        "event_type",
        "event_time",
        "symbol",
        "bar_id",
    )

    op.create_table(
        "runtime_controls",
        sa.Column("control_key", sa.String(length=80), nullable=False),
        sa.Column("state_json", sa.JSON(), nullable=False),
        sa.Column("updated_by", sa.String(length=80), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("control_key", name="pk_runtime_controls"),
    )

    op.create_table(
        "code_change_sessions",
        sa.Column("code_change_session_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("request", sa.Text(), nullable=False),
        sa.Column("scope_json", sa.JSON(), nullable=False),
        sa.Column("base_git_sha", sa.String(length=64), nullable=False),
        sa.Column("branch_name", sa.String(length=160), nullable=False),
        sa.Column("worktree_path", sa.Text(), nullable=True),
        sa.Column("diff_sha256", sa.String(length=64), nullable=True),
        sa.Column("diff_text", sa.Text(), nullable=True),
        sa.Column("tests_json", sa.JSON(), nullable=False),
        sa.Column("proposed_commit_subject", sa.String(length=240), nullable=True),
        sa.Column("committed_git_sha", sa.String(length=64), nullable=True),
        sa.Column("requested_by", sa.String(length=80), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint(
            "code_change_session_id", name="pk_code_change_sessions"
        ),
    )
    _index("code_change_sessions", "status", "updated_at")


def downgrade() -> None:
    for table in (
        "code_change_sessions",
        "shadow_events",
        "shadow_runs",
        "shadow_deployments",
        "strategy_adoptions",
        "steward_messages",
        "steward_conversations",
        "admin_action_requests",
        "thread_posts",
        "object_threads",
        "system_list_revisions",
        "system_lists",
        "admin_auth_events",
        "admin_sessions",
    ):
        op.drop_table(table)
    op.drop_table("runtime_controls")
