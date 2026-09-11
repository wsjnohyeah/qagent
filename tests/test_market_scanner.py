from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import hashlib
from pathlib import Path
from typing import Any

from pydantic import SecretStr

from agentic_quant.archive import FileRawArchive
from agentic_quant.config import Settings
from agentic_quant.control_plane import SystemObjectStore
from agentic_quant.coordinator import AutonomousCoordinator
from agentic_quant.coordinator_runtime import ResearchCoordinatorHandler
from agentic_quant.domain import (
    LLMInvocation,
    LLMInvocationStatus,
    LLMProviderName,
    LLMUsage,
    LLMWorkload,
    WorkflowJob,
)
from agentic_quant.ledger import EventLedger
from agentic_quant.market_scanner import (
    MARKET_SCAN_PROMPT_VERSION,
    MarketUniverseScanner,
    _json_object,
    load_market_scanner_policy,
)
from agentic_quant.market_store import MarketDataStore
from agentic_quant.migrations import upgrade_database
from agentic_quant.providers.base import (
    AssetCatalogPage,
    MarketScreenerPage,
    StockSnapshotsPage,
)
from agentic_quant.risk import RestrictionRegistry
from agentic_quant.workflow import WorkflowJobStore


NOW = datetime(2026, 9, 7, 19, 0, tzinfo=UTC)


def _snapshot(
    *, price: float, previous: float, volume: int = 1_000_000
) -> dict[str, Any]:
    return {
        "dailyBar": {
            "c": price,
            "h": price * 1.04,
            "l": price * 0.96,
            "v": volume,
            "n": 25_000,
        },
        "prevDailyBar": {"c": previous, "v": volume},
        "latestTrade": {"p": price},
        "latestQuote": {"bp": price - 0.02, "ap": price + 0.02},
    }


class FakeScannerProvider:
    def __init__(self) -> None:
        self.calls = 0
        self.snapshots = {
            "SNDK": _snapshot(price=120, previous=100),
            "NVDA": _snapshot(price=180, previous=178),
            "META": _snapshot(price=700, previous=640),
            "SPY": _snapshot(price=650, previous=640),
            "PENNY": _snapshot(price=1, previous=0.8),
            "ETFTEST": _snapshot(price=50, previous=40),
        }

    async def __aenter__(self) -> FakeScannerProvider:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def fetch_most_actives(self, *, top: int) -> MarketScreenerPage:
        self.calls += 1
        assert top == 100
        return MarketScreenerPage(
            provider="alpaca",
            data_type="stock_screener_most_actives",
            provider_received_at=NOW,
            request_metadata={"top": top},
            raw_payload={
                "most_actives": [
                    {"symbol": "NVDA", "volume": 2_000_000, "trade_count": 40_000},
                    {"symbol": "SNDK", "volume": 1_000_000, "trade_count": 30_000},
                    {"symbol": "META", "volume": 3_000_000, "trade_count": 50_000},
                    {"symbol": "SPY", "volume": 4_000_000, "trade_count": 60_000},
                    {"symbol": "PENNY", "volume": 50_000_000, "trade_count": 80_000},
                    {"symbol": "ETFTEST", "volume": 2_000_000, "trade_count": 40_000},
                ]
            },
        )

    async def fetch_active_assets(self) -> AssetCatalogPage:
        self.calls += 1
        return AssetCatalogPage(
            provider="alpaca",
            provider_received_at=NOW,
            request_metadata={"status": "active", "asset_class": "us_equity"},
            raw_payload={
                "assets": [
                    {
                        "symbol": symbol,
                        "class": "us_equity",
                        "status": "active",
                        "tradable": True,
                        "exchange": "NASDAQ",
                        "name": (
                            "Direxion Daily Example ETF"
                            if symbol == "ETFTEST"
                            else f"{symbol} Corporation Common Stock"
                        ),
                    }
                    for symbol in self.snapshots
                ]
            },
        )

    async def fetch_market_movers(self, *, top: int) -> MarketScreenerPage:
        self.calls += 1
        assert top == 50
        return MarketScreenerPage(
            provider="alpaca",
            data_type="stock_screener_movers",
            provider_received_at=NOW,
            request_metadata={"top": top},
            raw_payload={
                "gainers": [
                    {"symbol": "SNDK", "price": 120, "percent_change": 20},
                    {"symbol": "META", "price": 700, "percent_change": 9},
                ],
                "losers": [],
            },
        )

    async def fetch_stock_snapshots(
        self, *, symbols: tuple[str, ...], feed: str
    ) -> StockSnapshotsPage:
        self.calls += 1
        assert feed == "sip"
        return StockSnapshotsPage(
            provider="alpaca",
            provider_received_at=NOW,
            request_metadata={"symbols": symbols, "feed": feed},
            raw_payload={
                symbol: self.snapshots[symbol]
                for symbol in symbols
                if symbol in self.snapshots
            },
        )


class FakeLLMGateway:
    def __init__(self, output_text: str) -> None:
        self.output_text = output_text
        self.calls = 0

    async def complete(self, request: Any) -> LLMInvocation:
        self.calls += 1
        assert request.workload == LLMWorkload.ROUTINE_PIPELINE
        assert request.prompt_version == MARKET_SCAN_PROMPT_VERSION
        digest = hashlib.sha256(self.output_text.encode()).hexdigest()
        return LLMInvocation(
            invocation_id=f"00000000-0000-7000-8000-{self.calls:012d}",
            workload=LLMWorkload.ROUTINE_PIPELINE,
            routing_version="test-routing@0.0.0",
            routing_sha256="a" * 64,
            code_git_sha="test",
            provider=LLMProviderName.OPENAI,
            model="test-model",
            reasoning_effort="minimal",
            prompt_version=request.prompt_version,
            request_sha256="b" * 64,
            input_sha256="c" * 64,
            response_id=f"response-{self.calls}",
            output_text=self.output_text,
            output_sha256=digest,
            usage=LLMUsage(input_tokens=100, output_tokens=50, total_tokens=150),
            latency_ms=10,
            status=LLMInvocationStatus.COMPLETED,
            created_at=NOW,
            completed_at=NOW,
        )


def _build_scanner(
    settings: Settings,
    *,
    provider: FakeScannerProvider,
    llm: FakeLLMGateway,
    llm_enabled: bool,
    auto_trading_pool_enabled: bool = False,
) -> tuple[MarketUniverseScanner, SystemObjectStore]:
    configured = settings.model_copy(
        update={
            "market_scanner_enabled": True,
            "market_scanner_llm_enabled": llm_enabled,
            "market_scanner_auto_trading_pool_enabled": (
                auto_trading_pool_enabled
            ),
            "market_scanner_policy_path": Path("configs/market_scanner.yaml"),
            "alpaca_api_key": SecretStr("test-key"),
            "alpaca_api_secret": SecretStr("test-secret"),
        }
    )
    upgrade_database(configured.database_url)
    ledger = EventLedger(configured.database_url)
    objects = SystemObjectStore(ledger.engine, ledger)
    objects.ensure_defaults()
    return (
        MarketUniverseScanner(
            settings=configured,
            ledger=ledger,
            objects=objects,
            market=MarketDataStore(ledger.engine),
            archive=FileRawArchive(configured.object_store_root),
            llm_gateway=llm,  # type: ignore[arg-type]
            restrictions=RestrictionRegistry.from_yaml(
                configured.restricted_securities_path
            ),
            provider_factory=lambda: provider,
        ),
        objects,
    )


def test_market_scanner_discovers_hot_names_without_execution_authority(
    settings: Settings,
) -> None:
    provider = FakeScannerProvider()
    llm = FakeLLMGateway('{"picks":[]}')
    scanner, objects = _build_scanner(
        settings, provider=provider, llm=llm, llm_enabled=False
    )

    result = asyncio.run(scanner.run_once(as_of=NOW))

    assert result["status"] == "COMPLETED"
    assert result["selected_symbols"][0] == "SNDK"
    assert "NVDA" in result["selected_symbols"]
    assert "META" not in result["selected_symbols"]
    assert "SPY" not in result["selected_symbols"]
    assert "PENNY" not in result["selected_symbols"]
    assert "ETFTEST" not in result["selected_symbols"]
    assert result["automatic_execution"] is False
    assert result["llm_status"] == "DISABLED"
    assert len(result["raw_object_ids"]) >= 4
    candidate_list = objects.get_list("candidate-list")
    assert candidate_list is not None
    assert candidate_list["members"] == sorted(result["selected_symbols"])

    calls = provider.calls
    reused = asyncio.run(scanner.run_once(as_of=NOW + timedelta(minutes=30)))
    assert reused["reused"] is True
    assert provider.calls == calls
    assert len(scanner.store.recent()) == 1


def test_market_scanner_uses_bounded_llm_rerank_then_respects_interval(
    settings: Settings,
) -> None:
    llm = FakeLLMGateway(
        '{"picks":[{"symbol":"SNDK","priority_score":10,'
        '"attention_class":"EMERGING","thesis":"AI storage attention is broadening",'
        '"risks":["gap reversal"]}]}'
    )
    scanner, _ = _build_scanner(
        settings,
        provider=FakeScannerProvider(),
        llm=llm,
        llm_enabled=True,
    )

    first = asyncio.run(scanner.run_once(as_of=NOW))
    second = asyncio.run(scanner.run_once(as_of=NOW + timedelta(hours=1)))

    assert first["llm_status"] == "COMPLETED"
    assert first["llm_invocation_id"] is not None
    sndk = next(item for item in first["candidates"] if item["symbol"] == "SNDK")
    assert sndk["llm_priority_score"] == 10
    assert "AI storage" in sndk["llm_thesis"]
    assert second["llm_status"] == "SKIPPED_INTERVAL"
    assert llm.calls == 1


def test_llm_reviewed_scan_refreshes_audited_trading_pool(
    settings: Settings,
) -> None:
    llm = FakeLLMGateway(
        '{"picks":['
        '{"symbol":"SNDK","priority_score":10,"attention_class":"HOT",'
        '"thesis":"storage momentum","risks":["gap reversal"]},'
        '{"symbol":"NVDA","priority_score":8,"attention_class":"EMERGING",'
        '"thesis":"compute follow-through","risks":["crowding"]}'
        "]}"
    )
    scanner, objects = _build_scanner(
        settings,
        provider=FakeScannerProvider(),
        llm=llm,
        llm_enabled=True,
        auto_trading_pool_enabled=True,
    )

    first = asyncio.run(scanner.run_once(as_of=NOW))
    admission = first["trading_pool_admission"]
    pool = objects.get_list("scanner-trading-pool")

    assert pool is not None
    assert admission["status"] == "UPDATED"
    assert admission["basis_scan_id"] == first["scan_id"]
    assert admission["basis_llm_invocation_id"] == first["llm_invocation_id"]
    assert admission["admitted_symbols"] == pool["members"]
    assert admission["added_symbols"] == pool["members"]
    assert admission["removed_symbols"] == []
    assert pool["revision_created_by"] == "market-universe-scanner"
    assert first["automatic_execution"] is False

    handler = ResearchCoordinatorHandler.__new__(ResearchCoordinatorHandler)
    handler.settings = scanner.settings.model_copy(
        update={
            "autonomous_coordinator_enabled": True,
            "coordinator_auto_shadow_enabled": True,
        }
    )
    handler.ledger = scanner.ledger
    handler.objects = objects

    class FakeShadow:
        def __init__(self) -> None:
            self.adoptions: list[dict[str, Any]] = []
            self.deployments: list[dict[str, Any]] = []

        def virtual_account(self) -> dict[str, str]:
            return {"initial_cash": "100000"}

        def adopt_strategy(self, **kwargs: Any) -> dict[str, str]:
            self.adoptions.append(kwargs)
            return {"adoption_id": "auto-adoption"}

        def start_deployment(self, **kwargs: Any) -> dict[str, str]:
            self.deployments.append(kwargs)
            return {"shadow_deployment_id": "auto-deployment"}

    shadow = FakeShadow()
    handler.shadow = shadow
    ready = asyncio.run(
        handler._await_shadow_adoption(
            {
                "symbol": "SNDK",
                "validation_report_id": "validation-id",
                "strategy_spec_id": "strategy-id",
                "eligible_for_human_review": False,
                "eligible_for_candidate_shadow_review": True,
                "universe_scan_id": first["scan_id"],
            }
        )
    )
    assert ready["outcome"] == "AUTO_SHADOW_ACTIVE"
    assert ready["admission_tier"] == "CANDIDATE"
    assert ready["automatic_broker_orders"] is False
    assert ready["trading_pool_authority"] == "SCANNER_LLM_TRADING_POOL"
    assert ready["scanner_admission"]["basis_llm_invocation_id"] == (
        first["llm_invocation_id"]
    )
    assert shadow.adoptions[0]["author_kind"] == "system"
    assert shadow.adoptions[0]["allow_operator_override"] is False
    assert shadow.deployments[0]["allow_operator_resume"] is False

    second = asyncio.run(scanner.run_once(as_of=NOW + timedelta(hours=1)))
    assert second["llm_status"] == "SKIPPED_INTERVAL"
    assert second["trading_pool_admission"]["status"] == (
        "HELD_PREVIOUS_LLM_REVIEW"
    )
    assert second["trading_pool_admission"]["admitted_symbols"] == pool["members"]

    objects.replace_list_members(
        slug_or_id="scanner-trading-pool",
        members=["NVDA"],
        reason="Simulate an out-of-band list revision",
        created_by="test-administrator",
    )
    stale = asyncio.run(
        handler._await_shadow_adoption(
            {
                "symbol": "SNDK",
                "validation_report_id": "validation-id",
                "strategy_spec_id": "strategy-id",
                "eligible_for_human_review": False,
                "eligible_for_candidate_shadow_review": True,
                "universe_scan_id": first["scan_id"],
            }
        )
    )
    assert stale["outcome"] == "WAITING_TRADING_UNIVERSE_APPROVAL"
    assert len(shadow.adoptions) == 1
    assert len(shadow.deployments) == 1

    repaired = asyncio.run(scanner.run_once(as_of=NOW + timedelta(hours=2)))
    assert repaired["llm_status"] == "COMPLETED"
    assert repaired["trading_pool_admission"]["status"] == "UPDATED"
    assert "SNDK" in repaired["trading_pool_admission"]["admitted_symbols"]
    assert llm.calls == 2


def test_market_scanner_rejects_llm_symbol_invention_and_falls_back(
    settings: Settings,
) -> None:
    llm = FakeLLMGateway(
        '{"picks":[{"symbol":"INVENTED","priority_score":10,'
        '"attention_class":"HOT","thesis":"unsupported","risks":[]}]}'
    )
    scanner, _ = _build_scanner(
        settings,
        provider=FakeScannerProvider(),
        llm=llm,
        llm_enabled=True,
        auto_trading_pool_enabled=True,
    )

    result = asyncio.run(scanner.run_once(as_of=NOW))

    assert result["status"] == "COMPLETED"
    assert result["llm_status"] == "FAILED_FALLBACK"
    assert result["llm_error_code"] == "ValueError"
    assert result["llm_invocation_id"] is not None
    assert "INVENTED" not in result["selected_symbols"]
    assert result["selected_symbols"][0] == "SNDK"
    assert result["trading_pool_admission"]["status"] == "WAITING_LLM_REVIEW"
    assert result["trading_pool_admission"]["admitted_symbols"] == []


def test_market_scanner_policy_preserves_yaml_keyword_ticker() -> None:
    policy = load_market_scanner_policy(Path("configs/market_scanner.yaml"))

    assert policy.version == "market_scanner@0.2.0"
    assert policy.seed_themes["SNDK"] == "ai_compute_semiconductors"
    assert policy.seed_themes["ON"] == "ai_compute_semiconductors"
    assert "META" not in policy.seed_themes


def test_market_scanner_recovers_final_json_after_provider_draft() -> None:
    output = (
        '{"picks":[{"symbol":"BROKEN"}]\n'
        "I need to produce the final answer in the requested JSON format.\n"
        '{"picks":[{"symbol":"SNDK","priority_score":8}]}'
    )

    assert _json_object(output)["picks"][0]["symbol"] == "SNDK"


def test_coordinator_cycle_is_bound_to_market_scan_lineage(settings: Settings) -> None:
    upgrade_database(settings.database_url)
    jobs = WorkflowJobStore(EventLedger(settings.database_url).engine)

    async def handler(
        _job: WorkflowJob,
        _dependencies: tuple[WorkflowJob, ...],
    ) -> dict[str, Any]:
        return {"outcome": "COMPLETED"}

    coordinator = AutonomousCoordinator(jobs, handler=handler)
    scan_id = "00000000-0000-7000-8000-000000000099"

    scanned_group, planned = coordinator.plan(
        symbols=("SNDK",),
        as_of=NOW,
        universe_scan_id=scan_id,
    )
    legacy_group, legacy = coordinator.plan(symbols=("SNDK",), as_of=NOW)

    assert scanned_group != legacy_group
    assert all(job.payload["universe_scan_id"] == scan_id for job in planned)
    assert all("universe_scan_id" not in job.payload for job in legacy)
