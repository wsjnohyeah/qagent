from __future__ import annotations

import asyncio
from collections.abc import Mapping
from decimal import Decimal
from typing import Any
from urllib.parse import urlparse

import httpx

from agentic_quant.risk import (
    normalize_deployable_long_prices,
)


ALPACA_PAPER_BASE_URL = "https://paper-api.alpaca.markets"
QueryValue = str | int | float | bool | None


def normalize_long_bracket_prices(
    *,
    entry_limit_price: Decimal,
    take_profit_price: Decimal,
    stop_loss_price: Decimal,
) -> tuple[Decimal, Decimal, Decimal]:
    return normalize_deployable_long_prices(
        entry_limit_price=entry_limit_price,
        take_profit_price=take_profit_price,
        stop_loss_price=stop_loss_price,
    )


class AlpacaPaperConfigurationError(RuntimeError):
    pass


class AlpacaPaperResponseError(RuntimeError):
    def __init__(self, *, status_code: int, endpoint: str, detail: str) -> None:
        super().__init__(
            f"Alpaca paper request failed ({status_code}) at {endpoint}: {detail}"
        )
        self.status_code = status_code
        self.endpoint = endpoint
        self.detail = detail


def require_paper_endpoint(base_url: str) -> str:
    normalized = base_url.rstrip("/")
    parsed = urlparse(normalized)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "paper-api.alpaca.markets"
        or parsed.path not in {"", "/"}
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise AlpacaPaperConfigurationError(
            "Paper trading is hard-pinned to https://paper-api.alpaca.markets"
        )
    return ALPACA_PAPER_BASE_URL


def _json_object(response: httpx.Response, endpoint: str) -> dict[str, Any]:
    payload = response.json()
    if not isinstance(payload, dict):
        raise AlpacaPaperResponseError(
            status_code=response.status_code,
            endpoint=endpoint,
            detail="response was not a JSON object",
        )
    return {str(key): value for key, value in payload.items()}


class AlpacaPaperTradingProvider:
    """Narrow adapter permanently restricted to Alpaca's paper-trading host."""

    name = "alpaca_paper"

    def __init__(
        self,
        *,
        api_key: str,
        api_secret: str,
        base_url: str = ALPACA_PAPER_BASE_URL,
        client: httpx.AsyncClient | None = None,
        max_retries: int = 3,
        retry_base_seconds: float = 0.5,
    ) -> None:
        if not api_key or not api_secret:
            raise AlpacaPaperConfigurationError(
                "Alpaca API key and secret are required for paper trading"
            )
        verified_url = require_paper_endpoint(base_url)
        self._headers = {
            "APCA-API-KEY-ID": api_key,
            "APCA-API-SECRET-KEY": api_secret,
        }
        self._owns_client = client is None
        self._max_retries = max_retries
        self._retry_base_seconds = retry_base_seconds
        self._client = client or httpx.AsyncClient(
            base_url=verified_url,
            timeout=httpx.Timeout(30.0, connect=10.0),
            headers=self._headers,
        )

    async def __aenter__(self) -> AlpacaPaperTradingProvider:
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _request(
        self,
        method: str,
        endpoint: str,
        *,
        params: Mapping[str, QueryValue] | None = None,
        json_body: Mapping[str, object] | None = None,
        retry_safe: bool,
    ) -> httpx.Response:
        attempts = self._max_retries + 1 if retry_safe else 1
        last_error: httpx.RequestError | None = None
        for attempt in range(attempts):
            try:
                response = await self._client.request(
                    method,
                    endpoint,
                    params=params,
                    json=json_body,
                    headers=self._headers,
                )
            except httpx.RequestError as exc:
                last_error = exc
                if retry_safe and attempt + 1 < attempts:
                    await asyncio.sleep(self._retry_base_seconds * (2**attempt))
                    continue
                raise AlpacaPaperResponseError(
                    status_code=504,
                    endpoint=endpoint,
                    detail="upstream transport outcome is unknown",
                ) from exc
            if (
                retry_safe
                and (response.status_code == 429 or response.status_code >= 500)
                and attempt + 1 < attempts
            ):
                await asyncio.sleep(self._retry_base_seconds * (2**attempt))
                continue
            return response
        raise AlpacaPaperResponseError(
            status_code=504,
            endpoint=endpoint,
            detail="upstream transport failure after retries",
        ) from last_error

    @staticmethod
    def _raise_for_error(response: httpx.Response, endpoint: str) -> None:
        if response.is_error:
            detail = response.text[:500].replace("\n", " ")
            raise AlpacaPaperResponseError(
                status_code=response.status_code,
                endpoint=endpoint,
                detail=detail,
            )

    async def fetch_account(self) -> dict[str, Any]:
        endpoint = "/v2/account"
        response = await self._request("GET", endpoint, retry_safe=True)
        self._raise_for_error(response, endpoint)
        return _json_object(response, endpoint)

    async def fetch_positions(self) -> tuple[dict[str, Any], ...]:
        endpoint = "/v2/positions"
        response = await self._request("GET", endpoint, retry_safe=True)
        self._raise_for_error(response, endpoint)
        payload = response.json()
        if not isinstance(payload, list):
            raise AlpacaPaperResponseError(
                status_code=response.status_code,
                endpoint=endpoint,
                detail="response was not a JSON array",
            )
        return tuple(
            {str(key): value for key, value in item.items()}
            for item in payload
            if isinstance(item, dict)
        )

    async def fetch_order_by_client_id(
        self,
        client_order_id: str,
    ) -> dict[str, Any] | None:
        endpoint = "/v2/orders:by_client_order_id"
        response = await self._request(
            "GET",
            endpoint,
            params={"client_order_id": client_order_id, "nested": "true"},
            retry_safe=True,
        )
        if response.status_code == 404:
            return None
        self._raise_for_error(response, endpoint)
        return _json_object(response, endpoint)

    async def submit_bracket_order(
        self,
        *,
        client_order_id: str,
        symbol: str,
        quantity: int,
        entry_limit_price: Decimal,
        take_profit_price: Decimal,
        stop_loss_price: Decimal,
    ) -> dict[str, Any]:
        if quantity < 1:
            raise ValueError("Paper order quantity must be positive")
        entry_limit_price, take_profit_price, stop_loss_price = (
            normalize_long_bracket_prices(
                entry_limit_price=entry_limit_price,
                take_profit_price=take_profit_price,
                stop_loss_price=stop_loss_price,
            )
        )
        existing = await self.fetch_order_by_client_id(client_order_id)
        if existing is not None:
            return existing
        endpoint = "/v2/orders"
        payload: dict[str, object] = {
            "client_order_id": client_order_id,
            "symbol": symbol.upper(),
            "qty": str(quantity),
            "side": "buy",
            "type": "limit",
            "limit_price": str(entry_limit_price),
            "time_in_force": "day",
            "order_class": "bracket",
            "take_profit": {"limit_price": str(take_profit_price)},
            "stop_loss": {"stop_price": str(stop_loss_price)},
            "extended_hours": False,
        }
        try:
            response = await self._request(
                "POST",
                endpoint,
                json_body=payload,
                retry_safe=False,
            )
        except AlpacaPaperResponseError:
            recovered = await self.fetch_order_by_client_id(client_order_id)
            if recovered is not None:
                return recovered
            raise
        if response.status_code == 422:
            recovered = await self.fetch_order_by_client_id(client_order_id)
            if recovered is not None:
                return recovered
        self._raise_for_error(response, endpoint)
        return _json_object(response, endpoint)

    async def submit_market_exit_order(
        self,
        *,
        client_order_id: str,
        symbol: str,
        quantity: Decimal,
        time_in_force: str,
    ) -> dict[str, Any]:
        if quantity <= 0:
            raise ValueError("Paper exit quantity must be positive")
        if time_in_force not in {"cls", "day"}:
            raise ValueError("Paper market exit requires cls or day time-in-force")
        existing = await self.fetch_order_by_client_id(client_order_id)
        if existing is not None:
            return existing
        endpoint = "/v2/orders"
        payload: dict[str, object] = {
            "client_order_id": client_order_id,
            "symbol": symbol.upper(),
            "qty": str(quantity),
            "side": "sell",
            "type": "market",
            "time_in_force": time_in_force,
            "extended_hours": False,
        }
        try:
            response = await self._request(
                "POST",
                endpoint,
                json_body=payload,
                retry_safe=False,
            )
        except AlpacaPaperResponseError:
            recovered = await self.fetch_order_by_client_id(client_order_id)
            if recovered is not None:
                return recovered
            raise
        if response.status_code == 422:
            recovered = await self.fetch_order_by_client_id(client_order_id)
            if recovered is not None:
                return recovered
        self._raise_for_error(response, endpoint)
        return _json_object(response, endpoint)

    async def cancel_order(self, broker_order_id: str) -> None:
        endpoint = f"/v2/orders/{broker_order_id}"
        response = await self._request("DELETE", endpoint, retry_safe=False)
        if response.status_code in {204, 404}:
            return
        self._raise_for_error(response, endpoint)
