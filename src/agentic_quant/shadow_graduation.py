from __future__ import annotations

from decimal import Decimal
from typing import Any, Iterable


SHADOW_GRADUATION_POLICY_VERSION = "shadow_graduation@0.1.0"
SHADOW_GRADUATION_THRESHOLDS: dict[int, tuple[int, int]] = {
    1: (5, 7),
    2: (4, 10),
    5: (3, 15),
    10: (3, 25),
    20: (2, 40),
}
SHADOW_GRADUATION_MINIMUM_WINS = 2
SHADOW_GRADUATION_MINIMUM_PROFIT_FACTOR = Decimal("1.05")
SHADOW_GRADUATION_MAXIMUM_DRAWDOWN = Decimal("0.10")
SHADOW_GRADUATION_BEST_TRADE_TOLERANCE = Decimal("0.005")


def _maximum_drawdown(equity_values: Iterable[Decimal]) -> Decimal:
    peak: Decimal | None = None
    maximum = Decimal("0")
    for equity in equity_values:
        if peak is None or equity > peak:
            peak = equity
        if peak is None or peak <= 0:
            continue
        drawdown = (peak - equity) / peak
        maximum = max(maximum, drawdown)
    return maximum


def assess_shadow_graduation(
    *,
    holding_period_sessions: int,
    observation_sessions: int,
    initial_equity: Decimal,
    current_equity: Decimal,
    closed_trade_pnls: Iterable[Decimal],
    marked_equity_path: Iterable[Decimal],
    deployment_status: str,
    contract_current: bool,
) -> dict[str, Any]:
    """Derive a versioned, non-mutating graduation assessment from Shadow evidence."""

    trades = tuple(Decimal(value) for value in closed_trade_pnls)
    wins = tuple(value for value in trades if value > 0)
    losses = tuple(value for value in trades if value < 0)
    net = sum(trades, Decimal("0"))
    gross_profit = sum(wins, Decimal("0"))
    gross_loss = abs(sum(losses, Decimal("0")))
    profit_factor = (
        Decimal("999999") if not losses and wins else
        Decimal("0") if not losses else
        gross_profit / gross_loss
    )
    best_trade = max(trades) if trades else None
    without_best = net - best_trade if best_trade is not None else None
    equity_path = (initial_equity, *tuple(marked_equity_path), current_equity)
    maximum_drawdown = _maximum_drawdown(equity_path)
    threshold = SHADOW_GRADUATION_THRESHOLDS.get(holding_period_sessions)
    requirements: list[dict[str, Any]] = []

    def require(name: str, passed: bool, actual: Any, required: Any) -> None:
        requirements.append(
            {
                "name": name,
                "passed": passed,
                "actual": actual,
                "required": required,
            }
        )

    if threshold is None:
        return {
            "policy_version": SHADOW_GRADUATION_POLICY_VERSION,
            "status": "LONG_HORIZON_OBSERVING",
            "eligible_for_early_graduation": False,
            "holding_period_sessions": holding_period_sessions,
            "observation_sessions": observation_sessions,
            "closed_trades": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "realized_net_pnl": str(net),
            "profit_factor": str(profit_factor),
            "best_trade_excluded_pnl": (
                str(without_best) if without_best is not None else None
            ),
            "maximum_drawdown_fraction": str(maximum_drawdown),
            "requirements": [],
            "explanation": (
                "Early graduation is deliberately limited to 1/2/5/10/20-session "
                "strategies; longer horizons remain in Shadow observation."
            ),
        }

    minimum_trades, minimum_sessions = threshold
    require(
        "minimum_observation_sessions",
        observation_sessions >= minimum_sessions,
        observation_sessions,
        minimum_sessions,
    )
    require(
        "minimum_closed_trades",
        len(trades) >= minimum_trades,
        len(trades),
        minimum_trades,
    )
    require(
        "minimum_wins",
        len(wins) >= SHADOW_GRADUATION_MINIMUM_WINS,
        len(wins),
        SHADOW_GRADUATION_MINIMUM_WINS,
    )
    require("positive_realized_net", net > 0, str(net), "> 0")
    require(
        "minimum_profit_factor",
        profit_factor >= SHADOW_GRADUATION_MINIMUM_PROFIT_FACTOR,
        str(profit_factor),
        str(SHADOW_GRADUATION_MINIMUM_PROFIT_FACTOR),
    )
    require(
        "maximum_drawdown",
        maximum_drawdown <= SHADOW_GRADUATION_MAXIMUM_DRAWDOWN,
        str(maximum_drawdown),
        f"<= {SHADOW_GRADUATION_MAXIMUM_DRAWDOWN}",
    )
    tolerance = -(initial_equity * SHADOW_GRADUATION_BEST_TRADE_TOLERANCE)
    require(
        "best_trade_removed",
        without_best is not None and without_best >= tolerance,
        str(without_best) if without_best is not None else None,
        f">= {tolerance}",
    )
    require(
        "current_execution_contract",
        contract_current,
        "CURRENT" if contract_current else "REVALIDATION_REQUIRED",
        "CURRENT",
    )
    require(
        "nonterminal_sandbox",
        deployment_status == "ACTIVE",
        deployment_status,
        "ACTIVE",
    )
    graduated = all(bool(item["passed"]) for item in requirements)
    return {
        "policy_version": SHADOW_GRADUATION_POLICY_VERSION,
        "status": "SHADOW_GRADUATED_EARLY" if graduated else "OBSERVING",
        "eligible_for_early_graduation": graduated,
        "holding_period_sessions": holding_period_sessions,
        "observation_sessions": observation_sessions,
        "closed_trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "realized_net_pnl": str(net),
        "profit_factor": str(profit_factor),
        "best_trade_excluded_pnl": (
            str(without_best) if without_best is not None else None
        ),
        "maximum_drawdown_fraction": str(maximum_drawdown),
        "requirements": requirements,
        "explanation": (
            "Graduation is a derived evidence label. The sandbox remains active and "
            "continues accumulating forward trades; it does not authorize live money."
        ),
    }


def shadow_graduation_policy() -> dict[str, Any]:
    return {
        "version": SHADOW_GRADUATION_POLICY_VERSION,
        "thresholds": {
            str(horizon): {
                "minimum_closed_trades": values[0],
                "minimum_observation_sessions": values[1],
            }
            for horizon, values in SHADOW_GRADUATION_THRESHOLDS.items()
        },
        "minimum_wins": SHADOW_GRADUATION_MINIMUM_WINS,
        "minimum_profit_factor": str(SHADOW_GRADUATION_MINIMUM_PROFIT_FACTOR),
        "maximum_drawdown_fraction": str(SHADOW_GRADUATION_MAXIMUM_DRAWDOWN),
        "best_trade_removed_tolerance_fraction_of_initial_equity": str(
            SHADOW_GRADUATION_BEST_TRADE_TOLERANCE
        ),
        "long_horizons": [63, 126, 252],
        "paper_authority": False,
        "live_money_authority": False,
    }
