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
from agentic_quant.control_plane import SystemObjectStore
from agentic_quant.database import catalysts
from agentic_quant.document_store import DocumentStore
from agentic_quant.domain import (
    LLMProviderName,
    LLMUsage,
    LLMWorkload,
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
from agentic_quant.risk import RestrictionRegistry, RiskPolicy
from agentic_quant.shadow import ShadowRuntime


ROOT = Path(__file__).parents[1]
AS_OF = datetime(2026, 9, 12, 18, tzinfo=UTC)


class EventAlphaProvider:
    def __init__(self, routing: LLMRoutingConfig) -> None:
        self.name = LLMProviderName.META
        self.config = routing.providers[self.name]
        self.calls: list[str] = []

    async def complete(self, request: LLMRequest) -> LLMProviderResult:
        assert request.workload == LLMWorkload.EVENT_RESEARCH
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
                "expected_horizons": [1, 2, 5, 10, 20],
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
                    "minimum_relative_volume": "0",
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
        providers={LLMProviderName.META: provider},
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
    availability_basis: str = "PROVIDER_PUBLISHED_REPLAY",
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
            "availability_basis": availability_basis,
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
            "expected_horizons_json": [1, 2, 5, 10, 20],
            "evidence_json": {
                "fixture": True,
                "documents": [{"source_kind": "news"}],
            },
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
    available_from: datetime | None = None,
) -> None:
    store.record_outcome(
        {
            "event_outcome_id": uuid7(),
            "event_card_id": event_card_id,
            "horizon_sessions": 5,
            "entry_time": AS_OF - timedelta(days=20),
            "exit_time": AS_OF - timedelta(days=15),
            "available_from": available_from or AS_OF - timedelta(days=15),
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


def _daily_bars(symbol: str, count: int = 22) -> tuple[StockBar, ...]:
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

    assert [item["horizon_sessions"] for item in outcomes] == [1, 2, 5, 10, 20]
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
        "analog_count": 5,
        "unique_symbol_count": 5,
        "note": (
            "Research candidate only until later chronological news events pass "
            "the deterministic holdout gate and bind an exact Shadow certificate."
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
            minimum_relative_volume=Decimal("0"),
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
    assert status.json()["llm_workload"] == "event_research"
    assert status.json()["llm_provider"] == "meta"
    assert status.json()["llm_model"] == "muse-spark-1.3"
    assert "Event Alpha" in page.text


def test_event_card_prompt_exposes_strict_mechanism_storage_bound(
    settings: Settings,
) -> None:
    *_, service = _services(settings)

    assert EVENT_CARD_SCHEMA_VERSION == "event_card@0.2.0"
    assert "mechanism between 3 and 120 characters" in service._card_instructions()


def test_event_cycle_audits_unsafe_evidence_and_continues_to_next_card(
    settings: Settings,
) -> None:
    ledger, provider, store, _, service = _services(settings)
    documents = DocumentStore(ledger.engine)
    _persist_catalyst(
        documents,
        _document(
            symbol="MSFT",
            published_at=AS_OF - timedelta(days=60),
            ingested_at=AS_OF,
            corrected=True,
        ),
    )
    _persist_catalyst(
        documents,
        _document(
            symbol="AAPL",
            published_at=AS_OF - timedelta(hours=2),
            ingested_at=AS_OF - timedelta(hours=1),
        ),
    )

    result = asyncio.run(
        service.run_cycle(
            symbols=("MSFT", "AAPL"),
            as_of=AS_OF,
            max_cards=1,
        )
    )

    assert result["status"] == "COMPLETED"
    assert result["evidence_rejections"] == 1
    assert result["cards_recorded"] == 1
    assert provider.calls == [EVENT_CARD_PROMPT_VERSION]
    cards = store.cards(limit=10)
    assert {value["status"] for value in cards} == {"COMPLETED", "REJECTED"}
    rejected = next(value for value in cards if value["status"] == "REJECTED")
    assert rejected["llm_invocation_id"] is None
    assert "corrected backfilled" in rejected["rejection_reason"]


def test_event_cycle_excludes_sec_evidence_from_news_research_without_llm_spend(
    settings: Settings,
) -> None:
    ledger, provider, store, _, service = _services(settings)
    documents = DocumentStore(ledger.engine)
    document = _document(
        symbol="AAPL",
        published_at=AS_OF - timedelta(hours=2),
        ingested_at=AS_OF - timedelta(hours=1),
    ).model_copy(
        update={
            "provider": "sec_edgar",
            "source_kind": "sec_filing",
            "source_tier": SourceTier.PRIMARY,
            "title": "AAPL 8-K filing",
            "summary": "8-K; report date 2026-09-12",
            "body_text": None,
        }
    )
    _persist_catalyst(documents, document)

    result = asyncio.run(
        service.run_cycle(symbols=("AAPL",), as_of=AS_OF, max_cards=1)
    )

    assert result["status"] == "COMPLETED"
    assert result["selected_catalysts"] == 0
    assert result["evidence_rejections"] == 0
    assert result["cards_recorded"] == 0
    assert provider.calls == []
    assert store.cards(limit=1) == []


def test_event_cycle_reassesses_an_older_card_when_analogs_change(
    settings: Settings,
) -> None:
    _, provider, store, _, service = _services(settings)
    current = _record_card(
        store,
        symbol="SMCI",
        event_time=AS_OF - timedelta(days=1),
    )
    initial = asyncio.run(
        service.assess_card(str(current["event_card_id"]), as_of=AS_OF)
    )
    assert initial["status"] == "INSUFFICIENT_ANALOGS"
    assert provider.calls == []

    for index, (symbol, value) in enumerate(
        zip(
            ("AAPL", "MSFT", "NVDA", "DELL", "ORCL"),
            (
                Decimal("0.03"),
                Decimal("0.02"),
                Decimal("0.04"),
                Decimal("0.01"),
                Decimal("-0.005"),
            ),
            strict=True,
        )
    ):
        analog = _record_card(
            store,
            symbol=symbol,
            event_time=AS_OF - timedelta(days=100 - index),
        )
        _record_outcome(
            store,
            event_card_id=str(analog["event_card_id"]),
            total_return=value,
        )

    result = asyncio.run(service.run_cycle(symbols=(), as_of=AS_OF, max_cards=1))

    assert result["status"] == "COMPLETED"
    assert EVENT_ASSESSMENT_PROMPT_VERSION in provider.calls
    current_assessments = [
        value
        for value in store.assessments(limit=20)
        if value["event_card_id"] == current["event_card_id"]
    ]
    assert {value["status"] for value in current_assessments} == {
        "INSUFFICIENT_ANALOGS",
        "PLAYBOOK_CANDIDATE",
    }
    assert len(store.playbooks()) == 1


def test_news_playbook_requires_later_holdouts_before_forward_match(
    settings: Settings,
) -> None:
    ledger, provider, store, research, service = _services(settings)
    for index, (symbol, value) in enumerate(
        zip(
            ("AAPL", "MSFT", "NVDA", "DELL", "ORCL"),
            (
                Decimal("0.03"),
                Decimal("0.02"),
                Decimal("0.04"),
                Decimal("0.01"),
                Decimal("-0.005"),
            ),
            strict=True,
        )
    ):
        discovery = _record_card(
            store,
            symbol=symbol,
            event_time=AS_OF - timedelta(days=200 - index),
        )
        _record_outcome(
            store,
            event_card_id=str(discovery["event_card_id"]),
            total_return=value,
            available_from=AS_OF - timedelta(days=150),
        )
    anchor = _record_card(
        store,
        symbol="SMCI",
        event_time=AS_OF - timedelta(days=100),
    )
    assessment = asyncio.run(
        service.assess_card(
            str(anchor["event_card_id"]),
            as_of=AS_OF - timedelta(days=90),
        )
    )
    assert assessment["status"] == "PLAYBOOK_CANDIDATE"
    assert provider.calls == [EVENT_ASSESSMENT_PROMPT_VERSION]
    assert service.validate_playbooks(as_of=AS_OF - timedelta(days=90))[0][
        "status"
    ] == "INSUFFICIENT_HOLDOUT"

    for index, (symbol, value) in enumerate(
        zip(
            ("AVGO", "ANET", "VRT"),
            (Decimal("0.04"), Decimal("0.02"), Decimal("0.01")),
            strict=True,
        )
    ):
        holdout = _record_card(
            store,
            symbol=symbol,
            event_time=AS_OF - timedelta(days=80 - index * 10),
        )
        _record_outcome(
            store,
            event_card_id=str(holdout["event_card_id"]),
            total_return=value,
            available_from=AS_OF - timedelta(days=40 - index * 5),
        )

    validation = service.validate_playbooks(as_of=AS_OF)[0]
    assert validation["status"] == "SHADOW_ELIGIBLE"
    assert validation["holdout_statistics_json"]["analog_count"] == 3
    assert store.health_summary()["event_alpha_shadow_eligible_playbooks"] == 1

    objects = SystemObjectStore(ledger.engine, ledger)
    objects.ensure_defaults()
    shadow = ShadowRuntime(
        ledger.engine,
        research,
        objects,
        risk_policy=RiskPolicy.from_yaml(settings.risk_policy_path),
        restrictions=RestrictionRegistry.from_yaml(
            settings.restricted_securities_path
        ),
    )
    service.shadow = shadow
    service.auto_shadow_enabled = True

    trigger_time = datetime.now(UTC) + timedelta(minutes=1)
    trigger = _record_card(
        store,
        symbol="AAPL",
        event_time=trigger_time,
        availability_basis="FORWARD_FIRST_SEEN",
    )
    matches = service.activate_forward_matches(
        as_of=trigger_time + timedelta(hours=1)
    )
    assert len(matches) == 1
    assert matches[0]["event_card_id"] == trigger["event_card_id"]
    assert matches[0]["status"] == "SHADOW_STARTED"
    assert matches[0]["strategy_spec_id"] is not None
    deployment = shadow.deployment(str(matches[0]["shadow_deployment_id"]))
    assert deployment["status"] == "ACTIVE"
    assert deployment["admission_tier"] == "CANDIDATE"
    assert deployment["strategy_type"] == "event_playbook"

    for index, symbol in enumerate(("AMD", "MU", "ARM", "MRVL")):
        negative = _record_card(
            store,
            symbol=symbol,
            event_time=AS_OF - timedelta(days=35 - index * 5),
        )
        _record_outcome(
            store,
            event_card_id=str(negative["event_card_id"]),
            total_return=Decimal("-0.10"),
            available_from=AS_OF - timedelta(days=10 - index),
        )
    invalidated = service.validate_playbooks(as_of=AS_OF + timedelta(days=1))[0]
    assert invalidated["status"] == "REJECTED"
    assert store.health_summary()["event_alpha_shadow_eligible_playbooks"] == 0
    adoption = shadow.adoption(str(deployment["adoption_id"]))
    with pytest.raises(ValueError, match="stale or no longer Shadow eligible"):
        shadow.adoption_preview(
            strategy_spec_id=str(matches[0]["strategy_spec_id"]),
            validation_report_id=str(adoption["validation_report_id"]),
            admission_tier="CANDIDATE",
        )


def test_discovery_qualified_playbook_can_collect_exploratory_forward_shadow(
    settings: Settings,
) -> None:
    ledger, _, store, research, service = _services(settings)
    for index, (symbol, value) in enumerate(
        zip(
            ("AAPL", "MSFT", "NVDA", "DELL", "ORCL"),
            (
                Decimal("0.03"),
                Decimal("0.02"),
                Decimal("0.04"),
                Decimal("0.01"),
                Decimal("-0.005"),
            ),
            strict=True,
        )
    ):
        discovery = _record_card(
            store,
            symbol=symbol,
            event_time=AS_OF - timedelta(days=200 - index),
        )
        _record_outcome(
            store,
            event_card_id=str(discovery["event_card_id"]),
            total_return=value,
            available_from=AS_OF - timedelta(days=150),
        )
    anchor = _record_card(
        store,
        symbol="SMCI",
        event_time=AS_OF - timedelta(days=100),
    )
    assessment = asyncio.run(
        service.assess_card(
            str(anchor["event_card_id"]),
            as_of=AS_OF - timedelta(days=90),
        )
    )
    assert assessment["status"] == "PLAYBOOK_CANDIDATE"
    validation = service.validate_playbooks(as_of=AS_OF - timedelta(days=90))[0]
    assert validation["status"] == "INSUFFICIENT_HOLDOUT"

    objects = SystemObjectStore(ledger.engine, ledger)
    objects.ensure_defaults()
    shadow = ShadowRuntime(
        ledger.engine,
        research,
        objects,
        risk_policy=RiskPolicy.from_yaml(settings.risk_policy_path),
        restrictions=RestrictionRegistry.from_yaml(
            settings.restricted_securities_path
        ),
    )
    service.shadow = shadow
    service.auto_shadow_enabled = True

    trigger_time = datetime.now(UTC) + timedelta(minutes=1)
    trigger = _record_card(
        store,
        symbol="AAPL",
        event_time=trigger_time,
        availability_basis="FORWARD_FIRST_SEEN",
    )
    matches = service.activate_forward_matches(
        as_of=trigger_time + timedelta(hours=1)
    )

    assert len(matches) == 1
    assert matches[0]["event_card_id"] == trigger["event_card_id"]
    assert matches[0]["status"] == "SHADOW_STARTED"
    deployment = shadow.deployment(str(matches[0]["shadow_deployment_id"]))
    assert deployment["gate_assessment"]["candidate_shadow"]["status"] == (
        "EVENT_EXPLORATORY_FORWARD"
    )


def test_equivalent_event_hypothesis_reuses_existing_playbook_family(
    settings: Settings,
) -> None:
    _, _, store, _, service = _services(settings)
    for index, (symbol, value) in enumerate(
        zip(
            ("AAPL", "MSFT", "NVDA", "DELL", "ORCL"),
            (
                Decimal("0.03"),
                Decimal("0.02"),
                Decimal("0.04"),
                Decimal("0.01"),
                Decimal("-0.005"),
            ),
            strict=True,
        )
    ):
        discovery = _record_card(
            store,
            symbol=symbol,
            event_time=AS_OF - timedelta(days=200 - index),
        )
        _record_outcome(
            store,
            event_card_id=str(discovery["event_card_id"]),
            total_return=value,
            available_from=AS_OF - timedelta(days=150),
        )
    first = _record_card(
        store,
        symbol="SMCI",
        event_time=AS_OF - timedelta(days=100),
    )
    second = _record_card(
        store,
        symbol="VRT",
        event_time=AS_OF - timedelta(days=90),
    )

    first_assessment = asyncio.run(
        service.assess_card(str(first["event_card_id"]), as_of=AS_OF)
    )
    second_assessment = asyncio.run(
        service.assess_card(str(second["event_card_id"]), as_of=AS_OF)
    )

    assert first_assessment["status"] == "PLAYBOOK_CANDIDATE"
    assert second_assessment["status"] == "PLAYBOOK_FAMILY_EVIDENCE"
    assert len(store.playbooks()) == 1


def test_unprocessed_event_candidates_are_balanced_across_symbols(
    settings: Settings,
) -> None:
    ledger, _, store, _, _ = _services(settings)
    documents = DocumentStore(ledger.engine)
    for days_ago in (100, 99, 98):
        _persist_catalyst(
            documents,
            _document(
                symbol="AAPL",
                published_at=AS_OF - timedelta(days=days_ago),
                ingested_at=AS_OF - timedelta(days=days_ago),
            ),
        )
    _persist_catalyst(
        documents,
        _document(
            symbol="MSFT",
            published_at=AS_OF - timedelta(days=1),
            ingested_at=AS_OF - timedelta(days=1),
        ),
    )

    candidates = store.unprocessed_catalysts(
        symbols=("AAPL", "MSFT"),
        as_of=AS_OF,
        limit=2,
    )

    assert [item["primary_symbol"] for item in candidates] == ["AAPL", "MSFT"]


def test_event_alpha_requires_paid_autonomous_coordinator(
    settings: Settings,
) -> None:
    values = settings.model_dump()
    values["event_alpha_enabled"] = True
    with pytest.raises(ValidationError, match="autonomous coordinator and paid research"):
        Settings(_env_file=None, **values)
