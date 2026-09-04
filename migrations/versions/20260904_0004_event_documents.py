"""Add Phase 2 event, document, entity, catalyst, and fundamentals tables."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260904_0004"
down_revision: str | None = "20260904_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "entities",
        sa.Column("entity_id", sa.String(length=36), nullable=False),
        sa.Column("primary_symbol", sa.String(length=24), nullable=False),
        sa.Column("canonical_name", sa.Text(), nullable=True),
        sa.Column("cik", sa.String(length=10), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("entity_id", name="pk_entities"),
        sa.UniqueConstraint("primary_symbol", name="uq_entities_primary_symbol"),
        sa.UniqueConstraint("cik", name="uq_entities_cik"),
    )
    op.create_index("ix_entities_primary_symbol", "entities", ["primary_symbol"])
    op.create_index("ix_entities_cik", "entities", ["cik"])
    op.create_table(
        "source_documents",
        sa.Column("document_id", sa.String(length=36), nullable=False),
        sa.Column("provider", sa.String(length=40), nullable=False),
        sa.Column("provider_document_id", sa.String(length=160), nullable=False),
        sa.Column("canonical_url", sa.Text(), nullable=False),
        sa.Column("source_kind", sa.String(length=40), nullable=False),
        sa.Column("source_tier", sa.String(length=16), nullable=False),
        sa.Column("publisher", sa.Text(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("document_id", name="pk_source_documents"),
        sa.UniqueConstraint(
            "provider",
            "provider_document_id",
            name="uq_source_documents_provider_identity",
        ),
    )
    op.create_index(
        "ix_source_documents_source_kind", "source_documents", ["source_kind"]
    )
    op.create_index(
        "ix_source_documents_source_tier", "source_documents", ["source_tier"]
    )
    op.create_index(
        "ix_source_documents_published_at", "source_documents", ["published_at"]
    )
    op.create_table(
        "source_document_versions",
        sa.Column("version_id", sa.String(length=36), nullable=False),
        sa.Column("document_id", sa.String(length=36), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("body_text", sa.Text(), nullable=True),
        sa.Column("corrected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("raw_object_id", sa.String(length=36), nullable=False),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["source_documents.document_id"],
            name="fk_source_document_versions_document_id_source_documents",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("version_id", name="pk_source_document_versions"),
        sa.UniqueConstraint(
            "document_id",
            "content_sha256",
            name="uq_source_document_versions_content",
        ),
    )
    op.create_index(
        "ix_source_document_versions_document_id",
        "source_document_versions",
        ["document_id"],
    )
    op.create_index(
        "ix_source_document_versions_ingested_at",
        "source_document_versions",
        ["ingested_at"],
    )
    op.create_table(
        "document_entities",
        sa.Column("document_id", sa.String(length=36), nullable=False),
        sa.Column("entity_id", sa.String(length=36), nullable=False),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["source_documents.document_id"],
            name="fk_document_entities_document_id_source_documents",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["entity_id"],
            ["entities.entity_id"],
            name="fk_document_entities_entity_id_entities",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "document_id", "entity_id", name="pk_document_entities"
        ),
    )
    op.create_table(
        "catalysts",
        sa.Column("catalyst_id", sa.String(length=36), nullable=False),
        sa.Column("canonical_key", sa.String(length=64), nullable=False),
        sa.Column("catalyst_type", sa.String(length=40), nullable=False),
        sa.Column("primary_symbol", sa.String(length=24), nullable=False),
        sa.Column("event_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("available_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("headline", sa.Text(), nullable=False),
        sa.Column("primary_source_document_id", sa.String(length=36), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("source_count", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("catalyst_id", name="pk_catalysts"),
        sa.UniqueConstraint("canonical_key", name="uq_catalysts_canonical_key"),
    )
    op.create_index("ix_catalysts_catalyst_type", "catalysts", ["catalyst_type"])
    op.create_index("ix_catalysts_primary_symbol", "catalysts", ["primary_symbol"])
    op.create_index("ix_catalysts_event_time", "catalysts", ["event_time"])
    op.create_table(
        "catalyst_documents",
        sa.Column("catalyst_id", sa.String(length=36), nullable=False),
        sa.Column("document_id", sa.String(length=36), nullable=False),
        sa.Column("match_method", sa.String(length=40), nullable=False),
        sa.Column("match_score", sa.Numeric(precision=8, scale=6), nullable=False),
        sa.Column("linked_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["catalyst_id"],
            ["catalysts.catalyst_id"],
            name="fk_catalyst_documents_catalyst_id_catalysts",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["source_documents.document_id"],
            name="fk_catalyst_documents_document_id_source_documents",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "catalyst_id", "document_id", name="pk_catalyst_documents"
        ),
    )
    op.create_table(
        "corporate_facts",
        sa.Column("fact_id", sa.String(length=36), nullable=False),
        sa.Column("fact_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("entity_id", sa.String(length=36), nullable=True),
        sa.Column("symbol", sa.String(length=24), nullable=False),
        sa.Column("cik", sa.String(length=10), nullable=False),
        sa.Column("issuer_name", sa.Text(), nullable=False),
        sa.Column("taxonomy", sa.String(length=80), nullable=False),
        sa.Column("tag", sa.String(length=160), nullable=False),
        sa.Column("unit", sa.String(length=40), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("filed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fiscal_year", sa.Integer(), nullable=True),
        sa.Column("fiscal_period", sa.String(length=20), nullable=True),
        sa.Column("form", sa.String(length=20), nullable=False),
        sa.Column("accession_number", sa.String(length=32), nullable=True),
        sa.Column("numeric_value", sa.Numeric(precision=30, scale=10), nullable=True),
        sa.Column("value_text", sa.Text(), nullable=False),
        sa.Column("available_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("raw_object_id", sa.String(length=36), nullable=False),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("fact_id", name="pk_corporate_facts"),
        sa.UniqueConstraint("fact_fingerprint", name="uq_corporate_facts_fact_fingerprint"),
    )
    op.create_index("ix_corporate_facts_entity_id", "corporate_facts", ["entity_id"])
    op.create_index("ix_corporate_facts_symbol", "corporate_facts", ["symbol"])
    op.create_index("ix_corporate_facts_cik", "corporate_facts", ["cik"])
    op.create_index("ix_corporate_facts_tag", "corporate_facts", ["tag"])
    op.create_index("ix_corporate_facts_filed_at", "corporate_facts", ["filed_at"])


def downgrade() -> None:
    op.drop_index("ix_corporate_facts_filed_at", table_name="corporate_facts")
    op.drop_index("ix_corporate_facts_tag", table_name="corporate_facts")
    op.drop_index("ix_corporate_facts_cik", table_name="corporate_facts")
    op.drop_index("ix_corporate_facts_symbol", table_name="corporate_facts")
    op.drop_index("ix_corporate_facts_entity_id", table_name="corporate_facts")
    op.drop_table("corporate_facts")
    op.drop_table("catalyst_documents")
    op.drop_index("ix_catalysts_event_time", table_name="catalysts")
    op.drop_index("ix_catalysts_primary_symbol", table_name="catalysts")
    op.drop_index("ix_catalysts_catalyst_type", table_name="catalysts")
    op.drop_table("catalysts")
    op.drop_table("document_entities")
    op.drop_index(
        "ix_source_document_versions_ingested_at",
        table_name="source_document_versions",
    )
    op.drop_index(
        "ix_source_document_versions_document_id",
        table_name="source_document_versions",
    )
    op.drop_table("source_document_versions")
    op.drop_index("ix_source_documents_published_at", table_name="source_documents")
    op.drop_index("ix_source_documents_source_tier", table_name="source_documents")
    op.drop_index("ix_source_documents_source_kind", table_name="source_documents")
    op.drop_table("source_documents")
    op.drop_index("ix_entities_cik", table_name="entities")
    op.drop_index("ix_entities_primary_symbol", table_name="entities")
    op.drop_table("entities")
