from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import json
from urllib.parse import parse_qs, urlparse

from cryptography.fernet import Fernet
import httpx
from pydantic import SecretStr
import pytest
from sqlalchemy import select

from agentic_quant.api import create_app
from agentic_quant.config import Settings
from agentic_quant.database import (
    robinhood_mcp_calls,
    robinhood_mcp_connections,
)
from agentic_quant.ledger import EventLedger
from agentic_quant.migrations import upgrade_database
from agentic_quant.robinhood_mcp import (
    ROBINHOOD_MCP_SERVER_URL,
    RobinhoodMCPBridge,
    RobinhoodMCPConfigurationError,
    require_robinhood_mcp_endpoint,
)
from agentic_quant.robinhood_oauth_relay import (
    build_forward_url,
    require_forward_url,
)


def test_robinhood_endpoint_and_submission_are_fail_closed() -> None:
    assert require_robinhood_mcp_endpoint(ROBINHOOD_MCP_SERVER_URL + "/") == (
        ROBINHOOD_MCP_SERVER_URL
    )
    with pytest.raises(RobinhoodMCPConfigurationError):
        require_robinhood_mcp_endpoint("https://example.com/mcp/trading")
    with pytest.raises(ValueError, match="current operating contract"):
        Settings(
            _env_file=None,
            robinhood_order_submission_enabled=True,
        )
    with pytest.raises(ValueError, match="TOKEN_ENCRYPTION_KEY"):
        Settings(
            _env_file=None,
            robinhood_mcp_bridge_enabled=True,
            robinhood_oauth_redirect_uri="https://qagent.example/callback",
        )


def test_robinhood_oauth_tool_discovery_and_read_probe(
    settings: Settings,
) -> None:
    upgrade_database(settings.database_url)
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, str(request.url)))
        if str(request.url).endswith(
            "/.well-known/oauth-authorization-server/mcp/trading"
        ):
            return httpx.Response(
                200,
                json={
                    "authorization_endpoint": "https://robinhood.com/oauth",
                    "registration_endpoint": (
                        "https://agent.robinhood.com/oauth/trading/register"
                    ),
                    "token_endpoint": "https://api.robinhood.com/oauth2/token/",
                },
            )
        if str(request.url) == "https://agent.robinhood.com/oauth/trading/register":
            return httpx.Response(201, json={"client_id": "registered-client"})
        if str(request.url) == "https://api.robinhood.com/oauth2/token/":
            form = parse_qs(request.content.decode())
            assert form["code"] == ["one-time-code"]
            assert form["client_id"] == ["registered-client"]
            assert form["code_verifier"][0]
            assert form["resource"] == [ROBINHOOD_MCP_SERVER_URL]
            return httpx.Response(
                200,
                json={
                    "access_token": "sensitive-access-token",
                    "refresh_token": "sensitive-refresh-token",
                    "token_type": "Bearer",
                    "scope": "internal",
                    "expires_in": 3600,
                },
            )
        if str(request.url) == ROBINHOOD_MCP_SERVER_URL:
            payload = json.loads(request.content or b"{}")
            method = payload.get("method")
            if method == "initialize":
                return httpx.Response(
                    200,
                    headers={"Mcp-Session-Id": "test-session"},
                    json={
                        "jsonrpc": "2.0",
                        "id": "initialize",
                        "result": {
                            "protocolVersion": "2025-06-18",
                            "capabilities": {"tools": {}},
                            "serverInfo": {"name": "robinhood", "version": "test"},
                        },
                    },
                )
            if method == "notifications/initialized":
                return httpx.Response(202)
            if method == "tools/list":
                assert request.headers["Mcp-Session-Id"] == "test-session"
                return httpx.Response(
                    200,
                    json={
                        "jsonrpc": "2.0",
                        "id": "qagent-call",
                        "result": {
                            "tools": [
                                {"name": "get_accounts", "inputSchema": {}},
                                {"name": "get_portfolio", "inputSchema": {}},
                                {"name": "get_watchlists", "inputSchema": {}},
                                {"name": "get_watchlist_items", "inputSchema": {}},
                                {"name": "review_equity_order", "inputSchema": {}},
                                {"name": "place_equity_order", "inputSchema": {}},
                            ]
                        },
                    },
                )
            if method == "tools/call":
                tool_name = payload["params"]["name"]
                if tool_name == "get_accounts":
                    result = {
                        "data": {
                            "accounts": [
                                {
                                    "account_number": "secret-account-number",
                                    "agentic_allowed": True,
                                    "nickname": "Agentic",
                                }
                            ]
                        }
                    }
                elif tool_name == "get_portfolio":
                    assert payload["params"]["arguments"] == {
                        "account_number": "secret-account-number"
                    }
                    result = {"data": {"total_value": "500.00", "cash": "500.00"}}
                elif tool_name == "get_watchlists":
                    result = {
                        "data": {
                            "watchlists": [
                                {
                                    "id": "watchlist-id",
                                    "display_name": "Technology",
                                    "item_count": 2,
                                    "owner_type": "custom",
                                },
                                {
                                    "id": "options-watchlist-id",
                                    "display_name": "Options Watchlist",
                                    "item_count": 0,
                                    "owner_type": "system",
                                }
                            ]
                        }
                    }
                elif tool_name == "get_watchlist_items":
                    list_id = payload["params"]["arguments"]["list_id"]
                    if list_id == "options-watchlist-id":
                        return httpx.Response(
                            200,
                            json={
                                "jsonrpc": "2.0",
                                "id": "qagent-call",
                                "result": {
                                    "content": [
                                        {
                                            "type": "text",
                                            "text": "API error 400: options unavailable",
                                        }
                                    ],
                                    "isError": True,
                                },
                            },
                        )
                    assert list_id == "watchlist-id"
                    result = {
                        "data": {
                            "items": [{"symbol": "AAPL"}, {"symbol": "NVDA"}]
                        }
                    }
                else:
                    raise AssertionError(f"Unexpected tool call: {tool_name}")
                return httpx.Response(
                    200,
                    json={
                        "jsonrpc": "2.0",
                        "id": "qagent-call",
                        "result": {
                            "content": [
                                {"type": "text", "text": json.dumps(result)}
                            ],
                            "isError": False,
                        },
                    },
                )
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    key = Fernet.generate_key().decode()
    ledger = EventLedger(settings.database_url)
    bridge = RobinhoodMCPBridge(
        ledger.engine,
        enabled=True,
        redirect_uri="https://qagent.example/v1/robinhood/oauth/callback",
        encryption_key=key,
        ledger=ledger,
        client=client,
        now_provider=lambda: datetime(2026, 9, 9, 7, tzinfo=UTC),
    )

    start = asyncio.run(
        bridge.begin_oauth(
            requested_by="operator",
            reason="Connect the dedicated Robinhood Agentic account",
        )
    )
    query = parse_qs(urlparse(start["authorization_url"]).query)
    assert query["client_id"] == ["registered-client"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["resource"] == [ROBINHOOD_MCP_SERVER_URL]
    status = asyncio.run(
        bridge.complete_oauth(
            state=query["state"][0],
            code="one-time-code",
        )
    )
    assert status["connected"] is True
    tools = asyncio.run(bridge.list_tools())
    assert [tool["name"] for tool in tools] == [
        "get_accounts",
        "get_portfolio",
        "get_watchlists",
        "get_watchlist_items",
        "review_equity_order",
        "place_equity_order",
    ]
    portfolio = asyncio.run(bridge.agentic_portfolio())
    assert portfolio["account"]["nickname"] == "Agentic"
    assert portfolio["account"]["account_number_last_four"] == "mber"
    assert "account_number" not in portfolio["account"]
    assert portfolio["portfolio"][0]["data"]["total_value"] == "500.00"
    watchlists = asyncio.run(bridge.watchlists())
    assert watchlists == {
        "watchlists": [
            {
                "display_name": "Technology",
                "owner_type": "custom",
                "reported_item_count": 2,
                "symbols": ["AAPL", "NVDA"],
            },
            {
                "display_name": "Options Watchlist",
                "owner_type": "system",
                "reported_item_count": 0,
                "symbols": [],
                "read_status": "UNAVAILABLE",
                "warning": "Robinhood did not expose items for this watchlist",
            }
        ]
    }
    with pytest.raises(RobinhoodMCPConfigurationError, match="hard-disabled"):
        asyncio.run(bridge.place_equity_order(arguments={"symbol": "AAPL"}))

    engine = ledger.engine
    with engine.connect() as connection:
        stored = connection.execute(select(robinhood_mcp_connections)).one()
        calls = connection.execute(select(robinhood_mcp_calls)).all()
    assert "sensitive-access-token" not in stored.access_token_ciphertext
    assert "sensitive-refresh-token" not in stored.refresh_token_ciphertext
    assert [call.tool_name for call in calls] == [
        "tools/list",
        "get_accounts",
        "get_portfolio",
        "get_watchlists",
        "get_watchlist_items",
        "get_watchlist_items",
    ]
    assert {
        event["event_type"]
        for event in ledger.by_correlation_id(str(stored.connection_id))
    } == {"broker.robinhood.connected.v1"}
    assert bridge.disconnect(disconnected_by="operator")["connected"] is False
    assert {
        event["event_type"]
        for event in ledger.by_correlation_id(str(stored.connection_id))
    } == {
        "broker.robinhood.connected.v1",
        "broker.robinhood.disconnected.v1",
    }
    assert any(url == ROBINHOOD_MCP_SERVER_URL for _, url in requests)
    asyncio.run(client.aclose())


def test_robinhood_bridge_settings_accept_valid_dormant_configuration() -> None:
    value = Settings(
        _env_file=None,
        robinhood_mcp_bridge_enabled=True,
        robinhood_oauth_redirect_uri=(
            "https://qagent.example/v1/robinhood/oauth/callback"
        ),
        robinhood_token_encryption_key=SecretStr(Fernet.generate_key().decode()),
    )
    assert value.robinhood_mcp_bridge_enabled is True
    assert value.robinhood_order_submission_enabled is False


def test_production_robinhood_bridge_requires_exact_loopback_callback() -> None:
    common = {
        "_env_file": None,
        "app_env": "production",
        "auto_migrate": False,
        "source_git_sha": "a" * 40,
        "auth_required": True,
        "admin_username": "operator",
        "admin_password_hash": SecretStr("stored-argon2-hash"),
        "session_secret": SecretStr("s" * 64),
        "robinhood_mcp_bridge_enabled": True,
        "robinhood_token_encryption_key": SecretStr(Fernet.generate_key().decode()),
    }
    value = Settings(
        **common,
        robinhood_oauth_redirect_uri="http://127.0.0.1:8765/callback",
    )
    assert value.robinhood_oauth_redirect_uri == "http://127.0.0.1:8765/callback"
    with pytest.raises(ValueError, match="exact loopback callback"):
        Settings(
            **common,
            robinhood_oauth_redirect_uri=(
                "https://qagent.example/v1/robinhood/oauth/callback"
            ),
        )


def test_robinhood_loopback_relay_forwards_only_oauth_fields() -> None:
    target = "https://qagent.example/v1/robinhood/oauth/callback"
    forwarded = build_forward_url(
        target,
        "code=one-time-code&state=opaque-state&ignored=secret",
    )
    query = parse_qs(urlparse(forwarded).query)
    assert query == {"code": ["one-time-code"], "state": ["opaque-state"]}
    with pytest.raises(ValueError, match="HTTPS URL"):
        require_forward_url("http://qagent.example/v1/robinhood/oauth/callback")
    with pytest.raises(ValueError, match="repeated code"):
        build_forward_url(target, "code=one&code=two&state=opaque-state")


def test_control_api_exposes_only_dormant_robinhood_bridge(
    settings: Settings,
) -> None:
    from fastapi.testclient import TestClient

    with TestClient(create_app(settings)) as client:
        status = client.get("/v1/robinhood/status")
        assert status.status_code == 200
        assert status.json()["enabled"] is False
        assert status.json()["order_submission_enabled"] is False
        assert client.post(
            "/v1/robinhood/oauth/start",
            json={"reason": "Verify disabled bridge"},
        ).status_code == 409
        assert client.get("/v1/robinhood/watchlists").status_code == 409
        assert client.post(
            "/v1/robinhood/place-equity-order",
            json={"arguments": {"symbol": "AAPL"}},
        ).status_code == 404


def test_robinhood_oauth_callback_is_public_but_start_remains_authenticated(
    settings: Settings,
) -> None:
    from fastapi.testclient import TestClient

    secured = settings.model_copy(
        update={
            "auth_required": True,
            "admin_username": "operator",
            "admin_password": SecretStr("correct horse battery staple"),
            "session_secret": SecretStr("s" * 64),
        }
    )
    with TestClient(create_app(secured), follow_redirects=False) as client:
        callback = client.get("/v1/robinhood/oauth/callback")
        assert callback.status_code in {302, 307}
        assert callback.headers["location"].endswith("#robinhood")
        assert client.post(
            "/v1/robinhood/oauth/start",
            json={"reason": "Must require an admin session"},
        ).status_code == 401
