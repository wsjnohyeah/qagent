from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal, ROUND_CEILING
from pathlib import Path
from typing import Any, Self

from pydantic import Field, model_validator
from sqlalchemy import Engine, and_, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
import yaml

from agentic_quant.database import (
    llm_budget_reservations,
    llm_budget_revisions,
    llm_budget_windows,
)
from agentic_quant.domain import (
    EventEnvelope,
    FrozenModel,
    LLMProviderName,
    LLMUsage,
    LLMWorkload,
)
from agentic_quant.ids import uuid7
from agentic_quant.ledger import EventLedger


class LLMBudgetExceededError(RuntimeError):
    def __init__(self, scope: str) -> None:
        super().__init__(f"LLM budget exhausted for {scope}")
        self.scope = scope


class LLMTokenPricing(FrozenModel):
    input: Decimal = Field(ge=0)
    output: Decimal = Field(ge=0)


class LLMBudgetLimit(FrozenModel):
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


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


class LLMBudgetManager:
    def __init__(
        self,
        engine: Engine,
        policy: LLMBudgetPolicy,
        ledger: EventLedger | None = None,
        reservation_timeouts: dict[LLMProviderName, timedelta] | None = None,
    ) -> None:
        self.engine = engine
        self.policy = policy
        self.ledger = ledger
        self.reservation_timeouts = reservation_timeouts or {
            provider: timedelta(hours=1) for provider in LLMProviderName
        }
        self.base_policy_sha256 = _canonical_sha256(policy.model_dump(mode="json"))

    def configure_reservation_timeouts(
        self,
        values: dict[LLMProviderName, timedelta],
    ) -> None:
        if set(values) != set(LLMProviderName):
            raise ValueError("Reservation timeouts must define every LLM provider")
        if any(value <= timedelta(0) for value in values.values()):
            raise ValueError("Reservation timeouts must be positive")
        self.reservation_timeouts = dict(values)

    def release_expired(self, *, now: datetime | None = None) -> int:
        timestamp = now or datetime.now(UTC)
        if timestamp.tzinfo is None:
            raise ValueError("Budget expiration time must be timezone-aware")
        released = 0
        with self.engine.begin() as connection:
            rows = connection.execute(
                select(llm_budget_reservations)
                .where(llm_budget_reservations.c.status == "RESERVED")
                .with_for_update()
            ).all()
            for row in rows:
                created_at = row.created_at
                if created_at.tzinfo is None:
                    created_at = created_at.replace(tzinfo=UTC)
                provider = LLMProviderName(str(row.provider))
                if created_at + self.reservation_timeouts[provider] > timestamp:
                    continue
                expired = connection.execute(
                    update(llm_budget_reservations)
                    .where(
                        (llm_budget_reservations.c.invocation_id == row.invocation_id)
                        & (llm_budget_reservations.c.status == "RESERVED")
                    )
                    .values(status="EXPIRED", settled_at=timestamp)
                )
                if int(expired.rowcount or 0) != 1:
                    continue
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
                released += 1
        return released

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
        self.release_expired(now=timestamp)
        effective_policy = self._effective_policy()
        input_bytes = len((instructions + input_text).encode("utf-8"))
        divisor = effective_policy.reservation.input_bytes_per_token
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
            policy=effective_policy,
        )
        with self.engine.begin() as connection:
            for window_key, scope, period_kind, period_start, limit in windows:
                self._ensure_current_window(
                    connection,
                    window_key=window_key,
                    scope=scope,
                    period_kind=period_kind,
                    period_start=period_start,
                    limit=limit,
                    policy_version=effective_policy.version,
                    now=timestamp,
                )
            connection.execute(
                insert(llm_budget_reservations).values(
                    invocation_id=invocation_id,
                    policy_version=effective_policy.version,
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
                result = connection.execute(
                    update(llm_budget_windows)
                    .where(
                        and_(
                            llm_budget_windows.c.window_key == window_key,
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
                ).with_for_update()
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
                ).with_for_update()
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
        timestamp = datetime.now(UTC)
        self.release_expired(now=timestamp)
        revision = self.latest_revision()
        effective_policy = self._effective_policy(revision=revision)
        current_windows = self._all_current_window_specs(
            now=timestamp,
            policy=effective_policy,
        )
        with self.engine.begin() as connection:
            for window_key, scope, period_kind, period_start, limit in current_windows:
                self._ensure_current_window(
                    connection,
                    window_key=window_key,
                    scope=scope,
                    period_kind=period_kind,
                    period_start=period_start,
                    limit=limit,
                    policy_version=effective_policy.version,
                    now=timestamp,
                )
        with self.engine.connect() as connection:
            windows = [
                dict(row._mapping)
                for row in connection.execute(
                    select(llm_budget_windows)
                    .where(
                        llm_budget_windows.c.window_key.in_(
                            tuple(item[0] for item in current_windows)
                        )
                    )
                    .order_by(
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
            item.pop("token_limit", None)
        return {
            "base_policy_version": self.policy.version,
            "base_policy_sha256": self.base_policy_sha256,
            "policy_version": effective_policy.version,
            "policy_source": "control_center" if revision is not None else "yaml_base",
            "active_revision_id": (
                revision["budget_revision_id"] if revision is not None else None
            ),
            "pricing_is_estimate": True,
            "limits": effective_policy.limits.model_dump(mode="json"),
            "windows": windows,
            "reservation_counts": reservation_counts,
        }

    def preview_workload_limits(self, raw_limits: dict[str, Any]) -> dict[str, Any]:
        limits = self._validate_workload_limits(raw_limits)
        current = self._effective_policy().limits.workload_daily
        return {
            "summary": "Activate new daily LLM workload budget limits",
            "before": {
                workload.value: current[workload].model_dump(mode="json")
                for workload in LLMWorkload
            },
            "after": {
                workload.value: limits[workload].model_dump(mode="json")
                for workload in LLMWorkload
            },
            "project_daily_hard_cap": (
                self.policy.limits.project_daily.model_dump(mode="json")
            ),
            "resets_consumption": False,
            "live_broker_effect": False,
        }

    def effective_policy_version(self) -> str:
        return self._effective_policy().version

    def activate_workload_limits(
        self,
        *,
        raw_limits: dict[str, Any],
        reason: str,
        created_by: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        limits = self._validate_workload_limits(raw_limits)
        timestamp = now or datetime.now(UTC)
        if timestamp.tzinfo is None:
            raise ValueError("Budget revision time must be timezone-aware")
        if not 3 <= len(reason.strip()) <= 500:
            raise ValueError("Budget revision reason must contain 3 to 500 characters")
        revision_id = uuid7()
        policy_version = f"{self.policy.version}+control.{revision_id}"
        workload_json = {
            workload.value: limits[workload].model_dump(mode="json")
            for workload in LLMWorkload
        }
        effective_limits = self.policy.limits.model_copy(
            update={"workload_daily": limits}
        )
        policy_sha256 = _canonical_sha256(
            {
                "base_policy_sha256": self.base_policy_sha256,
                "limits": effective_limits.model_dump(mode="json"),
            }
        )
        values = {
            "budget_revision_id": revision_id,
            "base_policy_version": self.policy.version,
            "base_policy_sha256": self.base_policy_sha256,
            "policy_version": policy_version,
            "policy_sha256": policy_sha256,
            "workload_limits_json": workload_json,
            "reason": reason.strip(),
            "created_by": created_by,
            "created_at": timestamp,
        }
        daily_start = timestamp.astimezone(UTC).replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
        with self.engine.begin() as connection:
            connection.execute(insert(llm_budget_revisions).values(**values))
            for workload, limit in limits.items():
                window_key = self._window_key(
                    scope=f"workload:{workload.value}",
                    kind="daily",
                    start=daily_start,
                )
                connection.execute(
                    update(llm_budget_windows)
                    .where(llm_budget_windows.c.window_key == window_key)
                    .values(
                        policy_version=policy_version,
                        token_limit=0,
                        cost_limit_microusd=self._usd_to_microusd(
                            limit.max_estimated_cost_usd
                        ),
                        updated_at=timestamp,
                    )
                )
        if self.ledger is not None:
            self.ledger.append(
                EventEnvelope(
                    event_id=uuid7(),
                    event_type="llm.budget.activated.v1",
                    event_time=timestamp,
                    emitted_at=datetime.now(UTC),
                    producer="llm-budget-manager",
                    correlation_id=revision_id,
                    payload={
                        "policy_version": policy_version,
                        "policy_sha256": policy_sha256,
                        "workload_daily": workload_json,
                        "created_by": created_by,
                    },
                )
            )
        return self._serialize_revision(values)

    def latest_revision(self) -> dict[str, Any] | None:
        statement = (
            select(llm_budget_revisions)
            .where(
                # A same-version schema/UI refinement may remove a retired field
                # (the former token ceiling) without discarding the operator's USD
                # limits. Material base-policy changes must bump the policy version.
                llm_budget_revisions.c.base_policy_version
                == self.policy.version
            )
            .order_by(
                llm_budget_revisions.c.created_at.desc(),
                llm_budget_revisions.c.budget_revision_id.desc(),
            )
            .limit(1)
        )
        with self.engine.connect() as connection:
            row = connection.execute(statement).one_or_none()
        return self._serialize_revision(dict(row._mapping)) if row is not None else None

    def recent_revisions(self, *, limit: int = 50) -> list[dict[str, Any]]:
        statement = (
            select(llm_budget_revisions)
            .order_by(
                llm_budget_revisions.c.created_at.desc(),
                llm_budget_revisions.c.budget_revision_id.desc(),
            )
            .limit(limit)
        )
        with self.engine.connect() as connection:
            return [
                self._serialize_revision(dict(row._mapping))
                for row in connection.execute(statement)
            ]

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
                "llm_budget_revisions": int(
                    connection.execute(
                        select(func.count()).select_from(llm_budget_revisions)
                    ).scalar_one()
                ),
            }

    def _window_specs(
        self,
        *,
        provider: LLMProviderName,
        workload: LLMWorkload,
        now: datetime,
        policy: LLMBudgetPolicy,
    ) -> tuple[tuple[str, str, str, datetime, LLMBudgetLimit], ...]:
        utc_now = now.astimezone(UTC)
        daily_start = utc_now.replace(hour=0, minute=0, second=0, microsecond=0)
        monthly_start = daily_start.replace(day=1)
        definitions = (
            ("project", "daily", daily_start, policy.limits.project_daily),
            ("project", "monthly", monthly_start, policy.limits.project_monthly),
            (
                f"provider:{provider.value}",
                "daily",
                daily_start,
                policy.limits.provider_daily[provider],
            ),
            (
                f"workload:{workload.value}",
                "daily",
                daily_start,
                policy.limits.workload_daily[workload],
            ),
        )
        return tuple(
            (
                self._window_key(scope=scope, kind=kind, start=start),
                scope,
                kind,
                start,
                limit,
            )
            for scope, kind, start, limit in definitions
        )

    def _all_current_window_specs(
        self,
        *,
        now: datetime,
        policy: LLMBudgetPolicy,
    ) -> tuple[tuple[str, str, str, datetime, LLMBudgetLimit], ...]:
        values: dict[str, tuple[str, str, str, datetime, LLMBudgetLimit]] = {}
        for provider in LLMProviderName:
            for workload in LLMWorkload:
                for item in self._window_specs(
                    provider=provider,
                    workload=workload,
                    now=now,
                    policy=policy,
                ):
                    values[item[0]] = item
        return tuple(values[key] for key in sorted(values))

    def _ensure_current_window(
        self,
        connection: Any,
        *,
        window_key: str,
        scope: str,
        period_kind: str,
        period_start: datetime,
        limit: LLMBudgetLimit,
        policy_version: str,
        now: datetime,
    ) -> None:
        connection.execute(
            self._insert_ignore(llm_budget_windows).values(
                window_key=window_key,
                policy_version=policy_version,
                scope=scope,
                period_kind=period_kind,
                period_start=period_start,
                # Token totals remain diagnostic telemetry. Zero means
                # there is intentionally no operator token ceiling.
                token_limit=0,
                cost_limit_microusd=self._usd_to_microusd(
                    limit.max_estimated_cost_usd
                ),
                reserved_tokens=0,
                consumed_tokens=0,
                reserved_cost_microusd=0,
                consumed_cost_microusd=0,
                updated_at=now,
            )
        )
        # Lock the current-policy row before rebuilding its counters from the
        # reservation ledger. This carries same-period spend across policy-version
        # changes and prevents a config deployment from resetting today's budget.
        connection.execute(
            select(llm_budget_windows.c.window_key)
            .where(llm_budget_windows.c.window_key == window_key)
            .with_for_update()
        ).one()
        usage = self._reservation_usage(
            connection,
            scope=scope,
            period_kind=period_kind,
            period_start=period_start,
        )
        connection.execute(
            update(llm_budget_windows)
            .where(llm_budget_windows.c.window_key == window_key)
            .values(
                policy_version=policy_version,
                token_limit=0,
                cost_limit_microusd=self._usd_to_microusd(
                    limit.max_estimated_cost_usd
                ),
                reserved_tokens=usage[0],
                consumed_tokens=usage[1],
                reserved_cost_microusd=usage[2],
                consumed_cost_microusd=usage[3],
                updated_at=now,
            )
        )

    @staticmethod
    def _reservation_usage(
        connection: Any,
        *,
        scope: str,
        period_kind: str,
        period_start: datetime,
    ) -> tuple[int, int, int, int]:
        if period_kind == "daily":
            period_end = period_start + timedelta(days=1)
        elif period_kind == "monthly":
            period_end = (
                period_start.replace(year=period_start.year + 1, month=1)
                if period_start.month == 12
                else period_start.replace(month=period_start.month + 1)
            )
        else:
            raise ValueError("Unsupported LLM budget period")
        statement = select(llm_budget_reservations).where(
            llm_budget_reservations.c.created_at >= period_start,
            llm_budget_reservations.c.created_at < period_end,
        )
        if scope.startswith("provider:"):
            statement = statement.where(
                llm_budget_reservations.c.provider == scope.split(":", 1)[1]
            )
        elif scope.startswith("workload:"):
            statement = statement.where(
                llm_budget_reservations.c.workload == scope.split(":", 1)[1]
            )
        elif scope != "project":
            raise ValueError("Unsupported LLM budget scope")
        reserved_tokens = consumed_tokens = 0
        reserved_cost = consumed_cost = 0
        for row in connection.execute(statement):
            if str(row.status) == "RESERVED":
                reserved_tokens += int(row.reserved_input_tokens) + int(
                    row.reserved_output_tokens
                )
                reserved_cost += int(row.reserved_cost_microusd)
            elif str(row.status) == "SETTLED":
                consumed_tokens += int(row.actual_input_tokens or 0) + int(
                    row.actual_output_tokens or 0
                )
                consumed_cost += int(row.actual_cost_microusd or 0)
        return reserved_tokens, consumed_tokens, reserved_cost, consumed_cost

    def _effective_policy(
        self,
        *,
        revision: dict[str, Any] | None = None,
    ) -> LLMBudgetPolicy:
        selected = revision if revision is not None else self.latest_revision()
        if selected is None:
            return self.policy
        workload_limits = {
            LLMWorkload(workload): LLMBudgetLimit.model_validate(limit)
            for workload, limit in selected["workload_daily"].items()
        }
        return self.policy.model_copy(
            update={
                "version": str(selected["policy_version"]),
                "limits": self.policy.limits.model_copy(
                    update={"workload_daily": workload_limits}
                ),
            }
        )

    def _validate_workload_limits(
        self,
        raw_limits: dict[str, Any],
    ) -> dict[LLMWorkload, LLMBudgetLimit]:
        normalized = {
            (key.value if isinstance(key, LLMWorkload) else str(key)): value
            for key, value in raw_limits.items()
        }
        expected = {workload.value for workload in LLMWorkload}
        if set(normalized) != expected:
            raise ValueError("Budget revision must define every workload exactly once")
        limits = {
            workload: LLMBudgetLimit.model_validate(normalized[workload.value])
            for workload in LLMWorkload
        }
        project_cap = self.policy.limits.project_daily
        for workload, limit in limits.items():
            if limit.max_estimated_cost_usd > project_cap.max_estimated_cost_usd:
                raise ValueError(
                    f"{workload.value} cost limit exceeds the project daily hard cap"
                )
        return limits

    def _window_key(self, *, scope: str, kind: str, start: datetime) -> str:
        return f"{self.policy.version}:{scope}:{kind}:{start.date().isoformat()}"

    @staticmethod
    def _serialize_revision(item: dict[str, Any]) -> dict[str, Any]:
        created_at = item["created_at"]
        if isinstance(created_at, datetime) and created_at.tzinfo is None:
            item["created_at"] = created_at.replace(tzinfo=UTC)
        item["workload_daily"] = item.pop("workload_limits_json")
        return item

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
