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
