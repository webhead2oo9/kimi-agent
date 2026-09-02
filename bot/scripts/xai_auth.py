#!/usr/bin/env python
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from branding import DEFAULT_BOT_NAME  # noqa: E402
from xai.auth import (  # noqa: E402
    DEFAULT_XAI_TOKEN_FILE,
    XAI_OAUTH_CLIENT_ID,
    XAI_OAUTH_SCOPE,
    discover_xai_oauth,
    token_record,
    write_xai_tokens_locked,
)

_DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
_USER_AGENT = f"{DEFAULT_BOT_NAME}-XaiAuth/1.0"


class HttpJsonError(RuntimeError):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"request failed ({status}): {detail}")
        self.status = status
        self.detail = detail


def _post_form(url: str, payload: dict[str, str]) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": _USER_AGENT,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            text = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise HttpJsonError(exc.code, detail) from exc
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError("xAI returned invalid JSON") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("xAI returned a non-object JSON payload")
    return parsed


def _request_device_code(endpoint: str) -> dict[str, Any]:
    payload = _post_form(
        endpoint,
        {
            "client_id": XAI_OAUTH_CLIENT_ID,
            "scope": XAI_OAUTH_SCOPE,
        },
    )
    required = ("device_code", "user_code")
    if any(not isinstance(payload.get(field), str) or not payload[field] for field in required):
        raise RuntimeError("xAI device authorization response was incomplete")
    if not isinstance(payload.get("verification_uri"), str) and not isinstance(
        payload.get("verification_url"), str
    ):
        raise RuntimeError("xAI device authorization response omitted its verification URL")
    return payload


def _error_code(exc: HttpJsonError) -> str:
    try:
        payload = json.loads(exc.detail)
    except json.JSONDecodeError:
        return ""
    if not isinstance(payload, dict):
        return ""
    error = payload.get("error")
    if isinstance(error, dict):
        error = error.get("code") or error.get("error")
    return error if isinstance(error, str) else ""


def _poll_for_tokens(
    token_endpoint: str,
    *,
    device_code: str,
    interval_seconds: int,
    expires_in_seconds: int,
) -> dict[str, Any]:
    deadline = time.monotonic() + expires_in_seconds
    while True:
        if time.monotonic() >= deadline:
            raise RuntimeError("xAI device authorization expired")
        try:
            return _post_form(
                token_endpoint,
                {
                    "grant_type": _DEVICE_GRANT,
                    "client_id": XAI_OAUTH_CLIENT_ID,
                    "device_code": device_code,
                },
            )
        except HttpJsonError as exc:
            code = _error_code(exc)
            if code == "slow_down":
                interval_seconds += 5
            elif code != "authorization_pending":
                if code in {"access_denied", "expired_token"}:
                    raise RuntimeError(f"xAI device authorization failed: {code}") from exc
                raise RuntimeError(
                    f"xAI device authorization failed ({exc.status}): {code or exc.detail}"
                ) from exc
            print(".", end="", flush=True)
            time.sleep(min(interval_seconds, max(0.0, deadline - time.monotonic())))


def device_code_flow(token_file: Path) -> None:
    discovery = asyncio.run(discover_xai_oauth())
    device = _request_device_code(discovery.device_authorization_endpoint)
    verification_uri = str(device.get("verification_uri") or device.get("verification_url"))
    verification_complete = str(device.get("verification_uri_complete") or "")
    open_url = verification_complete or verification_uri
    user_code = str(device["user_code"])
    try:
        interval_seconds = max(1, int(device.get("interval", 5)))
    except TypeError, ValueError:
        interval_seconds = 5
    try:
        expires_in_seconds = max(1, int(device.get("expires_in", 900)))
    except TypeError, ValueError:
        expires_in_seconds = 900

    print("\nxAI Grok OAuth Device Authentication")
    print(f"1. Open: {verification_uri}")
    print(f"2. Enter code: {user_code}")
    with contextlib.suppress(Exception):
        webbrowser.open(open_url)
    print("Waiting for authorization", end="", flush=True)
    payload = _poll_for_tokens(
        discovery.token_endpoint,
        device_code=str(device["device_code"]),
        interval_seconds=interval_seconds,
        expires_in_seconds=expires_in_seconds,
    )
    tokens = token_record(payload)
    asyncio.run(write_xai_tokens_locked(token_file, tokens))
    print(f" authorized\nTokens saved to {token_file}")
    print(f"Account ID: {tokens['account_id'] or '(not found)'}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Authenticate the native xAI provider")
    parser.add_argument(
        "--token-file",
        default=str(DEFAULT_XAI_TOKEN_FILE),
        help="path to write xAI OAuth tokens",
    )
    args = parser.parse_args()
    try:
        device_code_flow(Path(args.token_file))
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
