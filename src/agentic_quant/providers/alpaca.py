from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx

from agentic_quant.domain import OptionSnapshot, StockBar
from agentic_quant.ids import stable_uuid
from agentic_quant.market_calendar import MarketSessionClock
from agentic_quant.providers.base import (
    EntitlementCheck,
    OptionChainRequest,
    OptionSnapshotsPage,
    StockBarsPage,
    StockBarsRequest,
)


class AlpacaConfigurationError(RuntimeError):
    pass


class AlpacaResponseError(RuntimeError):
    def __init__(self, *, status_code: int, endpoint: str, detail: str) -> None:
        super().__init__(f"Alpaca request failed ({status_code}) at {endpoint}: {detail}")
        self.status_code = status_code
        self.endpoint = endpoint
        self.detail = detail


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Provider request timestamps must be timezone-aware")
    return value.astimezone(UTC)


class AlpacaMarketDataProvider:
    """Read-only Alpaca Market Data adapter. This class has no trading endpoints."""

    name = "alpaca"

    def __init__(
        self,
        *,
        api_key: str,
        api_secret: str,
        base_url: str = "https://data.alpaca.markets",
        client: httpx.AsyncClient | None = None,
        calendar_name: str = "XNYS",
        max_retries: int = 3,
        retry_base_seconds: float = 0.5,
    ) -> None:
        if not api_key or not api_secret:
            raise AlpacaConfigurationError("Alpaca API key and secret are required")
        self._headers = {
            "APCA-API-KEY-ID": api_key,
            "APCA-API-SECRET-KEY": api_secret,
        }
        self._owns_client = client is None
        self._max_retries = max_retries
        self._retry_base_seconds = retry_base_seconds
        self._client = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(60.0, connect=15.0),
            headers=self._headers,
        )
        self._session_clock = MarketSessionClock(calendar_name)

    async def __aenter__(self) -> AlpacaMarketDataProvider:
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _get(self, endpoint: str, params: dict[str, Any]) -> httpx.Response:
        last_error: httpx.RequestError | None = None
        for attempt in range(self._max_retries + 1):
            try:
                response = await self._client.get(endpoint, params=params, headers=self._headers)
            except httpx.RequestError as exc:
                last_error = exc
                if attempt < self._max_retries:
                    await asyncio.sleep(self._retry_base_seconds * (2**attempt))
                    continue
                break
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < self._max_retries:
                    await asyncio.sleep(self._retry_base_seconds * (2**attempt))
                    continue
            if response.is_error:
                detail = response.text[:500].replace("\n", " ")
                raise AlpacaResponseError(
                    status_code=response.status_code,
                    endpoint=endpoint,
                    detail=detail,
                )
            return response
        raise AlpacaResponseError(
            status_code=504,
            endpoint=endpoint,
            detail="upstream transport failure after retries",
        ) from last_error

    async def fetch_stock_bars_page(
        self,
        request: StockBarsRequest,
        *,
        page_token: str | None = None,
    ) -> StockBarsPage:
        start = _utc(request.start)
        end = _utc(request.end)
        if start >= end:
            raise ValueError("Stock bar request start must be before end")
        params: dict[str, Any] = {
            "timeframe": request.timeframe,
            "start": start.isoformat().replace("+00:00", "Z"),
            "end": end.isoformat().replace("+00:00", "Z"),
            "limit": request.limit,
            "adjustment": request.adjustment,
            "feed": request.feed,
            "sort": "asc",
        }
        if page_token:
            params["page_token"] = page_token
        endpoint = f"/v2/stocks/{request.symbol.upper()}/bars"
        response = await self._get(endpoint, params)
        received_at = datetime.now(UTC)
        payload = response.json()
        normalized_bars = (
            self._normalize_bar(
                symbol=request.symbol,
                timeframe=request.timeframe,
                feed=request.feed,
                raw_object_id="PENDING_ARCHIVE",
                ingested_at=received_at,
                item=item,
            )
            for item in payload.get("bars") or ()
        )
        # Alpaca treats `end` as inclusive, while every internal request and store query
        # uses [start, end). Preserve the raw response but never normalize a boundary bar
        # into the requested dataset.
        bars = tuple(
            bar for bar in normalized_bars if start <= bar.event_time < end
        )
        return StockBarsPage(
            provider="alpaca",
            provider_received_at=received_at,
            request_metadata={"endpoint": endpoint, "params": params},
            raw_payload=payload,
            bars=bars,
            next_page_token=payload.get("next_page_token"),
        )

    def _normalize_bar(
        self,
        *,
        symbol: str,
        timeframe: str,
        feed: str,
        raw_object_id: str,
        ingested_at: datetime,
        item: dict[str, Any],
    ) -> StockBar:
        event_time = datetime.fromisoformat(str(item["t"]).replace("Z", "+00:00")).astimezone(UTC)
        return StockBar(
            bar_id=stable_uuid("bar", "alpaca", feed, symbol.upper(), timeframe, event_time),
            symbol=symbol.upper(),
            timeframe=timeframe,
            event_time=event_time,
            available_from=(
                event_time + timedelta(minutes=1)
                if timeframe == "1Min"
                else self._session_clock.daily_bar_available_from(event_time)
            ),
            open=Decimal(str(item["o"])),
            high=Decimal(str(item["h"])),
            low=Decimal(str(item["l"])),
            close=Decimal(str(item["c"])),
            volume=int(item["v"]),
            trade_count=int(item["n"]) if item.get("n") is not None else None,
            vwap=Decimal(str(item["vw"])) if item.get("vw") is not None else None,
            source="alpaca",
            feed=feed,
            raw_object_id=raw_object_id,
            ingested_at=ingested_at,
        )

    async def probe_entitlements(
        self,
        *,
        stock_feed: str,
        option_feed: str,
        now: datetime | None = None,
    ) -> tuple[EntitlementCheck, ...]:
        observed_at = (now or datetime.now(UTC)).astimezone(UTC)
        stock_params = {
            "timeframe": "1Day",
            "start": (observed_at - timedelta(days=10)).isoformat().replace("+00:00", "Z"),
            "end": observed_at.isoformat().replace("+00:00", "Z"),
            "feed": stock_feed,
            "limit": 1,
            "sort": "desc",
            "adjustment": "raw",
        }
        checks = [
            await self._probe_one(
                "stock_historical",
                stock_feed,
                "/v2/stocks/AAPL/bars",
                stock_params,
            ),
            await self._probe_one(
                "option_chain_snapshot",
                option_feed,
                "/v1beta1/options/snapshots/AAPL",
                {"feed": option_feed, "limit": 1},
            ),
        ]
        return tuple(checks)

    async def fetch_option_snapshots_page(
        self,
        request: OptionChainRequest,
        *,
        page_token: str | None = None,
    ) -> OptionSnapshotsPage:
        endpoint = f"/v1beta1/options/snapshots/{request.underlying_symbol.upper()}"
        params: dict[str, Any] = {"feed": request.feed, "limit": request.limit}
        if page_token:
            params["page_token"] = page_token
        response = await self._get(endpoint, params)
        received_at = datetime.now(UTC)
        payload = response.json()
        raw_snapshots = payload.get("snapshots") or {}
        snapshots = tuple(
            snapshot
            for contract_symbol, item in raw_snapshots.items()
            if (
                snapshot := self._normalize_option_snapshot(
                    contract_symbol=contract_symbol,
                    underlying_symbol=request.underlying_symbol,
                    feed=request.feed,
                    raw_object_id="PENDING_ARCHIVE",
                    ingested_at=received_at,
                    item=item,
                )
            )
            is not None
        )
        return OptionSnapshotsPage(
            provider=self.name,
            provider_received_at=received_at,
            request_metadata={"endpoint": endpoint, "params": params},
            raw_payload=payload,
            snapshots=snapshots,
            next_page_token=payload.get("next_page_token"),
        )

    @staticmethod
    def _normalize_option_snapshot(
        *,
        contract_symbol: str,
        underlying_symbol: str,
        feed: str,
        raw_object_id: str,
        ingested_at: datetime,
        item: dict[str, Any],
    ) -> OptionSnapshot | None:
        quote = item.get("latestQuote") or {}
        trade = item.get("latestTrade") or {}
        minute_bar = item.get("minuteBar") or {}
        daily_bar = item.get("dailyBar") or {}
        timestamp = quote.get("t") or trade.get("t") or minute_bar.get("t") or daily_bar.get("t")
        if timestamp is None:
            return None
        as_of = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00")).astimezone(UTC)
        greeks = item.get("greeks") or {}
        return OptionSnapshot(
            option_snapshot_id=stable_uuid(
                "option",
                "alpaca",
                feed,
                contract_symbol,
                as_of,
            ),
            contract_symbol=contract_symbol,
            underlying_symbol=underlying_symbol.upper(),
            as_of=as_of,
            available_from=ingested_at,
            bid_price=Decimal(str(quote["bp"])) if quote.get("bp") is not None else None,
            bid_size=int(quote["bs"]) if quote.get("bs") is not None else None,
            ask_price=Decimal(str(quote["ap"])) if quote.get("ap") is not None else None,
            ask_size=int(quote["as"]) if quote.get("as") is not None else None,
            last_trade_price=(
                Decimal(str(trade["p"])) if trade.get("p") is not None else None
            ),
            last_trade_size=int(trade["s"]) if trade.get("s") is not None else None,
            implied_volatility=(
                Decimal(str(item["impliedVolatility"]))
                if item.get("impliedVolatility") is not None
                else None
            ),
            delta=Decimal(str(greeks["delta"])) if greeks.get("delta") is not None else None,
            gamma=Decimal(str(greeks["gamma"])) if greeks.get("gamma") is not None else None,
            theta=Decimal(str(greeks["theta"])) if greeks.get("theta") is not None else None,
            vega=Decimal(str(greeks["vega"])) if greeks.get("vega") is not None else None,
            rho=Decimal(str(greeks["rho"])) if greeks.get("rho") is not None else None,
            source="alpaca",
            feed=feed,
            raw_object_id=raw_object_id,
            ingested_at=ingested_at,
        )

    async def _probe_one(
        self,
        capability: str,
        feed: str,
        endpoint: str,
        params: dict[str, Any],
    ) -> EntitlementCheck:
        try:
            response = await self._get(endpoint, params)
        except AlpacaResponseError as exc:
            return EntitlementCheck(
                capability=capability,
                feed=feed,
                accessible=False,
                status_code=exc.status_code,
                detail=exc.detail,
            )
        return EntitlementCheck(
            capability=capability,
            feed=feed,
            accessible=True,
            status_code=response.status_code,
            detail="authorized",
        )
