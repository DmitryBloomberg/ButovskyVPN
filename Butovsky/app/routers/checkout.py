# -*- coding: utf-8 -*-
"""Оформление заказа и оплата: Platega (app.platega.io) и Telegram Stars."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse

from .common import render, current_user
from ..database import (
    get_tariff,
    get_server,
    get_active_servers,
    create_web_order,
    get_order,
    update_order,
    mark_order_paid,
)
from ..fulfillment import fulfill_order
from ..payment import create_platega_transaction, platega_enabled
from ..payment.stars import create_stars_invoice, stars_enabled
from ..settings import platega_credentials, bot_token, site_url, get_db_path

logger = logging.getLogger("butovsky.checkout")

router = APIRouter()

# 1 RUB ~ 1 Star (конвертация для отображения цены в звёздах)
STARS_RATE = 1.0


def _get_conn_for_creds():
    import sqlite3
    conn = sqlite3.connect(get_db_path(), timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


@router.get("/checkout/{tariff_id}")
async def checkout_page(request: Request, tariff_id: int):
    user = current_user(request)
    if not user:
        return RedirectResponse(f"/login?next=/checkout/{tariff_id}", status_code=302)
    tariff = get_tariff(tariff_id)
    if not tariff:
        return render(request, "error.html", {"msg": "Тариф не найден или снят с продажи"}, status_code=404)
    servers = get_active_servers()
    with _get_conn_for_creds() as conn:
        merchant, secret = platega_credentials(conn)
    return render(request, "checkout.html", {
        "tariff": tariff,
        "servers": servers,
        "platega_on": platega_enabled(merchant, secret),
        "stars_on": bool(user.get("telegram_id")) and stars_enabled(bot_token()),
        "stars_amount": max(1, int(round(float(tariff["price_rub"]) / STARS_RATE))),
    })


@router.post("/checkout/{tariff_id}")
async def checkout_start(request: Request, tariff_id: int,
                         server_id: str = Form(""), method: str = Form("platega")):
    user = current_user(request)
    if not user:
        return RedirectResponse(f"/login?next=/checkout/{tariff_id}", status_code=302)
    tariff = get_tariff(tariff_id)
    if not tariff:
        return render(request, "error.html", {"msg": "Тариф не найден"}, status_code=404)

    sid = int(server_id) if server_id.isdigit() else None
    if sid is not None and not get_server(sid):
        sid = None

    amount_minor = int(round(float(tariff["price_rub"]) * 100))
    order = create_web_order(
        web_user_id=user["id"],
        telegram_id=user.get("telegram_id"),
        server_id=sid,
        tariff_id=tariff_id,
        amount_minor=amount_minor,
        method=method,
    )
    oid = order["order_id"]

    if method == "platega":
        with _get_conn_for_creds() as conn:
            merchant, secret = platega_credentials(conn)
        if not platega_enabled(merchant, secret):
            return render(request, "error.html", {"msg": "Оплата картой недоступна, попробуйте позже"}, status_code=400)
        try:
            tx = await create_platega_transaction(
                merchant, secret,
                amount_rub=float(tariff["price_rub"]),
                order_id=oid,
                description=f"ButovskyVPN: {tariff['name']}",
                return_url=f"{site_url()}/payment/status/{oid}",
                user_telegram_id=user.get("telegram_id"),
            )
        except Exception as exc:
            logger.error("Platega create failed for %s: %s", oid, exc)
            update_order(oid, status="failed")
            return render(request, "error.html", {"msg": "Не удалось создать счёт в Platega"}, status_code=502)
        update_order(oid, transaction_id=str(tx.get("transaction_id") or ""))
        pay_url = tx.get("payment_url")
        if pay_url:
            return RedirectResponse(pay_url, status_code=302)
        return render(request, "error.html", {"msg": "Platega не вернула ссылку на оплату"}, status_code=502)

    if method == "stars":
        tg_id = user.get("telegram_id")
        token = bot_token()
        if not tg_id or not stars_enabled(token):
            return render(request, "error.html", {
                "msg": "Для оплаты звёздами привяжите Telegram в личном кабинете"
            }, status_code=400)
        try:
            res = await create_stars_invoice(
                token,
                telegram_id=int(tg_id),
                amount_xtr=max(1, int(round(float(tariff["price_rub"]) / STARS_RATE))),
                title=f"VPN {tariff['name']}",
                description=f"Подписка ButovskyVPN, заказ {oid}",
                payload=oid,
                success_url=f"{site_url()}/payment/stars-success/{oid}",
            )
        except Exception as exc:
            logger.error("Stars invoice failed for %s: %s", oid, exc)
            return render(request, "error.html", {"msg": f"Не удалось отправить счёт: {exc}"}, status_code=502)
        link = res.get("invoice_link")
        if link:
            return RedirectResponse(link, status_code=302)
        return render(request, "stars_waiting.html", {"order_id": oid})

    return render(request, "error.html", {"msg": "Неизвестный способ оплаты"}, status_code=400)


@router.get("/payment/status/{order_id}")
async def payment_status(request: Request, order_id: str):
    """Возврат из Platega: проверяем статус транзакции и исполняем заказ."""
    order = get_order(order_id)
    if not order:
        return render(request, "error.html", {"msg": "Заказ не найден"}, status_code=404)
    if order["status"] in ("pending",) and order.get("transaction_id"):
        with _get_conn_for_creds() as conn:
            merchant, secret = platega_credentials(conn)
        try:
            from ..payment import check_platega_status
            status = await check_platega_status(merchant, secret, order["transaction_id"])
        except Exception as exc:
            logger.warning("status check error %s: %s", order_id, exc)
            status = "pending"
        if status == "succeeded":
            if mark_order_paid(order_id, order["transaction_id"]):
                await fulfill_order(order_id)
                order = get_order(order_id)
        elif status == "cancelled":
            update_order(order_id, status="failed")
            order = get_order(order_id)
    return render(request, "status.html", {"order": order})


@router.get("/payment/stars-success/{order_id}")
async def stars_success(request: Request, order_id: str):
    """Telegram редиректит сюда после успешной оплаты звёздами.

    Финальное подтверждение делает webhook бота /payments/webhook
    (pre_checkout -> successful_payment -> fulfill_by_payload).
    Здесь лишь показываем ожидающий статус.
    """
    order = get_order(order_id)
    if not order:
        return render(request, "error.html", {"msg": "Заказ не найден"}, status_code=404)
    return render(request, "status.html", {"order": order, "stars_pending": True})
