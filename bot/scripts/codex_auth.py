#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from codex.auth import CODEX_CLIENT_ID, CodexAuthManager, write_owner_only  # noqa: E402
from branding import DEFAULT_BOT_NAME  # noqa: E402

AUTH_ISSUER = "https://auth.openai.com"
API_BASE = f"{AUTH_ISSUER}/api/accounts"
TOKEN_URL = f"{AUTH_ISSUER}/oauth/token"

# auth.openai.com sits behind Cloudflare, which route-errors (HTTP 530
# cf_route_error) the default "Python-urllib/x.y" User-Agent. Send a real one
# so requests get through.
_USER_AGENT = f"{DEFAULT_BOT_NAME}-CodexAuth/1.0"
DEFAULT_TOKEN_FILE = Path("secrets/codex-auth.json")


class HttpJsonError(RuntimeError):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"request failed ({status}): {detail}")
        self.status = status
        self.detail = detail


def _post_json(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    return _read_json(request)


def _post_form(url: str, payload: dict[str, str]) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(payload).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    return _read_json(request)


def _read_json(request: urllib.request.Request) -> dict[str, Any]:
    request.add_header("User-Agent", _USER_AGENT)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise HttpJsonError(exc.code, detail) from exc
    parsed = json.loads(data)
    if not isinstance(parsed, dict):
        raise RuntimeError("response was not a JSON object")
    return parsed


def _request_user_code() -> tuple[str, str, int]:
    payload = _post_json(
        f"{API_BASE}/deviceauth/usercode",
        {"client_id": CODEX_CLIENT_ID},
    )
    device_auth_id = payload.get("device_auth_id")
    user_code = payload.get("user_code") or payload.get("usercode")
    interval = payload.get("interval") or 5
    if not isinstance(device_auth_id, str) or not isinstance(user_code, str):
        raise RuntimeError("device auth response did not include device_auth_id/user_code")
    try:
        interval_seconds = int(interval)
    except TypeError, ValueError:
        interval_seconds = 5
    return device_auth_id, user_code, interval_seconds


def _device_auth_error_code(exc: HttpJsonError) -> str:
    if exc.status in {403, 404}:
        return "authorization_pending"
    try:
        body = json.loads(exc.detail)
    except json.JSONDecodeError, TypeError:
        return ""
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        error = error.get("code") or error.get("error")
    return error if isinstance(error, str) else ""


def _poll_for_token(
    *,
    device_auth_id: str,
    user_code: str,
    interval_seconds: int,
) -> dict[str, str]:
    deadline = time.time() + 15 * 60
    while True:
        try:
            payload = _post_json(
                f"{API_BASE}/deviceauth/token",
                {"device_auth_id": device_auth_id, "user_code": user_code},
            )
        except HttpJsonError as exc:
            error_code = _device_auth_error_code(exc)
            if error_code not in {"authorization_pending", "slow_down"}:
                raise RuntimeError(f"device auth failed ({exc.status}): {exc.detail}") from exc
            if error_code == "slow_down":
                interval_seconds += 5
            if time.time() >= deadline:
                raise RuntimeError("device auth timed out after 15 minutes") from exc
            print(".", end="", flush=True)
            time.sleep(min(interval_seconds, max(0.0, deadline - time.time())))
            continue
        except RuntimeError as exc:
            raise RuntimeError(f"device auth failed: {exc}") from exc
        auth_code = payload.get("authorization_code")
        code_verifier = payload.get("code_verifier")
        if isinstance(auth_code, str) and isinstance(code_verifier, str):
            return {"authorization_code": auth_code, "code_verifier": code_verifier}
        if time.time() >= deadline:
            raise RuntimeError("device auth timed out after 15 minutes")
        print(".", end="", flush=True)
        time.sleep(min(interval_seconds, max(0.0, deadline - time.time())))


def _exchange_authorization_code(auth_code: str, code_verifier: str) -> dict[str, Any]:
    return _post_form(
        TOKEN_URL,
        {
            "grant_type": "authorization_code",
            "code": auth_code,
            "redirect_uri": f"{AUTH_ISSUER}/deviceauth/callback",
            "client_id": CODEX_CLIENT_ID,
            "code_verifier": code_verifier,
        },
    )


def _refresh_token(refresh_token: str) -> dict[str, Any]:
    return _post_form(
        TOKEN_URL,
        {
            "client_id": CODEX_CLIENT_ID,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "scope": "openid profile email",
        },
    )


def _save_tokens(
    token_file: Path, tokens: dict[str, Any], refresh_token: str | None = None
) -> None:
    raw_id_token = tokens.get("id_token")
    id_token = raw_id_token if isinstance(raw_id_token, str) else ""
    expires_in = tokens.get("expires_in")
    if expires_in is None:
        expires_in = 3600
    data = {
        "access_token": tokens["access_token"],
        "refresh_token": tokens.get("refresh_token") or refresh_token,
        "id_token": id_token,
        "account_id": CodexAuthManager.decode_account_id(id_token),
        "expires_at": int(time.time() * 1000) + int(expires_in) * 1000,
    }
    write_owner_only(token_file, json.dumps(data, indent=2))
    print(f"\nTokens saved to {token_file}")
    print(f"Account ID: {data['account_id'] or '(not found)'}")


def device_code_flow(token_file: Path) -> None:
    device_auth_id, user_code, interval_seconds = _request_user_code()
    print("\nCodex OAuth Device Code Authentication")
    print(f"1. Open: {AUTH_ISSUER}/codex/device")
    print(f"2. Enter code: {user_code}")
    print("Waiting for authorization", end="", flush=True)
    code = _poll_for_token(
        device_auth_id=device_auth_id,
        user_code=user_code,
        interval_seconds=interval_seconds,
    )
    print(" authorized")
    tokens = _exchange_authorization_code(
        code["authorization_code"],
        code["code_verifier"],
    )
    _save_tokens(token_file, tokens)


def manual_flow(token_file: Path) -> None:
    refresh_token = input("Paste your Codex refresh_token: ").strip()
    if not refresh_token:
        raise RuntimeError("No refresh token provided")
    tokens = _refresh_token(refresh_token)
    _save_tokens(token_file, tokens, refresh_token=refresh_token)


def main() -> None:
    parser = argparse.ArgumentParser(description="Authenticate the Codex provider")
    parser.add_argument("--manual", action="store_true", help="paste a refresh token manually")
    parser.add_argument(
        "--token-file",
        default=str(DEFAULT_TOKEN_FILE),
        help="path to write Codex OAuth tokens",
    )
    args = parser.parse_args()
    token_file = Path(args.token_file)
    try:
        if args.manual:
            manual_flow(token_file)
        else:
            device_code_flow(token_file)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
