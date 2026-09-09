from __future__ import annotations

import argparse
from html import escape
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx


LOOPBACK_HOST = "127.0.0.1"
LOOPBACK_PORT = 8765
LOOPBACK_PATH = "/callback"
PRODUCTION_CALLBACK_PATH = "/v1/robinhood/oauth/callback"
ALLOWED_CALLBACK_FIELDS = ("code", "state", "error", "error_description")


def require_forward_url(value: str) -> str:
    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.params
        or parsed.query
        or parsed.fragment
        or parsed.path != PRODUCTION_CALLBACK_PATH
    ):
        raise ValueError(
            "--forward-url must be an HTTPS URL with the exact "
            f"{PRODUCTION_CALLBACK_PATH} path"
        )
    return value


def build_forward_url(forward_url: str, raw_query: str) -> str:
    target = require_forward_url(forward_url)
    parsed = parse_qs(raw_query, keep_blank_values=True)
    for field in ALLOWED_CALLBACK_FIELDS:
        if len(parsed.get(field, [])) > 1:
            raise ValueError(f"Robinhood callback repeated {field}")
    state = parsed.get("state", [])
    code = parsed.get("code", [])
    error = parsed.get("error", [])
    if len(state) != 1 or not state[0]:
        raise ValueError("Robinhood callback omitted its single-use state")
    if (len(code) != 1 or not code[0]) and (len(error) != 1 or not error[0]):
        raise ValueError("Robinhood callback omitted both code and error")
    forwarded: list[tuple[str, str]] = []
    for field in ALLOWED_CALLBACK_FIELDS:
        values = parsed.get(field, [])
        if values:
            forwarded.append((field, values[0]))
    return f"{target}?{urlencode(forwarded)}"


class _RelayServer(HTTPServer):
    forward_url: str
    completed: bool = False


class _CallbackHandler(BaseHTTPRequestHandler):
    server: _RelayServer

    def log_message(self, format: str, *args: Any) -> None:
        # Authorization codes and state must never reach terminal history or logs.
        return

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != LOOPBACK_PATH:
            self._reply(404, "Not found")
            return
        try:
            target = build_forward_url(self.server.forward_url, parsed.query)
            response = httpx.get(
                target,
                headers={"User-Agent": "QAgent-OAuth-Relay/1"},
                follow_redirects=True,
                timeout=30,
            )
            response.raise_for_status()
            self.server.completed = True
            self._reply(
                200,
                "Robinhood authorization was delivered to QAgent. "
                "Return to the QAgent Robinhood bridge page and refresh it.",
            )
        except (ValueError, httpx.HTTPError) as exc:
            self.server.completed = True
            self._reply(502, f"QAgent authorization relay failed: {escape(str(exc))}")

    def _reply(self, status: int, message: str) -> None:
        body = (
            "<!doctype html><html><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>QAgent Robinhood authorization</title></head>"
            "<body style='font-family:system-ui;margin:48px;max-width:760px'>"
            f"<h1>{'Connected' if status == 200 else 'Connection failed'}</h1>"
            f"<p>{message}</p></body></html>"
        ).encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def relay_once(*, forward_url: str, timeout_seconds: int) -> bool:
    server = _RelayServer((LOOPBACK_HOST, LOOPBACK_PORT), _CallbackHandler)
    server.forward_url = require_forward_url(forward_url)
    server.timeout = timeout_seconds
    print(
        f"Waiting once on http://{LOOPBACK_HOST}:{LOOPBACK_PORT}{LOOPBACK_PATH} "
        f"for up to {timeout_seconds} seconds."
    )
    print("No authorization code, state, or token will be printed or stored locally.")
    try:
        server.handle_request()
    finally:
        server.server_close()
    return server.completed


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Relay one Robinhood loopback OAuth callback to QAgent over HTTPS"
    )
    parser.add_argument("--forward-url", required=True)
    parser.add_argument("--timeout-seconds", type=int, default=1_800)
    args = parser.parse_args()
    if args.timeout_seconds < 60 or args.timeout_seconds > 3_600:
        parser.error("--timeout-seconds must be between 60 and 3600")
    if not relay_once(
        forward_url=args.forward_url,
        timeout_seconds=args.timeout_seconds,
    ):
        raise SystemExit("Timed out before Robinhood returned to the loopback callback")
    print("Robinhood callback relayed; the one-shot listener is closed.")


if __name__ == "__main__":
    main()
