from __future__ import annotations

import argparse

import httpx

from agentic_quant.config import Settings


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("base_url")
    parser.add_argument("--full-stack", action="store_true")
    args = parser.parse_args()
    settings = Settings()
    with httpx.Client(base_url=args.base_url, timeout=30) as client:
        authenticated_session = False
        if settings.auth_required:
            if settings.admin_password is None or settings.admin_username is None:
                raise SystemExit(
                    "Authenticated doctor requires ADMIN_USERNAME and local "
                    "ADMIN_PASSWORD (production hash-only deployments use a separate "
                    "operator credential)."
                )
            response = client.post(
                "/v1/auth/login",
                json={
                    "username": settings.admin_username,
                    "password": settings.admin_password.get_secret_value(),
                },
            )
            response.raise_for_status()
            authenticated_session = True
        csrf = client.cookies.get("aq_csrf") or ""
        headers = {"X-CSRF-Token": csrf}
        system = client.get("/v1/system/status")
        system.raise_for_status()
        assert system.json()["live_trading_enabled"] is False
        summary = client.get("/v1/control/summary")
        summary.raise_for_status()
        assert summary.json()["constraints"]["broker_order_path_present"] is False
        demo = client.post("/v1/demo/run", headers=headers)
        demo.raise_for_status()
        payload = demo.json()
        assert payload["risk_decision"]["verdict"] == "APPROVE"
        assert payload["shadow_order"]["status"] == "RECORDED_NOT_SUBMITTED"
        if args.full_stack:
            health = client.get("/v1/data-health")
            health.raise_for_status()
            assert health.json()["raw_archive"] == "healthy"
            assert health.json()["event_bus"] == "healthy"
            market = client.post("/v1/demo/market-data", headers=headers)
            market.raise_for_status()
            assert market.json()["status"] == "COMPLETED"
        if authenticated_session:
            logout = client.post("/v1/auth/logout", headers=headers)
            logout.raise_for_status()
    print("authenticated-control-center: healthy")


if __name__ == "__main__":
    main()
