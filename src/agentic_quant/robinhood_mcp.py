from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
import hashlib
import json
import secrets
from typing import Any
from urllib.parse import urlencode, urlparse

from cryptography.fernet import Fernet, InvalidToken
import httpx
from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.engine import Engine

from agentic_quant.database import (
    robinhood_mcp_calls,
    robinhood_mcp_connections,
    robinhood_oauth_flows,
)
from agentic_quant.domain import EventEnvelope
from agentic_quant.ids import uuid7
from agentic_quant.ledger import EventLedger


ROBINHOOD_MCP_SERVER_URL = "https://agent.robinhood.com/mcp/trading"
ROBINHOOD_OAUTH_METADATA_URL = (
    "https://agent.robinhood.com/.well-known/oauth-authorization-server/mcp/trading"
)
ROBINHOOD_OAUTH_RESOURCE_METADATA_URL = (
    "https://agent.robinhood.com/.well-known/oauth-protected-resource/mcp/trading"
)
ROBINHOOD_PROVIDER = "robinhood_agentic"
MCP_PROTOCOL_VERSION = "2025-06-18"
READ_ONLY_TOOLS = {
    "get_accounts",
    "get_portfolio",
    "get_realized_pnl",
    "get_pnl_trade_history",
    "get_equity_positions",
    "get_equity_tax_lots",
    "get_equity_quotes",
    "get_equity_orders",
    "get_equity_tradability",
    "get_watchlists",
    "get_watchlist_items",
}
PREVIEW_TOOLS = {"review_equity_order"}
MUTATING_TOOLS = {"place_equity_order", "cancel_equity_order"}


class RobinhoodMCPError(RuntimeError):
    pass


class RobinhoodMCPConfigurationError(RobinhoodMCPError):
    pass


class RobinhoodMCPAuthorizationError(RobinhoodMCPError):
    pass


class RobinhoodMCPProtocolError(RobinhoodMCPError):
    pass


def require_robinhood_mcp_endpoint(value: str) -> str:
    normalized = value.rstrip("/")
    parsed = urlparse(normalized)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "agent.robinhood.com"
        or parsed.path != "/mcp/trading"
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise RobinhoodMCPConfigurationError(
            "Robinhood MCP is hard-pinned to "
            "https://agent.robinhood.com/mcp/trading"
        )
    return ROBINHOOD_MCP_SERVER_URL


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _canonical_hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _safe_json(value: Any) -> Any:
    blocked_fragments = (
        "access_token",
        "refresh_token",
        "authorization",
        "account_number",
        "client_secret",
        "code_verifier",
    )
    if isinstance(value, dict):
        return {
            str(key): (
                "[REDACTED]"
                if any(fragment in str(key).lower() for fragment in blocked_fragments)
                else _safe_json(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_safe_json(item) for item in value]
    return value


class TokenCipher:
    def __init__(self, key: str) -> None:
        try:
            self._fernet = Fernet(key.encode())
        except (TypeError, ValueError) as exc:
            raise RobinhoodMCPConfigurationError(
                "ROBINHOOD_TOKEN_ENCRYPTION_KEY must be a Fernet key"
            ) from exc

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode()).decode()

    def decrypt(self, value: str) -> str:
        try:
            return self._fernet.decrypt(value.encode()).decode()
        except InvalidToken as exc:
            raise RobinhoodMCPAuthorizationError(
                "Stored Robinhood authorization cannot be decrypted"
            ) from exc


class RobinhoodMCPBridge:
    """OAuth-backed remote MCP client with a permanently dormant live-order gate."""

    def __init__(
        self,
        engine: Engine,
        *,
        enabled: bool,
        server_url: str = ROBINHOOD_MCP_SERVER_URL,
        redirect_uri: str | None = None,
        encryption_key: str | None = None,
        order_submission_enabled: bool = False,
        ledger: EventLedger | None = None,
        client: httpx.AsyncClient | None = None,
        now_provider: Any | None = None,
    ) -> None:
        self.engine = engine
        self.enabled = enabled
        self.server_url = require_robinhood_mcp_endpoint(server_url)
        self.redirect_uri = redirect_uri
        self.order_submission_enabled = order_submission_enabled
        self.ledger = ledger
        self._cipher = TokenCipher(encryption_key) if encryption_key else None
        self._client = client
        self._owns_client = client is None
        self._now_provider = now_provider or (lambda: datetime.now(UTC))

    def _now(self) -> datetime:
        value = self._now_provider()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ValueError("Robinhood bridge clock must be timezone-aware")
        return value.astimezone(UTC)

    def _require_configured(self) -> TokenCipher:
        if not self.enabled:
            raise RobinhoodMCPConfigurationError("Robinhood MCP bridge is disabled")
        if not self.redirect_uri:
            raise RobinhoodMCPConfigurationError(
                "Robinhood OAuth redirect URI is not configured"
            )
        if self._cipher is None:
            raise RobinhoodMCPConfigurationError(
                "Robinhood token encryption key is not configured"
            )
        return self._cipher

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(30.0, connect=10.0),
                follow_redirects=False,
            )
        return self._client

    def status(self) -> dict[str, Any]:
        with self.engine.connect() as connection:
            row = connection.execute(
                select(robinhood_mcp_connections).where(
                    robinhood_mcp_connections.c.provider == ROBINHOOD_PROVIDER
                )
            ).one_or_none()
            call_count = connection.execute(
                select(func.count()).select_from(robinhood_mcp_calls)
            ).scalar_one()
            failed_count = connection.execute(
                select(func.count())
                .select_from(robinhood_mcp_calls)
                .where(robinhood_mcp_calls.c.status == "FAILED")
            ).scalar_one()
        connection_value = None
        if row is not None:
            item = dict(row._mapping)
            connection_value = {
                "connection_id": item["connection_id"],
                "status": item["status"],
                "scope": item["scope"],
                "expires_at": _utc(item["expires_at"]),
                "connected_by": item["connected_by"],
                "created_at": _utc(item["created_at"]),
                "updated_at": _utc(item["updated_at"]),
                "disconnected_at": _utc(item["disconnected_at"]),
            }
        return {
            "enabled": self.enabled,
            "configured": bool(self.redirect_uri and self._cipher),
            "server_url": self.server_url,
            "connected": bool(
                connection_value and connection_value["status"] == "CONNECTED"
            ),
            "connection": connection_value,
            "read_only_tools_enabled": self.enabled,
            "order_preview_enabled": self.enabled,
            "order_submission_compiled": True,
            "order_submission_enabled": self.order_submission_enabled,
            "live_money_enabled": False,
            "blocking_reasons": (
                []
                if self.order_submission_enabled
                else [
                    "current operating contract prohibits live-money submission",
                    "Robinhood idempotency and reconciliation contract is not certified",
                    "no Robinhood strategy enrollment is implemented",
                ]
            ),
            "audited_calls": int(call_count),
            "failed_calls": int(failed_count),
        }

    async def begin_oauth(self, *, requested_by: str, reason: str) -> dict[str, Any]:
        cipher = self._require_configured()
        assert self.redirect_uri is not None
        metadata = await self._oauth_metadata()
        registration_endpoint = str(metadata["registration_endpoint"])
        response = await self._http().post(
            registration_endpoint,
            json={
                "client_name": "QAgent Robinhood MCP Bridge",
                "redirect_uris": [self.redirect_uri],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
            },
        )
        self._require_success(response, "dynamic client registration")
        registration = self._json_object(response, "dynamic client registration")
        client_id = str(registration.get("client_id") or "")
        if not client_id:
            raise RobinhoodMCPProtocolError(
                "Robinhood client registration omitted client_id"
            )
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()
        ).decode().rstrip("=")
        now = self._now()
        flow_id = uuid7()
        with self.engine.begin() as connection:
            connection.execute(
                delete(robinhood_oauth_flows).where(
                    robinhood_oauth_flows.c.expires_at <= now
                )
            )
            connection.execute(
                insert(robinhood_oauth_flows).values(
                    oauth_flow_id=flow_id,
                    state_sha256=_sha256_text(state),
                    code_verifier_ciphertext=cipher.encrypt(verifier),
                    client_id=client_id,
                    redirect_uri=self.redirect_uri,
                    requested_by=requested_by[:80],
                    reason=reason.strip()[:2_000],
                    created_at=now,
                    expires_at=now + timedelta(minutes=15),
                    consumed_at=None,
                )
            )
        self._emit(
            "broker.robinhood.oauth.started.v1",
            flow_id,
            {
                "provider": ROBINHOOD_PROVIDER,
                "requested_by": requested_by[:80],
                "expires_at": (now + timedelta(minutes=15)).isoformat(),
            },
        )
        query = urlencode(
            {
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": self.redirect_uri,
                "scope": "internal",
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "resource": self.server_url,
            }
        )
        return {
            "authorization_url": f"{metadata['authorization_endpoint']}?{query}",
            "expires_at": now + timedelta(minutes=15),
        }

    async def complete_oauth(self, *, state: str, code: str) -> dict[str, Any]:
        cipher = self._require_configured()
        now = self._now()
        state_sha256 = _sha256_text(state)
        with self.engine.begin() as connection:
            row = connection.execute(
                select(robinhood_oauth_flows).where(
                    robinhood_oauth_flows.c.state_sha256 == state_sha256
                )
            ).one_or_none()
            if row is None:
                raise RobinhoodMCPAuthorizationError("OAuth state is unknown")
            item = dict(row._mapping)
            expires_at = _utc(item["expires_at"])
            if item["consumed_at"] is not None or expires_at is None or expires_at <= now:
                raise RobinhoodMCPAuthorizationError("OAuth state is expired or already used")
            connection.execute(
                update(robinhood_oauth_flows)
                .where(
                    (robinhood_oauth_flows.c.oauth_flow_id == item["oauth_flow_id"])
                    & robinhood_oauth_flows.c.consumed_at.is_(None)
                )
                .values(consumed_at=now)
            )
        metadata = await self._oauth_metadata()
        response = await self._http().post(
            str(metadata["token_endpoint"]),
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": str(item["client_id"]),
                "redirect_uri": str(item["redirect_uri"]),
                "code_verifier": cipher.decrypt(str(item["code_verifier_ciphertext"])),
                "resource": self.server_url,
            },
        )
        self._require_success(response, "authorization code exchange")
        token = self._json_object(response, "authorization code exchange")
        access_token = str(token.get("access_token") or "")
        if not access_token:
            raise RobinhoodMCPProtocolError("Robinhood token response omitted access_token")
        expires_at = self._token_expiry(token, now)
        values = {
            "client_id": str(item["client_id"]),
            "redirect_uri": str(item["redirect_uri"]),
            "access_token_ciphertext": cipher.encrypt(access_token),
            "refresh_token_ciphertext": (
                cipher.encrypt(str(token["refresh_token"]))
                if token.get("refresh_token")
                else None
            ),
            "token_type": str(token.get("token_type") or "Bearer")[:40],
            "scope": str(token.get("scope") or "internal")[:240],
            "expires_at": expires_at,
            "status": "CONNECTED",
            "connected_by": str(item["requested_by"])[:80],
            "updated_at": now,
            "disconnected_at": None,
        }
        with self.engine.begin() as connection:
            existing = connection.execute(
                select(robinhood_mcp_connections.c.connection_id).where(
                    robinhood_mcp_connections.c.provider == ROBINHOOD_PROVIDER
                )
            ).scalar_one_or_none()
            if existing is None:
                connection_id = uuid7()
                connection.execute(
                    insert(robinhood_mcp_connections).values(
                        connection_id=connection_id,
                        provider=ROBINHOOD_PROVIDER,
                        created_at=now,
                        **values,
                    )
                )
            else:
                connection_id = str(existing)
                connection.execute(
                    update(robinhood_mcp_connections)
                    .where(robinhood_mcp_connections.c.connection_id == existing)
                    .values(**values)
                )
        self._emit(
            "broker.robinhood.connected.v1",
            connection_id,
            {
                "provider": ROBINHOOD_PROVIDER,
                "scope": values["scope"],
                "connected_by": values["connected_by"],
                "expires_at": expires_at.isoformat() if expires_at else None,
                "order_submission_enabled": False,
            },
        )
        return self.status()

    def disconnect(self, *, disconnected_by: str) -> dict[str, Any]:
        now = self._now()
        with self.engine.begin() as connection:
            connection_id = connection.execute(
                select(robinhood_mcp_connections.c.connection_id).where(
                    robinhood_mcp_connections.c.provider == ROBINHOOD_PROVIDER
                )
            ).scalar_one_or_none()
            connection.execute(
                update(robinhood_mcp_connections)
                .where(robinhood_mcp_connections.c.provider == ROBINHOOD_PROVIDER)
                .values(
                    status="DISCONNECTED",
                    access_token_ciphertext="",
                    refresh_token_ciphertext=None,
                    expires_at=None,
                    updated_at=now,
                    disconnected_at=now,
                )
            )
        if connection_id is not None:
            self._emit(
                "broker.robinhood.disconnected.v1",
                str(connection_id),
                {
                    "provider": ROBINHOOD_PROVIDER,
                    "disconnected_by": disconnected_by[:80],
                    "local_tokens_erased": True,
                },
            )
        return self.status()

    async def list_tools(self) -> tuple[dict[str, Any], ...]:
        token, connection_id = await self._access_token()
        result = await self._mcp_request(
            token=token,
            method="tools/list",
            params={},
        )
        tools = result.get("tools")
        if not isinstance(tools, list):
            raise RobinhoodMCPProtocolError("Robinhood tools/list omitted tools")
        normalized = tuple(
            {str(key): value for key, value in tool.items()}
            for tool in tools
            if isinstance(tool, dict)
        )
        self._record_call(
            connection_id=connection_id,
            tool_name="tools/list",
            arguments={},
            response={"tool_names": [item.get("name") for item in normalized]},
        )
        return normalized

    async def call_read_tool(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        if tool_name not in READ_ONLY_TOOLS:
            raise RobinhoodMCPConfigurationError("Tool is not in the read-only allowlist")
        return await self._call_audited(tool_name=tool_name, arguments=arguments)

    async def review_equity_order(self, *, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self._call_audited(
            tool_name="review_equity_order",
            arguments=arguments,
        )

    async def agentic_portfolio(self) -> dict[str, Any]:
        accounts_result = await self.call_read_tool(
            tool_name="get_accounts",
            arguments={},
        )
        accounts: list[dict[str, Any]] = []
        for document in self._text_json_documents(accounts_result):
            data = document.get("data")
            if not isinstance(data, dict):
                continue
            values = data.get("accounts")
            if isinstance(values, list):
                accounts.extend(item for item in values if isinstance(item, dict))
        eligible = [item for item in accounts if item.get("agentic_allowed") is True]
        if len(eligible) != 1:
            raise RobinhoodMCPProtocolError(
                "Expected exactly one Robinhood account accessible to this agent"
            )
        account_number = eligible[0].get("account_number")
        if not isinstance(account_number, str) or not account_number:
            raise RobinhoodMCPProtocolError(
                "Agentic Robinhood account omitted its account number"
            )
        portfolio_result = await self.call_read_tool(
            tool_name="get_portfolio",
            arguments={"account_number": account_number},
        )
        account = {
            key: value
            for key, value in eligible[0].items()
            if key not in {"account_number", "rhc_account_number", "rhs_account_number"}
        }
        account["account_number_last_four"] = account_number[-4:]
        return {
            "account": account,
            "portfolio": self._text_json_documents(portfolio_result),
        }

    async def watchlists(self) -> dict[str, Any]:
        watchlists_result = await self.call_read_tool(
            tool_name="get_watchlists",
            arguments={},
        )
        watchlists: list[dict[str, Any]] = []
        for document in self._text_json_documents(watchlists_result):
            data = document.get("data")
            if not isinstance(data, dict):
                continue
            values = data.get("watchlists")
            if isinstance(values, list):
                watchlists.extend(item for item in values if isinstance(item, dict))
        summaries: list[dict[str, Any]] = []
        for item in watchlists:
            list_id = item.get("id")
            if not isinstance(list_id, str) or not list_id:
                continue
            items_result = await self.call_read_tool(
                tool_name="get_watchlist_items",
                arguments={"list_id": list_id},
            )
            symbols: list[str] = []
            self._collect_symbols(self._text_json_documents(items_result), symbols)
            summaries.append(
                {
                    "display_name": item.get("display_name"),
                    "owner_type": item.get("owner_type"),
                    "reported_item_count": item.get("item_count"),
                    "symbols": list(dict.fromkeys(symbols)),
                }
            )
        return {"watchlists": summaries}

    async def place_equity_order(self, *, arguments: dict[str, Any]) -> dict[str, Any]:
        if not self.order_submission_enabled:
            raise RobinhoodMCPConfigurationError(
                "Robinhood order submission is compiled but hard-disabled"
            )
        return await self._call_audited(
            tool_name="place_equity_order",
            arguments=arguments,
        )

    async def cancel_equity_order(self, *, arguments: dict[str, Any]) -> dict[str, Any]:
        if not self.order_submission_enabled:
            raise RobinhoodMCPConfigurationError(
                "Robinhood order cancellation is compiled but hard-disabled"
            )
        return await self._call_audited(
            tool_name="cancel_equity_order",
            arguments=arguments,
        )

    async def _call_audited(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        token, connection_id = await self._access_token()
        started_at = self._now()
        call_id = uuid7()
        safe_arguments = _safe_json(arguments)
        with self.engine.begin() as connection:
            connection.execute(
                insert(robinhood_mcp_calls).values(
                    mcp_call_id=call_id,
                    connection_id=connection_id,
                    tool_name=tool_name,
                    arguments_sha256=_canonical_hash(arguments),
                    arguments_json=safe_arguments,
                    response_sha256=None,
                    response_json=None,
                    status="RUNNING",
                    error_code=None,
                    started_at=started_at,
                    finished_at=None,
                )
            )
        try:
            result = await self._mcp_request(
                token=token,
                method="tools/call",
                params={"name": tool_name, "arguments": arguments},
            )
        except Exception as exc:
            with self.engine.begin() as connection:
                connection.execute(
                    update(robinhood_mcp_calls)
                    .where(robinhood_mcp_calls.c.mcp_call_id == call_id)
                    .values(
                        status="FAILED",
                        error_code=type(exc).__name__[:120],
                        finished_at=self._now(),
                    )
                )
            raise
        safe_response = {
            "is_error": bool(result.get("isError", False)),
            "content_types": [
                str(item.get("type"))
                for item in result.get("content", [])
                if isinstance(item, dict)
            ],
        }
        with self.engine.begin() as connection:
            connection.execute(
                update(robinhood_mcp_calls)
                .where(robinhood_mcp_calls.c.mcp_call_id == call_id)
                .values(
                    response_sha256=_canonical_hash(result),
                    response_json=safe_response,
                    status="COMPLETED",
                    finished_at=self._now(),
                )
            )
        return result

    def _record_call(
        self,
        *,
        connection_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        response: dict[str, Any],
    ) -> None:
        now = self._now()
        with self.engine.begin() as connection:
            connection.execute(
                insert(robinhood_mcp_calls).values(
                    mcp_call_id=uuid7(),
                    connection_id=connection_id,
                    tool_name=tool_name,
                    arguments_sha256=_canonical_hash(arguments),
                    arguments_json=_safe_json(arguments),
                    response_sha256=_canonical_hash(response),
                    response_json=_safe_json(response),
                    status="COMPLETED",
                    error_code=None,
                    started_at=now,
                    finished_at=now,
                )
            )

    async def _oauth_metadata(self) -> dict[str, Any]:
        response = await self._http().get(ROBINHOOD_OAUTH_METADATA_URL)
        self._require_success(response, "OAuth metadata")
        metadata = self._json_object(response, "OAuth metadata")
        expected = {
            "authorization_endpoint": "https://robinhood.com/oauth",
            "registration_endpoint": "https://agent.robinhood.com/oauth/trading/register",
            "token_endpoint": "https://api.robinhood.com/oauth2/token/",
        }
        for key, expected_value in expected.items():
            if metadata.get(key) != expected_value:
                raise RobinhoodMCPProtocolError(
                    f"Robinhood OAuth metadata changed unexpectedly: {key}"
                )
        return metadata

    async def _access_token(self) -> tuple[str, str]:
        cipher = self._require_configured()
        with self.engine.connect() as connection:
            row = connection.execute(
                select(robinhood_mcp_connections).where(
                    (robinhood_mcp_connections.c.provider == ROBINHOOD_PROVIDER)
                    & (robinhood_mcp_connections.c.status == "CONNECTED")
                )
            ).one_or_none()
        if row is None:
            raise RobinhoodMCPAuthorizationError("Robinhood MCP is not connected")
        item = dict(row._mapping)
        expires_at = _utc(item["expires_at"])
        if expires_at is not None and expires_at <= self._now() + timedelta(seconds=60):
            item = await self._refresh_token(item)
        return (
            cipher.decrypt(str(item["access_token_ciphertext"])),
            str(item["connection_id"]),
        )

    async def _refresh_token(self, item: dict[str, Any]) -> dict[str, Any]:
        cipher = self._require_configured()
        encrypted_refresh = item.get("refresh_token_ciphertext")
        if not encrypted_refresh:
            raise RobinhoodMCPAuthorizationError(
                "Robinhood access token expired without a refresh token"
            )
        metadata = await self._oauth_metadata()
        response = await self._http().post(
            str(metadata["token_endpoint"]),
            data={
                "grant_type": "refresh_token",
                "refresh_token": cipher.decrypt(str(encrypted_refresh)),
                "client_id": str(item["client_id"]),
                "resource": self.server_url,
            },
        )
        self._require_success(response, "refresh token exchange")
        token = self._json_object(response, "refresh token exchange")
        access_token = str(token.get("access_token") or "")
        if not access_token:
            raise RobinhoodMCPProtocolError("Robinhood refresh omitted access_token")
        now = self._now()
        updated = {
            **item,
            "access_token_ciphertext": cipher.encrypt(access_token),
            "refresh_token_ciphertext": (
                cipher.encrypt(str(token["refresh_token"]))
                if token.get("refresh_token")
                else item["refresh_token_ciphertext"]
            ),
            "expires_at": self._token_expiry(token, now),
            "scope": str(token.get("scope") or item.get("scope") or "internal")[:240],
            "updated_at": now,
        }
        with self.engine.begin() as connection:
            connection.execute(
                update(robinhood_mcp_connections)
                .where(
                    robinhood_mcp_connections.c.connection_id
                    == item["connection_id"]
                )
                .values(
                    access_token_ciphertext=updated["access_token_ciphertext"],
                    refresh_token_ciphertext=updated["refresh_token_ciphertext"],
                    expires_at=updated["expires_at"],
                    scope=updated["scope"],
                    updated_at=now,
                )
            )
        self._emit(
            "broker.robinhood.token_refreshed.v1",
            str(item["connection_id"]),
            {
                "provider": ROBINHOOD_PROVIDER,
                "expires_at": (
                    updated["expires_at"].isoformat()
                    if updated["expires_at"] is not None
                    else None
                ),
            },
        )
        return updated

    def _emit(
        self,
        event_type: str,
        correlation_id: str,
        payload: dict[str, Any],
    ) -> None:
        if self.ledger is None:
            return
        now = self._now()
        self.ledger.append(
            EventEnvelope(
                event_id=uuid7(),
                event_type=event_type,
                event_time=now,
                emitted_at=now,
                producer="robinhood-mcp-bridge",
                correlation_id=correlation_id,
                payload=payload,
            )
        )

    async def _mcp_request(
        self,
        *,
        token: str,
        method: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
        }
        initialize = await self._http().post(
            self.server_url,
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": "initialize",
                "method": "initialize",
                "params": {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "qagent", "version": "0.1.0"},
                },
            },
        )
        self._require_success(initialize, "MCP initialize")
        initialized = self._decode_mcp(initialize, "MCP initialize")
        if initialized.get("error"):
            raise RobinhoodMCPProtocolError("Robinhood MCP initialize returned an error")
        session_id = initialize.headers.get("Mcp-Session-Id")
        if session_id:
            headers["Mcp-Session-Id"] = session_id
        notification = await self._http().post(
            self.server_url,
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
                "params": {},
            },
        )
        self._require_success(notification, "MCP initialized notification")
        response = await self._http().post(
            self.server_url,
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": "qagent-call",
                "method": method,
                "params": params,
            },
        )
        self._require_success(response, f"MCP {method}")
        envelope = self._decode_mcp(response, f"MCP {method}")
        if envelope.get("error"):
            raise RobinhoodMCPProtocolError(
                f"Robinhood MCP {method} returned an error"
            )
        result = envelope.get("result")
        if not isinstance(result, dict):
            raise RobinhoodMCPProtocolError(
                f"Robinhood MCP {method} omitted an object result"
            )
        return {str(key): value for key, value in result.items()}

    @staticmethod
    def _decode_mcp(response: httpx.Response, operation: str) -> dict[str, Any]:
        if not response.content:
            return {}
        content_type = response.headers.get("content-type", "")
        try:
            if "text/event-stream" in content_type:
                events = [
                    json.loads(line[5:].strip())
                    for line in response.text.splitlines()
                    if line.startswith("data:") and line[5:].strip()
                ]
                payload = events[-1] if events else None
            else:
                payload = response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise RobinhoodMCPProtocolError(
                f"{operation} returned invalid JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise RobinhoodMCPProtocolError(
                f"{operation} returned a non-object envelope"
            )
        return {str(key): value for key, value in payload.items()}

    @staticmethod
    def _text_json_documents(result: dict[str, Any]) -> list[dict[str, Any]]:
        documents: list[dict[str, Any]] = []
        content = result.get("content")
        if not isinstance(content, list):
            return documents
        for item in content:
            if not isinstance(item, dict) or item.get("type") != "text":
                continue
            try:
                value = json.loads(str(item.get("text") or ""))
            except json.JSONDecodeError as exc:
                raise RobinhoodMCPProtocolError(
                    "Robinhood read tool returned invalid JSON text"
                ) from exc
            if not isinstance(value, dict):
                raise RobinhoodMCPProtocolError(
                    "Robinhood read tool returned a non-object JSON document"
                )
            documents.append({str(key): entry for key, entry in value.items()})
        return documents

    @staticmethod
    def _collect_symbols(node: Any, destination: list[str]) -> None:
        if isinstance(node, dict):
            symbol = node.get("symbol")
            if isinstance(symbol, str) and symbol:
                destination.append(symbol)
            for value in node.values():
                RobinhoodMCPBridge._collect_symbols(value, destination)
        elif isinstance(node, list):
            for value in node:
                RobinhoodMCPBridge._collect_symbols(value, destination)

    @staticmethod
    def _json_object(response: httpx.Response, operation: str) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise RobinhoodMCPProtocolError(
                f"{operation} returned invalid JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise RobinhoodMCPProtocolError(
                f"{operation} returned a non-object response"
            )
        return {str(key): value for key, value in payload.items()}

    @staticmethod
    def _require_success(response: httpx.Response, operation: str) -> None:
        if response.is_error:
            detail = response.text[:300].replace("\n", " ")
            raise RobinhoodMCPProtocolError(
                f"{operation} failed with HTTP {response.status_code}: {detail}"
            )

    @staticmethod
    def _token_expiry(token: dict[str, Any], now: datetime) -> datetime | None:
        raw = token.get("expires_in")
        if raw is None:
            return None
        try:
            return now + timedelta(seconds=max(0, int(raw)))
        except (TypeError, ValueError) as exc:
            raise RobinhoodMCPProtocolError(
                "Robinhood token response has invalid expires_in"
            ) from exc
