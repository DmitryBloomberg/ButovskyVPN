# -*- coding: utf-8 -*-
"""Оплата Telegram Stars через Bot API (sendInvoice / star_transactions)."""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger("butovsky.stars")

TELEGRAM_API_URL = "https://api.telegram.org"


async def _call(bot_token: str, method: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(f"{TELEGRAM_API_URL}/bot{bot_token}/{method}", json=payload)
        data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API {method}: {data.get('description')}")
    return data["result"]


async def create_stars_invoice(
    bot_token: str,
    *,
    telegram_id: int,
    amount_xtr: int,          # 1 XTR = 1 Telegram Star
    title: str,
    description: str,
    payload: str,             # order_id
    success_url: str,
) -> Dict[str, Any]:
    """Шлёт пользователю invoice со звёздами в личку бота. Возвращает invoice_link (если есть)."""
    currency_total_amount = {
        "XTR": {
            "type": "stars",
            "amount": int(amount_xtr),
        }
    }
    invoice = {
        "title": title[:40],
        "description": description[:80],
        "payload": payload,
        "currency": "XTR",
        "prices": [{"label": title[:40], "amount": int(amount_xtr)}],
        "provider_token": "",
    }
    params: Dict[str, Any] = {
        "chat_id": telegram_id,
        "invoice": invoice,
    }
    if success_url:
        params["success_url"] = success_url
    result = await _call(bot_token, "sendInvoice", params)
    link = None
    try:
        url_result = await _call(bot_token, "createInvoiceLink", {
            "title": invoice["title"],
            "description": invoice["description"],
            "payload": payload,
            "currency": "XTR",
            "prices": invoice["prices"],
        })
        link = url_result if isinstance(url_result, str) else url_result.get("url")
    except Exception as exc:  # не критично — invoice уже отправлен в чат
        logger.warning("createInvoiceLink failed: %s", exc)
    return {"sent": True, "invoice_link": link}


async def get_star_transaction(bot_token: str, transaction_id: str) -> Optional[Dict[str, Any]]:
    result = await _call(bot_token, "getStarTransaction", {"transaction_id": transaction_id})
    return result if isinstance(result, dict) else None


def stars_enabled(bot_token: str) -> bool:
    return bool(bot_token)
