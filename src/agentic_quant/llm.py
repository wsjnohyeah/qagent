from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Any, Protocol, Self

import httpx
from pydantic import Field, model_validator
import yaml

from agentic_quant.config import Settings
from agentic_quant.domain import (
    EventEnvelope,
    FrozenModel,
    LLMInvocation,
    LLMInvocationStatus,
    LLMProviderName,
    LLMUsage,
    LLMWorkload,
)
from agentic_quant.ids import uuid7
from agentic_quant.ledger import EventLedger
from agentic_quant.llm_store import LLMStore


class LLMConfigurationError(RuntimeError):
    pass


class LLMProviderError(RuntimeError):
    def __init__(
        self,
        *,
        provider: LLMProviderName,
        code: str,
        status_code: int | None = None,
    ) -> None:
        message = f"{provider.value} LLM request failed: {code}"
        if status_code is not None:
            message += f" ({status_code})"
        super().__init__(message)
        self.provider = provider
        self.code = code
        self.status_code = status_code


class LLMProviderConfig(FrozenModel):
    model: str = Field(min_length=1, max_length=120)
    base_url: str = Field(pattern=r"^https://")
    cost_tier: str = Field(pattern=r"^(premium|value)$")
    reasoning_effort: str = Field(min_length=1, max_length=24)
    max_output_tokens: int = Field(ge=1, le=128_000)
    timeout_seconds: float = Field(gt=0, le=3_600)
    max_retries: int = Field(default=2, ge=0, le=5)
    send_store_false: bool = True
    temperature: float | None = Field(default=None, ge=0, le=2)
    top_p: float | None = Field(default=None, gt=0, le=1)


class LLMRoutingConfig(FrozenModel):
    version: str = Field(pattern=r"^llm_routing@[0-9]+\.[0-9]+\.[0-9]+$")
    providers: dict[LLMProviderName, LLMProviderConfig]
    routes: dict[LLMWorkload, LLMProviderName]

    @model_validator(mode="after")
    def routes_are_complete(self) -> Self:
        missing_providers = set(LLMProviderName) - set(self.providers)
        if missing_providers:
            raise ValueError(
                f"Missing LLM provider definitions: {sorted(missing_providers)}"
            )
        missing = set(LLMWorkload) - set(self.routes)
        if missing:
            raise ValueError(f"Missing LLM workload routes: {sorted(missing)}")
        undefined = set(self.routes.values()) - set(self.providers)
        if undefined:
            raise ValueError(f"Routes reference undefined providers: {sorted(undefined)}")
        return self


class LLMRequest(FrozenModel):
    workload: LLMWorkload
    prompt_version: str = Field(min_length=1, max_length=120)
    instructions: str = Field(min_length=1, max_length=100_000)
    input_text: str = Field(min_length=1, max_length=2_000_000)
    max_output_tokens: int | None = Field(default=None, ge=1, le=128_000)
    reasoning_effort: str | None = Field(default=None, min_length=1, max_length=24)
    timeout_seconds: float | None = Field(default=None, gt=0, le=3_600)


class LLMProviderResult(FrozenModel):
    response_id: str
    output_text: str
    usage: LLMUsage


class LLMProvider(Protocol):
    name: LLMProviderName
    config: LLMProviderConfig

    async def complete(self, request: LLMRequest) -> LLMProviderResult: ...

    async def aclose(self) -> None: ...


def load_llm_routing_config(path: Path) -> LLMRoutingConfig:
    with path.open("r", encoding="utf-8") as handle:
        payload = yaml.safe_load(handle)
    return LLMRoutingConfig.model_validate(payload)


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


class ResponsesAPIProvider:
    def __init__(
        self,
        *,
        name: LLMProviderName,
        api_key: str,
        config: LLMProviderConfig,
        client: httpx.AsyncClient | None = None,
        retry_base_seconds: float = 0.5,
    ) -> None:
        if not api_key:
            raise LLMConfigurationError(f"{name.value} API key is required")
        self.name = name
        self.config = config
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._retry_base_seconds = retry_base_seconds
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=config.base_url.rstrip("/"),
            headers=self._headers,
            timeout=httpx.Timeout(config.timeout_seconds, connect=15.0),
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def complete(self, request: LLMRequest) -> LLMProviderResult:
        reasoning_effort = request.reasoning_effort or self.config.reasoning_effort
        timeout_seconds = min(
            request.timeout_seconds or self.config.timeout_seconds,
            self.config.timeout_seconds,
        )
        payload: dict[str, Any] = {
            "model": self.config.model,
            "instructions": request.instructions,
            "input": request.input_text,
            "max_output_tokens": min(
                request.max_output_tokens or self.config.max_output_tokens,
                self.config.max_output_tokens,
            ),
            "reasoning": {"effort": reasoning_effort},
        }
        if self.config.send_store_false:
            payload["store"] = False
        if self.config.temperature is not None:
            payload["temperature"] = self.config.temperature
        if self.config.top_p is not None:
            payload["top_p"] = self.config.top_p
        try:
            async with asyncio.timeout(timeout_seconds):
                response = await self._post(payload, timeout_seconds=timeout_seconds)
        except TimeoutError as exc:
            raise LLMProviderError(
                provider=self.name,
                code="upstream_timeout",
                status_code=504,
            ) from exc
        try:
            body = response.json()
        except ValueError as exc:
            raise LLMProviderError(
                provider=self.name,
                code="invalid_json_response",
                status_code=response.status_code,
            ) from exc
        if not isinstance(body, dict):
            raise LLMProviderError(
                provider=self.name,
                code="invalid_response_shape",
                status_code=response.status_code,
            )
        response_status = body.get("status")
        if response_status not in {None, "completed"}:
            raise LLMProviderError(
                provider=self.name,
                code=f"response_status_{response_status}",
                status_code=response.status_code,
            )
        output_text = self._output_text(body)
        if not output_text:
            raise LLMProviderError(
                provider=self.name,
                code="missing_output_text",
                status_code=response.status_code,
            )
        raw_usage = body.get("usage") or {}
        usage_payload = raw_usage if isinstance(raw_usage, dict) else {}
        raw_output_details = usage_payload.get("output_tokens_details") or {}
        output_details = (
            raw_output_details if isinstance(raw_output_details, dict) else {}
        )
        try:
            input_tokens = int(usage_payload.get("input_tokens") or 0)
            output_tokens = int(usage_payload.get("output_tokens") or 0)
            total_tokens = int(
                usage_payload.get("total_tokens") or input_tokens + output_tokens
            )
            reasoning_tokens = int(output_details.get("reasoning_tokens") or 0)
        except (TypeError, ValueError) as exc:
            raise LLMProviderError(
                provider=self.name,
                code="invalid_usage_shape",
                status_code=response.status_code,
            ) from exc
        return LLMProviderResult(
            response_id=str(body.get("id") or response.headers.get("x-request-id") or "UNKNOWN"),
            output_text=output_text,
            usage=LLMUsage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=total_tokens,
                reasoning_tokens=reasoning_tokens,
            ),
        )

    async def _post(
        self,
        payload: dict[str, Any],
        *,
        timeout_seconds: float,
    ) -> httpx.Response:
        last_error: httpx.RequestError | None = None
        for attempt in range(self.config.max_retries + 1):
            try:
                response = await self._client.post(
                    "/responses",
                    json=payload,
                    headers=self._headers,
                    timeout=timeout_seconds,
                )
            except httpx.RequestError as exc:
                last_error = exc
                if attempt < self.config.max_retries:
                    await asyncio.sleep(self._retry_base_seconds * (2**attempt))
                    continue
                break
            if (response.status_code == 429 or response.status_code >= 500) and (
                attempt < self.config.max_retries
            ):
                await asyncio.sleep(self._retry_base_seconds * (2**attempt))
                continue
            if response.is_error:
                raise LLMProviderError(
                    provider=self.name,
                    code="upstream_http_error",
                    status_code=response.status_code,
                )
            return response
        raise LLMProviderError(
            provider=self.name,
            code="upstream_transport_error",
            status_code=504,
        ) from last_error

    @staticmethod
    def _output_text(payload: dict[str, Any]) -> str:
        direct = payload.get("output_text")
        if isinstance(direct, str):
            return direct.strip()
        fragments = []
        for output in payload.get("output") or ():
            if not isinstance(output, dict) or output.get("type") != "message":
                continue
            for content in output.get("content") or ():
                if not isinstance(content, dict):
                    continue
                if content.get("type") in {"output_text", "text"} and isinstance(
                    content.get("text"), str
                ):
                    fragments.append(content["text"])
        return "\n".join(fragments).strip()


class LLMGateway:
    def __init__(
        self,
        *,
        routing: LLMRoutingConfig,
        providers: dict[LLMProviderName, LLMProvider],
        store: LLMStore,
        ledger: EventLedger | None = None,
        code_git_sha: str = "UNAVAILABLE",
    ) -> None:
        self.routing = routing
        self.routing_sha256 = _canonical_sha256(routing.model_dump(mode="json"))
        self.providers = providers
        self.store = store
        self.ledger = ledger
        self.code_git_sha = code_git_sha

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        for provider in self.providers.values():
            await provider.aclose()

    def status(self) -> dict[str, Any]:
        return {
            "routing_version": self.routing.version,
            "routing_sha256": self.routing_sha256,
            "providers": {
                name.value: {
                    "model": config.model,
                    "base_url": config.base_url,
                    "cost_tier": config.cost_tier,
                    "reasoning_effort": config.reasoning_effort,
                    "max_output_tokens": config.max_output_tokens,
                    "configured": name in self.providers,
                }
                for name, config in self.routing.providers.items()
            },
            "routes": {
                workload.value: provider.value
                for workload, provider in self.routing.routes.items()
            },
            "automatic_fallback": False,
        }

    async def complete(
        self,
        request: LLMRequest,
        *,
        provider_override: LLMProviderName | None = None,
    ) -> LLMInvocation:
        provider_name = provider_override or self.routing.routes[request.workload]
        config = self.routing.providers[provider_name]
        reasoning_effort = request.reasoning_effort or config.reasoning_effort
        timeout_seconds = min(
            request.timeout_seconds or config.timeout_seconds,
            config.timeout_seconds,
        )
        invocation_id = uuid7()
        created_at = datetime.now(UTC)
        input_sha256 = hashlib.sha256(request.input_text.encode()).hexdigest()
        request_sha256 = _canonical_sha256(
            {
                "routing_version": self.routing.version,
                "provider": provider_name.value,
                "model": config.model,
                "reasoning_effort": reasoning_effort,
                "prompt_version": request.prompt_version,
                "instructions": request.instructions,
                "input": request.input_text,
                "max_output_tokens": min(
                    request.max_output_tokens or config.max_output_tokens,
                    config.max_output_tokens,
                ),
                "timeout_seconds": timeout_seconds,
                "send_store_false": config.send_store_false,
                "temperature": config.temperature,
                "top_p": config.top_p,
            }
        )
        started = monotonic()
        provider = self.providers.get(provider_name)
        if provider is None:
            invocation = self._failed_invocation(
                invocation_id=invocation_id,
                request=request,
                provider=provider_name,
                config=config,
                reasoning_effort=reasoning_effort,
                request_sha256=request_sha256,
                input_sha256=input_sha256,
                created_at=created_at,
                started=started,
                error_code="provider_not_configured",
            )
            self._record(invocation)
            raise LLMConfigurationError(
                f"{provider_name.value} is selected but its API key is not configured"
            )
        try:
            result = await provider.complete(request)
        except LLMProviderError as exc:
            invocation = self._failed_invocation(
                invocation_id=invocation_id,
                request=request,
                provider=provider_name,
                config=config,
                reasoning_effort=reasoning_effort,
                request_sha256=request_sha256,
                input_sha256=input_sha256,
                created_at=created_at,
                started=started,
                error_code=(
                    f"{exc.code}_{exc.status_code}"
                    if exc.status_code is not None
                    else exc.code
                ),
            )
            self._record(invocation)
            raise
        completed_at = datetime.now(UTC)
        invocation = LLMInvocation(
            invocation_id=invocation_id,
            workload=request.workload,
            routing_version=self.routing.version,
            routing_sha256=self.routing_sha256,
            code_git_sha=self.code_git_sha,
            provider=provider_name,
            model=config.model,
            reasoning_effort=reasoning_effort,
            prompt_version=request.prompt_version,
            request_sha256=request_sha256,
            input_sha256=input_sha256,
            response_id=result.response_id,
            output_text=result.output_text,
            output_sha256=hashlib.sha256(result.output_text.encode()).hexdigest(),
            usage=result.usage,
            latency_ms=max(0, int((monotonic() - started) * 1_000)),
            status=LLMInvocationStatus.COMPLETED,
            created_at=created_at,
            completed_at=completed_at,
        )
        self._record(invocation)
        return invocation

    def _failed_invocation(
        self,
        *,
        invocation_id: str,
        request: LLMRequest,
        provider: LLMProviderName,
        config: LLMProviderConfig,
        reasoning_effort: str,
        request_sha256: str,
        input_sha256: str,
        created_at: datetime,
        started: float,
        error_code: str,
    ) -> LLMInvocation:
        return LLMInvocation(
            invocation_id=invocation_id,
            workload=request.workload,
            routing_version=self.routing.version,
            routing_sha256=self.routing_sha256,
            code_git_sha=self.code_git_sha,
            provider=provider,
            model=config.model,
            reasoning_effort=reasoning_effort,
            prompt_version=request.prompt_version,
            request_sha256=request_sha256,
            input_sha256=input_sha256,
            latency_ms=max(0, int((monotonic() - started) * 1_000)),
            status=LLMInvocationStatus.FAILED,
            error_code=error_code,
            created_at=created_at,
            completed_at=datetime.now(UTC),
        )

    def _record(self, invocation: LLMInvocation) -> None:
        self.store.record(invocation)
        if self.ledger is None:
            return
        self.ledger.append(
            EventEnvelope(
                event_id=uuid7(),
                event_type="llm.invocation.recorded.v1",
                event_time=invocation.completed_at,
                emitted_at=datetime.now(UTC),
                producer="llm-gateway",
                correlation_id=invocation.invocation_id,
                payload={
                    "invocation_id": invocation.invocation_id,
                    "workload": invocation.workload.value,
                    "routing_version": invocation.routing_version,
                    "routing_sha256": invocation.routing_sha256,
                    "code_git_sha": invocation.code_git_sha,
                    "provider": invocation.provider.value,
                    "model": invocation.model,
                    "prompt_version": invocation.prompt_version,
                    "request_sha256": invocation.request_sha256,
                    "output_sha256": invocation.output_sha256,
                    "usage": invocation.usage.model_dump(mode="json"),
                    "latency_ms": invocation.latency_ms,
                    "status": invocation.status.value,
                    "error_code": invocation.error_code,
                },
            )
        )


def build_llm_gateway(
    settings: Settings,
    *,
    store: LLMStore,
    ledger: EventLedger | None = None,
    code_git_sha: str | None = None,
) -> LLMGateway:
    routing = load_llm_routing_config(settings.llm_routing_path)
    providers: dict[LLMProviderName, LLMProvider] = {}
    if settings.openai_configured:
        assert settings.llm_openai_api_key is not None
        providers[LLMProviderName.OPENAI] = ResponsesAPIProvider(
            name=LLMProviderName.OPENAI,
            api_key=settings.llm_openai_api_key.get_secret_value(),
            config=routing.providers[LLMProviderName.OPENAI],
        )
    if settings.meta_model_configured:
        assert settings.llm_meta_api_key is not None
        providers[LLMProviderName.META] = ResponsesAPIProvider(
            name=LLMProviderName.META,
            api_key=settings.llm_meta_api_key.get_secret_value(),
            config=routing.providers[LLMProviderName.META],
        )
    return LLMGateway(
        routing=routing,
        providers=providers,
        store=store,
        ledger=ledger,
        code_git_sha=code_git_sha or settings.source_git_sha or "UNAVAILABLE",
    )
