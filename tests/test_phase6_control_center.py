from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
import time

from fastapi.testclient import TestClient
import pytest
from pydantic import SecretStr
from sqlalchemy import insert, select, update

from agentic_quant.api import create_app
from agentic_quant.config import Settings
from agentic_quant.database import (
    admin_sessions,
    validation_reports,
    virtual_accounts,
)
from agentic_quant.domain import BacktestCostModel, StockBar
from agentic_quant.domain import LLMUsage
from agentic_quant.ids import uuid7
from agentic_quant.ledger import EventLedger
from agentic_quant.llm import LLMProviderResult, LLMRequest, ResponsesAPIProvider
from agentic_quant.market_calendar import MarketSessionClock
from agentic_quant.market_store import MarketDataStore
from agentic_quant.migrations import upgrade_database
from agentic_quant.research import (
    BACKTEST_ENGINE_VERSION,
    FEATURE_SET_VERSION,
    ResearchBacktester,
    default_strategy_spec,
    research_code_sha256,
)
from agentic_quant.research_store import ResearchStore, _canonical_hash
from agentic_quant.risk import (
    BASELINE_EXECUTION_PROFILE_VERSION,
    RestrictionRegistry,
    RiskPolicy,
)


def test_admin_session_is_required_and_csrf_protects_writes(
    settings: Settings,
) -> None:
    secured = settings.model_copy(
        update={
            "auth_required": True,
            "admin_username": "operator",
            "admin_password": SecretStr("correct horse battery staple"),
            "session_secret": SecretStr("s" * 64),
        }
    )
    with TestClient(create_app(secured)) as client:
        assert client.get("/").status_code == 200
        assert client.get("/health/live").status_code == 200
        assert client.get("/v1/system/status").status_code == 401
        assert client.post(
            "/v1/auth/login",
            json={"username": "operator", "password": "wrong"},
        ).status_code == 401
        login = client.post(
            "/v1/auth/login",
            json={
                "username": "operator",
                "password": "correct horse battery staple",
            },
        )
        assert login.status_code == 200
        raw_session_token = client.cookies.get("aq_admin_session")
        assert raw_session_token
        assert client.get("/v1/auth/session").json()["authenticated"] is True
        no_csrf = client.post(
            "/v1/commands/pause",
            json={"reason": "Verify CSRF protection"},
        )
        assert no_csrf.status_code == 403
        csrf = client.cookies.get("aq_csrf")
        proposed = client.post(
            "/v1/commands/pause",
            headers={"X-CSRF-Token": csrf},
            json={"reason": "Verify two-step administrator action"},
        )
        assert proposed.status_code == 200
        assert proposed.json()["status"] == "PENDING_CONFIRMATION"
        logged_out = client.post(
            "/v1/auth/logout",
            headers={"X-CSRF-Token": csrf},
        )
        assert logged_out.status_code == 200
        assert client.get("/v1/system/status").status_code == 401

    ledger = EventLedger(secured.database_url)
    with ledger.engine.connect() as connection:
        rows = connection.execute(select(admin_sessions)).all()
    assert len(rows) == 1
    assert len(rows[0].token_sha256) == 64
    assert rows[0].token_sha256 != raw_session_token


def test_lists_are_versioned_and_admin_actions_require_exact_confirmation(
    settings: Settings,
) -> None:
    with TestClient(create_app(settings)) as client:
        lists = client.get("/v1/lists").json()
        assert {item["slug"] for item in lists} == {
            "benchmarks",
            "candidate-list",
            "focus-watchlist",
            "restricted",
            "shadow-active",
            "trading-universe",
        }
        focus = next(item for item in lists if item["slug"] == "focus-watchlist")
        proposed = client.post(
            "/v1/actions",
            json={
                "action_type": "list.add",
                "target_type": "list",
                "target_id": focus["list_id"],
                "parameters": {"members": ["aapl", "msft"]},
                "reason": "Add the initial focus symbols",
            },
        ).json()
        unchanged = client.get(f"/v1/lists/{focus['list_id']}").json()
        assert unchanged["members"] == []
        wrong = client.post(
            f"/v1/actions/{proposed['action_request_id']}/confirm",
            json={"confirmation_phrase": "CONFIRM WRONG"},
        )
        assert wrong.status_code == 409
        confirmed = client.post(
            f"/v1/actions/{proposed['action_request_id']}/confirm",
            json={"confirmation_phrase": proposed["confirmation_phrase"]},
        )
        assert confirmed.status_code == 200
        assert confirmed.json()["status"] == "EXECUTED"
        updated = client.get(f"/v1/lists/{focus['list_id']}").json()
        assert updated["members"] == ["AAPL", "MSFT"]
        assert updated["current_revision"] == 2
        duplicate = client.post(
            f"/v1/actions/{proposed['action_request_id']}/confirm",
            json={"confirmation_phrase": proposed["confirmation_phrase"]},
        )
        assert duplicate.status_code == 409

        pipeline = client.post(
            "/v1/actions",
            json={
                "action_type": "pipeline.pause",
                "target_type": "pipeline",
                "target_id": "documents",
                "parameters": {},
                "reason": "Pause document collection for maintenance",
            },
        ).json()
        assert client.post(
            f"/v1/actions/{pipeline['action_request_id']}/confirm",
            json={"confirmation_phrase": pipeline["confirmation_phrase"]},
        ).status_code == 200
        controls = client.get("/v1/runtime/controls").json()
        documents = next(
            item for item in controls["pipelines"] if item["pipeline"] == "documents"
        )
        assert documents["enabled"] is False
        cancellation = client.post(
            "/v1/actions",
            json={
                "action_type": "runtime.pause",
                "target_type": "runtime",
                "target_id": "new_exposure",
                "parameters": {},
                "reason": "Exercise explicit proposal cancellation",
            },
        ).json()
        cancelled = client.post(
            f"/v1/actions/{cancellation['action_request_id']}/cancel"
        )
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "CANCELLED"


def test_code_change_session_is_scoped_and_does_not_expose_a_shell(
    settings: Settings,
) -> None:
    with TestClient(create_app(settings)) as client:
        action = client.post(
            "/v1/actions",
            json={
                "action_type": "code_change.open",
                "target_type": "repository",
                "target_id": "agentic-quant",
                "parameters": {
                    "request": "Repair the bounded data explorer bug",
                    "scope": ["src/agentic_quant", "tests"],
                },
                "reason": "Administrator requested a reviewed code change",
            },
        ).json()
        confirmed = client.post(
            f"/v1/actions/{action['action_request_id']}/confirm",
            json={"confirmation_phrase": action["confirmation_phrase"]},
        )
        assert confirmed.status_code == 200
        sessions = client.get("/v1/code-changes").json()
        assert sessions[0]["status"] == "QUEUED"
        assert sessions[0]["scope_json"] == ["src/agentic_quant", "tests"]
        assert sessions[0]["branch_name"].startswith("steward/")
        assert sessions[0]["worktree_path"] is None


def test_workload_budget_update_requires_confirmation(settings: Settings) -> None:
    with TestClient(create_app(settings)) as client:
        initial = client.get("/v1/llm/budget").json()
        limits = initial["limits"]["workload_daily"]
        over_cap = {name: dict(limit) for name, limit in limits.items()}
        over_cap["interactive_explanation"]["max_estimated_cost_usd"] = "20.01"
        rejected = client.put(
            "/v1/llm/budget",
            json={"workload_daily": over_cap, "reason": "Unsafe oversized limit"},
        )
        assert rejected.status_code == 422
        limits["interactive_explanation"] = {
            "max_estimated_cost_usd": "4.00",
        }
        proposed = client.put(
            "/v1/llm/budget",
            json={
                "workload_daily": limits,
                "reason": "Tune each workload budget from Control Center",
            },
        )
        assert proposed.status_code == 200
        action = proposed.json()
        assert action["status"] == "PENDING_CONFIRMATION"
        assert action["preview"]["resets_consumption"] is False
        assert client.get("/v1/llm/budget").json()["policy_source"] == "yaml_base"

        confirmed = client.post(
            f"/v1/actions/{action['action_request_id']}/confirm",
            json={"confirmation_phrase": action["confirmation_phrase"]},
        )
        assert confirmed.status_code == 200
        effective = client.get("/v1/llm/budget").json()
        assert effective["policy_source"] == "control_center"
        assert effective["limits"]["workload_daily"][
            "interactive_explanation"
        ] == {
            "max_estimated_cost_usd": "4.00",
        }
        history = client.get("/v1/llm/budget/history").json()
        assert history[0]["reason"] == (
            "Tune each workload budget from Control Center"
        )


def test_shared_account_risk_update_requires_confirmation(settings: Settings) -> None:
    with TestClient(create_app(settings)) as client:
        before = client.get("/v1/shadow/account").json()
        proposal = client.post(
            "/v1/actions",
            json={
                "action_type": "account.risk.update",
                "target_type": "virtual_account",
                "target_id": before["virtual_account_id"],
                "parameters": {
                    "limits": {"maximum_trade_risk_usd": "180"}
                },
                "reason": "Increase bounded shared shadow risk for testing",
            },
        ).json()
        unchanged = client.get("/v1/shadow/account").json()
        assert unchanged["maximum_trade_risk_usd"] == "130.00000000"
        confirmed = client.post(
            f"/v1/actions/{proposal['action_request_id']}/confirm",
            json={"confirmation_phrase": proposal["confirmation_phrase"]},
        )
        assert confirmed.status_code == 200
        changed = client.get("/v1/shadow/account").json()
        assert changed["maximum_trade_risk_usd"] == "180.00000000"
        assert changed["risk_revision"] == 2


def test_concrete_coordinator_fails_closed_without_data_credentials(
    settings: Settings,
) -> None:
    with TestClient(create_app(settings)) as client:
        result = asyncio.run(
            client.app.state.coordinator.run_once(
                symbols=("AAPL",),
                as_of=datetime(2026, 9, 6, 20, tzinfo=UTC),
            )
        )
        assert result["completed"] is True
        assert result["business_waiting_count"] == 8
        jobs = client.get("/v1/coordinator/status").json()["cycles"][0]["jobs"]
        assert jobs[0]["result"]["outcome"] == "WAITING_CREDENTIALS"
        assert jobs[-1]["result"]["outcome"] == "WAITING_EXACT_VALIDATION"


def test_production_rejects_plaintext_admin_password() -> None:
    from pydantic import ValidationError
    with pytest.raises(ValidationError, match="ADMIN_PASSWORD_HASH"):
        Settings(
            _env_file=None,
            app_env="production",
            auto_migrate=False,
            auth_required=True,
            admin_username="admin",
            admin_password="plaintext",
            session_secret="x" * 64,
        )


def test_session_expiry_value_is_timezone_aware(settings: Settings) -> None:
    secured = settings.model_copy(
        update={
            "auth_required": True,
            "admin_username": "operator",
            "admin_password": SecretStr("password"),
            "session_secret": SecretStr("z" * 64),
        }
    )
    with TestClient(create_app(secured)) as client:
        value = client.post(
            "/v1/auth/login",
            json={"username": "operator", "password": "password"},
        ).json()
    expires = datetime.fromisoformat(value["expires_at"])
    assert expires.tzinfo == UTC


def test_shadow_runtime_recovers_after_transient_outbox_failure(
    settings: Settings,
    monkeypatch,
) -> None:
    calls = 0
    original = EventLedger.publish_pending

    def flaky_publish_pending(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("temporary database interruption")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(EventLedger, "publish_pending", flaky_publish_pending)
    runtime_settings = settings.model_copy(
        update={
            "shadow_runtime_enabled": True,
            "shadow_poll_seconds": 5,
            "global_new_exposure_paused": True,
        }
    )
    with TestClient(create_app(runtime_settings)) as client:
        time.sleep(1.5)
        assert calls >= 2
        assert client.app.state.shadow_task.done() is False


def test_shadow_runtime_processes_stored_bars_without_a_broker(
    settings: Settings,
) -> None:
    upgrade_database(settings.database_url)
    ledger = EventLedger(settings.database_url)
    research = ResearchStore(ledger.engine)
    market = MarketDataStore(ledger.engine)
    clock = MarketSessionClock("XNYS")
    sessions = clock.calendar.sessions_in_range("2025-01-02", "2025-04-30")[:33]
    bars: list[StockBar] = []
    price = Decimal("100")
    for index, session in enumerate(sessions):
        event_time = datetime.combine(session.date(), datetime.min.time(), tzinfo=UTC)
        close = price * Decimal("1.01")
        bars.append(
            StockBar(
                bar_id=uuid7(),
                symbol="AAPL",
                timeframe="1Day",
                event_time=event_time,
                available_from=clock.daily_bar_available_from(event_time),
                open=price,
                high=close * Decimal("1.001"),
                low=price * Decimal("0.999"),
                close=close,
                volume=1_000_000 + index,
                trade_count=10_000,
                vwap=(price + close) / Decimal("2"),
                source="phase6-test",
                feed="test",
                raw_object_id="TEST_RAW",
                ingested_at=datetime(2026, 9, 5, tzinfo=UTC),
            )
        )
        price = close
    market.insert_bars(tuple(bars[:-3]), raw_object_id="TEST_RAW")
    spec = research.record_strategy_spec(
        default_strategy_spec(
            "momentum",
            timeframe="1Day",
            code_sha256=research_code_sha256(),
        )
    )
    report_id = uuid7()
    execution_contract = {
        "subject": "static_strategy",
        "strategy_spec_ids": {"momentum": spec.strategy_spec_id},
        "feature_set_version": FEATURE_SET_VERSION,
        "backtest_engine_version": BACKTEST_ENGINE_VERSION,
        "cost_model": BacktestCostModel().model_dump(mode="json"),
        "risk_policy": RiskPolicy.from_yaml(
            settings.risk_policy_path
        ).model_dump(mode="json"),
        "restriction_registry_version": RestrictionRegistry.from_yaml(
            settings.restricted_securities_path
        ).version,
        "initial_equity": "100000",
        "execution_profile": BASELINE_EXECUTION_PROFILE_VERSION,
    }
    with ledger.engine.begin() as connection:
        connection.execute(
            insert(validation_reports).values(
                validation_report_id=report_id,
                symbol="AAPL",
                timeframe="1Day",
                strategy_types=["momentum"],
                validation_subject="static_strategy",
                validated_strategy_spec_ids={"momentum": spec.strategy_spec_id},
                execution_contract_json=execution_contract,
                execution_contract_sha256=_canonical_hash(execution_contract),
                selection_metric="sharpe_ratio",
                train_bars=20,
                test_bars=5,
                step_bars=5,
                embargo_bars=1,
                aggregate_metrics={},
                regime_metrics={},
                robustness_metrics={},
                gate_assessment={"eligible_for_human_review": True},
                report_hash="a" * 64,
                code_git_sha="test-git-sha",
                created_at=datetime.now(UTC),
            )
        )
    with TestClient(create_app(settings)) as client:
        altered = research.record_strategy_spec(
            spec.model_copy(
                update={
                    "strategy_spec_id": uuid7(),
                    "name": "untested-momentum-variant",
                    "version": "99.0.0+untested",
                    "parameters": {**spec.parameters, "minimum_return": "0.80"},
                    "code_sha256": "f" * 64,
                }
            )
        )
        untested = client.post(
            "/v1/actions",
            json={
                "action_type": "strategy.adopt",
                "target_type": "strategy",
                "target_id": altered.strategy_spec_id,
                "parameters": {"validation_report_id": report_id},
                "reason": "This exact variant was never validated",
            },
        )
        assert untested.status_code == 422
        assert "exact strategy" in untested.json()["detail"]
        current_policy = client.app.state.shadow.risk_policy
        client.app.state.shadow.risk_policy = current_policy.model_copy(
            update={"maximum_trade_risk_usd": Decimal("129")}
        )
        with pytest.raises(ValueError, match="execution contract"):
            client.app.state.shadow.adoption_preview(
                strategy_spec_id=spec.strategy_spec_id,
                validation_report_id=report_id,
            )
        client.app.state.shadow.risk_policy = current_policy
        adoption = client.post(
            "/v1/actions",
            json={
                "action_type": "strategy.adopt",
                "target_type": "strategy",
                "target_id": spec.strategy_spec_id,
                "parameters": {"validation_report_id": report_id},
                "reason": "Eligible validation report reviewed by administrator",
            },
        ).json()
        assert client.post(
            f"/v1/actions/{adoption['action_request_id']}/confirm",
            json={"confirmation_phrase": adoption["confirmation_phrase"]},
        ).status_code == 200
        deployment = client.post(
            "/v1/actions",
            json={
                "action_type": "shadow.start",
                "target_type": "strategy",
                "target_id": spec.strategy_spec_id,
                "parameters": {
                    "strategy_spec_id": spec.strategy_spec_id,
                    "symbol": "AAPL",
                    "initial_cash": "100000",
                },
                "reason": "Start the validated broker-free shadow deployment",
            },
        ).json()
        assert client.post(
            f"/v1/actions/{deployment['action_request_id']}/confirm",
            json={"confirmation_phrase": deployment["confirmation_phrase"]},
        ).status_code == 200
        market.insert_bars((bars[-3],), raw_object_id="TEST_RAW")
        pause = client.post(
            "/v1/commands/pause",
            json={"reason": "Verify the global pause blocks manual shadow ticks"},
        ).json()
        assert client.post(
            f"/v1/actions/{pause['action_request_id']}/confirm",
            json={"confirmation_phrase": pause["confirmation_phrase"]},
        ).status_code == 200
        blocked_tick = client.post(
            "/v1/actions",
            json={
                "action_type": "shadow.tick",
                "target_type": "runtime",
                "target_id": "shadow",
                "parameters": {},
                "reason": "A manual tick must not bypass the global pause",
            },
        ).json()
        blocked_confirmation = client.post(
            f"/v1/actions/{blocked_tick['action_request_id']}/confirm",
            json={"confirmation_phrase": blocked_tick["confirmation_phrase"]},
        )
        assert blocked_confirmation.status_code == 409
        assert "Global new-exposure pause" in blocked_confirmation.json()["detail"]
        assert client.get("/v1/shadow/events").json() == []
        resume = client.post(
            "/v1/commands/resume",
            json={"reason": "Resume after verifying the kill switch"},
        ).json()
        assert client.post(
            f"/v1/actions/{resume['action_request_id']}/confirm",
            json={"confirmation_phrase": resume["confirmation_phrase"]},
        ).status_code == 200
        tick = client.post(
            "/v1/actions",
            json={
                "action_type": "shadow.tick",
                "target_type": "runtime",
                "target_id": "shadow",
                "parameters": {},
                "reason": "Process newly available stored bars",
            },
        ).json()
        completed = client.post(
            f"/v1/actions/{tick['action_request_id']}/confirm",
            json={"confirmation_phrase": tick["confirmation_phrase"]},
        )
        assert completed.status_code == 200
        planned_events = client.get("/v1/shadow/events").json()
        assert {event["event_type"] for event in planned_events} >= {
            "SIGNAL_CANDIDATE",
            "RISK_DECISION",
            "TRADE_PLAN",
        }
        assert not any(
            event["event_type"] == "VIRTUAL_FILL" for event in planned_events
        )
        market.insert_bars((bars[-2],), raw_object_id="TEST_RAW")
        forward_tick = client.post(
            "/v1/actions",
            json={
                "action_type": "shadow.tick",
                "target_type": "runtime",
                "target_id": "shadow",
                "parameters": {},
                "reason": "A later completed bar may execute the persisted plan",
            },
        ).json()
        assert client.post(
            f"/v1/actions/{forward_tick['action_request_id']}/confirm",
            json={"confirmation_phrase": forward_tick["confirmation_phrase"]},
        ).status_code == 200
        events = client.get("/v1/shadow/events").json()
        assert events
        assert all(event["payload_json"]["virtual_only"] is True for event in events)
        assert {event["event_type"] for event in events} >= {
            "SIGNAL_CANDIDATE",
            "RISK_DECISION",
            "TRADE_PLAN",
            "VIRTUAL_ORDER",
            "VIRTUAL_FILL",
        }
        decisions = client.get("/v1/shadow/decisions").json()
        assert decisions[0]["verdict"] == "APPROVE"

        replay = ResearchBacktester(
            research,
            ledger,
            risk_policy=RiskPolicy.from_yaml(settings.risk_policy_path),
            restrictions=RestrictionRegistry.from_yaml(
                settings.restricted_securities_path
            ),
        ).run(
            spec=spec,
            symbol="AAPL",
            as_of_start=bars[-3].available_from,
            as_of_end=bars[-2].available_from,
            code_git_sha="test-git-sha",
            initial_equity=Decimal("100000"),
            cost_model=BacktestCostModel(),
        )
        entry_fill = next(
            event for event in events if event["event_type"] == "VIRTUAL_FILL"
        )
        active_after_trade = client.get("/v1/shadow/deployments").json()[0]
        assert int(Decimal(str(entry_fill["position_quantity"]))) == replay.trades[0].quantity
        assert Decimal(str(active_after_trade["cash_balance"])) == (
            Decimal("100000") + replay.trades[0].net_pnl
        ).quantize(Decimal("0.00000001"))

        active = active_after_trade
        with ledger.engine.begin() as connection:
            connection.execute(
                update(virtual_accounts)
                .where(
                    virtual_accounts.c.virtual_account_id
                    == active["virtual_account_id"]
                )
                .values(cash_balance=Decimal("30000"))
            )
        market.insert_bars((bars[-1],), raw_object_id="TEST_RAW")
        floor_tick = client.post(
            "/v1/actions",
            json={
                "action_type": "shadow.tick",
                "target_type": "runtime",
                "target_id": "shadow",
                "parameters": {},
                "reason": "Verify account floor at the actual order boundary",
            },
        ).json()
        assert client.post(
            f"/v1/actions/{floor_tick['action_request_id']}/confirm",
            json={"confirmation_phrase": floor_tick["confirmation_phrase"]},
        ).status_code == 200
        after_floor = client.get("/v1/shadow/events").json()
        assert sum(
            event["event_type"] == "VIRTUAL_FILL" for event in after_floor
        ) == sum(event["event_type"] == "VIRTUAL_FILL" for event in events)
        rejected = client.get("/v1/shadow/decisions").json()[0]
        assert rejected["verdict"] == "REJECT"
        assert "ACCOUNT_FLOOR_REACHED" in rejected["reason_codes_json"]
        assert rejected["trade_plan_id"] is None
        reports = client.get("/v1/shadow/reports?period=daily").json()
        assert sum(item["candidate_count"] for item in reports) == 3
        assert sum(item["approved_count"] for item in reports) == 2
        assert sum(item["rejected_count"] for item in reports) == 1
        assert sum(item["round_trip_count"] for item in reports) == 1
        alerts = client.get("/v1/shadow/alerts").json()
        assert alerts[0]["kind"] == "RISK_REJECTION"
        assert alerts[0]["occurrence_count"] == 1
        second_tick = client.post(
            "/v1/actions",
            json={
                "action_type": "shadow.tick",
                "target_type": "runtime",
                "target_id": "shadow",
                "parameters": {},
                "reason": "Verify idempotent replay",
            },
        ).json()
        assert client.post(
            f"/v1/actions/{second_tick['action_request_id']}/confirm",
            json={"confirmation_phrase": second_tick["confirmation_phrase"]},
        ).status_code == 200
        assert len(client.get("/v1/shadow/events").json()) == len(after_floor)


def test_system_steward_cites_snapshot_and_only_proposes_actions(
    settings: Settings,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    async def fake_complete(
        _provider: ResponsesAPIProvider,
        request: LLMRequest,
    ) -> LLMProviderResult:
        assert "system_snapshot" in request.input_text
        assert "direct_user_request" in request.input_text
        assert "GitHub-flavored Markdown" in request.instructions
        assert "Never put raw HTML" in request.instructions
        return LLMProviderResult(
            response_id="steward-test",
            output_text=(
                '{"answer":"Focus list is currently empty.",'
                '"citations":["LIST:focus-watchlist","MADE_UP:bad"],'
                '"proposed_action":{"action_type":"list.add",'
                '"target_type":"list","target_id":"focus-watchlist",'
                '"parameters":{"members":["AAPL"]},'
                '"reason":"Administrator asked to focus on AAPL"}}'
            ),
            usage=LLMUsage(input_tokens=10, output_tokens=10, total_tokens=20),
        )

    monkeypatch.setattr(ResponsesAPIProvider, "complete", fake_complete)
    configured = settings.model_copy(
        update={"llm_meta_api_key": SecretStr("test-meta-key")}
    )
    with TestClient(create_app(configured)) as client:
        result = client.post(
            "/v1/steward/ask",
            json={"message": "What is in my focus list, and add AAPL?"},
        )
        assert result.status_code == 200
        payload = result.json()
        assert payload["estimated_cost_usd"] == "0.000025"
        assert payload["citations"] == ["LIST:focus-watchlist"]
        assert payload["proposed_action"]["status"] == "PENDING_CONFIRMATION"
        assert client.get("/v1/lists/focus-watchlist").json()["members"] == []
        history = client.get(
            f"/v1/steward/conversations/{payload['conversation_id']}"
        ).json()
        assert [item["role"] for item in history["messages"]] == [
            "user",
            "assistant",
        ]
