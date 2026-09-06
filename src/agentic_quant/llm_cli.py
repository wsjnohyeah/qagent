from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
from typing import Any

from agentic_quant.config import Settings
from agentic_quant.domain import LLMProviderName, LLMWorkload
from agentic_quant.ledger import EventLedger
from agentic_quant.llm import (
    LLMConfigurationError,
    LLMGateway,
    LLMProviderError,
    LLMRequest,
    build_llm_gateway,
)
from agentic_quant.llm_store import LLMStore
from agentic_quant.migrations import prepare_database


def _git_sha(settings: Settings) -> str:
    if settings.source_git_sha:
        return settings.source_git_sha
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return "UNAVAILABLE"
    return result.stdout.strip() if result.returncode == 0 else "UNAVAILABLE"


def _services(settings: Settings) -> tuple[LLMStore, LLMGateway]:
    prepare_database(settings)
    ledger = EventLedger(settings.database_url)
    store = LLMStore(ledger.engine)
    return store, build_llm_gateway(
        settings,
        store=store,
        ledger=ledger,
        code_git_sha=_git_sha(settings),
    )


async def _probe(settings: Settings, provider_argument: str) -> None:
    _, gateway = _services(settings)
    providers = (
        tuple(LLMProviderName)
        if provider_argument == "all"
        else (LLMProviderName(provider_argument),)
    )
    results: list[dict[str, Any]] = []
    failed = False
    try:
        for provider in providers:
            workload = (
                LLMWorkload.CRITICAL_RESEARCH
                if provider == LLMProviderName.OPENAI
                else LLMWorkload.INTERACTIVE_EXPLANATION
            )
            try:
                invocation = await gateway.complete(
                    LLMRequest(
                        workload=workload,
                        prompt_version="llm_probe@0.1.0",
                        instructions=(
                            "You are a deterministic API connectivity probe. "
                            "Do not call tools."
                        ),
                        input_text="Reply with exactly LLM_PROVIDER_OK.",
                        max_output_tokens=128,
                        reasoning_effort=(
                            "low"
                            if provider == LLMProviderName.OPENAI
                            else "minimal"
                        ),
                        timeout_seconds=60,
                    ),
                    provider_override=provider,
                )
            except (LLMConfigurationError, LLMProviderError) as exc:
                failed = True
                results.append(
                    {
                        "provider": provider.value,
                        "status": "FAILED",
                        "error": str(exc),
                    }
                )
            else:
                results.append(
                    {
                        "provider": provider.value,
                        "model": invocation.model,
                        "status": invocation.status.value,
                        "response_id": invocation.response_id,
                        "output_text": invocation.output_text,
                        "usage": invocation.usage.model_dump(mode="json"),
                        "latency_ms": invocation.latency_ms,
                    }
                )
    finally:
        await gateway.aclose()
    print(json.dumps(results, indent=2))
    if failed:
        raise SystemExit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="Audited multi-provider LLM gateway")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("routes", help="Show model routing and credential readiness")
    probe = subparsers.add_parser("probe", help="Run a minimal provider connectivity call")
    probe.add_argument(
        "--provider",
        choices=("all", *(provider.value for provider in LLMProviderName)),
        default="all",
    )
    list_invocations = subparsers.add_parser(
        "list",
        help="List recent immutable LLM invocation records",
    )
    list_invocations.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()
    settings = Settings()
    if args.command == "probe":
        asyncio.run(_probe(settings, args.provider))
        return
    store, gateway = _services(settings)
    if args.command == "routes":
        print(json.dumps(gateway.status(), indent=2))
    else:
        print(json.dumps(store.recent(limit=args.limit), indent=2, default=str))
    asyncio.run(gateway.aclose())


if __name__ == "__main__":
    main()
