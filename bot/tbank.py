from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

import aiohttp


class TBankError(RuntimeError):
    pass


@dataclass(frozen=True)
class TBankInitResult:
    order_id: str
    payment_id: str
    payment_url: str


def _normalize_token_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def build_token(payload: dict[str, Any], password: str) -> str:
    token_parts: dict[str, str] = {}
    for key, value in payload.items():
        if key == "Token":
            continue
        if isinstance(value, (dict, list, tuple)):
            continue
        if value is None:
            continue
        token_parts[key] = _normalize_token_value(value)
    token_parts["Password"] = password

    concatenated = "".join(token_parts[key] for key in sorted(token_parts))
    return hashlib.sha256(concatenated.encode("utf-8")).hexdigest()


def validate_notification_token(payload: dict[str, Any], password: str) -> bool:
    incoming_token = str(payload.get("Token", "")).strip().lower()
    if not incoming_token:
        return False
    expected = build_token(payload, password)
    return expected.lower() == incoming_token


class TBankClient:
    def __init__(
        self,
        terminal_key: str,
        password: str,
        api_url: str = "https://securepay.tinkoff.ru/v2",
        notification_url: str = "",
        success_url: str = "",
        fail_url: str = "",
        timeout_seconds: float = 15,
    ) -> None:
        self.terminal_key = terminal_key
        self.password = password
        self.api_url = api_url.rstrip("/")
        self.notification_url = notification_url
        self.success_url = success_url
        self.fail_url = fail_url
        self.timeout = aiohttp.ClientTimeout(total=timeout_seconds)

    async def init_payment(
        self,
        order_id: str,
        amount_kopecks: int,
        description: str,
        data: dict[str, str] | None = None,
    ) -> TBankInitResult:
        payload: dict[str, Any] = {
            "TerminalKey": self.terminal_key,
            "Amount": amount_kopecks,
            "OrderId": order_id,
            "Description": description,
            "Language": "ru",
            "PayType": "O",
        }
        if self.notification_url:
            payload["NotificationURL"] = self.notification_url
        if self.success_url:
            payload["SuccessURL"] = self.success_url
        if self.fail_url:
            payload["FailURL"] = self.fail_url
        if data:
            payload["DATA"] = data

        payload["Token"] = build_token(payload, self.password)

        endpoint = f"{self.api_url}/Init"
        async with aiohttp.ClientSession(timeout=self.timeout) as session:
            async with session.post(endpoint, json=payload) as response:
                response_payload = await response.json(content_type=None)

        if not isinstance(response_payload, dict):
            raise TBankError("Некорректный ответ от T-Bank Init.")

        raw_success = response_payload.get("Success")
        success = raw_success is True or str(raw_success).lower() in {"true", "1"}
        error_code = str(response_payload.get("ErrorCode", ""))
        if not success or (error_code and error_code != "0"):
            message = str(response_payload.get("Message", "")).strip()
            details = str(response_payload.get("Details", "")).strip()
            hint = ""
            if error_code in {"204", "205"}:
                hint = " hint=Проверьте пару TerminalKey/SecretKey (TBANK_TERMINAL_KEY/TBANK_PASSWORD)."
            raise TBankError(
                f"T-Bank Init error: code={error_code}, message={message}, details={details}.{hint}"
            )

        payment_url = (
            str(response_payload.get("PaymentURL") or response_payload.get("paymentURL") or "").strip()
        )
        payment_id = str(response_payload.get("PaymentId") or response_payload.get("paymentId") or "").strip()

        if not payment_url:
            raise TBankError("В ответе Init отсутствует PaymentURL.")

        return TBankInitResult(order_id=order_id, payment_id=payment_id, payment_url=payment_url)
