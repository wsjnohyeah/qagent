from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol, Self

from pydantic import Field, model_validator

from agentic_quant.domain import (
    CorporateFact,
    FrozenModel,
    OptionSnapshot,
    SourceDocument,
    StockBar,
)


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


class DocumentFetchRequest(FrozenModel):
    symbols: tuple[str, ...] = Field(min_length=1)
    start: datetime | None = None
    end: datetime | None = None
    limit: int = Field(default=50, ge=1, le=1_000)
    max_pages: int = Field(default=1, ge=1, le=100)
    cik: str | None = None
    forms: tuple[str, ...] = ()

    @model_validator(mode="after")
    def timestamps_are_point_in_time_safe(self) -> Self:
        for value in (self.start, self.end):
            if value is not None and value.tzinfo is None:
                raise ValueError("Document request timestamps must be timezone-aware")
        if self.start is not None and self.end is not None and self.start >= self.end:
            raise ValueError("Document request start must be before end")
        return self


class DocumentPage(FrozenModel):
    provider: str
    data_type: str
    provider_received_at: datetime
    request_metadata: dict[str, Any]
    raw_payload: dict[str, Any]
    documents: tuple[SourceDocument, ...]
    next_page_token: str | None = None


class CorporateFactsRequest(FrozenModel):
    symbol: str = Field(min_length=1, max_length=24)
    cik: str = Field(pattern=r"^\d{1,10}$")
    max_facts: int = Field(default=1_000, ge=1, le=20_000)
    taxonomies: tuple[str, ...] = ("us-gaap",)


class CorporateFactsPage(FrozenModel):
    provider: str
    data_type: str
    provider_received_at: datetime
    request_metadata: dict[str, Any]
    raw_payload: dict[str, Any]
    facts: tuple[CorporateFact, ...]


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


class DocumentProvider(Protocol):
    name: str

    async def fetch_documents_page(
        self,
        request: DocumentFetchRequest,
        *,
        page_token: str | None = None,
    ) -> DocumentPage: ...


class EventPublisher(Protocol):
    def publish(self, *, event_type: str, event_id: str, envelope_json: str) -> str | None: ...

    def health(self) -> bool: ...
