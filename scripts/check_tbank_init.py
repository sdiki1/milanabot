#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any


def build_token(payload: dict[str, Any], secret_key: str) -> str:
    token_parts: dict[str, str] = {}
    for key, value in payload.items():
        if key == "Token":
            continue
        if isinstance(value, (dict, list, tuple)) or value is None:
            continue
        token_parts[key] = str(value)
    token_parts["Password"] = secret_key
    base = "".join(token_parts[key] for key in sorted(token_parts))
    print(f"Token base string: {base}")
    print(f"Token base SHA256: {hashlib.sha256(base.encode('utf-8')).hexdigest()}")
    return hashlib.sha256(base.encode("utf-8")).hexdigest()


def call_init(api_url: str, payload: dict[str, Any], timeout: int) -> dict[str, Any]:
    endpoint = f"{api_url.rstrip('/')}/Init"
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {raw}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Network error: {exc}") from exc

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON response: {raw}") from exc

    if not isinstance(parsed, dict):
        raise RuntimeError(f"Unexpected response type: {type(parsed)!r}")
    return parsed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Manual T-Bank /Init checker for TerminalKey + SecretKey."
    )
    parser.add_argument("--terminal-key", required=True, help="TBANK_TERMINAL_KEY")
    parser.add_argument("--secret-key", required=True, help="TBANK_PASSWORD (SecretKey)")
    parser.add_argument(
        "--api-url",
        default="https://securepay.tinkoff.ru/v2",
        help="T-Bank API base URL (default: https://securepay.tinkoff.ru/v2)",
    )
    parser.add_argument(
        "--amount-kop",
        type=int,
        default=1000,
        help="Amount in kopecks (default: 1000 = 10 RUB)",
    )
    parser.add_argument(
        "--description",
        default="Manual credentials check",
        help="Payment description",
    )
    parser.add_argument("--order-id", default="", help="OrderId (auto if empty)")
    parser.add_argument("--notification-url", default="", help="Optional NotificationURL")
    parser.add_argument("--success-url", default="", help="Optional SuccessURL")
    parser.add_argument("--fail-url", default="", help="Optional FailURL")
    parser.add_argument("--timeout", type=int, default=15, help="HTTP timeout seconds")
    return parser.parse_args()


def main() -> int:
    order_id = f"manual_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
    payload: dict[str, Any] = {
        "TerminalKey": "1773932386613DEMO",
        "Amount": 1000,
        "OrderId": order_id,
        "Description": "Manual credentials check",
        "Language": "ru",
        "PayType": "O",
    }
    payload["NotificationURL"] = "https://your-domain.com"
    payload["SuccessURL"] = "https://your-domain.com"
    payload["FailURL"] = "https://your-domain.com"

    payload["Token"] = build_token(payload, "Ujdya2uX2LagNl!#")



    try:
        response = call_init("https://securepay.tinkoff.ru/v2", payload, 15)
    except RuntimeError as exc:
        print(f"\nRequest failed: {exc}")
        return 2

    print("\nResponse:")
    print(json.dumps(response, ensure_ascii=False, indent=2))

    success = response.get("Success") is True or str(response.get("Success")).lower() in {"true", "1"}
    error_code = str(response.get("ErrorCode", ""))
    if success and error_code in {"", "0"}:
        print("\nOK: credentials are valid for Init.")
        return 0

    if error_code in {"204", "205"}:
        print(
            "\nHint: Error 204/205 usually means invalid TerminalKey/SecretKey pair "
            "(or SecretKey belongs to another terminal)."
        )
    return 1


if __name__ == "__main__":
    sys.exit(main())
