# -*- coding: utf-8 -*-
"""ButovskyVPN — веб-сайт продажи VPN, общий бэкенд с Telegram-ботом."""
from __future__ import annotations

import asyncio
import logging
import os
import sys

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import URLSafeTimedSerializer
from starlette.middleware.sessions import SessionMiddleware

from .database import init_db
from .settings import secret_key, web_port, site_url

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("butovsky.web")

app = FastAPI(title="ButovskyVPN Web", docs_url=None, redoc_url=None)

app.add_middleware(SessionMiddleware, secret_key=secret_key())

TEMPLATES_DIR = "app/templates"
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory=TEMPLATES_DIR)


def get_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(secret_key(), salt="butovsky-web")


@app.on_event("startup")
async def on_startup() -> None:
    init_db()
    logger.info("ButovskyVPN website started at %s (port %s)", site_url(), web_port())
    if "pytest" not in sys.modules and not os.getenv("BV_DISABLE_LOOPS"):
        asyncio.create_task(order_checker_loop())


@app.get("/")
async def index(request: Request):
    from .routers.main import render_index
    return await render_index(request)


@app.get("/healthz")
async def healthz():
    return {"ok": True}


async def order_checker_loop() -> None:
    """Фоновая проверка неоплаченных Platega-заказов (аналог payment_auto_checks в боте)."""
    from datetime import datetime, timedelta
    from .database import get_conn, get_order, mark_order_paid
    from .payment.platega import check_platega_status
    from .settings import platega_credentials
    from .fulfillment import fulfill_order

    while True:
        try:
            with get_conn() as conn:
                merchant, secret = platega_credentials(conn)
                rows = conn.execute(
                    "SELECT * FROM web_orders WHERE method='platega' AND status='pending' "
                    "AND transaction_id IS NOT NULL AND created_at > datetime('now','-2 days')"
                ).fetchall()
            if merchant and secret:
                for row in rows:
                    order = dict(row)
                    try:
                        status = await check_platega_status(merchant, secret, order["transaction_id"])
                    except Exception:
                        continue
                    if status == "succeeded":
                        if mark_order_paid(order["order_id"], order["transaction_id"]):
                            await fulfill_order(order["order_id"])
                    elif status == "cancelled":
                        from .database import update_order
                        update_order(order["order_id"], status="failed")
        except Exception as exc:
            logger.error("order checker loop error: %s", exc)
        await asyncio.sleep(30)


# регистрация роутеров
from .routers import auth, catalog, checkout, keys, sync  # noqa: E402
app.include_router(auth.router)
app.include_router(catalog.router)
app.include_router(checkout.router)
app.include_router(keys.router)
app.include_router(sync.router)
