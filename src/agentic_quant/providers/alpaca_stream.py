from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator, Iterable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import websockets

from agentic_quant.domain import StockBar, StockQuote, StockTrade
from agentic_quant.ids import stable_uuid


class AlpacaStreamError(RuntimeError):
    pass


def normalize_stream_message(
    item: dict[str, Any],
    *,
    feed: str,
    raw_object_id: str,
    received_at: datetime,
) -> StockBar | StockTrade | StockQuote | None:
    event_type = item.get("T")
    symbol = str(item.get("S", "")).upper()
    if not symbol or "t" not in item:
        return None
    event_time = datetime.fromisoformat(str(item["t"]).replace("Z", "+00:00")).astimezone(UTC)
    common = {
        "symbol": symbol,
        "event_time": event_time,
        "source": "alpaca",
        "feed": feed,
        "raw_object_id": raw_object_id,
        "ingested_at": received_at,
    }
    if event_type == "b":
        return StockBar(
            bar_id=stable_uuid("bar", "alpaca", feed, symbol, "1Min", event_time),
            timeframe="1Min",
            available_from=event_time + timedelta(minutes=1),
            open=Decimal(str(item["o"])),
            high=Decimal(str(item["h"])),
            low=Decimal(str(item["l"])),
            close=Decimal(str(item["c"])),
            volume=int(item["v"]),
            trade_count=int(item["n"]) if item.get("n") is not None else None,
            vwap=Decimal(str(item["vw"])) if item.get("vw") is not None else None,
            **common,
        )
    if event_type == "t":
        return StockTrade(
            trade_id=stable_uuid("trade", "alpaca", feed, symbol, item["i"]),
            provider_trade_id=str(item["i"]),
            available_from=received_at,
            price=Decimal(str(item["p"])),
            size=int(item["s"]),
            exchange=str(item.get("x", "")),
            conditions=tuple(str(value) for value in item.get("c", ())),
            tape=str(item["z"]) if item.get("z") is not None else None,
            **common,
        )
    if event_type == "q":
        fingerprint_source = json.dumps(item, sort_keys=True, separators=(",", ":"))
        return StockQuote(
            quote_id=stable_uuid("quote", "alpaca", feed, fingerprint_source),
            quote_fingerprint=hashlib.sha256(fingerprint_source.encode()).hexdigest(),
            available_from=received_at,
            bid_exchange=str(item.get("bx", "")),
            bid_price=Decimal(str(item["bp"])),
            bid_size=int(item["bs"]),
            ask_exchange=str(item.get("ax", "")),
            ask_price=Decimal(str(item["ap"])),
            ask_size=int(item["as"]),
            conditions=tuple(str(value) for value in item.get("c", ())),
            tape=str(item["z"]) if item.get("z") is not None else None,
            **common,
        )
    return None


class AlpacaStockStream:
    """Authenticated, read-only SIP/IEX market-data WebSocket client."""

    def __init__(
        self,
        *,
        api_key: str,
        api_secret: str,
        feed: str,
        base_url: str = "wss://stream.data.alpaca.markets/v2",
        max_reconnect_attempts: int = 3,
    ) -> None:
        if not api_key or not api_secret:
            raise AlpacaStreamError("Alpaca API key and secret are required")
        self.api_key = api_key
        self.api_secret = api_secret
        self.feed = feed
        self.url = f"{base_url.rstrip('/')}/{feed}"
        self.max_reconnect_attempts = max_reconnect_attempts

    async def probe(self, symbols: Iterable[str] = ("AAPL",)) -> dict[str, Any]:
        normalized = self._symbols(symbols)
        last_error: Exception | None = None
        for attempt in range(self.max_reconnect_attempts + 1):
            try:
                async with websockets.connect(
                    self.url,
                    open_timeout=20,
                    close_timeout=5,
                    ping_timeout=20,
                ) as socket:
                    await self._authenticate(socket)
                    await socket.send(json.dumps({"action": "subscribe", "bars": normalized}))
                    response = self._decode(await socket.recv())
                    self._raise_for_error(response)
                    subscribed = next(
                        (message for message in response if message.get("T") == "subscription"),
                        {},
                    )
                    return {
                        "authenticated": True,
                        "feed": self.feed,
                        "bars": subscribed.get("bars", []),
                    }
            except Exception as exc:
                last_error = exc
                if attempt < self.max_reconnect_attempts:
                    await asyncio.sleep(0.5 * (2**attempt))
                    continue
        raise AlpacaStreamError(
            "Unable to authenticate Alpaca stock stream after retries"
        ) from last_error

    async def frames(
        self,
        symbols: Iterable[str],
        channels: Iterable[str] = ("trades", "quotes", "bars"),
    ) -> AsyncIterator[list[dict[str, Any]]]:
        normalized = self._symbols(symbols)
        subscriptions = self._subscriptions(channels, normalized)
        attempts = 0
        while attempts <= self.max_reconnect_attempts:
            delivered_market_data = False
            try:
                async with websockets.connect(
                    self.url,
                    open_timeout=20,
                    close_timeout=5,
                    ping_interval=20,
                    ping_timeout=20,
                ) as socket:
                    await self._authenticate(socket)
                    await socket.send(json.dumps({"action": "subscribe", **subscriptions}))
                    subscription = self._decode(await socket.recv())
                    self._raise_for_error(subscription)
                    async for raw_message in socket:
                        messages = self._decode(raw_message)
                        self._raise_for_error(messages)
                        market_messages = [
                            message
                            for message in messages
                            if message.get("T") in {"t", "q", "b"}
                        ]
                        if market_messages:
                            delivered_market_data = True
                            attempts = 0
                            yield market_messages
                    raise AlpacaStreamError("Alpaca stock stream closed")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                attempts = 1 if delivered_market_data else attempts + 1
                if attempts > self.max_reconnect_attempts:
                    raise AlpacaStreamError(
                        "Alpaca stock stream exhausted reconnect attempts"
                    ) from exc
                await asyncio.sleep(0.5 * (2 ** (attempts - 1)))

    async def _authenticate(self, socket: Any) -> None:
        connected = self._decode(await socket.recv())
        self._require_success(connected, "connected")
        await socket.send(
            json.dumps(
                {"action": "auth", "key": self.api_key, "secret": self.api_secret}
            )
        )
        authenticated = self._decode(await socket.recv())
        self._require_success(authenticated, "authenticated")

    @staticmethod
    def _symbols(symbols: Iterable[str]) -> list[str]:
        normalized = sorted({symbol.strip().upper() for symbol in symbols if symbol.strip()})
        if not normalized:
            raise ValueError("At least one symbol is required")
        return normalized

    @staticmethod
    def _subscriptions(
        channels: Iterable[str], symbols: list[str]
    ) -> dict[str, list[str]]:
        allowed = {"trades", "quotes", "bars"}
        normalized = {channel.strip().lower() for channel in channels if channel.strip()}
        unsupported = normalized - allowed
        if unsupported:
            raise ValueError(
                f"Unsupported stream channels: {', '.join(sorted(unsupported))}"
            )
        if not normalized:
            raise ValueError("At least one stream channel is required")
        return {channel: symbols for channel in sorted(normalized)}

    @staticmethod
    def _decode(raw_message: str | bytes) -> list[dict[str, Any]]:
        decoded = json.loads(raw_message)
        if not isinstance(decoded, list) or not all(isinstance(item, dict) for item in decoded):
            raise AlpacaStreamError("Unexpected Alpaca stream payload")
        return decoded

    @staticmethod
    def _raise_for_error(messages: list[dict[str, Any]]) -> None:
        error = next((item for item in messages if item.get("T") == "error"), None)
        if error:
            raise AlpacaStreamError(str(error.get("msg", "unknown stream error")))

    @classmethod
    def _require_success(cls, messages: list[dict[str, Any]], expected: str) -> None:
        cls._raise_for_error(messages)
        if not any(item.get("T") == "success" and item.get("msg") == expected for item in messages):
            raise AlpacaStreamError(f"Expected Alpaca stream status: {expected}")
