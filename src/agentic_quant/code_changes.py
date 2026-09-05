from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Engine, insert, select, update

from agentic_quant.database import code_change_sessions
from agentic_quant.ids import uuid7


class CodeChangeStore:
    """Approval queue for isolated code work; it never exposes a shell to the web app."""

    def __init__(self, engine: Engine, *, base_git_sha: str) -> None:
        self.engine = engine
        self.base_git_sha = base_git_sha

    def open(
        self,
        *,
        request: str,
        scope: list[str],
        requested_by: str,
    ) -> dict[str, Any]:
        preview = self.preview(request=request, scope=scope)
        normalized_scope = list(preview["scope"])
        now = datetime.now(UTC)
        session_id = uuid7()
        values: dict[str, Any] = {
            "code_change_session_id": session_id,
            "status": "QUEUED",
            "request": request.strip(),
            "scope_json": normalized_scope,
            "base_git_sha": self.base_git_sha,
            "branch_name": f"steward/{session_id}",
            "worktree_path": None,
            "diff_sha256": None,
            "diff_text": None,
            "tests_json": [],
            "proposed_commit_subject": None,
            "committed_git_sha": None,
            "requested_by": requested_by,
            "created_at": now,
            "updated_at": now,
            "approved_at": None,
        }
        with self.engine.begin() as connection:
            connection.execute(insert(code_change_sessions).values(**values))
        return values

    def preview(self, *, request: str, scope: list[str]) -> dict[str, Any]:
        normalized_request = request.strip()
        if len(normalized_request) < 3 or len(normalized_request) > 20_000:
            raise ValueError("Code change request must contain 3 to 20000 characters")
        return {
            "summary": "Queue an isolated, scoped code-change session",
            "request": normalized_request,
            "scope": self._validate_scope(scope),
            "base_git_sha": self.base_git_sha,
            "web_shell_available": False,
        }

    def recent(self, *, limit: int = 100) -> list[dict[str, Any]]:
        statement = (
            select(code_change_sessions)
            .order_by(code_change_sessions.c.updated_at.desc())
            .limit(limit)
        )
        with self.engine.connect() as connection:
            return [dict(row._mapping) for row in connection.execute(statement)]

    def get(self, session_id: str) -> dict[str, Any] | None:
        with self.engine.connect() as connection:
            row = connection.execute(
                select(code_change_sessions).where(
                    code_change_sessions.c.code_change_session_id == session_id
                )
            ).one_or_none()
        return dict(row._mapping) if row is not None else None

    def record_candidate(
        self,
        *,
        session_id: str,
        diff_text: str,
        tests: list[dict[str, Any]],
        proposed_commit_subject: str,
    ) -> dict[str, Any]:
        current = self.get(session_id)
        if current is None:
            raise ValueError("Code change session not found")
        if current["status"] not in {"QUEUED", "RUNNING"}:
            raise ValueError("Code change session no longer accepts candidate output")
        passing = bool(tests) and all(item.get("passed") is True for item in tests)
        with self.engine.begin() as connection:
            connection.execute(
                update(code_change_sessions)
                .where(
                    code_change_sessions.c.code_change_session_id == session_id
                )
                .values(
                    status="READY_FOR_APPROVAL" if passing else "FAILED",
                    diff_sha256=hashlib.sha256(diff_text.encode()).hexdigest(),
                    diff_text=diff_text,
                    tests_json=tests,
                    proposed_commit_subject=proposed_commit_subject[:240],
                    updated_at=datetime.now(UTC),
                )
            )
        updated = self.get(session_id)
        assert updated is not None
        return updated

    def approve_commit(self, *, session_id: str) -> dict[str, Any]:
        current = self.get(session_id)
        if current is None:
            raise ValueError("Code change session not found")
        if current["status"] != "READY_FOR_APPROVAL":
            raise ValueError("Only a tested candidate can be approved for commit")
        with self.engine.begin() as connection:
            connection.execute(
                update(code_change_sessions)
                .where(
                    code_change_sessions.c.code_change_session_id == session_id
                )
                .values(status="APPROVED_FOR_COMMIT", approved_at=datetime.now(UTC))
            )
        updated = self.get(session_id)
        assert updated is not None
        return updated

    @staticmethod
    def _validate_scope(scope: list[str]) -> list[str]:
        if not scope or len(scope) > 20:
            raise ValueError("Code change scope must contain between 1 and 20 paths")
        normalized = sorted({value.strip().rstrip("/") for value in scope})
        for value in normalized:
            if (
                not value
                or value.startswith(("/", "~"))
                or ".." in value.split("/")
                or not re.fullmatch(r"[A-Za-z0-9_.@/\-]+", value)
            ):
                raise ValueError("Code change scope must contain safe repository paths")
        return normalized
