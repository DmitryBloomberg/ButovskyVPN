# -*- coding: utf-8 -*-
"""Platega (app.platega.io) — прямые вызовы API, идентичные логике бота."""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger("butovsky.platega")

PLATEGA_API_URL = "https://app.platega.io"


async def create_platega_transaction(
    merchant_id: str,
    secret: str,
    *,
    amount_rub: float,
    order_id: str,
    description: str,
    return_url: str,
    user_telegram_id: Optional[int] = None,
) -> Dict[str, Any]:
    """POST /v2/transaction/process — создаёт транзакцию, возвращает ссылку на оплату."""
    payload = {
        "paymentDetails": {"amount": round(float(amount_rub), 2), "currency": "RUB"},
        "description": description[:255],
        "return": return_url,
        "failedUrl": return_url,
        "payload": order_id,
    }
    if user_telegram_id is not None:
        payload["metadata"] = {"userId": str(user_telegram_id)}

    headers = {
        "X-MerchantId": merchant_id,
        "X-Secret": secret,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(f"{PLATEGA_API_URL}/v2/transaction/process", json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()
    transaction_id = data.get("id") or data.get("transactionId") or data.get("uuid")
    return {
        "transaction_id": transaction_id,
        "payment_url": data.get("redirectUrl") or data.get("redirectURL") or data.get("paymentUrl"),
        "qr_url": data.get("qrData") or data.get("qrUrl"),
        "status": data.get("status"),
    }


async def check_platega_status(merchant_id: str, secret: str, transaction_id: str) -> str:
    """GET /transaction/{id} -> 'pending' | 'succeeded' | 'cancelled'."""
    headers = {"X-MerchantId": merchant_id, "X-Secret": secret, "Accept": "application/json"}
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(f"{PLATEGA_API_URL}/transaction/{transaction_id}", headers=headers)
        resp.raise_for_status()
        data = resp.json()
    status = str(data.get("status", "")).upper()
    if status == "CONFIRMED":
        return "succeeded"
    if status in ("CANCELED", "CHARGEBACKED", "REJECTED"):
        return "cancelled"
    return "pending"


def platega_enabled(merchant_id: str, secret: str) -> bool:
    return bool(merchant_id and secret)
