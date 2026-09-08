from __future__ import annotations

from decimal import Decimal

import pytest

from agentic_quant.config import AppEnvironment
from agentic_quant.environment import EnvironmentRegistry
from agentic_quant.ledger import EventLedger
from agentic_quant.migrations import upgrade_database
from agentic_quant.risk import RiskPolicy
from agentic_quant.virtual_account import VirtualAccountStore


def test_shared_account_reservations_are_atomic_and_settle_once(
    settings,  # type: ignore[no-untyped-def]
) -> None:
    upgrade_database(settings.database_url)
    ledger = EventLedger(settings.database_url)
    policy = RiskPolicy.from_yaml(settings.risk_policy_path)
    store = VirtualAccountStore(ledger.engine)
    account = store.ensure_main_account(
        initial_cash=Decimal("100000"),
        risk_policy=policy,
    )
    with ledger.engine.begin() as connection:
        assert store.reserve(
            account_id=str(account["virtual_account_id"]),
            cash=Decimal("1000"),
            risk_usd=Decimal("500"),
            connection=connection,
        )
    with ledger.engine.begin() as connection:
        assert not store.reserve(
            account_id=str(account["virtual_account_id"]),
            cash=Decimal("1000"),
            risk_usd=Decimal("500"),
            connection=connection,
        )
        store.release_or_settle(
            account_id=str(account["virtual_account_id"]),
            reserved_cash=Decimal("1000"),
            reserved_risk_usd=Decimal("500"),
            realized_pnl_delta=Decimal("25.50"),
            connection=connection,
        )
    settled = store.account(str(account["virtual_account_id"]))
    assert settled["cash_balance"] == Decimal("100025.50000000")
    assert settled["reserved_cash"] == Decimal("0E-8")
    assert settled["reserved_risk_usd"] == Decimal("0E-8")


def test_risk_revision_changes_effective_contract(settings) -> None:  # type: ignore[no-untyped-def]
    upgrade_database(settings.database_url)
    ledger = EventLedger(settings.database_url)
    base = RiskPolicy.from_yaml(settings.risk_policy_path)
    store = VirtualAccountStore(ledger.engine)
    account = store.ensure_main_account(
        initial_cash=Decimal("100000"),
        risk_policy=base,
    )
    updated = store.update_risk(
        str(account["virtual_account_id"]),
        {
            "baseline_stop_fraction": "0.125",
            "maximum_trade_risk_usd": "200",
        },
        reason="Increase bounded shadow experiment capacity",
        created_by="test-admin",
    )
    effective = store.effective_risk_policy(base)
    assert updated["risk_revision"] == 2
    assert effective.maximum_trade_risk_usd == Decimal("200.00000000")
    assert effective.baseline_stop_fraction == Decimal("0.12500000")
    assert effective.version == "risk_policy@0.3.0+account-r2"


def test_database_cannot_switch_environment_identity(
    settings,  # type: ignore[no-untyped-def]
) -> None:
    upgrade_database(settings.database_url)
    ledger = EventLedger(settings.database_url)
    registry = EnvironmentRegistry(ledger.engine)
    registry.register(
        environment=AppEnvironment.DEVELOPMENT,
        environment_id="dev-test",
    )
    with pytest.raises(RuntimeError, match="environment identity mismatch"):
        registry.register(
            environment=AppEnvironment.PRODUCTION,
            environment_id="prod-test",
        )
