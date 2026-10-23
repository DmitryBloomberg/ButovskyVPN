# -*- coding: utf-8 -*-
"""Личный кабинет: список ключей (сайт + бот), привязка Telegram."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from .common import render, current_user
from ..database import get_user_keys, get_conn

router = APIRouter()


@router.get("/account")
async def account(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    keys = get_user_keys(user)
    orders = []
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT o.*, t.name AS tariff_name
               FROM web_orders o LEFT JOIN tariffs t ON t.id = o.tariff_id
               WHERE o.web_user_id = ? ORDER BY o.created_at DESC LIMIT 30""",
            (user["id"],),
        ).fetchall()
        orders = [dict(r) for r in rows]
    return render(request, "account.html", {"keys": keys, "orders": orders})
