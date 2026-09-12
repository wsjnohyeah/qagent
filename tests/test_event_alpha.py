from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import hashlib
import json
from pathlib import Path

from fastapi.testclient import TestClient
import pytest
from pydantic import ValidationError
from sqlalchemy import insert

from agentic_quant.api import create_app
from agentic_quant.config import Settings
from agentic_quant.database import catalysts
from agentic_quant.document_store import DocumentStore
from agentic_quant.domain import (
    LLMProviderName,
    LLMUsage,
    SourceDocument,
    SourceTier,
    StockBar,
)
from agentic_quant.event_alpha import (
    EVENT_ALPHA_GATE_VERSION,
    EVENT_ASSESSMENT_PROMPT_VERSION,
    EVENT_ASSESSMENT_SCHEMA_VERSION,
    EVENT_CARD_PROMPT_VERSION,
    EVENT_CARD_SCHEMA_VERSION,
    EventAlphaService,
    EventAlphaStore,
    EventEntryConfirmation,
    EventPlaybookProposal,
    EventRecommendation,
)
from agentic_quant.ids import uuid7
from agentic_quant.ledger import EventLedger
from agentic_quant.llm import (
    LLMGateway,
    LLMProviderResult,
    LLMRequest,
    LLMRoutingConfig,
    load_llm_routing_config,
)
from agentic_quant.llm_budget import LLMBudgetManager, load_llm_budget_policy
from agentic_quant.llm_store import LLMStore
from agentic_quant.market_calendar import MarketSessionClock
from agentic_quant.market_store import MarketDataStore
from agentic_quant.migrations import upgrade_database
from agentic_quant.research_store import ResearchStore


ROOT = Path(__file__).parents[1]
AS_OF = datetime(2026, 9, 12, 18, tzinfo=UTC)


class EventAlphaProvider:
    def __init__(self, routing: LLMRoutingConfig) -> None:
        self.name = LLMProviderName.OPENAI
        self.config = routing.providers[self.name]
        self.calls: list[str] = []

    async def complete(self, request: LLMRequest) -> LLMProviderResult:
        self.calls.append(request.prompt_version)
        payload = json.loads(request.input_text)
        if request.prompt_version == EVENT_CARD_PROMPT_VERSION:
            document = payload["documents"][0]
            output = {
                "schema_version": EVENT_CARD_SCHEMA_VERSION,
                "symbol": payload["catalyst"]["symbol"],
                "event_type": "contract_award",
                "direction": "BULLISH",
                "mechanism": "material demand acceleration",
                "narrative": "A material contract may pull demand into future periods.",
                "novelty_score": "0.80",
                "surprise_score": "0.75",
                "source_quality_score": "0.90",
                "confidence": "0.72",
                "generalized_tags": ["contract_award", "demand_acceleration"],
                "expected_horizons": [1, 2, 5],
                "evidence_quotes": [
                    {
                        "citation_id": document["citation_id"],
                        "quote": document["text"].split("\n", 1)[0],
                    }
                ],
                "risk_factors": ["The order may already be reflected in price."],
            }
        elif request.prompt_version == EVENT_ASSESSMENT_PROMPT_VERSION:
            current = payload["current_event"]
            analogs = payload["analogs"]["5"]
            output = {
                "schema_version": EVENT_ASSESSMENT_SCHEMA_VERSION,
                "recommendation": "RESEARCH_LONG",
                "selected_horizon_sessions": 5,
                "confidence": "0.68",
                "playbook_name": "Cross-issuer contract continuation",
                "event_pattern": (
                    "Material contract awards with demand acceleration and bounded gaps."
                ),
                "analogy_reasoning": (
                    "The current event shares its mechanism with several prior issuers, "
                    "while the mixed analog set limits confidence."
                ),
                "entry_confirmation": {
                    "maximum_opening_gap_fraction": "0.12",
                    "minimum_relative_volume": "1.5",
                    "maximum_event_age_hours": 48,
                },
                "invalidation_conditions": [
                    "The issuer retracts or materially reduces the contract."
                ],
                "cited_event_ids": [
                    f"EVENT:{current['event_card_id']}",
                    *[
                        f"EVENT:{item['event_card_id']}"
                        for item in analogs[:3]
                    ],
                ],
            }
        else:
            raise AssertionError(request.prompt_version)
        text = json.dumps(output)
        return LLMProviderResult(
            response_id=f"response-{len(self.calls)}",
            output_text=text,
            usage=LLMUsage(input_tokens=500, output_tokens=200, total_tokens=700),
        )

    async def aclose(self) -> None:
        return None


def _services(
    settings: Settings,
) -> tuple[
    EventLedger,
    EventAlphaProvider,
    EventAlphaStore,
    ResearchStore,
    EventAlphaService,
]:
    upgrade_database(settings.database_url)
    ledger = EventLedger(settings.database_url)
    routing = load_llm_routing_config(ROOT / "configs/model_routing.yaml")
    provider = EventAlphaProvider(routing)
    gateway = LLMGateway(
        routing=routing,
        providers={LLMProviderName.OPENAI: provider},
        store=LLMStore(ledger.engine),
        ledger=ledger,
        budget_manager=LLMBudgetManager(
            ledger.engine,
            load_llm_budget_policy(ROOT / "configs/llm_budget.yaml"),
        ),
        code_git_sha="test-sha",
    )
    store = EventAlphaStore(ledger.engine, ledger)
    research = ResearchStore(ledger.engine)
    return (
        ledger,
        provider,
        store,
        research,
        EventAlphaService(
            store,
            research,
            gateway,
            ledger=ledger,
            code_git_sha="test-sha",
        ),
    )


def _document(
    *,
    symbol: str,
    published_at: datetime,
    ingested_at: datetime,
    corrected: bool = False,
) -> SourceDocument:
    title = f"{symbol} wins material infrastructure contract"
    return SourceDocument(
        document_id=uuid7(),
        provider_document_id=f"{symbol}-{published_at.isoformat()}",
        provider="fixture_news",
        canonical_url=f"https://example.invalid/{symbol.casefold()}",
        source_kind="news",
        source_tier=SourceTier.SECONDARY,
        publisher="Fixture Wire",
        title=title,
        summary="The company announced a multi-year customer award.",
        body_text="Management expects the agreement to accelerate demand.",
        symbols=(symbol,),
        issuer_name=f"{symbol} Inc.",
        cik=None,
        published_at=published_at,
        updated_at=(published_at + timedelta(hours=2) if corrected else published_at),
        ingested_at=ingested_at,
        raw_object_id="TEST_RAW",
    )


def _persist_catalyst(
    document_store: DocumentStore,
    document: SourceDocument,
) -> str:
    stored = document_store.upsert_document(document, "TEST_RAW")
    result = document_store.resolve_catalyst(
        document.model_copy(update={"document_id": stored.document_id}),
        document_id=stored.document_id,
    )
    return result.catalyst_id


def _record_card(
    store: EventAlphaStore,
    *,
    symbol: str,
    event_time: datetime,
) -> dict[str, object]:
    catalyst_id = uuid7()
    with store.engine.begin() as connection:
        connection.execute(
            insert(catalysts).values(
                catalyst_id=catalyst_id,
                canonical_key=hashlib.sha256(catalyst_id.encode()).hexdigest(),
                catalyst_type="contract",
                primary_symbol=symbol,
                event_time=event_time,
                available_from=event_time,
                last_updated_at=event_time,
                headline=f"{symbol} historical contract fixture",
                primary_source_document_id=None,
                status="ACTIVE",
                source_count=1,
            )
        )
    return store.record_card(
        {
            "event_card_id": uuid7(),
            "catalyst_id": catalyst_id,
            "symbol": symbol,
            "event_time": event_time,
            "available_from": event_time,
            "availability_basis": "PROVIDER_PUBLISHED_REPLAY",
            "as_of": AS_OF,
            "schema_version": EVENT_CARD_SCHEMA_VERSION,
            "prompt_version": EVENT_CARD_PROMPT_VERSION,
            "input_sha256": hashlib.sha256(
                f"{symbol}:{event_time.isoformat()}".encode()
            ).hexdigest(),
            "status": "COMPLETED",
            "event_type": "contract_award",
            "direction": "BULLISH",
            "mechanism": "material demand acceleration",
            "novelty_score": Decimal("0.8"),
            "surprise_score": Decimal("0.75"),
            "source_quality_score": Decimal("0.9"),
            "confidence": Decimal("0.7"),
            "generalized_tags_json": ["contract_award", "demand_acceleration"],
            "expected_horizons_json": [1, 2, 5],
            "evidence_json": {"fixture": True},
            "card_json": {"narrative": "Fixture historical case"},
            "llm_invocation_id": None,
            "rejection_reason": None,
            "code_git_sha": "test-sha",
            "created_at": AS_OF,
        }
    )


def _record_outcome(
    store: EventAlphaStore,
    *,
    event_card_id: str,
    total_return: Decimal,
) -> None:
    store.record_outcome(
        {
            "event_outcome_id": uuid7(),
            "event_card_id": event_card_id,
            "horizon_sessions": 5,
            "entry_time": AS_OF - timedelta(days=20),
            "exit_time": AS_OF - timedelta(days=15),
            "available_from": AS_OF - timedelta(days=15),
            "entry_price": Decimal("100"),
            "exit_price": Decimal("100") * (Decimal("1") + total_return),
            "total_return": total_return,
            "maximum_favorable_return": max(total_return, Decimal("0.04")),
            "maximum_adverse_return": min(total_return, Decimal("-0.02")),
            "data_sha256": hashlib.sha256(
                f"{event_card_id}:5".encode()
            ).hexdigest(),
            "created_at": AS_OF,
        }
    )


def _daily_bars(symbol: str, count: int = 10) -> tuple[StockBar, ...]:
    clock = MarketSessionClock("XNYS")
    sessions = clock.calendar.sessions_in_range("2026-08-03", "2026-09-01")[:count]
    price = Decimal("100")
    values = []
    for index, session in enumerate(sessions):
        event_time = datetime.combine(session.date(), datetime.min.time(), tzinfo=UTC)
        close = price + Decimal(index + 1)
        values.append(
            StockBar(
                bar_id=uuid7(),
                symbol=symbol,
                timeframe="1Day",
                event_time=event_time,
                available_from=clock.daily_bar_available_from(event_time),
                open=price,
                high=close + Decimal("1"),
                low=price - Decimal("1"),
                close=close,
                volume=1_000_000,
                trade_count=10_000,
                vwap=(price + close) / Decimal("2"),
                source="synthetic",
                feed="test",
                raw_object_id="TEST_RAW",
                ingested_at=AS_OF,
            )
        )
        price = close
    return tuple(values)


def test_event_card_is_citation_bound_and_corrected_backfill_fails_closed(
    settings: Settings,
) -> None:
    ledger, provider, store, _, service = _services(settings)
    documents = DocumentStore(ledger.engine)
    catalyst_id = _persist_catalyst(
        documents,
        _document(
            symbol="AAPL",
            published_at=AS_OF - timedelta(hours=2),
            ingested_at=AS_OF - timedelta(hours=1),
        ),
    )
    card = asyncio.run(service.extract_card(catalyst_id, as_of=AS_OF))

    assert card["status"] == "COMPLETED"
    assert card["availability_basis"] == "FORWARD_FIRST_SEEN"
    assert card["event_type"] == "contract_award"
    assert provider.calls == [EVENT_CARD_PROMPT_VERSION]

    corrected_id = _persist_catalyst(
        documents,
        _document(
            symbol="MSFT",
            published_at=AS_OF - timedelta(days=60),
            ingested_at=AS_OF,
            corrected=True,
        ),
    )
    with pytest.raises(ValueError, match="corrected backfilled"):
        store.catalyst_evidence(corrected_id, as_of=AS_OF)


def test_event_outcomes_use_first_causal_open_and_fixed_horizons(
    settings: Settings,
) -> None:
    ledger, _, store, _, service = _services(settings)
    bars = _daily_bars("AAPL")
    MarketDataStore(ledger.engine).insert_bars(bars, raw_object_id="TEST_RAW")
    card = _record_card(
        store,
        symbol="AAPL",
        event_time=(
            MarketSessionClock("XNYS").daily_bar_session_open(bars[0].event_time)
            - timedelta(hours=1)
        ),
    )
    outcomes = service.materialize_outcomes(
        str(card["event_card_id"]), observed_as_of=AS_OF
    )

    assert [item["horizon_sessions"] for item in outcomes] == [1, 2, 5]
    assert outcomes[0]["entry_time"] == MarketSessionClock(
        "XNYS"
    ).daily_bar_session_open(bars[0].event_time)
    assert Decimal(str(outcomes[2]["total_return"])) > 0

    first_open = MarketSessionClock("XNYS").daily_bar_session_open(bars[0].event_time)
    exact_open_card = _record_card(
        store,
        symbol="AAPL",
        event_time=first_open,
    )
    exact_open_outcomes = service.materialize_outcomes(
        str(exact_open_card["event_card_id"]), observed_as_of=AS_OF
    )
    assert exact_open_outcomes[0]["entry_time"] == MarketSessionClock(
        "XNYS"
    ).daily_bar_session_open(bars[1].event_time)


def test_cross_symbol_analog_statistics_create_research_only_playbook(
    settings: Settings,
) -> None:
    ledger, provider, store, _, service = _services(settings)
    returns = (
        Decimal("0.03"),
        Decimal("0.02"),
        Decimal("0.04"),
        Decimal("0.01"),
        Decimal("-0.005"),
    )
    for index, (symbol, value) in enumerate(
        zip(("AAPL", "MSFT", "NVDA", "DELL", "ORCL"), returns, strict=True)
    ):
        card = _record_card(
            store,
            symbol=symbol,
            event_time=AS_OF - timedelta(days=100 - index),
        )
        _record_outcome(
            store,
            event_card_id=str(card["event_card_id"]),
            total_return=value,
        )

    documents = DocumentStore(ledger.engine)
    catalyst_id = _persist_catalyst(
        documents,
        _document(
            symbol="SMCI",
            published_at=AS_OF - timedelta(hours=2),
            ingested_at=AS_OF - timedelta(hours=1),
        ),
    )
    current = asyncio.run(service.extract_card(catalyst_id, as_of=AS_OF))
    assessment = asyncio.run(
        service.assess_card(str(current["event_card_id"]), as_of=AS_OF)
    )

    assert assessment["status"] == "PLAYBOOK_CANDIDATE"
    assert assessment["gate_assessment_json"] == {
        "version": EVENT_ALPHA_GATE_VERSION,
        "eligible_for_playbook_candidate": True,
        "eligible_for_event_shadow": False,
        "failures": [],
        "note": (
            "Research candidate only. Event-aware walk-forward replay and an exact "
            "Shadow execution certificate are not implemented in V1."
        ),
    }
    playbooks = store.playbooks()
    assert len(playbooks) == 1
    assert playbooks[0]["holding_period_sessions"] == 5
    assert playbooks[0]["status"] == "RESEARCH_CANDIDATE"
    assert provider.calls == [
        EVENT_CARD_PROMPT_VERSION,
        EVENT_ASSESSMENT_PROMPT_VERSION,
    ]

    repeated = asyncio.run(
        service.assess_card(
            str(current["event_card_id"]),
            as_of=AS_OF + timedelta(hours=1),
        )
    )
    assert repeated["event_assessment_id"] == assessment["event_assessment_id"]
    assert provider.calls == [
        EVENT_CARD_PROMPT_VERSION,
        EVENT_ASSESSMENT_PROMPT_VERSION,
    ]


def test_outlier_driven_event_pattern_fails_deterministic_gate(
    settings: Settings,
) -> None:
    _, _, _, _, service = _services(settings)
    statistics = service._statistics(
        [
            {
                "symbol": symbol,
                "total_return": value,
                "availability_basis": "PROVIDER_PUBLISHED_REPLAY",
            }
            for symbol, value in zip(
                ("AAPL", "MSFT", "NVDA", "DELL", "ORCL"),
                (
                    Decimal("1.00"),
                    Decimal("-0.01"),
                    Decimal("-0.01"),
                    Decimal("-0.01"),
                    Decimal("-0.01"),
                ),
                strict=True,
            )
        ]
    )
    proposal = EventPlaybookProposal(
        schema_version=EVENT_ASSESSMENT_SCHEMA_VERSION,
        recommendation=EventRecommendation.RESEARCH_LONG,
        selected_horizon_sessions=5,
        confidence=Decimal("0.9"),
        playbook_name="Outlier-driven fixture",
        event_pattern="One large event dominates otherwise losing analog outcomes.",
        analogy_reasoning="The aggregate mean is positive only because one case dominates.",
        entry_confirmation=EventEntryConfirmation(
            maximum_opening_gap_fraction=Decimal("0.1"),
            minimum_relative_volume=Decimal("1.5"),
            maximum_event_age_hours=24,
        ),
        invalidation_conditions=("The event thesis is contradicted.",),
        cited_event_ids=("EVENT:fixture",),
    )

    gate = service._gate(statistics, proposal)

    assert gate["eligible_for_playbook_candidate"] is False
    assert "median analog return is not positive" in gate["failures"]
    assert "analog return depends on the best event" in gate["failures"]


def test_event_alpha_is_safe_off_and_visible_in_control_center(
    settings: Settings,
) -> None:
    with TestClient(create_app(settings)) as client:
        status = client.get("/v1/event-alpha/status")
        page = client.get("/")

    assert status.status_code == 200
    assert status.json()["enabled"] is False
    assert status.json()["predictive_ml_used"] is False
    assert status.json()["shadow_eligible"] is False
    assert "Event Alpha" in page.text


def test_event_alpha_requires_paid_autonomous_coordinator(
    settings: Settings,
) -> None:
    values = settings.model_dump()
    values["event_alpha_enabled"] = True
    with pytest.raises(ValidationError, match="autonomous coordinator and paid research"):
        Settings(_env_file=None, **values)
