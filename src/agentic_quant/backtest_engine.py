from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from agentic_quant.domain import (
    BacktestCostModel,
    BacktestEventType,
    BacktestPortfolioEvent,
    BacktestTrade,
    CorporateAction,
    CorporateActionType,
    PointInTimeFeatureSnapshot,
    SignalAction,
)
from agentic_quant.ids import uuid7


_ZERO = Decimal("0")
_ONE = Decimal("1")


@dataclass
class _OpenPosition:
    signal_as_of: datetime
    entry_time: datetime
    entry_quantity: Decimal
    entry_price: Decimal
    entry_raw_price: Decimal
    entry_commission: Decimal
    starting_cash: Decimal
    feature_snapshot_id: str
    corporate_action_cash: Decimal = _ZERO


class EventDrivenPortfolio:
    """Single-symbol long/cash accounting engine for deterministic research replays."""

    def __init__(
        self,
        *,
        experiment_run_id: str,
        symbol: str,
        initial_cash: Decimal,
        cost_model: BacktestCostModel,
    ) -> None:
        if initial_cash <= 0:
            raise ValueError("Initial cash must be positive")
        self.experiment_run_id = experiment_run_id
        self.symbol = symbol.upper()
        self.cash = initial_cash
        self.quantity = _ZERO
        self.cost_model = cost_model
        self._position: _OpenPosition | None = None
        self._events: list[BacktestPortfolioEvent] = []

    @property
    def events(self) -> tuple[BacktestPortfolioEvent, ...]:
        return tuple(self._events)

    @staticmethod
    def commission(quantity: Decimal, costs: BacktestCostModel) -> Decimal:
        return max(
            costs.minimum_commission_per_order,
            quantity * costs.commission_per_share,
        )

    def record_signal(
        self,
        *,
        snapshot: PointInTimeFeatureSnapshot,
        action: SignalAction,
    ) -> None:
        self._append(
            event_type=BacktestEventType.SIGNAL,
            event_time=snapshot.as_of,
            feature_snapshot_id=snapshot.feature_snapshot_id,
            details={"action": action.value},
        )

    def enter_long(
        self,
        *,
        signal_as_of: datetime,
        entry_time: datetime,
        raw_price: Decimal,
        available_volume: int,
        feature_snapshot_id: str,
        quantity_limit: int | None = None,
    ) -> bool:
        if self.quantity != _ZERO or self._position is not None:
            raise ValueError("Cannot enter while a position is already open")
        execution_bps = (
            self.cost_model.slippage_bps_per_side
            + self.cost_model.half_spread_bps_per_side
            + self.cost_model.market_impact_bps_per_side
        )
        execution_cost = execution_bps / Decimal("10000")
        fill_price = raw_price * (_ONE + execution_cost)
        volume_capacity = Decimal(available_volume) * self.cost_model.max_volume_participation
        quantity = min(
            Decimal(int(self.cash // fill_price)),
            Decimal(int(volume_capacity)),
        )
        if quantity_limit is not None:
            if quantity_limit < 0:
                raise ValueError("quantity_limit cannot be negative")
            quantity = min(quantity, Decimal(quantity_limit))
        while quantity > 0:
            commission = self.commission(quantity, self.cost_model)
            if fill_price * quantity + commission <= self.cash:
                break
            quantity -= _ONE
        if quantity <= 0:
            return False
        starting_cash = self.cash
        commission = self.commission(quantity, self.cost_model)
        self._append(
            event_type=BacktestEventType.ORDER_SUBMITTED,
            event_time=entry_time,
            feature_snapshot_id=feature_snapshot_id,
            details={"side": "buy", "quantity": str(quantity)},
        )
        cash_delta = -(fill_price * quantity + commission)
        self.cash += cash_delta
        self.quantity = quantity
        self._position = _OpenPosition(
            signal_as_of=signal_as_of,
            entry_time=entry_time,
            entry_quantity=quantity,
            entry_price=fill_price,
            entry_raw_price=raw_price,
            entry_commission=commission,
            starting_cash=starting_cash,
            feature_snapshot_id=feature_snapshot_id,
        )
        self._append(
            event_type=BacktestEventType.FILL,
            event_time=entry_time,
            cash_delta=cash_delta,
            quantity_delta=quantity,
            price=fill_price,
            feature_snapshot_id=feature_snapshot_id,
            details={
                "side": "buy",
                "raw_price": str(raw_price),
                "commission": str(commission),
                "slippage_bps": str(self.cost_model.slippage_bps_per_side),
                "half_spread_bps": str(
                    self.cost_model.half_spread_bps_per_side
                ),
                "market_impact_bps": str(
                    self.cost_model.market_impact_bps_per_side
                ),
                "volume_participation": str(
                    quantity / Decimal(available_volume)
                    if available_volume > 0
                    else _ZERO
                ),
            },
        )
        return True

    def apply_corporate_action(self, action: CorporateAction) -> None:
        if action.symbol.upper() != self.symbol:
            raise ValueError("Corporate action symbol does not match portfolio symbol")
        if action.action_type == CorporateActionType.SYMBOL_CHANGE:
            raise ValueError("Symbol changes require a cross-symbol market-data replay")
        if action.action_type == CorporateActionType.SPLIT:
            ratio = action.split_ratio
            if ratio is None:
                raise ValueError("Split action is missing split_ratio")
            old_quantity = self.quantity
            self.quantity *= ratio
            quantity_delta = self.quantity - old_quantity
            self._append(
                event_type=BacktestEventType.SPLIT,
                event_time=action.effective_at,
                quantity_delta=quantity_delta,
                corporate_action_id=action.corporate_action_id,
                details={"ratio": str(ratio)},
            )
            return
        amount = action.cash_amount
        if amount is None:
            raise ValueError("Cash-dividend action is missing cash_amount")
        cash_delta = self.quantity * amount
        self.cash += cash_delta
        if self._position is not None:
            self._position.corporate_action_cash += cash_delta
        self._append(
            event_type=BacktestEventType.CASH_DIVIDEND,
            event_time=action.effective_at,
            cash_delta=cash_delta,
            corporate_action_id=action.corporate_action_id,
            details={"cash_per_share": str(amount), "currency": action.currency},
        )

    def mark(self, *, event_time: datetime, raw_price: Decimal) -> Decimal:
        equity = self.cash + self.quantity * raw_price
        self._append(
            event_type=BacktestEventType.MARK,
            event_time=event_time,
            price=raw_price,
            details={"equity": str(equity)},
        )
        return equity

    def exit_long(
        self,
        *,
        exit_time: datetime,
        raw_price: Decimal,
        available_volume: int,
        exit_reason: str,
    ) -> BacktestTrade | None:
        position = self._position
        if position is None or self.quantity <= 0:
            return None
        exit_quantity = self.quantity
        volume_capacity = Decimal(available_volume) * self.cost_model.max_volume_participation
        if exit_quantity > volume_capacity:
            raise ValueError(
                "Insufficient bar liquidity to close the position within the configured "
                "participation cap"
            )
        execution_bps = (
            self.cost_model.slippage_bps_per_side
            + self.cost_model.half_spread_bps_per_side
            + self.cost_model.market_impact_bps_per_side
        )
        execution_cost = execution_bps / Decimal("10000")
        fill_price = raw_price * (_ONE - execution_cost)
        commission = self.commission(exit_quantity, self.cost_model)
        self._append(
            event_type=BacktestEventType.ORDER_SUBMITTED,
            event_time=exit_time,
            feature_snapshot_id=position.feature_snapshot_id,
            details={"side": "sell", "quantity": str(exit_quantity)},
        )
        cash_delta = fill_price * exit_quantity - commission
        self.cash += cash_delta
        self.quantity = _ZERO
        self._append(
            event_type=BacktestEventType.FILL,
            event_time=exit_time,
            cash_delta=cash_delta,
            quantity_delta=-exit_quantity,
            price=fill_price,
            feature_snapshot_id=position.feature_snapshot_id,
            details={
                "side": "sell",
                "raw_price": str(raw_price),
                "commission": str(commission),
                "slippage_bps": str(self.cost_model.slippage_bps_per_side),
                "half_spread_bps": str(
                    self.cost_model.half_spread_bps_per_side
                ),
                "market_impact_bps": str(
                    self.cost_model.market_impact_bps_per_side
                ),
                "volume_participation": str(
                    exit_quantity / Decimal(available_volume)
                    if available_volume > 0
                    else _ZERO
                ),
                "exit_reason": exit_reason,
            },
        )
        gross_pnl = (
            raw_price * exit_quantity
            - position.entry_raw_price * position.entry_quantity
            + position.corporate_action_cash
        )
        net_pnl = self.cash - position.starting_cash
        transaction_cost = gross_pnl - net_pnl
        trade = BacktestTrade(
            trade_id=uuid7(),
            experiment_run_id=self.experiment_run_id,
            symbol=self.symbol,
            action=SignalAction.LONG,
            signal_as_of=position.signal_as_of,
            entry_time=position.entry_time,
            exit_time=exit_time,
            quantity=int(position.entry_quantity),
            exit_quantity=exit_quantity,
            entry_price=position.entry_price,
            exit_price=fill_price,
            gross_pnl=gross_pnl,
            corporate_action_cash=position.corporate_action_cash,
            transaction_cost=max(transaction_cost, _ZERO),
            net_pnl=net_pnl,
            feature_snapshot_id=position.feature_snapshot_id,
            exit_reason=exit_reason,
        )
        self._position = None
        return trade

    def _append(
        self,
        *,
        event_type: BacktestEventType,
        event_time: datetime,
        cash_delta: Decimal = _ZERO,
        quantity_delta: Decimal = _ZERO,
        price: Decimal | None = None,
        corporate_action_id: str | None = None,
        feature_snapshot_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        if self._events and event_time < self._events[-1].event_time:
            raise ValueError("Portfolio events must be appended in chronological order")
        self._events.append(
            BacktestPortfolioEvent(
                portfolio_event_id=uuid7(),
                experiment_run_id=self.experiment_run_id,
                sequence=len(self._events) + 1,
                event_type=event_type,
                event_time=event_time,
                symbol=self.symbol,
                cash_balance=self.cash,
                position_quantity=self.quantity,
                cash_delta=cash_delta,
                quantity_delta=quantity_delta,
                price=price,
                corporate_action_id=corporate_action_id,
                feature_snapshot_id=feature_snapshot_id,
                details=details or {},
            )
        )
