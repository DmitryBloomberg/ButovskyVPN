# -*- coding: utf-8 -*-
"""Внутренние эндпоинты синхронизации сайта и бота.

1) POST /internal/stars-paid   — бот подтвердил оплату Stars (successful_payment),
   заказ сайта исполняется на стороне сайта (создание клиента в 3x-ui).
2) POST /internal/bot-key-sync  — бот присылает ключ, выданный в Telegram,
   чтобы он появился в личном кабинете сайта (дублирование).
3) GET  /telegram-bind/{token}  — обработан в routers/auth.py.
"""
from __future__ import annotations

import hmac
import hashlib
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..database import get_order, mark_order_paid, register_bot_key
from ..fulfillment import fulfill_order
from ..settings import sync_secret

logger = logging.getLogger("butovsky.sync")

router = APIRouter()


def _check_sign(payload: dict) -> bool:
    sign = str(payload.get("sync_sign") or "")
    base = f'{payload.get("order_id", "")}:{payload.get("telegram_id", "")}'
    expected = hmac.new(sync_secret().encode(), base.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sign, expected)


@router.post("/internal/stars-paid")
async def stars_paid(request: Request):
    """Вызывается ботом после successful_payment по заказу сайта."""
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "bad json"}, status_code=400)
    if not _check_sign(payload):
        logger.warning("stars-paid: bad signature")
        return JSONResponse({"ok": False, "error": "bad signature"}, status_code=403)

    order_id = str(payload.get("order_id") or "")
    order = get_order(order_id)
    if not order:
        return JSONResponse({"ok": False, "error": "order not found"}, status_code=404)
    if order["status"] == "pending":
        tx = str(payload.get("telegram_payment_charge_id") or "")
        if mark_order_paid(order_id, tx):
            ok = await fulfill_order(order_id)
            return JSONResponse({"ok": True, "fulfilled": ok})
    return JSONResponse({"ok": True, "already": True})


@router.post("/internal/bot-key-sync")
async def bot_key_sync(request: Request):
    """Бот дублирует выданный ключ на сайт (нужен привязанный telegram_id)."""
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "bad json"}, status_code=400)
    ok = register_bot_key(payload)
    return JSONResponse({"ok": ok})
