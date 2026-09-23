from decimal import Decimal

from agentic_quant.shadow_graduation import assess_shadow_graduation


def test_fast_one_session_shadow_graduation() -> None:
    result = assess_shadow_graduation(
        holding_period_sessions=1,
        observation_sessions=7,
        initial_equity=Decimal("10000"),
        current_equity=Decimal("10060"),
        closed_trade_pnls=(
            Decimal("40"),
            Decimal("30"),
            Decimal("20"),
            Decimal("-20"),
            Decimal("-10"),
        ),
        marked_equity_path=(
            Decimal("10040"),
            Decimal("10070"),
            Decimal("10090"),
            Decimal("10070"),
        ),
        deployment_status="ACTIVE",
        contract_current=True,
    )

    assert result["status"] == "SHADOW_GRADUATED_EARLY"
    assert result["closed_trades"] == 5
    assert result["wins"] == 3
    assert result["best_trade_excluded_pnl"] == "20"


def test_graduation_rejects_one_winner_dependency() -> None:
    result = assess_shadow_graduation(
        holding_period_sessions=5,
        observation_sessions=20,
        initial_equity=Decimal("10000"),
        current_equity=Decimal("10030"),
        closed_trade_pnls=(Decimal("200"), Decimal("-80"), Decimal("-90")),
        marked_equity_path=(Decimal("10100"), Decimal("10030")),
        deployment_status="ACTIVE",
        contract_current=True,
    )

    assert result["status"] == "OBSERVING"
    requirement = next(
        item for item in result["requirements"] if item["name"] == "best_trade_removed"
    )
    assert requirement["passed"] is False


def test_long_horizon_stays_in_observation() -> None:
    result = assess_shadow_graduation(
        holding_period_sessions=63,
        observation_sessions=100,
        initial_equity=Decimal("10000"),
        current_equity=Decimal("10500"),
        closed_trade_pnls=(Decimal("300"), Decimal("200")),
        marked_equity_path=(),
        deployment_status="ACTIVE",
        contract_current=True,
    )

    assert result["status"] == "LONG_HORIZON_OBSERVING"
    assert result["eligible_for_early_graduation"] is False
