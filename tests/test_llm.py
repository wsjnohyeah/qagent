from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from agentic_quant.api import create_app
from agentic_quant.config import Settings
from agentic_quant.domain import (
    LLMInvocationStatus,
    LLMProviderName,
    LLMUsage,
    LLMWorkload,
)
from agentic_quant.ledger import EventLedger
from agentic_quant.llm import (
    LLMConfigurationError,
    LLMGateway,
    LLMProviderConfig,
    LLMProviderError,
    LLMProviderResult,
    LLMRequest,
    ResponsesAPIProvider,
    load_llm_routing_config,
)
from agentic_quant.llm_store import LLMStore
from agentic_quant.migrations import upgrade_database


def _routing_path() -> Path:
    return Path(__file__).parents[1] / "configs/model_routing.yaml"


def _provider_config(
    *,
    model: str,
    send_store_false: bool,
    temperature: float | None = None,
    top_p: float | None = None,
) -> LLMProviderConfig:
    return LLMProviderConfig(
        model=model,
        base_url="https://example.invalid/v1",
        cost_tier="premium" if model.startswith("gpt") else "value",
        reasoning_effort="low",
        max_output_tokens=100,
        timeout_seconds=10,
        max_retries=0,
        send_store_false=send_store_false,
        temperature=temperature,
        top_p=top_p,
    )


def test_llm_credentials_are_project_scoped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "machine-wide-test-value")
    monkeypatch.delenv("LLM_OPENAI_API_KEY", raising=False)
    isolated = Settings(_env_file=None)
    assert isolated.openai_configured is False

    monkeypatch.setenv("LLM_OPENAI_API_KEY", "project-test-value")
    configured = Settings(_env_file=None)
    assert configured.openai_configured is True


@pytest.mark.parametrize(
    ("provider_name", "model", "send_store_false", "temperature", "top_p"),
    (
        (LLMProviderName.OPENAI, "gpt-5.6-sol", True, None, None),
        (LLMProviderName.META, "muse-spark-1.3", False, 1.0, 1.0),
    ),
)
def test_responses_api_provider_builds_request_and_parses_usage(
    provider_name: LLMProviderName,
    model: str,
    send_store_false: bool,
    temperature: float | None,
    top_p: float | None,
) -> None:
    requests: list[httpx.Request] = []

    async def scenario() -> LLMProviderResult:
        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200,
                json={
                    "id": "resp_test",
                    "status": "completed",
                    "output": [
                        {
                            "type": "message",
                            "content": [
                                {"type": "output_text", "text": "LLM_PROVIDER_OK"}
                            ],
                        }
                    ],
                    "usage": {
                        "input_tokens": 12,
                        "output_tokens": 3,
                        "total_tokens": 15,
                        "output_tokens_details": {"reasoning_tokens": 2},
                    },
                },
            )

        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://example.invalid/v1",
        )
        provider = ResponsesAPIProvider(
            name=provider_name,
            api_key="test-key",
            config=_provider_config(
                model=model,
                send_store_false=send_store_false,
                temperature=temperature,
                top_p=top_p,
            ),
            client=client,
        )
        result = await provider.complete(
            LLMRequest(
                workload=LLMWorkload.CRITICAL_RESEARCH,
                prompt_version="test@0.1.0",
                instructions="Do not call tools.",
                input_text="Reply OK.",
                max_output_tokens=32,
            )
        )
        await client.aclose()
        return result

    result = asyncio.run(scenario())

    payload = json.loads(requests[0].content)
    assert requests[0].url.path == "/v1/responses"
    assert requests[0].headers["authorization"] == "Bearer test-key"
    assert payload["model"] == model
    assert payload["max_output_tokens"] == 32
    assert (payload.get("store") is False) is send_store_false
    assert payload.get("temperature") == temperature
    assert payload.get("top_p") == top_p
    assert result.output_text == "LLM_PROVIDER_OK"
    assert result.usage == LLMUsage(
        input_tokens=12,
        output_tokens=3,
        total_tokens=15,
        reasoning_tokens=2,
    )


def test_responses_api_provider_does_not_retry_authentication_failure() -> None:
    request_count = 0

    async def scenario() -> None:
        async def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal request_count
            request_count += 1
            return httpx.Response(401, json={"error": {"message": "unauthorized"}})

        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://example.invalid/v1",
        )
        provider = ResponsesAPIProvider(
            name=LLMProviderName.OPENAI,
            api_key="invalid-test-key",
            config=_provider_config(
                model="gpt-5.6-sol",
                send_store_false=True,
            ),
            client=client,
        )
        with pytest.raises(LLMProviderError) as exc_info:
            await provider.complete(
                LLMRequest(
                    workload=LLMWorkload.CRITICAL_RESEARCH,
                    prompt_version="test@0.1.0",
                    instructions="Do not call tools.",
                    input_text="Reply OK.",
                )
            )
        await client.aclose()
        assert exc_info.value.status_code == 401

    asyncio.run(scenario())
    assert request_count == 1


def test_responses_api_provider_retries_rate_limit_once() -> None:
    request_count = 0

    async def scenario() -> LLMProviderResult:
        async def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal request_count
            request_count += 1
            if request_count == 1:
                return httpx.Response(429, json={"error": {"message": "rate limited"}})
            return httpx.Response(
                200,
                json={
                    "id": "resp_retry",
                    "status": "completed",
                    "output_text": "LLM_PROVIDER_OK",
                    "usage": {"input_tokens": 2, "output_tokens": 2},
                },
            )

        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://example.invalid/v1",
        )
        config = _provider_config(
            model="gpt-5.6-sol",
            send_store_false=True,
        ).model_copy(update={"max_retries": 1})
        provider = ResponsesAPIProvider(
            name=LLMProviderName.OPENAI,
            api_key="test-key",
            config=config,
            client=client,
            retry_base_seconds=0,
        )
        result = await provider.complete(
            LLMRequest(
                workload=LLMWorkload.CRITICAL_RESEARCH,
                prompt_version="test@0.1.0",
                instructions="Do not call tools.",
                input_text="Reply OK.",
            )
        )
        await client.aclose()
        return result

    result = asyncio.run(scenario())
    assert request_count == 2
    assert result.response_id == "resp_retry"


class FakeProvider:
    def __init__(self, name: LLMProviderName, config: LLMProviderConfig) -> None:
        self.name = name
        self.config = config
        self.calls = 0

    async def complete(self, request: LLMRequest) -> LLMProviderResult:
        self.calls += 1
        return LLMProviderResult(
            response_id=f"resp_{self.name.value}",
            output_text=f"completed by {self.name.value}",
            usage=LLMUsage(input_tokens=10, output_tokens=4, total_tokens=14),
        )

    async def aclose(self) -> None:
        return None


def test_gateway_routes_and_persists_immutable_audit(
    settings,  # type: ignore[no-untyped-def]
) -> None:
    upgrade_database(settings.database_url)
    ledger = EventLedger(settings.database_url)
    store = LLMStore(ledger.engine)
    routing = load_llm_routing_config(_routing_path())
    openai = FakeProvider(
        LLMProviderName.OPENAI,
        routing.providers[LLMProviderName.OPENAI],
    )
    meta = FakeProvider(
        LLMProviderName.META,
        routing.providers[LLMProviderName.META],
    )
    gateway = LLMGateway(
        routing=routing,
        providers={LLMProviderName.OPENAI: openai, LLMProviderName.META: meta},
        store=store,
        ledger=ledger,
    )

    invocation = asyncio.run(
        gateway.complete(
            LLMRequest(
                workload=LLMWorkload.CRITICAL_RESEARCH,
                prompt_version="research_synthesis@0.1.0",
                instructions="Use only supplied evidence.",
                input_text="Evidence packet IDs: packet-1, packet-2",
            )
        )
    )

    assert invocation.provider == LLMProviderName.OPENAI
    assert invocation.model == "gpt-5.6-sol"
    assert len(invocation.routing_sha256) == 64
    assert invocation.code_git_sha == "UNAVAILABLE"
    assert invocation.status == LLMInvocationStatus.COMPLETED
    assert openai.calls == 1
    assert meta.calls == 0
    assert store.health_summary() == {
        "llm_invocations": 1,
        "llm_routing_revisions": 0,
    }
    assert store.recent(limit=1)[0]["output_preview"] == "completed by openai"
    stored = store.get(invocation.invocation_id)
    assert stored is not None
    assert stored["output_text"] == "completed by openai"
    assert stored["request_envelope"] == {
        "model": "gpt-5.6-sol",
        "instructions": "Use only supplied evidence.",
        "input": "Evidence packet IDs: packet-1, packet-2",
        "max_output_tokens": 4096,
        "reasoning": {"effort": "high"},
        "store": False,
        "temperature": None,
        "top_p": None,
        "audit_note": (
            "Application payload after credential redaction; HTTP headers omitted"
        ),
    }
    events = ledger.by_correlation_id(invocation.invocation_id)
    assert events[-1]["event_type"] == "llm.invocation.recorded.v1"
    assert "input_text" not in events[-1]["payload"]


def test_gateway_fails_closed_and_audits_missing_credentials(
    settings,  # type: ignore[no-untyped-def]
) -> None:
    upgrade_database(settings.database_url)
    ledger = EventLedger(settings.database_url)
    store = LLMStore(ledger.engine)
    gateway = LLMGateway(
        routing=load_llm_routing_config(_routing_path()),
        providers={},
        store=store,
        ledger=ledger,
    )

    with pytest.raises(LLMConfigurationError, match="API key is not configured"):
        asyncio.run(
            gateway.complete(
                LLMRequest(
                    workload=LLMWorkload.INTERACTIVE_EXPLANATION,
                    prompt_version="explanation@0.1.0",
                    instructions="Explain supplied results.",
                    input_text="No provider credentials are available.",
                )
            )
        )

    stored = store.recent(limit=1)[0]
    assert stored["provider"] == "meta"
    assert stored["status"] == "FAILED"
    assert stored["error_code"] == "provider_not_configured"
    assert stored["output_preview"] is None


def test_control_center_route_revision_changes_effective_provider(
    settings,  # type: ignore[no-untyped-def]
) -> None:
    upgrade_database(settings.database_url)
    ledger = EventLedger(settings.database_url)
    store = LLMStore(ledger.engine)
    routing = load_llm_routing_config(_routing_path())
    openai = FakeProvider(
        LLMProviderName.OPENAI,
        routing.providers[LLMProviderName.OPENAI],
    )
    meta = FakeProvider(
        LLMProviderName.META,
        routing.providers[LLMProviderName.META],
    )
    gateway = LLMGateway(
        routing=routing,
        providers={LLMProviderName.OPENAI: openai, LLMProviderName.META: meta},
        store=store,
        ledger=ledger,
    )
    routes = dict(routing.routes)
    routes[LLMWorkload.INTERACTIVE_EXPLANATION] = LLMProviderName.OPENAI

    revision = gateway.activate_routes(
        routes=routes,
        reason="Test interactive premium routing",
    )
    status = gateway.status()
    invocation = asyncio.run(
        gateway.complete(
            LLMRequest(
                workload=LLMWorkload.INTERACTIVE_EXPLANATION,
                prompt_version="test_chat@0.1.0",
                instructions="Explain only.",
                input_text="Hello",
            )
        )
    )

    assert status["route_source"] == "control_center"
    assert status["active_revision_id"] == revision.routing_revision_id
    assert status["routes"]["interactive_explanation"] == "openai"
    assert invocation.provider == LLMProviderName.OPENAI
    assert invocation.routing_version == revision.routing_version
    assert invocation.routing_sha256 == revision.routing_sha256
    assert openai.calls == 1
    assert meta.calls == 0
    assert store.recent_routing_revisions(limit=1)[0]["reason"] == (
        "Test interactive premium routing"
    )
    events = ledger.by_correlation_id(revision.routing_revision_id)
    assert events[-1]["event_type"] == "llm.routing.activated.v1"

    changed_base = routing.model_copy(update={"version": "llm_routing@0.1.1"})
    gateway_after_base_change = LLMGateway(
        routing=changed_base,
        providers={LLMProviderName.OPENAI: openai, LLMProviderName.META: meta},
        store=store,
    )
    changed_status = gateway_after_base_change.status()
    assert changed_status["route_source"] == "yaml_base"
    assert changed_status["routes"]["interactive_explanation"] == "meta"


def test_chat_api_supports_explicit_provider_and_bounded_history(
    settings,  # type: ignore[no-untyped-def]
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[LLMRequest] = []

    async def fake_complete(
        _provider: ResponsesAPIProvider,
        request: LLMRequest,
    ) -> LLMProviderResult:
        captured.append(request)
        return LLMProviderResult(
            response_id="resp_chat_test",
            output_text="测试回复",
            usage=LLMUsage(input_tokens=9, output_tokens=4, total_tokens=13),
        )

    monkeypatch.setattr(ResponsesAPIProvider, "complete", fake_complete)
    configured = settings.model_copy(
        update={
            "llm_openai_api_key": SecretStr("project-test-openai-key"),
            "llm_meta_api_key": SecretStr("project-test-meta-key"),
        }
    )
    with TestClient(create_app(configured)) as client:
        response = client.post(
            "/v1/llm/chat",
            json={
                "message": "继续解释",
                "history": [
                    {"role": "user", "content": "解释这个研究结果"},
                    {"role": "assistant", "content": "这是上轮回答"},
                ],
                "provider": "openai",
            },
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["provider"] == "openai"
        assert payload["model"] == "gpt-5.6-sol"
        assert payload["output_text"] == "测试回复"
        assert payload["prompt_version"] == "research_copilot@0.1.0"
        audit = client.get(f"/v1/llm/invocations/{payload['invocation_id']}").json()
        assert audit["output_text"] == "测试回复"

    transcript = captured[0].input_text
    assert "解释这个研究结果" in transcript
    assert "这是上轮回答" in transcript
    assert "继续解释" in transcript
