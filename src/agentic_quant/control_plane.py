from __future__ import annotations

import re
from datetime import UTC, datetime
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
    market_bars,
    market_quotes,
    market_trades,
    ml_models,
    object_threads,
    raw_objects,
    research_analyses,
    shadow_deployments,
    shadow_events,
    strategy_adoptions,
    strategy_specs,
    steward_conversations,
    steward_messages,
    system_list_revisions,
    system_lists,
    thread_posts,
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
                values.append(item)
            return values

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
