from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import json
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from sqlalchemy import insert
from sqlalchemy import update

from agentic_quant.database import (
    paper_enrollments,
    shadow_deployments,
    shadow_signal_candidates,
    shadow_trade_plans,
    strategy_specs,
)
from agentic_quant.ledger import EventLedger
from agentic_quant.migrations import upgrade_database
from agentic_quant.paper import (
    PAPER_EXECUTION_PROFILE_VERSION,
    PaperBroker,
    PaperTradingRuntime,
)
from agentic_quant.providers.alpaca_paper import (
    AlpacaPaperConfigurationError,
    AlpacaPaperResponseError,
    AlpacaPaperTradingProvider,
    normalize_long_bracket_prices,
)
from agentic_quant.risk import BASELINE_EXECUTION_PROFILE_VERSION
from agentic_quant.shadow import ShadowRuntime


def test_alpaca_adapter_is_pinned_to_paper_and_submission_is_idempotent() -> None:
    with pytest.raises(AlpacaPaperConfigurationError, match="hard-pinned"):
        AlpacaPaperTradingProvider(
            api_key="key",
            api_secret="secret",
            base_url="https://api.alpaca.markets",
        )

    stored: dict[str, Any] | None = None
    posts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal posts, stored
        if request.url.path == "/v2/orders:by_client_order_id":
            if stored is None:
                return httpx.Response(404, json={"message": "not found"})
            return httpx.Response(200, json=stored)
        if request.url.path == "/v2/orders" and request.method == "POST":
            posts += 1
            submitted = json.loads(request.content)
            assert submitted["type"] == "limit"
            assert submitted["limit_price"] == "100.00"
            assert submitted["order_class"] == "bracket"
            assert submitted["time_in_force"] == "day"
            stored = {
                "id": "paper-order-1",
                "client_order_id": "qagent-test",
                "symbol": "AAPL",
                "status": "accepted",
                "filled_qty": "0",
                "legs": [],
            }
            return httpx.Response(200, json=stored)
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    async def exercise() -> None:
        client = httpx.AsyncClient(
            base_url="https://paper-api.alpaca.markets",
            transport=httpx.MockTransport(handler),
        )
        provider = AlpacaPaperTradingProvider(
            api_key="key",
            api_secret="secret",
            client=client,
        )
        try:
            first = await provider.submit_bracket_order(
                client_order_id="qagent-test",
                symbol="AAPL",
                quantity=2,
                entry_limit_price=Decimal("100"),
                take_profit_price=Decimal("110"),
                stop_loss_price=Decimal("95"),
            )
            second = await provider.submit_bracket_order(
                client_order_id="qagent-test",
                symbol="AAPL",
                quantity=2,
                entry_limit_price=Decimal("100"),
                take_profit_price=Decimal("110"),
                stop_loss_price=Decimal("95"),
            )
            assert first == second
        finally:
            await client.aclose()

    asyncio.run(exercise())
    assert posts == 1


def test_alpaca_price_precision_is_directional_and_geometry_stays_valid() -> None:
    entry, target, stop = normalize_long_bracket_prices(
        entry_limit_price=Decimal("121.379"),
        take_profit_price=Decimal("126.2248"),
        stop_loss_price=Decimal("118.9426"),
    )
    assert (entry, target, stop) == (
        Decimal("121.37"),
        Decimal("126.22"),
        Decimal("118.95"),
    )

    sub_dollar = normalize_long_bracket_prices(
        entry_limit_price=Decimal("0.92347"),
        take_profit_price=Decimal("0.95009"),
        stop_loss_price=Decimal("0.90009"),
    )
    assert sub_dollar == (
        Decimal("0.9234"),
        Decimal("0.9500"),
        Decimal("0.9001"),
    )
    with pytest.raises(ValueError, match="stop < entry < target"):
        normalize_long_bracket_prices(
            entry_limit_price=Decimal("1.001"),
            take_profit_price=Decimal("1.004"),
            stop_loss_price=Decimal("0.99999"),
        )


class _FakeShadow:
    restrictions = SimpleNamespace(is_restricted=lambda *_args, **_kwargs: False)

    def __init__(self) -> None:
        self.deployment_status = "ACTIVE"
        self.contract_status = "CURRENT"
        self.execution_profile = PAPER_EXECUTION_PROFILE_VERSION
        self.maximum_trade_risk_usd = "130"

    def deployment(self, deployment_id: str) -> dict[str, Any]:
        return {
            "shadow_deployment_id": deployment_id,
            "strategy_name": "Paper test momentum",
            "strategy_spec_id": "strategy-1",
            "symbol": "AAPL",
            "status": self.deployment_status,
            "contract_status": self.contract_status,
            "execution_contract_json": {
                "execution_profile": self.execution_profile,
            },
        }

    def virtual_account(self) -> dict[str, Any]:
        return {
            "account_floor_usd": "40000",
            "daily_loss_stop_usd": "520",
            "maximum_trade_risk_usd": self.maximum_trade_risk_usd,
            "maximum_concurrent_risk_usd": "780",
        }

    def effective_risk_policy(self) -> Any:
        return SimpleNamespace(minimum_reward_risk=Decimal("1.5"))


class _FakeBroker:
    def __init__(self) -> None:
        self.orders: dict[str, dict[str, Any]] = {}
        self.submissions = 0
        self.cancellations = 0
        self.account_id = "paper-account-1"
        self.positions: tuple[dict[str, Any], ...] = ()

    async def __aenter__(self) -> _FakeBroker:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def fetch_account(self) -> dict[str, Any]:
        return {
            "id": self.account_id,
            "account_number": "DO-NOT-PERSIST",
            "status": "ACTIVE",
            "currency": "USD",
            "cash": "100000",
            "buying_power": "200000",
            "equity": "100000",
            "portfolio_value": "100000",
            "last_equity": "100000",
            "pattern_day_trader": False,
            "trading_blocked": False,
            "transfers_blocked": False,
            "account_blocked": False,
        }

    async def fetch_positions(self) -> tuple[dict[str, Any], ...]:
        return self.positions

    async def fetch_order_by_client_id(
        self, client_order_id: str
    ) -> dict[str, Any] | None:
        return self.orders.get(client_order_id)

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
        existing = self.orders.get(client_order_id)
        if existing is not None:
            return existing
        self.submissions += 1
        value: dict[str, Any] = {
            "id": f"broker-{self.submissions}",
            "client_order_id": client_order_id,
            "symbol": symbol,
            "qty": str(quantity),
            "limit_price": str(entry_limit_price),
            "status": "accepted",
            "filled_qty": "0",
            "filled_avg_price": None,
            "legs": [
                {"status": "new", "limit_price": str(take_profit_price)},
                {"status": "held", "stop_price": str(stop_loss_price)},
            ],
        }
        self.orders[client_order_id] = value
        return value

    async def cancel_order(self, broker_order_id: str) -> None:
        self.cancellations += 1
        for value in self.orders.values():
            if value["id"] == broker_order_id:
                value["status"] = "canceled"
                value["legs"] = []


class _UnknownOnceBroker(_FakeBroker):
    def __init__(self, *, accepted_before_timeout: bool = False) -> None:
        super().__init__()
        self.accepted_before_timeout = accepted_before_timeout
        self.post_attempts = 0

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
        self.post_attempts += 1
        if self.post_attempts == 1:
            if self.accepted_before_timeout:
                await super().submit_bracket_order(
                    client_order_id=client_order_id,
                    symbol=symbol,
                    quantity=quantity,
                    entry_limit_price=entry_limit_price,
                    take_profit_price=take_profit_price,
                    stop_loss_price=stop_loss_price,
                )
            raise AlpacaPaperResponseError(
                status_code=504,
                endpoint="/v2/orders",
                detail="outcome unknown",
            )
        return await super().submit_bracket_order(
            client_order_id=client_order_id,
            symbol=symbol,
            quantity=quantity,
            entry_limit_price=entry_limit_price,
            take_profit_price=take_profit_price,
            stop_loss_price=stop_loss_price,
        )


def _insert_plan(
    ledger: EventLedger,
    *,
    deployment_id: str,
    now: datetime,
    suffix: str,
) -> str:
    candidate_id = f"candidate-{suffix}"
    plan_id = f"plan-{suffix}"
    with ledger.engine.begin() as connection:
        connection.execute(
            insert(shadow_signal_candidates).values(
                candidate_id=candidate_id,
                shadow_deployment_id=deployment_id,
                shadow_run_id=f"run-{suffix}",
                strategy_spec_id="strategy-1",
                feature_snapshot_id=f"feature-{suffix}",
                decision_bar_id=f"bar-{suffix}",
                symbol="AAPL",
                action="LONG",
                as_of=now - timedelta(minutes=2),
                planned_entry=Decimal("100"),
                invalidation=Decimal("95"),
                targets_json=["110"],
                expires_at=now + timedelta(days=1),
                execution_contract_sha256="a" * 64,
                created_at=now,
            )
        )
        connection.execute(
            insert(shadow_trade_plans).values(
                trade_plan_id=plan_id,
                candidate_id=candidate_id,
                risk_decision_id=f"risk-{suffix}",
                symbol="AAPL",
                direction="LONG",
                quantity=2,
                reserved_cash=Decimal("200"),
                reserved_risk_usd=Decimal("10"),
                limit_price=Decimal("100"),
                invalidation=Decimal("95"),
                targets_json=["110"],
                expires_at=now + timedelta(days=1),
                status="OPEN",
                created_at=now,
                closed_at=None,
            )
        )
    return plan_id


def _paper_fixture(
    settings,  # type: ignore[no-untyped-def]
    *,
    broker: _FakeBroker,
    now: datetime,
) -> tuple[EventLedger, PaperTradingRuntime, _FakeShadow, list[datetime]]:
    upgrade_database(settings.database_url)
    ledger = EventLedger(settings.database_url)
    deployment_id = "deployment-1"
    with ledger.engine.begin() as connection:
        connection.execute(
            insert(strategy_specs).values(
                strategy_spec_id="strategy-1",
                name="Paper test momentum",
                version="1.0.0",
                strategy_type="momentum",
                timeframe="1Day",
                feature_set_version="test",
                parameters_json={},
                data_requirements_json={},
                code_sha256="b" * 64,
                created_at=now - timedelta(days=1),
            )
        )
        connection.execute(
            insert(shadow_deployments).values(
                shadow_deployment_id=deployment_id,
                virtual_account_id="virtual-account-1",
                strategy_sleeve_id=None,
                strategy_spec_id="strategy-1",
                adoption_id=None,
                symbol="AAPL",
                status="ACTIVE",
                initial_cash=Decimal("100000"),
                cash_balance=Decimal("100000"),
                position_quantity=Decimal("0"),
                average_entry_price=None,
                last_price=Decimal("100"),
                realized_pnl=Decimal("0"),
                unrealized_pnl=Decimal("0"),
                last_processed_bar_time=None,
                created_at=now - timedelta(days=1),
                updated_at=now - timedelta(days=1),
            )
        )
        connection.execute(
            insert(paper_enrollments).values(
                paper_enrollment_id="enrollment-1",
                shadow_deployment_id=deployment_id,
                broker_account_id=None,
                status="ACTIVE",
                created_by="operator",
                reason="Approved for paper test",
                created_at=now - timedelta(minutes=1),
                updated_at=now - timedelta(minutes=1),
            )
        )
    _insert_plan(ledger, deployment_id=deployment_id, now=now, suffix="one")
    shadow = _FakeShadow()
    clock = [now + timedelta(seconds=1)]
    runtime = PaperTradingRuntime(
        ledger.engine,
        cast(ShadowRuntime, shadow),
        broker_factory=lambda: cast(PaperBroker, broker),
        enabled=True,
        trading_mode="paper",
        now_provider=lambda: clock[0],
    )
    return ledger, runtime, shadow, clock


def test_paper_runtime_submits_once_reconciles_and_pause_blocks_new_exposure(
    settings,  # type: ignore[no-untyped-def]
) -> None:
    upgrade_database(settings.database_url)
    ledger = EventLedger(settings.database_url)
    now = datetime(2026, 9, 8, 20, tzinfo=UTC)
    deployment_id = "deployment-1"
    enrollment_id = "enrollment-1"
    with ledger.engine.begin() as connection:
        connection.execute(
            insert(strategy_specs).values(
                strategy_spec_id="strategy-1",
                name="Paper test momentum",
                version="1.0.0",
                strategy_type="momentum",
                timeframe="1Day",
                feature_set_version="test",
                parameters_json={},
                data_requirements_json={},
                code_sha256="b" * 64,
                created_at=now - timedelta(days=1),
            )
        )
        connection.execute(
            insert(shadow_deployments).values(
                shadow_deployment_id=deployment_id,
                virtual_account_id="virtual-account-1",
                strategy_sleeve_id=None,
                strategy_spec_id="strategy-1",
                adoption_id=None,
                symbol="AAPL",
                status="ACTIVE",
                initial_cash=Decimal("100000"),
                cash_balance=Decimal("100000"),
                position_quantity=Decimal("0"),
                average_entry_price=None,
                last_price=Decimal("100"),
                realized_pnl=Decimal("0"),
                unrealized_pnl=Decimal("0"),
                last_processed_bar_time=None,
                created_at=now - timedelta(days=1),
                updated_at=now - timedelta(days=1),
            )
        )
        connection.execute(
            insert(paper_enrollments).values(
                paper_enrollment_id=enrollment_id,
                shadow_deployment_id=deployment_id,
                broker_account_id=None,
                status="ACTIVE",
                created_by="operator",
                reason="Approved for paper test",
                created_at=now - timedelta(minutes=1),
                updated_at=now - timedelta(minutes=1),
            )
        )
    _insert_plan(
        ledger,
        deployment_id=deployment_id,
        now=now,
        suffix="one",
    )
    broker = _FakeBroker()
    clock = [now + timedelta(seconds=1)]
    runtime = PaperTradingRuntime(
        ledger.engine,
        cast(ShadowRuntime, _FakeShadow()),
        broker_factory=lambda: cast(PaperBroker, broker),
        enabled=True,
        trading_mode="paper",
        now_provider=lambda: clock[0],
    )

    first = asyncio.run(
        runtime.tick(trigger="test", new_exposure_paused=False)
    )
    assert first["orders_submitted"] == 1
    assert broker.submissions == 1
    stored = runtime.orders()[0]
    assert stored["status"] == "accepted"
    assert stored["client_order_id"].startswith("qagent-")
    assert "account_number" not in runtime.latest_account()["payload_json"]

    clock[0] += timedelta(seconds=30)
    second = asyncio.run(
        runtime.tick(trigger="test-replay", new_exposure_paused=False)
    )
    assert second["orders_submitted"] == 0
    assert second["orders_reconciled"] == 1
    assert broker.submissions == 1

    _insert_plan(
        ledger,
        deployment_id=deployment_id,
        now=clock[0],
        suffix="two",
    )
    clock[0] += timedelta(seconds=1)
    paused = asyncio.run(
        runtime.tick(trigger="paused", new_exposure_paused=True)
    )
    assert paused["orders_submitted"] == 0
    assert broker.submissions == 1
    assert len(runtime.orders()) == 1

    order_id = str(stored["paper_order_id"])
    asyncio.run(runtime.cancel_order(order_id))
    assert broker.cancellations == 1
    assert runtime.orders()[0]["status"] == "canceled"
    assert runtime.orders()[0]["lifecycle_complete"] is True


@pytest.mark.parametrize(
    ("mutation", "expected_blocker"),
    (
        ("pause", "PAPER_ENROLLMENT_NOT_ACTIVE"),
        ("retire", "PAPER_ENROLLMENT_NOT_ACTIVE"),
        ("contract", "PAPER_EXECUTION_CONTRACT_NOT_VALIDATED"),
        ("risk", "PAPER_TRADE_RISK_LIMIT"),
        ("plan", "SHADOW_PLAN_NOT_OPEN"),
        ("global", "GLOBAL_NEW_EXPOSURE_PAUSED"),
    ),
)
def test_unknown_submission_is_reauthorized_before_every_retry(
    settings,  # type: ignore[no-untyped-def]
    mutation: str,
    expected_blocker: str,
) -> None:
    now = datetime(2026, 9, 8, 20, tzinfo=UTC)
    broker = _UnknownOnceBroker()
    ledger, runtime, shadow, clock = _paper_fixture(
        settings,
        broker=broker,
        now=now,
    )
    with pytest.raises(AlpacaPaperResponseError):
        asyncio.run(runtime.tick(trigger="unknown", new_exposure_paused=False))
    assert broker.post_attempts == 1
    assert runtime.orders()[0]["status"] == "SUBMISSION_UNKNOWN"

    paused = False
    if mutation in {"pause", "retire"}:
        runtime.set_enrollment_status(
            enrollment_id="enrollment-1",
            status="PAUSED" if mutation == "pause" else "RETIRED",
            reason="adversarial retry test",
            updated_by="test",
        )
    elif mutation == "contract":
        shadow.execution_profile = BASELINE_EXECUTION_PROFILE_VERSION
    elif mutation == "risk":
        shadow.maximum_trade_risk_usd = "1"
    elif mutation == "plan":
        with ledger.engine.begin() as connection:
            connection.execute(
                update(shadow_trade_plans)
                .where(shadow_trade_plans.c.trade_plan_id == "plan-one")
                .values(status="CANCELLED")
            )
    else:
        paused = True

    clock[0] += timedelta(seconds=1)
    result = asyncio.run(
        runtime.tick(trigger="retry", new_exposure_paused=paused)
    )
    assert result["orders_submitted"] == 0
    assert broker.post_attempts == 1
    blocked = runtime.orders()[0]
    assert blocked["status"] == "SUBMISSION_BLOCKED"
    assert expected_blocker in str(blocked["error_message"])


def test_unknown_submission_is_never_forwarded_to_a_different_account(
    settings,  # type: ignore[no-untyped-def]
) -> None:
    now = datetime(2026, 9, 8, 20, tzinfo=UTC)
    broker = _UnknownOnceBroker()
    _, runtime, _, clock = _paper_fixture(settings, broker=broker, now=now)
    with pytest.raises(AlpacaPaperResponseError):
        asyncio.run(runtime.tick(trigger="unknown", new_exposure_paused=False))
    broker.account_id = "paper-account-2"
    clock[0] += timedelta(seconds=1)
    with pytest.raises(RuntimeError, match="account changed"):
        asyncio.run(runtime.tick(trigger="account-change", new_exposure_paused=False))
    clock[0] += timedelta(seconds=1)
    result = asyncio.run(
        runtime.tick(trigger="account-change-retry", new_exposure_paused=False)
    )
    assert result["orders_submitted"] == 0
    assert broker.post_attempts == 1
    blocked = runtime.orders()[0]
    assert blocked["status"] == "SUBMISSION_BLOCKED"
    assert "ORDER_ACCOUNT_MISMATCH" in str(blocked["error_message"])


def test_lost_successful_response_is_reconciled_without_a_second_post(
    settings,  # type: ignore[no-untyped-def]
) -> None:
    now = datetime(2026, 9, 8, 20, tzinfo=UTC)
    broker = _UnknownOnceBroker(accepted_before_timeout=True)
    _, runtime, _, clock = _paper_fixture(settings, broker=broker, now=now)
    with pytest.raises(AlpacaPaperResponseError):
        asyncio.run(runtime.tick(trigger="lost-response", new_exposure_paused=False))
    clock[0] += timedelta(seconds=1)
    result = asyncio.run(
        runtime.tick(trigger="reconcile", new_exposure_paused=False)
    )
    assert result["orders_reconciled"] == 1
    assert result["orders_submitted"] == 0
    assert broker.post_attempts == 1
    assert runtime.orders()[0]["status"] == "accepted"


def test_partial_fill_expiry_cancels_remainder_but_keeps_lifecycle_open(
    settings,  # type: ignore[no-untyped-def]
) -> None:
    now = datetime(2026, 9, 8, 20, tzinfo=UTC)
    broker = _FakeBroker()
    ledger, runtime, _, clock = _paper_fixture(settings, broker=broker, now=now)
    asyncio.run(runtime.tick(trigger="submit", new_exposure_paused=False))
    order = runtime.orders()[0]
    client_order_id = str(order["client_order_id"])
    broker.orders[client_order_id]["status"] = "partially_filled"
    broker.orders[client_order_id]["filled_qty"] = "1"
    broker.positions = ({"symbol": "AAPL", "qty": "1"},)
    clock[0] = now + timedelta(days=2)

    result = asyncio.run(
        runtime.tick(trigger="expired-partial", new_exposure_paused=False)
    )
    assert result["orders_submitted"] == 0
    assert broker.cancellations == 1
    stored = runtime.orders()[0]
    assert stored["lifecycle_complete"] is False
    assert stored["status"] == "SUBMISSION_BLOCKED"
    assert "POSITION_OPEN_REQUIRES_EXIT" in str(stored["error_message"])

    _insert_plan(
        ledger,
        deployment_id="deployment-1",
        now=clock[0],
        suffix="after-partial",
    )
    clock[0] += timedelta(seconds=1)
    second = asyncio.run(
        runtime.tick(trigger="still-open", new_exposure_paused=False)
    )
    assert second["orders_submitted"] == 0
    assert broker.submissions == 1


def test_reconciliation_refreshes_position_after_order_state_changes(
    settings,  # type: ignore[no-untyped-def]
) -> None:
    class FillAfterInitialPositionRead(_FakeBroker):
        race = False

        async def fetch_order_by_client_id(
            self,
            client_order_id: str,
        ) -> dict[str, Any] | None:
            if self.race:
                self.race = False
                self.orders[client_order_id].update(
                    status="canceled",
                    filled_qty="1",
                    filled_avg_price="100",
                    legs=[],
                )
                self.positions = ({"symbol": "AAPL", "qty": "1"},)
            return await super().fetch_order_by_client_id(client_order_id)

    now = datetime(2026, 9, 8, 20, tzinfo=UTC)
    broker = FillAfterInitialPositionRead()
    _, runtime, shadow, clock = _paper_fixture(
        settings,
        broker=broker,
        now=now,
    )
    asyncio.run(runtime.tick(trigger="initial", new_exposure_paused=False))
    shadow.execution_profile = BASELINE_EXECUTION_PROFILE_VERSION
    broker.race = True
    clock[0] += timedelta(seconds=1)

    result = asyncio.run(runtime.tick(trigger="race", new_exposure_paused=True))
    stored = runtime.orders()[0]

    assert result["orders_submitted"] == 0
    assert stored["filled_quantity"] == Decimal("1")
    assert stored["lifecycle_complete"] is False


def test_paper_enrollment_rejects_shadow_only_execution_certificate(
    settings,  # type: ignore[no-untyped-def]
) -> None:
    upgrade_database(settings.database_url)
    ledger = EventLedger(settings.database_url)
    shadow = _FakeShadow()
    shadow.execution_profile = BASELINE_EXECUTION_PROFILE_VERSION
    runtime = PaperTradingRuntime(
        ledger.engine,
        cast(ShadowRuntime, shadow),
        broker_factory=lambda: cast(PaperBroker, _FakeBroker()),
        enabled=True,
        trading_mode="paper",
    )
    with pytest.raises(ValueError, match="separately validated"):
        runtime.enrollment_preview("deployment-1")
