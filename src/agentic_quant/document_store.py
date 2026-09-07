from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict
from sqlalchemy import Engine, and_, func, insert, or_, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from agentic_quant.database import (
    catalyst_documents,
    catalysts,
    corporate_facts,
    document_entities,
    document_symbols,
    entities,
    entity_symbols,
    source_document_versions,
    source_documents,
)
from agentic_quant.domain import CorporateFact, SourceDocument, SourceTier
from agentic_quant.ids import uuid7


class DocumentWriteResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    document_id: str
    version_id: str | None
    document_inserted: bool
    version_inserted: bool


class CatalystWriteResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    catalyst_id: str
    inserted: bool
    document_linked: bool
    match_method: str
    match_score: Decimal


_TOKEN_PATTERN = re.compile(r"[a-z0-9]+")
_STOP_WORDS = {
    "a",
    "an",
    "and",
    "announces",
    "announced",
    "as",
    "at",
    "by",
    "company",
    "for",
    "from",
    "in",
    "inc",
    "its",
    "of",
    "on",
    "reports",
    "says",
    "the",
    "to",
    "with",
}


def _content_hash(document: SourceDocument) -> str:
    value = json.dumps(
        {
            "title": document.title.strip(),
            "summary": (document.summary or "").strip(),
            "body_text": (document.body_text or "").strip(),
            "published_at": document.published_at.isoformat(),
            "updated_at": (
                document.updated_at.isoformat() if document.updated_at else None
            ),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(value).hexdigest()


def _headline_tokens(value: str) -> frozenset[str]:
    return frozenset(
        token for token in _TOKEN_PATTERN.findall(value.casefold()) if token not in _STOP_WORDS
    )


def _similarity(left: str, right: str) -> Decimal:
    left_tokens = _headline_tokens(left)
    right_tokens = _headline_tokens(right)
    if not left_tokens or not right_tokens:
        return Decimal("0")
    return Decimal(len(left_tokens & right_tokens)) / Decimal(
        len(left_tokens | right_tokens)
    )


def classify_catalyst(document: SourceDocument) -> str:
    text = f"{document.title} {document.summary or ''}".casefold()
    categories = (
        ("earnings", ("earnings", "revenue", "guidance", "quarterly results")),
        ("contract", ("contract", "agreement", "award", "partnership")),
        ("regulatory", ("fda", "regulatory", "approval", "investigation")),
        ("litigation", ("lawsuit", "litigation", "settlement", "court")),
        ("capital", ("offering", "buyback", "dividend", "financing")),
        ("leadership", ("appoints", "resigns", "chief executive", "ceo")),
    )
    for category, keywords in categories:
        if any(keyword in text for keyword in keywords):
            return category
    if document.source_kind == "sec_filing":
        return "regulatory_filing"
    return "corporate_event"


class DocumentStore:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def upsert_document(
        self,
        document: SourceDocument,
        raw_object_id: str,
    ) -> DocumentWriteResult:
        identity = and_(
            source_documents.c.provider == document.provider,
            source_documents.c.provider_document_id == document.provider_document_id,
        )
        content_sha256 = _content_hash(document)
        document_inserted = False
        version_inserted = False
        version_id: str | None = None
        with self.engine.begin() as connection:
            document_id = connection.execute(
                select(source_documents.c.document_id).where(identity)
            ).scalar_one_or_none()
            if document_id is None:
                document_id = document.document_id
                connection.execute(
                    insert(source_documents).values(
                        document_id=document_id,
                        provider=document.provider,
                        provider_document_id=document.provider_document_id,
                        canonical_url=document.canonical_url,
                        source_kind=document.source_kind,
                        source_tier=document.source_tier.value,
                        publisher=document.publisher,
                        published_at=document.published_at,
                        first_seen_at=document.ingested_at,
                        last_seen_at=document.ingested_at,
                    )
                )
                document_inserted = True
            else:
                connection.execute(
                    update(source_documents)
                    .where(source_documents.c.document_id == document_id)
                    .values(
                        canonical_url=document.canonical_url,
                        publisher=document.publisher,
                        last_seen_at=document.ingested_at,
                    )
                )

            existing_version = connection.execute(
                select(source_document_versions.c.version_id).where(
                    and_(
                        source_document_versions.c.document_id == document_id,
                        source_document_versions.c.content_sha256 == content_sha256,
                    )
                )
            ).scalar_one_or_none()
            if existing_version is None:
                version_id = uuid7()
                connection.execute(
                    insert(source_document_versions).values(
                        version_id=version_id,
                        document_id=document_id,
                        content_sha256=content_sha256,
                        title=document.title,
                        summary=document.summary,
                        body_text=document.body_text,
                        corrected_at=(
                            document.updated_at
                            if document.updated_at
                            and document.updated_at > document.published_at
                            else None
                        ),
                        ingested_at=document.ingested_at,
                        raw_object_id=raw_object_id,
                    )
                )
                version_inserted = True

            for symbol in sorted(set(document.symbols)):
                entity_id = self._upsert_entity(
                    connection,
                    symbol=symbol,
                    canonical_name=document.issuer_name,
                    cik=document.cik,
                    observed_at=document.ingested_at,
                )
                self._insert_ignore(
                    connection,
                    document_entities,
                    {"document_id": document_id, "entity_id": entity_id},
                    ["document_id", "entity_id"],
                )
                self._insert_ignore(
                    connection,
                    document_symbols,
                    {"document_id": document_id, "symbol": symbol.upper()},
                    ["document_id", "symbol"],
                )

        return DocumentWriteResult(
            document_id=str(document_id),
            version_id=version_id,
            document_inserted=document_inserted,
            version_inserted=version_inserted,
        )

    def fact_id_for_fingerprint(self, fact_fingerprint: str) -> str:
        with self.engine.connect() as connection:
            value = connection.execute(
                select(corporate_facts.c.fact_id).where(
                    corporate_facts.c.fact_fingerprint == fact_fingerprint
                )
            ).scalar_one_or_none()
        if value is None:
            raise ValueError("Corporate fact was not persisted")
        return str(value)

    def resolve_catalyst(
        self,
        document: SourceDocument,
        *,
        document_id: str,
        similarity_threshold: Decimal = Decimal("0.58"),
    ) -> CatalystWriteResult:
        symbol = document.symbols[0].upper()
        catalyst_type = classify_catalyst(document)
        window_start = document.published_at - timedelta(hours=36)
        window_end = document.published_at + timedelta(hours=36)
        with self.engine.begin() as connection:
            existing_link = connection.execute(
                select(catalyst_documents.c.catalyst_id).where(
                    catalyst_documents.c.document_id == document_id
                )
            ).scalar_one_or_none()
            if existing_link is not None:
                return CatalystWriteResult(
                    catalyst_id=str(existing_link),
                    inserted=False,
                    document_linked=False,
                    match_method="existing_document",
                    match_score=Decimal("1"),
                )

            candidates = connection.execute(
                select(
                    catalysts.c.catalyst_id,
                    catalysts.c.headline,
                ).where(
                    and_(
                        catalysts.c.primary_symbol == symbol,
                        catalysts.c.catalyst_type == catalyst_type,
                        catalysts.c.event_time >= window_start,
                        catalysts.c.event_time <= window_end,
                    )
                )
            ).all()
            best_id: str | None = None
            best_score = Decimal("0")
            for candidate in candidates:
                score = _similarity(document.title, str(candidate.headline))
                if score > best_score:
                    best_id = str(candidate.catalyst_id)
                    best_score = score

            inserted = best_id is None or best_score < similarity_threshold
            if inserted:
                canonical_material = (
                    f"{symbol}|{catalyst_type}|{document.published_at.date().isoformat()}|"
                    f"{' '.join(sorted(_headline_tokens(document.title)))}"
                )
                canonical_key = hashlib.sha256(canonical_material.encode()).hexdigest()
                best_id = uuid7()
                connection.execute(
                    insert(catalysts).values(
                        catalyst_id=best_id,
                        canonical_key=canonical_key,
                        catalyst_type=catalyst_type,
                        primary_symbol=symbol,
                        event_time=document.published_at,
                        available_from=document.ingested_at,
                        last_updated_at=document.ingested_at,
                        headline=document.title,
                        primary_source_document_id=(
                            document_id
                            if document.source_tier == SourceTier.PRIMARY
                            else None
                        ),
                        status="ACTIVE",
                        source_count=1,
                    )
                )
                match_method = "new_catalyst"
                best_score = Decimal("1")
            else:
                values: dict[str, Any] = {
                    "last_updated_at": document.ingested_at,
                    "source_count": catalysts.c.source_count + 1,
                }
                if document.source_tier == SourceTier.PRIMARY:
                    values["primary_source_document_id"] = document_id
                connection.execute(
                    update(catalysts)
                    .where(catalysts.c.catalyst_id == best_id)
                    .values(**values)
                )
                match_method = "headline_similarity"

            connection.execute(
                insert(catalyst_documents).values(
                    catalyst_id=best_id,
                    document_id=document_id,
                    match_method=match_method,
                    match_score=best_score,
                    linked_at=document.ingested_at,
                )
            )
        return CatalystWriteResult(
            catalyst_id=best_id,
            inserted=inserted,
            document_linked=True,
            match_method=match_method,
            match_score=best_score,
        )

    def insert_facts(self, facts: tuple[CorporateFact, ...], raw_object_id: str) -> tuple[str, ...]:
        if not facts:
            return ()
        with self.engine.begin() as connection:
            records: list[dict[str, Any]] = []
            for fact in facts:
                entity_id = self._upsert_entity(
                    connection,
                    symbol=fact.symbol,
                    canonical_name=fact.issuer_name,
                    cik=fact.cik,
                    observed_at=fact.ingested_at,
                )
                records.append(
                    fact.model_copy(
                        update={"raw_object_id": raw_object_id}
                    ).model_dump()
                    | {"entity_id": entity_id}
                )
            statement = self._insert_statement(
                corporate_facts,
                records,
                ["fact_fingerprint"],
            ).returning(corporate_facts.c.fact_id)
            return tuple(str(value) for value in connection.execute(statement).scalars().all())

    def search_documents(
        self,
        *,
        query: str,
        symbol: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        latest_version = (
            select(
                source_document_versions.c.document_id,
                func.max(source_document_versions.c.ingested_at).label("latest_ingested_at"),
            )
            .group_by(source_document_versions.c.document_id)
            .subquery()
        )
        statement = (
            select(
                source_documents,
                source_document_versions.c.version_id,
                source_document_versions.c.title,
                source_document_versions.c.summary,
                source_document_versions.c.corrected_at,
                source_document_versions.c.ingested_at,
            )
            .join(
                latest_version,
                latest_version.c.document_id == source_documents.c.document_id,
            )
            .join(
                source_document_versions,
                and_(
                    source_document_versions.c.document_id
                    == latest_version.c.document_id,
                    source_document_versions.c.ingested_at
                    == latest_version.c.latest_ingested_at,
                ),
            )
        )
        if query.strip():
            pattern = f"%{query.strip().casefold()}%"
            statement = statement.where(
                or_(
                    func.lower(source_document_versions.c.title).like(pattern),
                    func.lower(func.coalesce(source_document_versions.c.summary, "")).like(
                        pattern
                    ),
                    func.lower(func.coalesce(source_document_versions.c.body_text, "")).like(
                        pattern
                    ),
                )
            )
        if symbol:
            statement = (
                statement.join(
                    document_symbols,
                    document_symbols.c.document_id == source_documents.c.document_id,
                ).where(document_symbols.c.symbol == symbol.upper())
            )
        statement = statement.order_by(source_documents.c.published_at.desc()).limit(limit)
        with self.engine.connect() as connection:
            return [
                self._normalize_row_times(
                    dict(row._mapping),
                    (
                        "published_at",
                        "first_seen_at",
                        "last_seen_at",
                        "corrected_at",
                        "ingested_at",
                    ),
                )
                for row in connection.execute(statement)
            ]

    def latest_document_published_at(
        self,
        *,
        symbol: str,
        provider: str,
    ) -> datetime | None:
        """Return the newest provider publication time stored for one symbol."""
        statement = (
            select(func.max(source_documents.c.published_at))
            .join(
                document_symbols,
                document_symbols.c.document_id == source_documents.c.document_id,
            )
            .where(document_symbols.c.symbol == symbol.upper())
            .where(source_documents.c.provider == provider)
        )
        with self.engine.connect() as connection:
            value = connection.execute(statement).scalar_one()
        if not isinstance(value, datetime):
            return None
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)

    def research_documents_as_of(
        self,
        *,
        symbol: str,
        as_of: datetime,
        since: datetime,
        limit: int = 12,
    ) -> list[dict[str, Any]]:
        """Return the newest version that was actually available by the research cutoff."""
        if as_of.tzinfo is None or since.tzinfo is None:
            raise ValueError("Research document bounds must be timezone-aware")
        if since >= as_of:
            raise ValueError("Research document start must be before as_of")
        latest_available = (
            select(
                source_document_versions.c.document_id,
                func.max(source_document_versions.c.ingested_at).label(
                    "latest_ingested_at"
                ),
            )
            .where(source_document_versions.c.ingested_at <= as_of)
            .group_by(source_document_versions.c.document_id)
            .subquery()
        )
        statement = (
            select(
                source_documents.c.document_id,
                source_documents.c.provider,
                source_documents.c.canonical_url,
                source_documents.c.source_kind,
                source_documents.c.source_tier,
                source_documents.c.publisher,
                source_documents.c.published_at,
                source_document_versions.c.version_id,
                source_document_versions.c.content_sha256,
                source_document_versions.c.title,
                source_document_versions.c.summary,
                source_document_versions.c.body_text,
                source_document_versions.c.ingested_at,
            )
            .join(
                document_symbols,
                document_symbols.c.document_id == source_documents.c.document_id,
            )
            .join(
                latest_available,
                latest_available.c.document_id == source_documents.c.document_id,
            )
            .join(
                source_document_versions,
                and_(
                    source_document_versions.c.document_id
                    == latest_available.c.document_id,
                    source_document_versions.c.ingested_at
                    == latest_available.c.latest_ingested_at,
                ),
            )
            .where(
                and_(
                    document_symbols.c.symbol == symbol.upper(),
                    source_documents.c.published_at >= since,
                    source_documents.c.published_at <= as_of,
                )
            )
            .order_by(
                source_documents.c.published_at.desc(),
                source_documents.c.document_id.asc(),
            )
            .limit(limit)
        )
        with self.engine.connect() as connection:
            return [
                self._normalize_row_times(
                    dict(row._mapping),
                    ("published_at", "ingested_at"),
                )
                for row in connection.execute(statement)
            ]

    def recent_catalysts(self, *, limit: int = 50) -> list[dict[str, Any]]:
        statement = catalysts.select().order_by(catalysts.c.event_time.desc()).limit(limit)
        with self.engine.connect() as connection:
            return [
                self._normalize_row_times(
                    dict(row._mapping),
                    ("event_time", "available_from", "last_updated_at"),
                )
                for row in connection.execute(statement)
            ]

    def health_summary(self) -> dict[str, int]:
        with self.engine.connect() as connection:
            return {
                "source_documents": int(
                    connection.execute(
                        select(func.count()).select_from(source_documents)
                    ).scalar_one()
                ),
                "document_versions": int(
                    connection.execute(
                        select(func.count()).select_from(source_document_versions)
                    ).scalar_one()
                ),
                "entities": int(
                    connection.execute(select(func.count()).select_from(entities)).scalar_one()
                ),
                "catalysts": int(
                    connection.execute(select(func.count()).select_from(catalysts)).scalar_one()
                ),
                "corporate_facts": int(
                    connection.execute(
                        select(func.count()).select_from(corporate_facts)
                    ).scalar_one()
                ),
            }

    def _upsert_entity(
        self,
        connection: Any,
        *,
        symbol: str,
        canonical_name: str | None,
        cik: str | None,
        observed_at: Any,
    ) -> str:
        normalized_symbol = symbol.upper()
        identity = entities.c.primary_symbol == normalized_symbol
        if cik:
            identity = or_(identity, entities.c.cik == cik)
        entity_id = connection.execute(
            select(entities.c.entity_id).where(identity)
        ).scalar_one_or_none()
        if entity_id is None:
            entity_id = uuid7()
            connection.execute(
                insert(entities).values(
                    entity_id=entity_id,
                    primary_symbol=normalized_symbol,
                    canonical_name=canonical_name,
                    cik=cik,
                    created_at=observed_at,
                    updated_at=observed_at,
                )
            )
        else:
            values: dict[str, Any] = {"updated_at": observed_at}
            if canonical_name:
                values["canonical_name"] = canonical_name
            if cik:
                values["cik"] = cik
            connection.execute(
                update(entities).where(entities.c.entity_id == entity_id).values(**values)
            )
        self._insert_ignore(
            connection,
            entity_symbols,
            {"entity_id": entity_id, "symbol": normalized_symbol},
            ["entity_id", "symbol"],
        )
        return str(entity_id)

    def _insert_statement(
        self,
        table: Any,
        values: Any,
        identity_columns: list[str],
    ) -> Any:
        if self.engine.dialect.name == "postgresql":
            return postgresql_insert(table).values(values).on_conflict_do_nothing(
                index_elements=identity_columns
            )
        if self.engine.dialect.name == "sqlite":
            return sqlite_insert(table).values(values).on_conflict_do_nothing(
                index_elements=identity_columns
            )
        raise RuntimeError(f"Unsupported SQL dialect: {self.engine.dialect.name}")

    @staticmethod
    def _normalize_row_times(
        row: dict[str, Any],
        fields: tuple[str, ...],
    ) -> dict[str, Any]:
        for field in fields:
            value = row.get(field)
            if isinstance(value, datetime) and value.tzinfo is None:
                row[field] = value.replace(tzinfo=UTC)
        return row

    def _insert_ignore(
        self,
        connection: Any,
        table: Any,
        values: dict[str, Any],
        identity_columns: list[str],
    ) -> None:
        connection.execute(
            self._insert_statement(table, values, identity_columns)
        )
