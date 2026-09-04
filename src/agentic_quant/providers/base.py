from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

from pydantic import Field

from agentic_quant.domain import FrozenModel, OptionSnapshot, StockBar


class StockBarsRequest(FrozenModel):
    symbol: str = Field(min_length=1, max_length=24)
    start: datetime
    end: datetime
    timeframe: str = "1Min"
    feed: str = "sip"
    adjustment: str = "raw"
    limit: int = Field(default=10_000, ge=1, le=10_000)


class StockBarsPage(FrozenModel):
    provider: str
    provider_received_at: datetime
    request_metadata: dict[str, Any]
    raw_payload: dict[str, Any]
    bars: tuple[StockBar, ...]
    next_page_token: str | None = None


class OptionChainRequest(FrozenModel):
    underlying_symbol: str = Field(min_length=1, max_length=24)
    feed: str = "opra"
    limit: int = Field(default=100, ge=1, le=1_000)
    max_pages: int = Field(default=1, ge=1, le=1_000)


class OptionSnapshotsPage(FrozenModel):
    provider: str
    provider_received_at: datetime
    request_metadata: dict[str, Any]
    raw_payload: dict[str, Any]
    snapshots: tuple[OptionSnapshot, ...]
    next_page_token: str | None = None


class EntitlementCheck(FrozenModel):
    capability: str
    feed: str
    accessible: bool
    status_code: int | None = None
    detail: str


class EquityMarketDataProvider(Protocol):
    name: str

    async def fetch_stock_bars_page(
        self,
        request: StockBarsRequest,
        *,
        page_token: str | None = None,
    ) -> StockBarsPage: ...


class OptionMarketDataProvider(Protocol):
    name: str

    async def fetch_option_snapshots_page(
        self,
        request: OptionChainRequest,
        *,
        page_token: str | None = None,
    ) -> OptionSnapshotsPage: ...


class EventPublisher(Protocol):
    def publish(self, *, event_type: str, event_id: str, envelope_json: str) -> str | None: ...

    def health(self) -> bool: ...
