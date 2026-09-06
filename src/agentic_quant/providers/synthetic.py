from __future__ import annotations

from datetime import UTC, timedelta
from decimal import Decimal

from agentic_quant.domain import StockBar
from agentic_quant.ids import stable_uuid
from agentic_quant.providers.base import StockBarsPage, StockBarsRequest


class SyntheticMarketDataProvider:
    """Deterministic provider used to prove infrastructure without external credentials."""

    name = "synthetic"

    async def fetch_stock_bars_page(
        self,
        request: StockBarsRequest,
        *,
        page_token: str | None = None,
    ) -> StockBarsPage:
        if page_token is not None:
            raise ValueError("Synthetic provider has exactly one page")
        event_time = request.start.astimezone(UTC).replace(second=0, microsecond=0)
        received_at = event_time + timedelta(minutes=2)
        payload = {
            "bars": [
                {
                    "t": event_time.isoformat().replace("+00:00", "Z"),
                    "o": 100.0,
                    "h": 101.0,
                    "l": 99.5,
                    "c": 100.5,
                    "v": 10000,
                    "n": 250,
                    "vw": 100.25,
                }
            ],
            "next_page_token": None,
            "symbol": request.symbol.upper(),
        }
        return StockBarsPage(
            provider=self.name,
            provider_received_at=received_at,
            request_metadata=request.model_dump(mode="json"),
            raw_payload=payload,
            bars=(
                StockBar(
                    bar_id=stable_uuid(
                        "bar",
                        self.name,
                        "test",
                        request.symbol.upper(),
                        request.timeframe,
                        event_time,
                    ),
                    symbol=request.symbol.upper(),
                    timeframe=request.timeframe,
                    event_time=event_time,
                    available_from=event_time + timedelta(minutes=1),
                    open=Decimal("100.0"),
                    high=Decimal("101.0"),
                    low=Decimal("99.5"),
                    close=Decimal("100.5"),
                    volume=10000,
                    trade_count=250,
                    vwap=Decimal("100.25"),
                    source=self.name,
                    feed="test",
                    raw_object_id="PENDING_ARCHIVE",
                    ingested_at=received_at,
                ),
            ),
        )
