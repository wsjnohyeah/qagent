from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, ROUND_CEILING
from pathlib import Path
from typing import Any, Self

from pydantic import Field, model_validator
from sqlalchemy import Engine, and_, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
import yaml

from agentic_quant.database import llm_budget_reservations, llm_budget_windows
from agentic_quant.domain import (
    FrozenModel,
    LLMProviderName,
    LLMUsage,
    LLMWorkload,
)


class LLMBudgetExceededError(RuntimeError):
    def __init__(self, scope: str) -> None:
        super().__init__(f"LLM budget exhausted for {scope}")
        self.scope = scope


class LLMTokenPricing(FrozenModel):
    input: Decimal = Field(ge=0)
    output: Decimal = Field(ge=0)


class LLMBudgetLimit(FrozenModel):
    max_tokens: int = Field(gt=0)
    max_estimated_cost_usd: Decimal = Field(gt=0)


class LLMBudgetLimits(FrozenModel):
    project_daily: LLMBudgetLimit
    project_monthly: LLMBudgetLimit
    provider_daily: dict[LLMProviderName, LLMBudgetLimit]
    workload_daily: dict[LLMWorkload, LLMBudgetLimit]


class LLMReservationPolicy(FrozenModel):
    input_bytes_per_token: int = Field(default=1, ge=1, le=8)


class LLMBudgetPolicy(FrozenModel):
    version: str = Field(pattern=r"^llm_budget@[0-9]+\.[0-9]+\.[0-9]+$")
    pricing_usd_per_million_tokens: dict[LLMProviderName, LLMTokenPricing]
    reservation: LLMReservationPolicy
    limits: LLMBudgetLimits

    @model_validator(mode="after")
    def configuration_is_complete(self) -> Self:
        if set(self.pricing_usd_per_million_tokens) != set(LLMProviderName):
            raise ValueError("LLM budget pricing must define every provider")
        if set(self.limits.provider_daily) != set(LLMProviderName):
            raise ValueError("LLM provider budgets must define every provider")
        if set(self.limits.workload_daily) != set(LLMWorkload):
            raise ValueError("LLM workload budgets must define every workload")
        return self


def load_llm_budget_policy(path: Path) -> LLMBudgetPolicy:
    with path.open("r", encoding="utf-8") as handle:
        return LLMBudgetPolicy.model_validate(yaml.safe_load(handle))


class LLMBudgetManager:
    def __init__(
        self,
        engine: Engine,
        policy: LLMBudgetPolicy,
    ) -> None:
        self.engine = engine
        self.policy = policy

    def reserve(
        self,
        *,
        invocation_id: str,
        provider: LLMProviderName,
        workload: LLMWorkload,
        input_text: str,
        instructions: str,
        max_output_tokens: int,
        now: datetime | None = None,
    ) -> None:
        timestamp = now or datetime.now(UTC)
        if timestamp.tzinfo is None:
            raise ValueError("Budget reservation time must be timezone-aware")
        input_bytes = len((instructions + input_text).encode("utf-8"))
        divisor = self.policy.reservation.input_bytes_per_token
        reserved_input = (input_bytes + divisor - 1) // divisor
        reserved_tokens = reserved_input + max_output_tokens
        reserved_cost = self._cost_microusd(
            provider,
            input_tokens=reserved_input,
            output_tokens=max_output_tokens,
        )
        windows = self._window_specs(
            provider=provider,
            workload=workload,
            now=timestamp,
        )
        with self.engine.begin() as connection:
            connection.execute(
                insert(llm_budget_reservations).values(
                    invocation_id=invocation_id,
                    policy_version=self.policy.version,
                    provider=provider.value,
                    workload=workload.value,
                    reserved_input_tokens=reserved_input,
                    reserved_output_tokens=max_output_tokens,
                    reserved_cost_microusd=reserved_cost,
                    actual_input_tokens=None,
                    actual_output_tokens=None,
                    actual_cost_microusd=None,
                    window_keys_json=[item[0] for item in windows],
                    status="RESERVED",
                    created_at=timestamp,
                    settled_at=None,
                )
            )
            for window_key, scope, period_kind, period_start, limit in windows:
                connection.execute(
                    self._insert_ignore(llm_budget_windows).values(
                        window_key=window_key,
                        policy_version=self.policy.version,
                        scope=scope,
                        period_kind=period_kind,
                        period_start=period_start,
                        token_limit=limit.max_tokens,
                        cost_limit_microusd=self._usd_to_microusd(
                            limit.max_estimated_cost_usd
                        ),
                        reserved_tokens=0,
                        consumed_tokens=0,
                        reserved_cost_microusd=0,
                        consumed_cost_microusd=0,
                        updated_at=timestamp,
                    )
                )
                result = connection.execute(
                    update(llm_budget_windows)
                    .where(
                        and_(
                            llm_budget_windows.c.window_key == window_key,
                            (
                                llm_budget_windows.c.reserved_tokens
                                + llm_budget_windows.c.consumed_tokens
                                + reserved_tokens
                                <= llm_budget_windows.c.token_limit
                            ),
                            (
                                llm_budget_windows.c.reserved_cost_microusd
                                + llm_budget_windows.c.consumed_cost_microusd
                                + reserved_cost
                                <= llm_budget_windows.c.cost_limit_microusd
                            ),
                        )
                    )
                    .values(
                        reserved_tokens=(
                            llm_budget_windows.c.reserved_tokens + reserved_tokens
                        ),
                        reserved_cost_microusd=(
                            llm_budget_windows.c.reserved_cost_microusd + reserved_cost
                        ),
                        updated_at=timestamp,
                    )
                )
                if result.rowcount != 1:
                    raise LLMBudgetExceededError(scope)

    def settle(
        self,
        *,
        invocation_id: str,
        usage: LLMUsage,
        now: datetime | None = None,
    ) -> None:
        timestamp = now or datetime.now(UTC)
        with self.engine.begin() as connection:
            row = connection.execute(
                select(llm_budget_reservations).where(
                    llm_budget_reservations.c.invocation_id == invocation_id
                )
            ).one()
            if row.status != "RESERVED":
                return
            provider = LLMProviderName(str(row.provider))
            actual_cost = self._cost_microusd(
                provider,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
            )
            reserved_tokens = int(row.reserved_input_tokens) + int(
                row.reserved_output_tokens
            )
            for window_key in row.window_keys_json:
                connection.execute(
                    update(llm_budget_windows)
                    .where(llm_budget_windows.c.window_key == str(window_key))
                    .values(
                        reserved_tokens=(
                            llm_budget_windows.c.reserved_tokens - reserved_tokens
                        ),
                        consumed_tokens=(
                            llm_budget_windows.c.consumed_tokens + usage.total_tokens
                        ),
                        reserved_cost_microusd=(
                            llm_budget_windows.c.reserved_cost_microusd
                            - int(row.reserved_cost_microusd)
                        ),
                        consumed_cost_microusd=(
                            llm_budget_windows.c.consumed_cost_microusd + actual_cost
                        ),
                        updated_at=timestamp,
                    )
                )
            connection.execute(
                update(llm_budget_reservations)
                .where(llm_budget_reservations.c.invocation_id == invocation_id)
                .values(
                    actual_input_tokens=usage.input_tokens,
                    actual_output_tokens=usage.output_tokens,
                    actual_cost_microusd=actual_cost,
                    status="SETTLED",
                    settled_at=timestamp,
                )
            )

    def release(
        self,
        *,
        invocation_id: str,
        now: datetime | None = None,
    ) -> None:
        timestamp = now or datetime.now(UTC)
        with self.engine.begin() as connection:
            row = connection.execute(
                select(llm_budget_reservations).where(
                    llm_budget_reservations.c.invocation_id == invocation_id
                )
            ).one_or_none()
            if row is None or row.status != "RESERVED":
                return
            reserved_tokens = int(row.reserved_input_tokens) + int(
                row.reserved_output_tokens
            )
            for window_key in row.window_keys_json:
                connection.execute(
                    update(llm_budget_windows)
                    .where(llm_budget_windows.c.window_key == str(window_key))
                    .values(
                        reserved_tokens=(
                            llm_budget_windows.c.reserved_tokens - reserved_tokens
                        ),
                        reserved_cost_microusd=(
                            llm_budget_windows.c.reserved_cost_microusd
                            - int(row.reserved_cost_microusd)
                        ),
                        updated_at=timestamp,
                    )
                )
            connection.execute(
                update(llm_budget_reservations)
                .where(llm_budget_reservations.c.invocation_id == invocation_id)
                .values(status="RELEASED", settled_at=timestamp)
            )

    def summary(self) -> dict[str, Any]:
        with self.engine.connect() as connection:
            windows = [
                dict(row._mapping)
                for row in connection.execute(
                    select(llm_budget_windows).order_by(
                        llm_budget_windows.c.period_start.desc(),
                        llm_budget_windows.c.scope.asc(),
                    )
                )
            ]
            reservation_counts = {
                str(status): int(count)
                for status, count in connection.execute(
                    select(
                        llm_budget_reservations.c.status,
                        func.count(),
                    ).group_by(llm_budget_reservations.c.status)
                )
            }
        for item in windows:
            if isinstance(item["period_start"], datetime) and item[
                "period_start"
            ].tzinfo is None:
                item["period_start"] = item["period_start"].replace(tzinfo=UTC)
            item["estimated_cost_limit_usd"] = str(
                self._microusd_to_usd(item.pop("cost_limit_microusd"))
            )
            item["reserved_estimated_cost_usd"] = str(
                self._microusd_to_usd(item.pop("reserved_cost_microusd"))
            )
            item["consumed_estimated_cost_usd"] = str(
                self._microusd_to_usd(item.pop("consumed_cost_microusd"))
            )
        return {
            "policy_version": self.policy.version,
            "pricing_is_estimate": True,
            "windows": windows,
            "reservation_counts": reservation_counts,
        }

    def health_summary(self) -> dict[str, int]:
        with self.engine.connect() as connection:
            return {
                "llm_budget_windows": int(
                    connection.execute(
                        select(func.count()).select_from(llm_budget_windows)
                    ).scalar_one()
                ),
                "llm_budget_reservations": int(
                    connection.execute(
                        select(func.count()).select_from(llm_budget_reservations)
                    ).scalar_one()
                ),
            }

    def _window_specs(
        self,
        *,
        provider: LLMProviderName,
        workload: LLMWorkload,
        now: datetime,
    ) -> tuple[tuple[str, str, str, datetime, LLMBudgetLimit], ...]:
        utc_now = now.astimezone(UTC)
        daily_start = utc_now.replace(hour=0, minute=0, second=0, microsecond=0)
        monthly_start = daily_start.replace(day=1)
        definitions = (
            ("project", "daily", daily_start, self.policy.limits.project_daily),
            ("project", "monthly", monthly_start, self.policy.limits.project_monthly),
            (
                f"provider:{provider.value}",
                "daily",
                daily_start,
                self.policy.limits.provider_daily[provider],
            ),
            (
                f"workload:{workload.value}",
                "daily",
                daily_start,
                self.policy.limits.workload_daily[workload],
            ),
        )
        return tuple(
            (
                f"{self.policy.version}:{scope}:{kind}:{start.date().isoformat()}",
                scope,
                kind,
                start,
                limit,
            )
            for scope, kind, start, limit in definitions
        )

    def _cost_microusd(
        self,
        provider: LLMProviderName,
        *,
        input_tokens: int,
        output_tokens: int,
    ) -> int:
        pricing = self.policy.pricing_usd_per_million_tokens[provider]
        value = Decimal(input_tokens) * pricing.input + Decimal(
            output_tokens
        ) * pricing.output
        return int(value.to_integral_value(rounding=ROUND_CEILING))

    @staticmethod
    def _usd_to_microusd(value: Decimal) -> int:
        return int(
            (value * Decimal("1000000")).to_integral_value(rounding=ROUND_CEILING)
        )

    @staticmethod
    def _microusd_to_usd(value: int) -> Decimal:
        return Decimal(value) / Decimal("1000000")

    def _insert_ignore(self, table: Any) -> Any:
        if self.engine.dialect.name == "postgresql":
            return postgresql_insert(table).on_conflict_do_nothing(
                index_elements=["window_key"]
            )
        if self.engine.dialect.name == "sqlite":
            return sqlite_insert(table).on_conflict_do_nothing(
                index_elements=["window_key"]
            )
        raise RuntimeError(f"Unsupported SQL dialect: {self.engine.dialect.name}")
