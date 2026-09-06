from __future__ import annotations

from datetime import datetime

import exchange_calendars as exchange_calendars  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict


class MarketDataGap(BaseModel):
    model_config = ConfigDict(frozen=True)

    symbol: str
    missing_from: datetime
    missing_to: datetime
    missing_minutes: int


class MarketSessionClock:
    def __init__(self, calendar_name: str = "XNYS") -> None:
        self.calendar = exchange_calendars.get_calendar(calendar_name)

    def daily_bar_available_from(self, event_time: datetime) -> datetime:
        session = self._session_for_daily_bar(event_time)
        close = self.calendar.session_close(session).to_pydatetime()
        if not isinstance(close, datetime):
            raise TypeError("Exchange calendar returned a non-datetime session close")
        return close

    def daily_bar_session_open(self, event_time: datetime) -> datetime:
        session = self._session_for_daily_bar(event_time)
        session_open = self.calendar.session_open(session).to_pydatetime()
        if not isinstance(session_open, datetime):
            raise TypeError("Exchange calendar returned a non-datetime session open")
        return session_open

    def next_daily_session_open(self, event_time: datetime) -> datetime:
        session = self._session_for_daily_bar(event_time)
        next_session = self.calendar.next_session(session)
        session_open = self.calendar.session_open(next_session).to_pydatetime()
        if not isinstance(session_open, datetime):
            raise TypeError("Exchange calendar returned a non-datetime session open")
        return session_open

    def _session_for_daily_bar(self, event_time: datetime):  # type: ignore[no-untyped-def]
        if event_time.tzinfo is None:
            raise ValueError("Daily bar event_time must be timezone-aware")
        session_label = event_time.date().isoformat()
        try:
            session = self.calendar.date_to_session(session_label, direction="none")
        except ValueError as exc:
            raise ValueError(
                f"Daily bar date {session_label} is not a {self.calendar.name} session"
            ) from exc
        return session


class MarketGapDetector:
    def __init__(self, calendar_name: str = "XNYS") -> None:
        self.calendar = exchange_calendars.get_calendar(calendar_name)
        self._last_bar_by_symbol: dict[str, datetime] = {}

    def seed(self, symbol: str, event_time: datetime | None) -> None:
        if event_time is not None and symbol.upper() not in self._last_bar_by_symbol:
            self._last_bar_by_symbol[symbol.upper()] = event_time

    def observe(self, symbol: str, event_time: datetime) -> MarketDataGap | None:
        normalized = symbol.upper()
        previous = self._last_bar_by_symbol.get(normalized)
        if previous is None or event_time <= previous:
            self._last_bar_by_symbol[normalized] = max(previous or event_time, event_time)
            return None
        self._last_bar_by_symbol[normalized] = event_time
        try:
            previous_session = self.calendar.minute_to_session(previous, direction="none")
            current_session = self.calendar.minute_to_session(event_time, direction="none")
        except ValueError:
            return None
        if previous_session != current_session:
            return None
        expected = self.calendar.minutes_in_range(previous, event_time)
        missing = tuple(value.to_pydatetime() for value in expected[1:-1])
        if not missing:
            return None
        return MarketDataGap(
            symbol=normalized,
            missing_from=missing[0],
            missing_to=event_time,
            missing_minutes=len(missing),
        )
