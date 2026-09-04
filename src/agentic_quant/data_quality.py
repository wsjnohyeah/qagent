from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any

import exchange_calendars as exchange_calendars  # type: ignore[import-untyped]
from sqlalchemy import Engine, func, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from agentic_quant.database import data_quality_reports
from agentic_quant.domain import (
    DataQualityReport,
    DataQualityStatus,
    EventEnvelope,
    StockBar,
)
from agentic_quant.ids import uuid7
from agentic_quant.ledger import EventLedger
from agentic_quant.market_calendar import MarketGapDetector


DATA_QUALITY_RULESET_VERSION = "market_data_quality@0.1.0"


class DataQualityError(RuntimeError):
    def __init__(self, report: DataQualityReport) -> None:
        failed = ", ".join(
            name for name, count in report.issue_counts.items() if count > 0
        )
        super().__init__(f"Market data quality failed: {failed or 'unknown'}")
        self.report = report


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def inspect_market_bars(
    bars: tuple[StockBar, ...],
    *,
    symbol: str,
    timeframe: str,
    code_git_sha: str,
    calendar_name: str = "XNYS",
    require_complete: bool = True,
    expected_start: datetime | None = None,
    expected_end: datetime | None = None,
) -> DataQualityReport:
    if (expected_start is None) != (expected_end is None):
        raise ValueError("Expected data-quality bounds must be provided together")
    if expected_start is not None and expected_end is not None:
        if expected_start.tzinfo is None or expected_end.tzinfo is None:
            raise ValueError("Expected data-quality bounds must be timezone-aware")
        if expected_start >= expected_end:
            raise ValueError("Expected data-quality start must be before end")
    normalized_symbol = symbol.upper()
    ordered = tuple(sorted(bars, key=lambda item: item.event_time))
    duplicate_timestamps = len(ordered) - len({bar.event_time for bar in ordered})
    mixed_symbols = sum(bar.symbol.upper() != normalized_symbol for bar in ordered)
    mixed_timeframes = sum(bar.timeframe != timeframe for bar in ordered)
    invalid_ohlc = sum(
        not (
            bar.low <= min(bar.open, bar.close)
            and max(bar.open, bar.close) <= bar.high
        )
        for bar in ordered
    )
    invalid_availability = sum(
        bar.available_from < bar.event_time or bar.event_time.tzinfo is None
        or bar.available_from.tzinfo is None
        for bar in ordered
    )
    non_monotonic = sum(
        current.event_time <= previous.event_time
        for previous, current in zip(bars, bars[1:])
    )
    zero_volume = sum(bar.volume == 0 for bar in ordered)
    missing_intervals = 0
    expected_interval_count: int | None = None
    if require_complete:
        calendar = exchange_calendars.get_calendar(calendar_name)
        if expected_start is not None and expected_end is not None:
            if timeframe == "1Day":
                sessions = calendar.sessions_in_range(
                    expected_start.date().isoformat(),
                    expected_end.date().isoformat(),
                )
                expected_dates = {
                    session.date()
                    for session in sessions
                    if expected_start
                    <= calendar.session_close(session).to_pydatetime()
                    < expected_end
                }
                actual_dates = {bar.event_time.date() for bar in ordered}
                expected_interval_count = len(expected_dates)
                missing_intervals = len(expected_dates - actual_dates)
            elif timeframe == "1Min":
                expected_minutes = {
                    minute.to_pydatetime()
                    for minute in calendar.minutes_in_range(
                        expected_start,
                        expected_end,
                    )
                    if minute.to_pydatetime() < expected_end
                }
                actual_minutes = {bar.event_time for bar in ordered}
                expected_interval_count = len(expected_minutes)
                missing_intervals = len(expected_minutes - actual_minutes)
        elif ordered and timeframe == "1Day":
            sessions = calendar.sessions_in_range(
                ordered[0].event_time.date().isoformat(),
                ordered[-1].event_time.date().isoformat(),
            )
            expected_dates = {session.date() for session in sessions}
            actual_dates = {bar.event_time.date() for bar in ordered}
            missing_intervals = len(expected_dates - actual_dates)
            expected_interval_count = len(expected_dates)
        elif ordered and timeframe == "1Min":
            detector = MarketGapDetector(calendar_name)
            for bar in ordered:
                gap = detector.observe(normalized_symbol, bar.event_time)
                if gap is not None:
                    missing_intervals += gap.missing_minutes
    empty_dataset = int(
        not ordered
        and (expected_interval_count is None or expected_interval_count > 0)
    )
    issue_counts = {
        "empty_dataset": empty_dataset,
        "duplicate_timestamps": duplicate_timestamps,
        "mixed_symbols": mixed_symbols,
        "mixed_timeframes": mixed_timeframes,
        "invalid_ohlc": invalid_ohlc,
        "invalid_availability": invalid_availability,
        "non_monotonic": non_monotonic,
        "missing_intervals": missing_intervals,
        "zero_volume_warnings": zero_volume,
    }
    fatal_issue_count = sum(
        count for name, count in issue_counts.items() if not name.endswith("_warnings")
    )
    checks: dict[str, bool | int | str] = {
        "records_present": not empty_dataset,
        "identity_consistent": mixed_symbols == 0 and mixed_timeframes == 0,
        "timestamps_unique_and_ordered": (
            duplicate_timestamps == 0 and non_monotonic == 0
        ),
        "ohlc_envelope_valid": invalid_ohlc == 0,
        "availability_valid": invalid_availability == 0,
        "expected_intervals_present": missing_intervals == 0,
        "require_complete": require_complete,
        "calendar": calendar_name,
        "expected_interval_count": expected_interval_count or 0,
        "expected_start": expected_start.isoformat() if expected_start else "",
        "expected_end": expected_end.isoformat() if expected_end else "",
    }
    data_material = [
        {
            "bar_id": bar.bar_id,
            "symbol": bar.symbol,
            "timeframe": bar.timeframe,
            "event_time": bar.event_time.isoformat(),
            "available_from": bar.available_from.isoformat(),
            "open": str(bar.open),
            "high": str(bar.high),
            "low": str(bar.low),
            "close": str(bar.close),
            "volume": bar.volume,
            "source": bar.source,
            "feed": bar.feed,
        }
        for bar in ordered
    ]
    data_sha256 = _canonical_hash(data_material)
    scope_sha256 = _canonical_hash(
        {
            "require_complete": require_complete,
            "calendar": calendar_name,
            "expected_start": expected_start.isoformat() if expected_start else None,
            "expected_end": expected_end.isoformat() if expected_end else None,
        }
    )
    identity = _canonical_hash(
        {
            "ruleset": DATA_QUALITY_RULESET_VERSION,
            "symbol": normalized_symbol,
            "timeframe": timeframe,
            "data_sha256": data_sha256,
            "scope_sha256": scope_sha256,
        }
    )
    return DataQualityReport(
        data_quality_report_id=str(uuid.uuid5(uuid.NAMESPACE_URL, identity)),
        ruleset_version=DATA_QUALITY_RULESET_VERSION,
        dataset_type="market_bars",
        symbol=normalized_symbol,
        timeframe=timeframe,
        window_start=expected_start or (ordered[0].event_time if ordered else None),
        window_end=expected_end or (ordered[-1].event_time if ordered else None),
        record_count=len(ordered),
        data_sha256=data_sha256,
        scope_sha256=scope_sha256,
        status=(
            DataQualityStatus.PASSED
            if fatal_issue_count == 0
            else DataQualityStatus.FAILED
        ),
        checks=checks,
        issue_counts=issue_counts,
        code_git_sha=code_git_sha,
        created_at=datetime.now(UTC),
    )


class MarketDataQualityService:
    def __init__(
        self,
        engine: Engine,
        ledger: EventLedger | None = None,
        *,
        calendar_name: str = "XNYS",
    ) -> None:
        self.engine = engine
        self.ledger = ledger
        self.calendar_name = calendar_name

    def assess_bars(
        self,
        bars: tuple[StockBar, ...],
        *,
        symbol: str,
        timeframe: str,
        code_git_sha: str,
        require_complete: bool = True,
        expected_start: datetime | None = None,
        expected_end: datetime | None = None,
    ) -> DataQualityReport:
        report = inspect_market_bars(
            bars,
            symbol=symbol,
            timeframe=timeframe,
            code_git_sha=code_git_sha,
            calendar_name=self.calendar_name,
            require_complete=require_complete,
            expected_start=expected_start,
            expected_end=expected_end,
        )
        inserted = self._record(report)
        if inserted and self.ledger is not None:
            self.ledger.append(
                EventEnvelope(
                    event_id=uuid7(),
                    event_type="data.quality.assessed.v1",
                    event_time=report.created_at,
                    emitted_at=datetime.now(UTC),
                    producer="market-data-quality",
                    correlation_id=report.data_quality_report_id,
                    payload=report.model_dump(mode="json"),
                )
            )
        return report

    def require_bars(
        self,
        bars: tuple[StockBar, ...],
        *,
        symbol: str,
        timeframe: str,
        code_git_sha: str,
        require_complete: bool = True,
        expected_start: datetime | None = None,
        expected_end: datetime | None = None,
    ) -> DataQualityReport:
        report = self.assess_bars(
            bars,
            symbol=symbol,
            timeframe=timeframe,
            code_git_sha=code_git_sha,
            require_complete=require_complete,
            expected_start=expected_start,
            expected_end=expected_end,
        )
        if report.status == DataQualityStatus.FAILED:
            raise DataQualityError(report)
        return report

    def _record(self, report: DataQualityReport) -> bool:
        values = {
            **report.model_dump(exclude={"checks", "issue_counts"}),
            "status": report.status.value,
            "checks_json": report.checks,
            "issue_counts_json": report.issue_counts,
        }
        if self.engine.dialect.name == "postgresql":
            statement = (
                postgresql_insert(data_quality_reports)
                .values(values)
                .on_conflict_do_nothing(
                    index_elements=[
                        "ruleset_version",
                        "dataset_type",
                        "symbol",
                        "timeframe",
                        "data_sha256",
                        "scope_sha256",
                    ]
                )
                .returning(data_quality_reports.c.data_quality_report_id)
            )
        elif self.engine.dialect.name == "sqlite":
            statement = (
                sqlite_insert(data_quality_reports)
                .values(values)
                .on_conflict_do_nothing(
                    index_elements=[
                        "ruleset_version",
                        "dataset_type",
                        "symbol",
                        "timeframe",
                        "data_sha256",
                        "scope_sha256",
                    ]
                )
                .returning(data_quality_reports.c.data_quality_report_id)
            )
        else:
            raise RuntimeError(f"Unsupported SQL dialect: {self.engine.dialect.name}")
        with self.engine.begin() as connection:
            return connection.execute(statement).scalar_one_or_none() is not None

    def recent(self, *, limit: int = 50) -> list[dict[str, Any]]:
        statement = (
            select(data_quality_reports)
            .order_by(data_quality_reports.c.created_at.desc())
            .limit(limit)
        )
        with self.engine.connect() as connection:
            results = []
            for row in connection.execute(statement):
                item = dict(row._mapping)
                item["checks"] = item.pop("checks_json")
                item["issue_counts"] = item.pop("issue_counts_json")
                results.append(item)
            return results

    def health_summary(self) -> dict[str, int]:
        with self.engine.connect() as connection:
            return {
                "data_quality_reports": int(
                    connection.execute(
                        select(func.count()).select_from(data_quality_reports)
                    ).scalar_one()
                ),
                "failed_data_quality_reports": int(
                    connection.execute(
                        select(func.count())
                        .select_from(data_quality_reports)
                        .where(
                            data_quality_reports.c.status
                            == DataQualityStatus.FAILED.value
                        )
                    ).scalar_one()
                ),
            }
