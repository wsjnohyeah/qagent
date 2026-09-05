from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from sqlalchemy import Engine, func, insert, select, update

from agentic_quant.config import Settings
from agentic_quant.database import admin_auth_events, admin_sessions
from agentic_quant.ids import uuid7


SESSION_COOKIE = "aq_admin_session"
CSRF_COOKIE = "aq_csrf"


class AuthenticationError(RuntimeError):
    pass


class LoginRateLimitedError(AuthenticationError):
    pass


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class AdminAuthService:
    def __init__(self, engine: Engine, settings: Settings) -> None:
        self.engine = engine
        self.settings = settings
        self.password_hasher = PasswordHasher()

    def login(
        self,
        *,
        username: str,
        password: str,
        user_agent: str | None,
        client_ip: str | None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        observed_at = now or datetime.now(UTC)
        normalized = username.strip()
        if self._recent_failures(normalized, observed_at) >= 5:
            self._record_auth_event(
                event_type="LOGIN_RATE_LIMITED",
                username=normalized,
                success=False,
                detail="too_many_recent_failures",
                created_at=observed_at,
            )
            raise LoginRateLimitedError("Too many login attempts; retry in 15 minutes")
        if not self._credentials_match(normalized, password):
            self._record_auth_event(
                event_type="LOGIN_FAILED",
                username=normalized,
                success=False,
                detail="invalid_credentials",
                created_at=observed_at,
            )
            raise AuthenticationError("Invalid username or password")
        token = secrets.token_urlsafe(48)
        csrf_token = secrets.token_urlsafe(32)
        expires_at = observed_at + timedelta(days=self.settings.session_max_age_days)
        values = {
            "session_id": uuid7(),
            "token_sha256": _sha256(token),
            "username": normalized,
            "credential_fingerprint": self.settings.admin_credential_fingerprint,
            "created_at": observed_at,
            "last_seen_at": observed_at,
            "expires_at": expires_at,
            "revoked_at": None,
            "user_agent_sha256": _sha256(user_agent) if user_agent else None,
            "client_ip_sha256": self._private_hash(client_ip) if client_ip else None,
        }
        with self.engine.begin() as connection:
            connection.execute(insert(admin_sessions).values(**values))
        self._record_auth_event(
            event_type="LOGIN_SUCCEEDED",
            username=normalized,
            success=True,
            detail="session_created",
            created_at=observed_at,
        )
        return {
            "session_id": values["session_id"],
            "username": normalized,
            "token": token,
            "csrf_token": csrf_token,
            "expires_at": expires_at,
        }

    def authenticate(
        self,
        token: str | None,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        if not token:
            return None
        observed_at = now or datetime.now(UTC)
        with self.engine.begin() as connection:
            row = connection.execute(
                select(admin_sessions).where(
                    admin_sessions.c.token_sha256 == _sha256(token)
                )
            ).one_or_none()
            if row is None:
                return None
            item = dict(row._mapping)
            if item["revoked_at"] is not None:
                return None
            if _utc(item["expires_at"]) <= observed_at:
                return None
            if not hmac.compare_digest(
                str(item["credential_fingerprint"]),
                self.settings.admin_credential_fingerprint,
            ):
                return None
            last_seen_at = _utc(item["last_seen_at"])
            if observed_at - last_seen_at >= timedelta(hours=1):
                expires_at = observed_at + timedelta(
                    days=self.settings.session_max_age_days
                )
                connection.execute(
                    update(admin_sessions)
                    .where(admin_sessions.c.session_id == item["session_id"])
                    .values(last_seen_at=observed_at, expires_at=expires_at)
                )
                item["last_seen_at"] = observed_at
                item["expires_at"] = expires_at
        item.pop("token_sha256", None)
        item.pop("credential_fingerprint", None)
        return item

    def logout(self, token: str | None, *, now: datetime | None = None) -> None:
        if not token:
            return
        observed_at = now or datetime.now(UTC)
        with self.engine.begin() as connection:
            row = connection.execute(
                select(admin_sessions.c.username, admin_sessions.c.session_id).where(
                    admin_sessions.c.token_sha256 == _sha256(token)
                )
            ).one_or_none()
            if row is None:
                return
            connection.execute(
                update(admin_sessions)
                .where(admin_sessions.c.session_id == row.session_id)
                .values(revoked_at=observed_at)
            )
        self._record_auth_event(
            event_type="LOGOUT",
            username=str(row.username),
            success=True,
            detail="session_revoked",
            created_at=observed_at,
        )

    def revoke_all(self, *, username: str, now: datetime | None = None) -> int:
        observed_at = now or datetime.now(UTC)
        with self.engine.begin() as connection:
            result = connection.execute(
                update(admin_sessions)
                .where(
                    (admin_sessions.c.username == username)
                    & admin_sessions.c.revoked_at.is_(None)
                )
                .values(revoked_at=observed_at)
            )
        count = int(result.rowcount or 0)
        self._record_auth_event(
            event_type="SESSIONS_REVOKED",
            username=username,
            success=True,
            detail=f"revoked:{count}",
            created_at=observed_at,
        )
        return count

    def recent_events(self, *, limit: int = 50) -> list[dict[str, Any]]:
        statement = (
            select(admin_auth_events)
            .order_by(admin_auth_events.c.created_at.desc())
            .limit(limit)
        )
        with self.engine.connect() as connection:
            return [dict(row._mapping) for row in connection.execute(statement)]

    def health_summary(self) -> dict[str, int]:
        now = datetime.now(UTC)
        with self.engine.connect() as connection:
            active = connection.execute(
                select(func.count())
                .select_from(admin_sessions)
                .where(
                    admin_sessions.c.revoked_at.is_(None)
                    & (admin_sessions.c.expires_at > now)
                )
            ).scalar_one()
        return {"active_admin_sessions": int(active)}

    def _credentials_match(self, username: str, password: str) -> bool:
        expected_username = self.settings.admin_username or ""
        if not hmac.compare_digest(username, expected_username):
            return False
        if self.settings.admin_password_hash is not None:
            try:
                return self.password_hasher.verify(
                    self.settings.admin_password_hash.get_secret_value(),
                    password,
                )
            except (InvalidHashError, VerificationError, VerifyMismatchError):
                return False
        if self.settings.admin_password is None:
            return False
        return hmac.compare_digest(
            password,
            self.settings.admin_password.get_secret_value(),
        )

    def _recent_failures(self, username: str, now: datetime) -> int:
        with self.engine.connect() as connection:
            value = connection.execute(
                select(func.count())
                .select_from(admin_auth_events)
                .where(
                    (admin_auth_events.c.username == username)
                    & (admin_auth_events.c.success.is_(False))
                    & (admin_auth_events.c.created_at >= now - timedelta(minutes=15))
                )
            ).scalar_one()
        return int(value)

    def _record_auth_event(
        self,
        *,
        event_type: str,
        username: str,
        success: bool,
        detail: str,
        created_at: datetime,
    ) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                insert(admin_auth_events).values(
                    auth_event_id=uuid7(),
                    event_type=event_type,
                    username=username[:80],
                    success=success,
                    detail=detail[:160],
                    created_at=created_at,
                )
            )

    def _private_hash(self, value: str) -> str:
        assert self.settings.session_secret is not None
        return hmac.new(
            self.settings.session_secret.get_secret_value().encode(),
            value.encode(),
            hashlib.sha256,
        ).hexdigest()


def cookie_settings(settings: Settings) -> dict[str, Any]:
    return {
        "httponly": True,
        "secure": settings.app_env.value == "production",
        "samesite": "strict",
        "max_age": settings.session_max_age_days * 24 * 60 * 60,
        "path": "/",
    }
