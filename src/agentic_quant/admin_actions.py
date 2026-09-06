from __future__ import annotations

import hmac
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Callable

from sqlalchemy import Engine, insert, select, update

from agentic_quant.code_changes import CodeChangeStore
from agentic_quant.control_plane import SystemObjectStore
from agentic_quant.database import admin_action_requests, runtime_controls
from agentic_quant.domain import EventEnvelope
from agentic_quant.ids import uuid7
from agentic_quant.ledger import EventLedger
from agentic_quant.shadow import ShadowRuntime


ALLOWED_ACTIONS = {
    "list.replace",
    "list.add",
    "list.remove",
    "runtime.pause",
    "runtime.resume",
    "pipeline.pause",
    "pipeline.resume",
    "llm.budget.update",
    "llm.routes.update",
    "ml.model.promote",
    "strategy.adopt",
    "strategy.pause",
    "strategy.retire",
    "shadow.start",
    "shadow.pause",
    "shadow.resume",
    "shadow.retire",
    "shadow.tick",
    "code_change.open",
    "code_change.approve_commit",
}


class AdminActionService:
    def __init__(
        self,
        engine: Engine,
        objects: SystemObjectStore,
        shadow: ShadowRuntime,
        code_changes: CodeChangeStore,
        *,
        runtime_callback: Callable[[bool], None],
        runtime_paused_callback: Callable[[], bool],
        route_callback: Callable[
            [dict[str, str], str, str], dict[str, Any]
        ],
        budget_preview_callback: Callable[[dict[str, Any]], dict[str, Any]],
        budget_update_callback: Callable[
            [dict[str, Any], str, str], dict[str, Any]
        ],
        model_promote_callback: Callable[[str, str, str], dict[str, Any]],
        ledger: EventLedger | None = None,
    ) -> None:
        self.engine = engine
        self.objects = objects
        self.shadow = shadow
        self.code_changes = code_changes
        self.runtime_callback = runtime_callback
        self.runtime_paused_callback = runtime_paused_callback
        self.route_callback = route_callback
        self.budget_preview_callback = budget_preview_callback
        self.budget_update_callback = budget_update_callback
        self.model_promote_callback = model_promote_callback
        self.ledger = ledger

    def propose(
        self,
        *,
        action_type: str,
        target_type: str,
        target_id: str,
        parameters: dict[str, Any],
        reason: str,
        requested_by: str,
    ) -> dict[str, Any]:
        if action_type not in ALLOWED_ACTIONS:
            raise ValueError("Action is not in the administrator allowlist")
        if len(reason.strip()) < 3 or len(reason.strip()) > 2_000:
            raise ValueError("Action reason must contain 3 to 2000 characters")
        action_id = uuid7()
        now = datetime.now(UTC)
        preview = self._preview(action_type, target_id, parameters)
        values = {
            "action_request_id": action_id,
            "action_type": action_type,
            "target_type": target_type[:60],
            "target_id": target_id[:160],
            "parameters_json": parameters,
            "reason": reason.strip(),
            "preview_json": preview,
            "status": "PENDING_CONFIRMATION",
            "requested_by": requested_by,
            "created_at": now,
            "expires_at": now + timedelta(minutes=15),
            "confirmed_at": None,
            "executed_at": None,
            "result_json": {},
            "error_code": None,
        }
        with self.engine.begin() as connection:
            connection.execute(insert(admin_action_requests).values(**values))
        self._emit(
            "admin.action.proposed.v1",
            action_id,
            {
                "action_type": action_type,
                "target_type": target_type,
                "target_id": target_id,
                "preview": preview,
                "requested_by": requested_by,
            },
        )
        result = {**values, "parameters": parameters, "preview": preview}
        result["confirmation_phrase"] = self.confirmation_phrase(action_id)
        return result

    async def confirm(
        self,
        *,
        action_id: str,
        confirmation_phrase: str,
        confirmed_by: str,
    ) -> dict[str, Any]:
        expected = self.confirmation_phrase(action_id)
        if not hmac.compare_digest(confirmation_phrase.strip(), expected):
            raise ValueError("Confirmation phrase does not match this action")
        now = datetime.now(UTC)
        with self.engine.begin() as connection:
            row = connection.execute(
                select(admin_action_requests).where(
                    admin_action_requests.c.action_request_id == action_id
                )
            ).one_or_none()
            if row is None:
                raise ValueError("Admin action not found")
            if row.status != "PENDING_CONFIRMATION":
                raise ValueError("Admin action is no longer pending")
            expires_at = row.expires_at
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=UTC)
            if expires_at <= now:
                connection.execute(
                    update(admin_action_requests)
                    .where(admin_action_requests.c.action_request_id == action_id)
                    .values(status="EXPIRED")
                )
                raise ValueError("Admin action has expired")
            claimed = connection.execute(
                update(admin_action_requests)
                .where(
                    (admin_action_requests.c.action_request_id == action_id)
                    & (
                        admin_action_requests.c.status
                        == "PENDING_CONFIRMATION"
                    )
                )
                .values(status="CONFIRMED", confirmed_at=now)
            )
            if int(claimed.rowcount or 0) != 1:
                raise ValueError("Admin action was already claimed")
            item = dict(row._mapping)
        try:
            result = await self._execute(item, confirmed_by=confirmed_by)
            stored_result = json.loads(json.dumps(result, default=str))
        except Exception as exc:
            with self.engine.begin() as connection:
                connection.execute(
                    update(admin_action_requests)
                    .where(admin_action_requests.c.action_request_id == action_id)
                    .values(
                        status="FAILED",
                        executed_at=datetime.now(UTC),
                        error_code=type(exc).__name__[:120],
                        result_json={"error": str(exc)},
                    )
                )
            self._emit(
                "admin.action.failed.v1",
                action_id,
                {"action_type": item["action_type"], "error_code": type(exc).__name__},
            )
            raise
        with self.engine.begin() as connection:
            connection.execute(
                update(admin_action_requests)
                .where(admin_action_requests.c.action_request_id == action_id)
                .values(
                    status="EXECUTED",
                    executed_at=datetime.now(UTC),
                    result_json=stored_result,
                )
            )
        self.objects.post(
            object_type=str(item["target_type"]),
            object_id=str(item["target_id"]),
            title=f"Admin action: {item['action_type']}",
            author_kind="admin",
            author_name=confirmed_by,
            body=f"Confirmed and executed. {item['reason']}",
            metadata={"action_request_id": action_id, "result": stored_result},
        )
        self._emit(
            "admin.action.executed.v1",
            action_id,
            {
                "action_type": item["action_type"],
                "target_type": item["target_type"],
                "target_id": item["target_id"],
                "confirmed_by": confirmed_by,
                "result": stored_result,
            },
        )
        value = self.get(action_id)
        assert value is not None
        return value

    def get(self, action_id: str) -> dict[str, Any] | None:
        self._expire_stale()
        with self.engine.connect() as connection:
            row = connection.execute(
                select(admin_action_requests).where(
                    admin_action_requests.c.action_request_id == action_id
                )
            ).one_or_none()
        return self._serialize(row) if row is not None else None

    def cancel(self, *, action_id: str, cancelled_by: str) -> dict[str, Any]:
        with self.engine.begin() as connection:
            result = connection.execute(
                update(admin_action_requests)
                .where(
                    (admin_action_requests.c.action_request_id == action_id)
                    & (
                        admin_action_requests.c.status
                        == "PENDING_CONFIRMATION"
                    )
                )
                .values(
                    status="CANCELLED",
                    result_json={"cancelled_by": cancelled_by},
                )
            )
        if int(result.rowcount or 0) != 1:
            raise ValueError("Admin action is no longer pending")
        self._emit(
            "admin.action.cancelled.v1",
            action_id,
            {"cancelled_by": cancelled_by},
        )
        value = self.get(action_id)
        assert value is not None
        return value

    def recent(self, *, limit: int = 100) -> list[dict[str, Any]]:
        self._expire_stale()
        statement = (
            select(admin_action_requests)
            .order_by(admin_action_requests.c.created_at.desc())
            .limit(limit)
        )
        with self.engine.connect() as connection:
            return [self._serialize(row) for row in connection.execute(statement)]

    def _expire_stale(self) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                update(admin_action_requests)
                .where(
                    (admin_action_requests.c.status == "PENDING_CONFIRMATION")
                    & (admin_action_requests.c.expires_at <= datetime.now(UTC))
                )
                .values(status="EXPIRED")
            )

    @staticmethod
    def confirmation_phrase(action_id: str) -> str:
        return f"CONFIRM {action_id[-6:]}"

    def _preview(
        self,
        action_type: str,
        target_id: str,
        parameters: dict[str, Any],
    ) -> dict[str, Any]:
        if action_type.startswith("list."):
            current = self.objects.get_list(target_id)
            if current is None:
                raise ValueError("System list not found")
            if current["mode"] == "POLICY":
                raise ValueError("Policy list cannot be edited from the control center")
            requested = {
                str(value).strip().upper()
                for value in parameters.get("members", [])
                if str(value).strip()
            }
            before = set(current["members"])
            if action_type == "list.add":
                after = before | requested
            elif action_type == "list.remove":
                after = before - requested
            else:
                after = requested
            return {
                "summary": f"Revise {current['name']}",
                "before": sorted(before),
                "after": sorted(after),
                "added": sorted(after - before),
                "removed": sorted(before - after),
            }
        if action_type == "strategy.adopt":
            report_id = parameters.get("validation_report_id")
            if not isinstance(report_id, str) or not report_id:
                raise ValueError("validation_report_id is required")
            return self.shadow.adoption_preview(
                strategy_spec_id=target_id,
                validation_report_id=report_id,
            )
        if action_type == "shadow.start":
            try:
                return self.shadow.deployment_preview(
                    strategy_spec_id=str(parameters["strategy_spec_id"]),
                    symbol=str(parameters["symbol"]),
                    initial_cash=Decimal(
                        str(parameters.get("initial_cash", "100000"))
                    ),
                )
            except KeyError as exc:
                raise ValueError("strategy_spec_id and symbol are required") from exc
        if action_type in {"pipeline.pause", "pipeline.resume"}:
            self.pipeline_enabled(target_id)
        if action_type == "llm.routes.update":
            raw_routes = parameters.get("routes")
            expected = {
                "interactive_explanation",
                "routine_pipeline",
                "critical_research",
                "strategy_generation",
                "strategy_critique",
            }
            if not isinstance(raw_routes, dict) or set(raw_routes) != expected:
                raise ValueError("LLM route action must define every workload exactly once")
            if set(str(value) for value in raw_routes.values()) - {"openai", "meta"}:
                raise ValueError("LLM route action contains an unknown provider")
        if action_type == "llm.budget.update":
            raw_limits = parameters.get("workload_daily")
            if not isinstance(raw_limits, dict):
                raise ValueError("LLM budget action requires workload_daily limits")
            return self.budget_preview_callback(raw_limits)
        if action_type == "code_change.open":
            try:
                return self.code_changes.preview(
                    request=str(parameters["request"]),
                    scope=[str(value) for value in parameters["scope"]],
                )
            except (KeyError, TypeError) as exc:
                raise ValueError("Code change request and scope are required") from exc
        summaries = {
            "runtime.pause": "Pause all new shadow exposure",
            "runtime.resume": "Resume eligible shadow exposure",
            "pipeline.pause": "Pause this pipeline",
            "pipeline.resume": "Enable this pipeline",
            "llm.budget.update": "Activate new daily LLM workload budget limits",
            "llm.routes.update": "Activate a new immutable LLM routing revision",
            "ml.model.promote": "Promote a gate-eligible ML challenger to champion",
            "strategy.adopt": "Adopt a gate-eligible strategy for shadow use",
            "strategy.pause": "Pause this adopted strategy and its deployments",
            "strategy.retire": "Retire this strategy and its deployments",
            "shadow.start": "Start a broker-free virtual deployment",
            "shadow.pause": "Pause this virtual deployment",
            "shadow.resume": "Resume this virtual deployment",
            "shadow.retire": "Permanently retire this virtual deployment",
            "shadow.tick": "Process newly available stored bars once",
            "code_change.open": "Queue an isolated, scoped code-change session",
            "code_change.approve_commit": "Approve a tested diff for local commit",
        }
        return {
            "summary": summaries[action_type],
            "target_id": target_id,
            "parameters": parameters,
            "live_broker_effect": False,
        }

    async def _execute(
        self,
        item: dict[str, Any],
        *,
        confirmed_by: str,
    ) -> dict[str, Any]:
        action_type = str(item["action_type"])
        target_id = str(item["target_id"])
        parameters = dict(item["parameters_json"])
        reason = str(item["reason"])
        if action_type.startswith("list."):
            current = self.objects.get_list(target_id)
            if current is None:
                raise ValueError("System list not found")
            requested = {
                str(value).strip().upper()
                for value in parameters.get("members", [])
                if str(value).strip()
            }
            before = set(current["members"])
            if action_type == "list.add":
                members = sorted(before | requested)
            elif action_type == "list.remove":
                members = sorted(before - requested)
            else:
                members = sorted(requested)
            return self.objects.replace_list_members(
                slug_or_id=target_id,
                members=members,
                reason=reason,
                created_by=confirmed_by,
            )
        if action_type in {"runtime.pause", "runtime.resume"}:
            paused = action_type == "runtime.pause"
            self._set_runtime_control(paused=paused, updated_by=confirmed_by)
            self.runtime_callback(paused)
            return {"new_exposure_paused": paused}
        if action_type in {"pipeline.pause", "pipeline.resume"}:
            enabled = action_type == "pipeline.resume"
            self._set_pipeline_control(
                pipeline=target_id,
                enabled=enabled,
                updated_by=confirmed_by,
            )
            return {"pipeline": target_id, "enabled": enabled}
        if action_type == "llm.routes.update":
            raw_routes = parameters.get("routes")
            if not isinstance(raw_routes, dict):
                raise ValueError("LLM route parameters are invalid")
            return self.route_callback(
                {str(key): str(value) for key, value in raw_routes.items()},
                reason,
                confirmed_by,
            )
        if action_type == "llm.budget.update":
            raw_limits = parameters.get("workload_daily")
            if not isinstance(raw_limits, dict):
                raise ValueError("LLM budget parameters are invalid")
            return self.budget_update_callback(raw_limits, reason, confirmed_by)
        if action_type == "ml.model.promote":
            return self.model_promote_callback(target_id, reason, confirmed_by)
        if action_type == "strategy.adopt":
            return self.shadow.adopt_strategy(
                strategy_spec_id=target_id,
                validation_report_id=str(parameters["validation_report_id"]),
                reason=reason,
                approved_by=confirmed_by,
            )
        if action_type in {"strategy.pause", "strategy.retire"}:
            return self.shadow.set_adoption_status(
                strategy_spec_id=target_id,
                status="PAUSED" if action_type.endswith("pause") else "RETIRED",
                reason=reason,
                approved_by=confirmed_by,
            )
        if action_type == "shadow.start":
            return self.shadow.start_deployment(
                strategy_spec_id=str(parameters["strategy_spec_id"]),
                symbol=str(parameters["symbol"]),
                initial_cash=Decimal(str(parameters.get("initial_cash", "100000"))),
                requested_by=confirmed_by,
            )
        if action_type in {"shadow.pause", "shadow.resume", "shadow.retire"}:
            states = {
                "shadow.pause": "PAUSED",
                "shadow.resume": "ACTIVE",
                "shadow.retire": "RETIRED",
            }
            return self.shadow.set_deployment_status(
                deployment_id=target_id,
                status=states[action_type],
                requested_by=confirmed_by,
                reason=reason,
            )
        if action_type == "shadow.tick":
            if not self.pipeline_enabled("shadow"):
                raise ValueError("Shadow pipeline is paused")
            return await self.shadow.tick(
                trigger=f"admin:{confirmed_by}",
                new_exposure_paused=self.runtime_paused_callback(),
            )
        if action_type == "code_change.open":
            return self.code_changes.open(
                request=str(parameters["request"]),
                scope=[str(value) for value in parameters["scope"]],
                requested_by=confirmed_by,
            )
        if action_type == "code_change.approve_commit":
            return self.code_changes.approve_commit(session_id=target_id)
        raise ValueError("Action handler is not implemented")

    def _set_runtime_control(self, *, paused: bool, updated_by: str) -> None:
        values = {
            "control_key": "new_exposure_paused",
            "state_json": {"paused": paused},
            "updated_by": updated_by,
            "updated_at": datetime.now(UTC),
        }
        with self.engine.begin() as connection:
            current = connection.execute(
                select(runtime_controls.c.control_key).where(
                    runtime_controls.c.control_key == "new_exposure_paused"
                )
            ).scalar_one_or_none()
            if current is None:
                connection.execute(insert(runtime_controls).values(**values))
            else:
                connection.execute(
                    update(runtime_controls)
                    .where(runtime_controls.c.control_key == "new_exposure_paused")
                    .values(**values)
                )

    def pipeline_controls(self) -> list[dict[str, Any]]:
        defaults = {
            "market-data": True,
            "documents": True,
            "research": True,
            "ml": True,
            "llm": True,
            "shadow": True,
        }
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(runtime_controls).where(
                    runtime_controls.c.control_key.like("pipeline:%")
                )
            ).all()
        overrides = {
            str(row.control_key).split(":", 1)[1]: {
                **dict(row.state_json),
                "updated_by": row.updated_by,
                "updated_at": row.updated_at,
            }
            for row in rows
        }
        return [
            {
                "pipeline": name,
                "enabled": bool(overrides.get(name, {}).get("enabled", enabled)),
                "updated_by": overrides.get(name, {}).get("updated_by"),
                "updated_at": overrides.get(name, {}).get("updated_at"),
            }
            for name, enabled in defaults.items()
        ]

    def new_exposure_paused(self, *, default: bool) -> bool:
        with self.engine.connect() as connection:
            value = connection.execute(
                select(runtime_controls.c.state_json).where(
                    runtime_controls.c.control_key == "new_exposure_paused"
                )
            ).scalar_one_or_none()
        if not isinstance(value, dict):
            return default
        return bool(value.get("paused", default))

    def pipeline_enabled(self, pipeline: str) -> bool:
        values = {
            item["pipeline"]: bool(item["enabled"])
            for item in self.pipeline_controls()
        }
        if pipeline not in values:
            raise ValueError("Unknown pipeline")
        return values[pipeline]

    def _set_pipeline_control(
        self,
        *,
        pipeline: str,
        enabled: bool,
        updated_by: str,
    ) -> None:
        known = {item["pipeline"] for item in self.pipeline_controls()}
        if pipeline not in known:
            raise ValueError("Unknown pipeline")
        key = f"pipeline:{pipeline}"
        values = {
            "control_key": key,
            "state_json": {"enabled": enabled},
            "updated_by": updated_by,
            "updated_at": datetime.now(UTC),
        }
        with self.engine.begin() as connection:
            current = connection.execute(
                select(runtime_controls.c.control_key).where(
                    runtime_controls.c.control_key == key
                )
            ).scalar_one_or_none()
            if current is None:
                connection.execute(insert(runtime_controls).values(**values))
            else:
                connection.execute(
                    update(runtime_controls)
                    .where(runtime_controls.c.control_key == key)
                    .values(**values)
                )

    @staticmethod
    def _serialize(row: Any) -> dict[str, Any]:
        value = dict(row._mapping)
        value["parameters"] = value.pop("parameters_json")
        value["preview"] = value.pop("preview_json")
        value["result"] = value.pop("result_json")
        if value["status"] == "PENDING_CONFIRMATION":
            value["confirmation_phrase"] = AdminActionService.confirmation_phrase(
                str(value["action_request_id"])
            )
        return value

    def _emit(
        self,
        event_type: str,
        correlation_id: str,
        payload: dict[str, Any],
    ) -> None:
        if self.ledger is None:
            return
        now = datetime.now(UTC)
        self.ledger.append(
            EventEnvelope(
                event_id=uuid7(),
                event_type=event_type,
                event_time=now,
                emitted_at=now,
                producer="phase6-admin-actions",
                correlation_id=correlation_id,
                payload=json.loads(json.dumps(payload, default=str)),
            )
        )
