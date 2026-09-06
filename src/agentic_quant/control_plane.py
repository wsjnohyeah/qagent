from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import Engine, and_, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from agentic_quant.database import (
    admin_action_requests,
    backtest_trades,
    catalysts,
    code_change_sessions,
    data_quality_reports,
    experiment_runs,
    feature_snapshots,
    ingestion_runs,
    ledger_events,
    llm_budget_reservations,
    llm_invocations,
    market_bars,
    market_quotes,
    market_trades,
    ml_forecasts,
    ml_models,
    ml_training_runs,
    object_threads,
    raw_objects,
    research_analyses,
    shadow_deployments,
    shadow_events,
    source_document_versions,
    source_documents,
    corporate_facts,
    strategy_adoptions,
    strategy_generation_attempts,
    strategy_specs,
    steward_conversations,
    steward_messages,
    system_list_revisions,
    system_lists,
    thread_posts,
    validation_reports,
    workflow_jobs,
)
from agentic_quant.domain import EventEnvelope
from agentic_quant.ids import uuid7
from agentic_quant.ledger import EventLedger


DEFAULT_LISTS: tuple[dict[str, Any], ...] = (
    {
        "slug": "trading-universe",
        "list_type": "TRADING_UNIVERSE",
        "name": "Trading Universe",
        "description": "Symbols eligible for bounded scanning and research.",
        "mode": "GOVERNED",
        "members": ("AAPL", "SPY", "QQQ", "IWM"),
    },
    {
        "slug": "focus-watchlist",
        "list_type": "FOCUS_WATCHLIST",
        "name": "Focus Watchlist",
        "description": "Administrator-selected symbols receiving additional attention.",
        "mode": "MANUAL",
        "members": (),
    },
    {
        "slug": "candidate-list",
        "list_type": "CANDIDATE_LIST",
        "name": "Candidate List",
        "description": "Dynamic shortlist produced by deterministic scanners.",
        "mode": "DYNAMIC",
        "members": (),
    },
    {
        "slug": "shadow-active",
        "list_type": "SHADOW_ACTIVE",
        "name": "Shadow Active",
        "description": "Symbols with an active shadow deployment.",
        "mode": "SYSTEM",
        "members": (),
    },
    {
        "slug": "benchmarks",
        "list_type": "BENCHMARK",
        "name": "Benchmarks",
        "description": "Context instruments; inclusion does not authorize trading.",
        "mode": "GOVERNED",
        "members": ("SPY", "QQQ", "IWM"),
    },
    {
        "slug": "restricted",
        "list_type": "RESTRICTED",
        "name": "Restricted Securities",
        "description": "Symbols or policy classes that fail closed before strategy use.",
        "mode": "POLICY",
        "members": ("META", "*PENNY_OR_ILLIQUID*"),
    },
)


def _utc(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


def _normalize_symbols(values: list[str] | tuple[str, ...]) -> list[str]:
    symbols = sorted({value.strip().upper() for value in values if value.strip()})
    if any(not re.fullmatch(r"[A-Z*][A-Z0-9.*_-]{0,63}", value) for value in symbols):
        raise ValueError("List members must be normalized symbol or policy identifiers")
    if len(symbols) > 5_000:
        raise ValueError("A list revision is limited to 5000 members")
    return symbols


class SystemObjectStore:
    def __init__(self, engine: Engine, ledger: EventLedger | None = None) -> None:
        self.engine = engine
        self.ledger = ledger

    def ensure_defaults(self) -> None:
        for item in DEFAULT_LISTS:
            if self.get_list(str(item["slug"])) is None:
                self.create_list(
                    slug=str(item["slug"]),
                    list_type=str(item["list_type"]),
                    name=str(item["name"]),
                    description=str(item["description"]),
                    mode=str(item["mode"]),
                    members=list(item["members"]),
                    reason="Phase 6 default list seed",
                    created_by="system-bootstrap",
                )

    def create_list(
        self,
        *,
        slug: str,
        list_type: str,
        name: str,
        description: str,
        mode: str,
        members: list[str],
        reason: str,
        created_by: str,
    ) -> dict[str, Any]:
        normalized_slug = slug.strip().lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,79}", normalized_slug):
            raise ValueError("List slug must use lowercase letters, numbers, and hyphens")
        now = datetime.now(UTC)
        list_id = uuid7()
        revision_id = uuid7()
        normalized_members = _normalize_symbols(members)
        with self.engine.begin() as connection:
            connection.execute(
                insert(system_lists).values(
                    list_id=list_id,
                    slug=normalized_slug,
                    list_type=list_type,
                    name=name.strip(),
                    description=description.strip(),
                    mode=mode,
                    status="ACTIVE",
                    current_revision=1,
                    created_at=now,
                    updated_at=now,
                )
            )
            connection.execute(
                insert(system_list_revisions).values(
                    list_revision_id=revision_id,
                    list_id=list_id,
                    revision_number=1,
                    members_json=normalized_members,
                    reason=reason,
                    created_by=created_by,
                    created_at=now,
                )
            )
        self.ensure_thread("list", list_id, name)
        self._emit(
            "system.list.created.v1",
            list_id,
            {"slug": normalized_slug, "members": normalized_members, "reason": reason},
        )
        value = self.get_list(normalized_slug)
        assert value is not None
        return value

    def lists(self) -> list[dict[str, Any]]:
        statement = system_lists.select().order_by(system_lists.c.name.asc())
        with self.engine.connect() as connection:
            rows = connection.execute(statement).all()
            values = []
            for row in rows:
                item = dict(row._mapping)
                revision = connection.execute(
                    select(system_list_revisions).where(
                        (system_list_revisions.c.list_id == item["list_id"])
                        & (
                            system_list_revisions.c.revision_number
                            == item["current_revision"]
                        )
                    )
                ).one()
                item["members"] = list(revision.members_json)
                item["member_count"] = len(item["members"])
                item["revision_reason"] = str(revision.reason)
                item["revision_created_by"] = str(revision.created_by)
                values.append(item)
            return values

    def get_list(self, slug_or_id: str) -> dict[str, Any] | None:
        statement = select(system_lists).where(
            (system_lists.c.slug == slug_or_id.lower())
            | (system_lists.c.list_id == slug_or_id)
        )
        with self.engine.connect() as connection:
            row = connection.execute(statement).one_or_none()
            if row is None:
                return None
            item = dict(row._mapping)
            revision = connection.execute(
                select(system_list_revisions).where(
                    (system_list_revisions.c.list_id == item["list_id"])
                    & (
                        system_list_revisions.c.revision_number
                        == item["current_revision"]
                    )
                )
            ).one()
            history = connection.execute(
                select(system_list_revisions)
                .where(system_list_revisions.c.list_id == item["list_id"])
                .order_by(system_list_revisions.c.revision_number.desc())
                .limit(20)
            ).all()
        item["members"] = list(revision.members_json)
        item["member_count"] = len(item["members"])
        item["revision_reason"] = str(revision.reason)
        item["revision_created_by"] = str(revision.created_by)
        item["history"] = [dict(value._mapping) for value in history]
        return item

    def replace_list_members(
        self,
        *,
        slug_or_id: str,
        members: list[str],
        reason: str,
        created_by: str,
    ) -> dict[str, Any]:
        current = self.get_list(slug_or_id)
        if current is None:
            raise ValueError("System list not found")
        if current["mode"] == "POLICY":
            raise ValueError("Policy lists are changed through reviewed policy configuration")
        normalized = _normalize_symbols(members)
        if normalized == current["members"]:
            return current
        now = datetime.now(UTC)
        revision_number = int(current["current_revision"]) + 1
        with self.engine.begin() as connection:
            connection.execute(
                insert(system_list_revisions).values(
                    list_revision_id=uuid7(),
                    list_id=current["list_id"],
                    revision_number=revision_number,
                    members_json=normalized,
                    reason=reason,
                    created_by=created_by,
                    created_at=now,
                )
            )
            connection.execute(
                update(system_lists)
                .where(system_lists.c.list_id == current["list_id"])
                .values(current_revision=revision_number, updated_at=now)
            )
        self.post(
            object_type="list",
            object_id=str(current["list_id"]),
            title=str(current["name"]),
            author_kind="system",
            author_name="control-plane",
            body=(
                f"Revision {revision_number}: {len(current['members'])} → "
                f"{len(normalized)} members. {reason}"
            ),
            metadata={"before": current["members"], "after": normalized},
        )
        self._emit(
            "system.list.revised.v1",
            str(current["list_id"]),
            {
                "slug": current["slug"],
                "revision_number": revision_number,
                "members": normalized,
                "reason": reason,
                "created_by": created_by,
            },
        )
        updated = self.get_list(str(current["list_id"]))
        assert updated is not None
        return updated

    def ensure_thread(self, object_type: str, object_id: str, title: str) -> str:
        identity = and_(
            object_threads.c.object_type == object_type,
            object_threads.c.object_id == object_id,
        )
        with self.engine.begin() as connection:
            existing = connection.execute(
                select(object_threads.c.thread_id).where(identity)
            ).scalar_one_or_none()
            if existing is not None:
                return str(existing)
            values = {
                "thread_id": uuid7(),
                "object_type": object_type,
                "object_id": object_id,
                "title": title[:240],
                "created_at": datetime.now(UTC),
                "updated_at": datetime.now(UTC),
            }
            if self.engine.dialect.name == "postgresql":
                statement = (
                    postgresql_insert(object_threads)
                    .values(**values)
                    .on_conflict_do_nothing(
                        index_elements=["object_type", "object_id"]
                    )
                    .returning(object_threads.c.thread_id)
                )
            elif self.engine.dialect.name == "sqlite":
                statement = (
                    sqlite_insert(object_threads)
                    .values(**values)
                    .on_conflict_do_nothing(
                        index_elements=["object_type", "object_id"]
                    )
                    .returning(object_threads.c.thread_id)
                )
            else:
                raise RuntimeError(f"Unsupported SQL dialect: {self.engine.dialect.name}")
            inserted = connection.execute(statement).scalar_one_or_none()
            if inserted is not None:
                return str(inserted)
            return str(
                connection.execute(
                    select(object_threads.c.thread_id).where(identity)
                ).scalar_one()
            )

    def post(
        self,
        *,
        object_type: str,
        object_id: str,
        title: str,
        author_kind: str,
        author_name: str,
        body: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        thread_id = self.ensure_thread(object_type, object_id, title)
        now = datetime.now(UTC)
        values = {
            "post_id": uuid7(),
            "thread_id": thread_id,
            "author_kind": author_kind,
            "author_name": author_name[:80],
            "body": body,
            "metadata_json": metadata or {},
            "created_at": now,
        }
        with self.engine.begin() as connection:
            connection.execute(insert(thread_posts).values(**values))
            connection.execute(
                update(object_threads)
                .where(object_threads.c.thread_id == thread_id)
                .values(updated_at=now)
            )
        return {**values, "metadata": values["metadata_json"]}

    def thread(self, object_type: str, object_id: str) -> dict[str, Any]:
        with self.engine.connect() as connection:
            thread = connection.execute(
                select(object_threads).where(
                    (object_threads.c.object_type == object_type)
                    & (object_threads.c.object_id == object_id)
                )
            ).one_or_none()
            if thread is None:
                return {"thread": None, "posts": []}
            posts = connection.execute(
                select(thread_posts)
                .where(thread_posts.c.thread_id == thread.thread_id)
                .order_by(thread_posts.c.created_at.asc())
            ).all()
        return {
            "thread": dict(thread._mapping),
            "posts": [
                {
                    **dict(row._mapping),
                    "metadata": row.metadata_json,
                }
                for row in posts
            ],
        }

    def data_catalog(self) -> dict[str, Any]:
        with self.engine.connect() as connection:
            raw = connection.execute(
                select(
                    raw_objects.c.provider,
                    raw_objects.c.data_type,
                    func.count().label("object_count"),
                    func.sum(raw_objects.c.payload_bytes).label("payload_bytes"),
                    func.min(raw_objects.c.provider_received_at).label("earliest"),
                    func.max(raw_objects.c.provider_received_at).label("latest"),
                )
                .group_by(raw_objects.c.provider, raw_objects.c.data_type)
                .order_by(raw_objects.c.provider, raw_objects.c.data_type)
            ).all()
            coverage = connection.execute(
                select(
                    market_bars.c.symbol,
                    market_bars.c.timeframe,
                    func.count().label("row_count"),
                    func.min(market_bars.c.event_time).label("earliest"),
                    func.max(market_bars.c.event_time).label("latest"),
                )
                .group_by(market_bars.c.symbol, market_bars.c.timeframe)
                .order_by(market_bars.c.symbol, market_bars.c.timeframe)
            ).all()
        return {
            "raw_datasets": [dict(row._mapping) for row in raw],
            "market_bar_coverage": [dict(row._mapping) for row in coverage],
        }

    def raw_object_list(self, *, limit: int = 100) -> list[dict[str, Any]]:
        statement = (
            select(raw_objects)
            .order_by(raw_objects.c.ingested_at.desc())
            .limit(limit)
        )
        with self.engine.connect() as connection:
            return [dict(row._mapping) for row in connection.execute(statement)]

    def raw_object(self, raw_object_id: str) -> dict[str, Any] | None:
        with self.engine.connect() as connection:
            row = connection.execute(
                select(raw_objects).where(raw_objects.c.raw_object_id == raw_object_id)
            ).one_or_none()
        return dict(row._mapping) if row is not None else None

    def dataset_page(
        self,
        *,
        provider: str,
        data_type: str,
        limit: int,
        offset: int,
        day: date | None = None,
    ) -> dict[str, Any]:
        filters = [
            raw_objects.c.provider == provider,
            raw_objects.c.data_type == data_type,
        ]
        if day is not None:
            start = datetime.combine(day, datetime.min.time(), tzinfo=UTC)
            filters.extend(
                (
                    raw_objects.c.ingested_at >= start,
                    raw_objects.c.ingested_at < start + timedelta(days=1),
                )
            )
        with self.engine.connect() as connection:
            total = int(
                connection.execute(
                    select(func.count()).select_from(raw_objects).where(*filters)
                ).scalar_one()
            )
            items = [
                dict(row._mapping)
                for row in connection.execute(
                    select(raw_objects)
                    .where(*filters)
                    .order_by(raw_objects.c.ingested_at.desc())
                    .offset(offset)
                    .limit(limit)
                )
            ]
            page_raw_object_ids = [str(item["raw_object_id"]) for item in items]
            date_groups = [
                {
                    "date": str(row._mapping["day"]),
                    "count": int(row._mapping["count"]),
                }
                for row in connection.execute(
                    select(
                        func.date(raw_objects.c.ingested_at).label("day"),
                        func.count().label("count"),
                    )
                    .where(
                        raw_objects.c.provider == provider,
                        raw_objects.c.data_type == data_type,
                    )
                    .group_by(func.date(raw_objects.c.ingested_at))
                    .order_by(func.date(raw_objects.c.ingested_at).desc())
                    .limit(90)
                )
            ]
            documents = [
                dict(row._mapping)
                for row in connection.execute(
                    select(
                        source_documents.c.document_id,
                        source_documents.c.source_kind,
                        source_documents.c.publisher,
                        source_documents.c.published_at,
                        source_documents.c.canonical_url,
                        source_document_versions.c.version_id,
                        source_document_versions.c.title,
                        source_document_versions.c.summary,
                        source_document_versions.c.ingested_at,
                    )
                    .join(
                        source_document_versions,
                        source_document_versions.c.document_id
                        == source_documents.c.document_id,
                    )
                    .where(
                        source_documents.c.provider == provider,
                        source_document_versions.c.raw_object_id.in_(
                            page_raw_object_ids
                        ),
                    )
                    .order_by(source_documents.c.published_at.desc())
                    .limit(500)
                )
            ] if page_raw_object_ids else []
            facts = [
                dict(row._mapping)
                for row in connection.execute(
                    select(
                        corporate_facts.c.fact_id,
                        corporate_facts.c.symbol,
                        corporate_facts.c.tag,
                        corporate_facts.c.form,
                        corporate_facts.c.period_end,
                        corporate_facts.c.filed_at,
                        corporate_facts.c.available_from,
                        corporate_facts.c.value_text,
                    )
                    .where(
                        corporate_facts.c.raw_object_id.in_(page_raw_object_ids)
                    )
                    .order_by(corporate_facts.c.available_from.desc())
                    .limit(500)
                )
            ] if page_raw_object_ids else []
        return {
            "provider": provider,
            "data_type": data_type,
            "total": total,
            "limit": limit,
            "offset": offset,
            "selected_date": day,
            "date_groups": date_groups,
            "raw_objects": items,
            "documents": documents,
            "corporate_facts": facts,
        }

    def market_bar_page(
        self,
        *,
        symbol: str,
        timeframe: str,
        limit: int,
        offset: int,
        day: date | None = None,
    ) -> dict[str, Any]:
        filters = [
            market_bars.c.symbol == symbol.upper(),
            market_bars.c.timeframe == timeframe,
        ]
        if day is not None:
            start = datetime.combine(day, datetime.min.time(), tzinfo=UTC)
            filters.extend(
                (
                    market_bars.c.event_time >= start,
                    market_bars.c.event_time < start + timedelta(days=1),
                )
            )
        with self.engine.connect() as connection:
            total = int(
                connection.execute(
                    select(func.count()).select_from(market_bars).where(*filters)
                ).scalar_one()
            )
            items = [
                dict(row._mapping)
                for row in connection.execute(
                    select(market_bars)
                    .where(*filters)
                    .order_by(market_bars.c.event_time.desc())
                    .offset(offset)
                    .limit(limit)
                )
            ]
            date_groups = [
                {
                    "date": str(row._mapping["day"]),
                    "count": int(row._mapping["count"]),
                }
                for row in connection.execute(
                    select(
                        func.date(market_bars.c.event_time).label("day"),
                        func.count().label("count"),
                    )
                    .where(*filters[:2])
                    .group_by(func.date(market_bars.c.event_time))
                    .order_by(func.date(market_bars.c.event_time).desc())
                    .limit(90)
                )
            ]
        return {
            "symbol": symbol.upper(),
            "timeframe": timeframe,
            "total": total,
            "limit": limit,
            "offset": offset,
            "selected_date": day,
            "date_groups": date_groups,
            "items": items,
        }

    def strategies(self, *, limit: int = 200) -> list[dict[str, Any]]:
        statement = (
            select(strategy_specs, strategy_adoptions.c.status.label("adoption_status"))
            .outerjoin(
                strategy_adoptions,
                strategy_adoptions.c.strategy_spec_id == strategy_specs.c.strategy_spec_id,
            )
            .order_by(strategy_specs.c.created_at.desc())
            .limit(limit)
        )
        with self.engine.connect() as connection:
            values = []
            for row in connection.execute(statement):
                item = dict(row._mapping)
                item["experiment_count"] = int(
                    connection.execute(
                        select(func.count())
                        .select_from(experiment_runs)
                        .where(
                            experiment_runs.c.strategy_spec_id
                            == item["strategy_spec_id"]
                        )
                    ).scalar_one()
                )
                item["trade_count"] = int(
                    connection.execute(
                        select(func.count())
                        .select_from(backtest_trades)
                        .join(
                            experiment_runs,
                            experiment_runs.c.experiment_run_id
                            == backtest_trades.c.experiment_run_id,
                        )
                        .where(
                            experiment_runs.c.strategy_spec_id
                            == item["strategy_spec_id"]
                        )
                    ).scalar_one()
                )
                item["symbols"] = [
                    str(value)
                    for value in connection.execute(
                        select(experiment_runs.c.symbol)
                        .where(
                            experiment_runs.c.strategy_spec_id
                            == item["strategy_spec_id"]
                        )
                        .distinct()
                        .order_by(experiment_runs.c.symbol)
                    ).scalars()
                ]
                latest = connection.execute(
                    select(experiment_runs.c.metrics_json)
                    .where(
                        experiment_runs.c.strategy_spec_id
                        == item["strategy_spec_id"]
                    )
                    .order_by(experiment_runs.c.finished_at.desc())
                    .limit(1)
                ).scalar_one_or_none()
                item["latest_metrics"] = latest
                item["origin_kind"] = (
                    "HYBRID_ML_LLM"
                    if connection.execute(
                        select(func.count())
                        .select_from(strategy_generation_attempts)
                        .where(
                            strategy_generation_attempts.c.strategy_spec_id
                            == item["strategy_spec_id"]
                        )
                    ).scalar_one()
                    else "DETERMINISTIC_BASELINE"
                )
                values.append(item)
            return values

    def strategy(self, strategy_spec_id: str) -> dict[str, Any] | None:
        item = next(
            (
                value
                for value in self.strategies(limit=10_000)
                if value["strategy_spec_id"] == strategy_spec_id
            ),
            None,
        )
        if item is None:
            return None
        with self.engine.connect() as connection:
            experiments = [
                self._normalize_times(
                    dict(row._mapping),
                    ("as_of_start", "as_of_end", "started_at", "finished_at"),
                )
                for row in connection.execute(
                    select(experiment_runs)
                    .where(experiment_runs.c.strategy_spec_id == strategy_spec_id)
                    .order_by(experiment_runs.c.finished_at.desc())
                    .limit(100)
                )
            ]
            trades = [
                self._normalize_times(
                    dict(row._mapping),
                    ("signal_as_of", "entry_time", "exit_time"),
                )
                for row in connection.execute(
                    select(backtest_trades)
                    .join(
                        experiment_runs,
                        experiment_runs.c.experiment_run_id
                        == backtest_trades.c.experiment_run_id,
                    )
                    .where(experiment_runs.c.strategy_spec_id == strategy_spec_id)
                    .order_by(backtest_trades.c.exit_time.desc())
                    .limit(250)
                )
            ]
            reports = [
                self._normalize_times(dict(row._mapping), ("created_at",))
                for row in connection.execute(
                    select(validation_reports)
                    .order_by(validation_reports.c.created_at.desc())
                    .limit(250)
                )
                if strategy_spec_id
                in set(dict(row.validated_strategy_spec_ids or {}).values())
            ]
            deployments = [
                self._normalize_times(
                    dict(row._mapping),
                    ("last_processed_bar_time", "created_at", "updated_at"),
                )
                for row in connection.execute(
                    select(shadow_deployments)
                    .where(shadow_deployments.c.strategy_spec_id == strategy_spec_id)
                    .order_by(shadow_deployments.c.updated_at.desc())
                )
            ]
            lineage = self._strategy_lineage(connection, item)
        item["experiments"] = experiments
        item["trades"] = trades
        item["validations"] = reports
        item["shadow_deployments"] = deployments
        item["lineage"] = lineage
        return item

    def _strategy_lineage(
        self,
        connection: Any,
        strategy: dict[str, Any],
    ) -> dict[str, Any]:
        """Return a compact, user-facing chain from evidence to executable spec."""
        strategy_spec_id = str(strategy["strategy_spec_id"])
        attempt_row = connection.execute(
            select(strategy_generation_attempts)
            .where(
                strategy_generation_attempts.c.strategy_spec_id
                == strategy_spec_id
            )
            .order_by(strategy_generation_attempts.c.updated_at.desc())
            .limit(1)
        ).one_or_none()
        requirements = dict(strategy.get("data_requirements_json") or {})
        if attempt_row is None:
            return {
                "origin_kind": "DETERMINISTIC_BASELINE",
                "origin_label": "Deterministic baseline",
                "summary": (
                    "This specification was created by versioned application code, "
                    "not proposed by an LLM. ML and Research LLM therefore did not "
                    "participate in this strategy version."
                ),
                "created_at": strategy.get("created_at"),
                "feature_snapshot": None,
                "ml_forecast": None,
                "research_llm": None,
                "generation": None,
                "critique": None,
                "advanced": {
                    "strategy_spec_id": strategy_spec_id,
                    "origin": requirements.get("origin", "deterministic_baseline"),
                    "feature_set_version": strategy.get("feature_set_version"),
                    "code_sha256": strategy.get("code_sha256"),
                },
            }

        attempt = self._normalize_times(
            dict(attempt_row._mapping),
            ("created_at", "updated_at"),
        )
        snapshot_row = connection.execute(
            select(feature_snapshots).where(
                feature_snapshots.c.feature_snapshot_id
                == attempt["feature_snapshot_id"]
            )
        ).one_or_none()
        forecast_row = connection.execute(
            select(ml_forecasts).where(
                ml_forecasts.c.forecast_id == attempt["forecast_id"]
            )
        ).one_or_none()
        analysis_row = connection.execute(
            select(research_analyses).where(
                research_analyses.c.analysis_id == attempt["analysis_id"]
            )
        ).one_or_none()

        snapshot = None
        if snapshot_row is not None:
            raw_snapshot = self._normalize_times(
                dict(snapshot_row._mapping),
                ("as_of", "source_max_available_from", "created_at"),
            )
            snapshot = {
                "feature_snapshot_id": raw_snapshot["feature_snapshot_id"],
                "symbol": raw_snapshot["symbol"],
                "timeframe": raw_snapshot["timeframe"],
                "as_of": raw_snapshot["as_of"],
                "feature_set_version": raw_snapshot["feature_set_version"],
                "values": raw_snapshot["feature_values"],
                "source_max_available_from": raw_snapshot[
                    "source_max_available_from"
                ],
                "data_hash": raw_snapshot["data_hash"],
            }

        forecast = None
        if forecast_row is not None:
            raw_forecast = self._normalize_times(
                dict(forecast_row._mapping),
                ("as_of", "training_data_cutoff", "created_at"),
            )
            model_row = connection.execute(
                select(ml_models, ml_training_runs.c.sample_count)
                .join(
                    ml_training_runs,
                    ml_training_runs.c.training_run_id
                    == ml_models.c.training_run_id,
                )
                .where(ml_models.c.model_id == raw_forecast["model_id"])
            ).one_or_none()
            model = None
            if model_row is not None:
                raw_model = self._normalize_times(
                    dict(model_row._mapping),
                    (
                        "training_start",
                        "training_end",
                        "training_data_cutoff",
                        "created_at",
                    ),
                )
                model = {
                    "model_id": raw_model["model_id"],
                    "model_name": raw_model["model_name"],
                    "model_version": raw_model["model_version"],
                    "kind": raw_model["kind"],
                    "sample_count": raw_model["sample_count"],
                    "metrics": raw_model["metrics_json"],
                    "training_data_cutoff": raw_model["training_data_cutoff"],
                }
            forecast = {
                "forecast_id": raw_forecast["forecast_id"],
                "symbol": raw_forecast["symbol"],
                "as_of": raw_forecast["as_of"],
                "horizon": raw_forecast["horizon"],
                "expected_return": raw_forecast["expected_return"],
                "probability_up": raw_forecast["probability_up"],
                "uncertainty": raw_forecast["uncertainty"],
                "model_version": raw_forecast["model_version"],
                "training_data_cutoff": raw_forecast["training_data_cutoff"],
                "model": model,
            }

        analysis = None
        if analysis_row is not None:
            raw_analysis = self._normalize_times(
                dict(analysis_row._mapping),
                ("as_of", "created_at"),
            )
            bundle = dict(raw_analysis.get("evidence_bundle_json") or {})
            items = list(bundle.get("items") or [])
            analysis = {
                "analysis_id": raw_analysis["analysis_id"],
                "status": raw_analysis["status"],
                "symbol": raw_analysis["symbol"],
                "as_of": raw_analysis["as_of"],
                "analysis": raw_analysis["analysis_json"],
                "citation_validation": raw_analysis["citation_validation_json"],
                "rejection_reason": raw_analysis["rejection_reason"],
                "evidence_summary": {
                    "count": len(items),
                    "citation_ids": [item.get("citation_id") for item in items],
                    "types": sorted(
                        {
                            str(item.get("evidence_type"))
                            for item in items
                            if item.get("evidence_type")
                        }
                    ),
                    "bundle_hash": raw_analysis["evidence_bundle_hash"],
                },
                "invocation": self._llm_invocation_summary(
                    connection,
                    raw_analysis.get("llm_invocation_id"),
                ),
            }

        return {
            "origin_kind": "HYBRID_ML_LLM",
            "origin_label": "ML + Research LLM",
            "summary": (
                "A point-in-time feature snapshot fed a trained ML forecast and "
                "a cited Research LLM analysis. A generation LLM proposed bounded "
                "parameters and a second LLM critique had to accept them before "
                "this immutable executable specification was compiled."
            ),
            "created_at": strategy.get("created_at"),
            "feature_snapshot": snapshot,
            "ml_forecast": forecast,
            "research_llm": analysis,
            "generation": {
                "generation_attempt_id": attempt["generation_attempt_id"],
                "status": attempt["status"],
                "proposal": attempt["proposal_json"],
                "invocation": self._llm_invocation_summary(
                    connection,
                    attempt.get("generation_invocation_id"),
                ),
            },
            "critique": {
                "result": attempt["critique_json"],
                "invocation": self._llm_invocation_summary(
                    connection,
                    attempt.get("critique_invocation_id"),
                ),
            },
            "advanced": {
                "strategy_spec_id": strategy_spec_id,
                "generation_attempt_id": attempt["generation_attempt_id"],
                "feature_snapshot_id": attempt["feature_snapshot_id"],
                "analysis_id": attempt["analysis_id"],
                "forecast_id": attempt["forecast_id"],
                "feature_set_version": strategy.get("feature_set_version"),
                "code_sha256": strategy.get("code_sha256"),
            },
        }

    @staticmethod
    def _llm_invocation_summary(
        connection: Any,
        invocation_id: str | None,
    ) -> dict[str, Any] | None:
        if not invocation_id:
            return None
        row = connection.execute(
            select(
                llm_invocations.c.invocation_id,
                llm_invocations.c.workload,
                llm_invocations.c.provider,
                llm_invocations.c.model,
                llm_invocations.c.reasoning_effort,
                llm_invocations.c.prompt_version,
                llm_invocations.c.usage_json,
                llm_invocations.c.status,
                llm_invocations.c.created_at,
                llm_invocations.c.completed_at,
                llm_budget_reservations.c.actual_cost_microusd,
                llm_budget_reservations.c.reserved_cost_microusd,
            )
            .outerjoin(
                llm_budget_reservations,
                llm_budget_reservations.c.invocation_id
                == llm_invocations.c.invocation_id,
            )
            .where(llm_invocations.c.invocation_id == invocation_id)
        ).one_or_none()
        if row is None:
            return {
                "invocation_id": invocation_id,
                "status": "NOT_RECORDED",
            }
        value = dict(row._mapping)
        actual = value.pop("actual_cost_microusd")
        reserved = value.pop("reserved_cost_microusd")
        value["estimated_cost_usd"] = str(
            (actual if actual is not None else reserved or 0) / 1_000_000
        )
        return SystemObjectStore._normalize_times(
            value,
            ("created_at", "completed_at"),
        )

    def object_summary(self) -> dict[str, Any]:
        counts = {
            "raw_objects": raw_objects,
            "market_bars": market_bars,
            "market_trades": market_trades,
            "market_quotes": market_quotes,
            "catalysts": catalysts,
            "feature_snapshots": feature_snapshots,
            "strategies": strategy_specs,
            "experiments": experiment_runs,
            "backtest_trades": backtest_trades,
            "research_analyses": research_analyses,
            "ml_models": ml_models,
            "workflow_jobs": workflow_jobs,
            "shadow_deployments": shadow_deployments,
            "shadow_events": shadow_events,
            "pending_actions": admin_action_requests,
            "code_change_sessions": code_change_sessions,
        }
        with self.engine.connect() as connection:
            result = {
                key: int(connection.execute(select(func.count()).select_from(table)).scalar_one())
                for key, table in counts.items()
            }
            result["failed_ingestions"] = int(
                connection.execute(
                    select(func.count())
                    .select_from(ingestion_runs)
                    .where(ingestion_runs.c.status == "FAILED")
                ).scalar_one()
            )
            result["failed_quality_reports"] = int(
                connection.execute(
                    select(func.count())
                    .select_from(data_quality_reports)
                    .where(data_quality_reports.c.status == "FAILED")
                ).scalar_one()
            )
        return result

    def activity(self, *, limit: int = 100) -> list[dict[str, Any]]:
        statement = (
            select(ledger_events)
            .order_by(ledger_events.c.sequence.desc())
            .limit(limit)
        )
        with self.engine.connect() as connection:
            return [dict(row._mapping) for row in connection.execute(statement)]

    def health_summary(self) -> dict[str, int]:
        with self.engine.connect() as connection:
            return {
                "system_lists": int(
                    connection.execute(
                        select(func.count()).select_from(system_lists)
                    ).scalar_one()
                ),
                "object_threads": int(
                    connection.execute(
                        select(func.count()).select_from(object_threads)
                    ).scalar_one()
                ),
                "thread_posts": int(
                    connection.execute(select(func.count()).select_from(thread_posts)).scalar_one()
                ),
                "steward_conversations": int(
                    connection.execute(
                        select(func.count()).select_from(steward_conversations)
                    ).scalar_one()
                ),
                "steward_messages": int(
                    connection.execute(
                        select(func.count()).select_from(steward_messages)
                    ).scalar_one()
                ),
            }

    @staticmethod
    def _normalize_times(
        item: dict[str, Any],
        fields: tuple[str, ...],
    ) -> dict[str, Any]:
        for field in fields:
            value = item.get(field)
            if isinstance(value, datetime) and value.tzinfo is None:
                item[field] = value.replace(tzinfo=UTC)
        return item

    def _emit(self, event_type: str, correlation_id: str, payload: dict[str, Any]) -> None:
        if self.ledger is None:
            return
        now = datetime.now(UTC)
        self.ledger.append(
            EventEnvelope(
                event_id=uuid7(),
                event_type=event_type,
                event_time=now,
                emitted_at=now,
                producer="phase6-control-plane",
                correlation_id=correlation_id,
                payload=payload,
            )
        )
