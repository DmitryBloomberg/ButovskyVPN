# -*- coding: utf-8 -*-
"""Исполнение заказа: оплата подтверждена -> создать клиента в 3x-ui -> сохранить ключ ->
дублировать в бота (webhook) и показать на сайте."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

import httpx

from .database import (
    get_conn,
    get_order,
    get_server,
    get_tariff,
    update_order,
    save_web_key,
    get_or_create_bot_user,
    mirror_key_to_bot_table,
)
from .panel import provision_key
from .settings import bot_token, sync_secret

logger = logging.getLogger("butovsky.fulfill")


async def fulfill_order(order_id: str) -> bool:
    order = get_order(order_id)
    if not order or order["status"] not in ("paid",):
        return False

    tariff = get_tariff(int(order["tariff_id"]))
    if not tariff:
        # тариф мог быть снят с публикации — берём напрямую из БД
        with get_conn() as conn:
            row = conn.execute("SELECT * FROM tariffs WHERE id = ?", (order["tariff_id"],)).fetchone()
        tariff = dict(row) if row else None
    if not tariff:
        update_order(order_id, status="failed")
        logger.error("Order %s: tariff %s not found", order_id, order["tariff_id"])
        return False

    server_id = order.get("server_id")
    server = get_server(int(server_id)) if server_id else None
    if not server:
        servers = [s for s in _all_servers()]
        if not servers:
            update_order(order_id, status="failed")
            logger.error("Order %s: no active servers configured", order_id)
            return False
        server = servers[0]

    telegram_id = order.get("telegram_id")
    outcome = await provision_key(server, tariff, telegram_id)
    if not outcome.ok:
        update_order(order_id, status="failed")
        logger.error("Order %s: panel provisioning failed: %s", order_id, outcome.error)
        return False

    days = int(tariff.get("duration_days") or 30)
    expires_at = datetime.now() + timedelta(days=days)

    sub_url = outcome.subscription_url
    if not sub_url:
        proto = server.get("protocol") or "https"
        sub_url = f"{proto}://{server['host']}:{server['port']}/sub/{outcome.sub_id}"

    save_web_key(
        web_order_id=int(order["id"]),
        telegram_id=telegram_id,
        panel_email=outcome.email,
        sub_id=outcome.sub_id,
        subscription_url=sub_url,
        server_name=server.get("name") or "",
        tariff_name=tariff.get("name") or "",
        expires_at=expires_at,
        source="site",
    )

    # Дублирование ключа в ядро бота: запись в users/vpn_keys — ключ сразу
    # виден в «Мои ключи» в Telegram-боте (та же таблица, что использует бот).
    vpn_limit_gb = int(tariff.get("traffic_limit_gb") or 0)
    if telegram_id:
        bot_user_id = get_or_create_bot_user(int(telegram_id))
        if bot_user_id:
            mirror_key_to_bot_table(
                bot_user_id=bot_user_id,
                server_id=int(server["id"]),
                tariff_id=int(tariff["id"]),
                panel_email=outcome.email,
                sub_id=outcome.sub_id,
                days=days,
                traffic_limit_bytes=vpn_limit_gb * (1024 ** 3),
            )

    update_order(order_id, status="fulfilled", panel_email=outcome.email, sub_id=outcome.sub_id)
    logger.info("Order %s fulfilled: email=%s", order_id, outcome.email)

    # Дублирование в бота: просим бота отправить ключ пользователю в Telegram
    await notify_bot(order_id)
    return True


def _all_servers():
    from .database import get_active_servers
    return get_active_servers()


async def notify_bot(order_id: str) -> None:
    """Зовёт вебхук бота /internal/grant-site-key если он доступен; иначе тихо пропускает.

    Также отправляет сообщение через Bot API напрямую — гарантированное дублирование
    ключа в меню бота даже без кастомизации бота.
    """
    order = get_order(order_id)
    tg_id = order.get("telegram_id") if order else None
    token = bot_token()
    if not tg_id or not token:
        return
    try:
        from .database import get_conn
        with get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM web_keys WHERE web_order_id = ? ORDER BY id DESC LIMIT 1",
                (order["id"],),
            ).fetchone()
        if not row:
            return
        key = dict(row)
        text = (
            "🌐 <b>Покупка на сайте ButovskyVPN</b>\n\n"
            f"Тариф: <b>{key.get('tariff_name') or '-'}</b>\n"
            f"Сервер: <b>{key.get('server_name') or '-'}</b>\n"
            f"Действует до: <b>{key.get('expires_at') or '-'}</b>\n\n"
            f"🔑 Ссылка подписки:\n{key.get('subscription_url')}\n\n"
            "Ключ также доступен в личном кабинете на сайте."
        )
        async with httpx.AsyncClient(timeout=15) as client:
            await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={
                    "chat_id": tg_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
            )
    except Exception as exc:
        logger.warning("notify_bot failed for %s: %s", order_id, exc)
