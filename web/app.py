"""Email-authenticated storefront sharing the bot's SQLite business data."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from urllib.parse import urlparse

from aiohttp import web

logger = logging.getLogger("butovsky.web")
ROOT = Path(__file__).resolve().parent.parent
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s.]+(?:\.[^@\s.]+)+$")
SESSION_COOKIE = "butovsky_session"
SESSION_MAX_AGE = 30 * 24 * 60 * 60
PASSWORD_ITERATIONS = 310_000
PAYMENT_CHECK_INTERVAL = 30
AUTH_FAILURE_WINDOW = 15 * 60
AUTH_FAILURE_LIMIT = 10
_AUTH_FAILURES: dict[str, list[float]] = {}


def _config(name: str, default=None):
    try:
        import config
    except ModuleNotFoundError:
        config = None
    return getattr(config, name, default) if config is not None else default


def _bot_token() -> str:
    return str(os.environ.get("BOT_TOKEN") or _config("BOT_TOKEN", "") or "").strip()


def _valid_bot_token(token: str) -> bool:
    return bool(token and "ВАШ_ТОКЕН" not in token and "YOUR_" not in token.upper())


def _password_hash(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS
    )
    return f"pbkdf2_sha256${PASSWORD_ITERATIONS}${salt.hex()}${digest.hex()}"


def _verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations_raw, salt_hex, digest_hex = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        iterations = int(iterations_raw)
        if iterations < 100_000 or iterations > 2_000_000:
            return False
        expected = bytes.fromhex(digest_hex)
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), iterations
        )
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def _email(value) -> str:
    normalized = str(value or "").strip().casefold()
    if len(normalized) > 254 or not EMAIL_RE.fullmatch(normalized):
        raise web.HTTPBadRequest(text="Укажите корректный адрес электронной почты.")
    return normalized


async def _json_body(request: web.Request) -> dict:
    try:
        value = await request.json()
    except Exception:
        raise web.HTTPBadRequest(text="Не удалось прочитать запрос.")
    if not isinstance(value, dict):
        raise web.HTTPBadRequest(text="Некорректный формат запроса.")
    return value


def _auth_client_key(request: web.Request) -> str:
    # Nginx overwrites X-Real-IP with the remote client address.
    return str(request.headers.get("X-Real-IP") or request.remote or "unknown")


def _check_auth_rate(request: web.Request) -> str:
    key = _auth_client_key(request)
    now = time.monotonic()
    failures = [
        stamp for stamp in _AUTH_FAILURES.get(key, [])
        if now - stamp < AUTH_FAILURE_WINDOW
    ]
    _AUTH_FAILURES[key] = failures
    if len(failures) >= AUTH_FAILURE_LIMIT:
        raise web.HTTPTooManyRequests(
            text="Слишком много попыток. Повторите через несколько минут."
        )
    return key


def _record_auth_failure(key: str) -> None:
    _AUTH_FAILURES.setdefault(key, []).append(time.monotonic())


def _check_origin(request: web.Request) -> None:
    origin = request.headers.get("Origin")
    if not origin:
        return
    parsed = urlparse(origin)
    expected_host = request.headers.get("Host", "").casefold()
    expected_scheme = str(
        request.headers.get("X-Forwarded-Proto") or request.scheme
    ).casefold()
    if (
        parsed.netloc.casefold() != expected_host
        or parsed.scheme.casefold() != expected_scheme
    ):
        raise web.HTTPForbidden(text="Запрос отклонён.")


def _session_for(request: web.Request) -> dict | None:
    from database.db_web import get_web_session, hash_token

    raw_token = request.cookies.get(SESSION_COOKIE, "")
    if not raw_token or len(raw_token) > 256:
        return None
    return get_web_session(hash_token(raw_token))


def _require_session(request: web.Request) -> dict:
    session = _session_for(request)
    if not session:
        raise web.HTTPUnauthorized(text="Войдите в аккаунт.")
    return session


def _require_csrf(request: web.Request, session: dict) -> None:
    supplied = request.headers.get("X-CSRF-Token", "")
    if not supplied or not hmac.compare_digest(
        supplied, str(session.get("csrf_token") or "")
    ):
        raise web.HTTPForbidden(text="Обновите страницу и повторите действие.")


def _account_summary(account_id: int) -> dict:
    from database.db_web import get_web_account, get_web_account_users

    account = get_web_account(account_id)
    if not account:
        raise web.HTTPUnauthorized(text="Аккаунт не найден.")
    users = get_web_account_users(account_id)
    primary = next((user for user in users if user["is_primary"]), users[0] if users else None)
    return {
        "email": account["email"],
        "telegram_linked": any(int(user.get("telegram_id") or 0) > 0 for user in users),
        "telegram_primary": bool(primary and int(primary.get("telegram_id") or 0) > 0),
        "created_at": account.get("created_at"),
    }


def _create_session(account_id: int) -> tuple[str, str]:
    from database.db_web import create_web_session, hash_token

    raw_token = secrets.token_urlsafe(36)
    csrf_token = secrets.token_urlsafe(24)
    create_web_session(account_id, hash_token(raw_token), csrf_token)
    return raw_token, csrf_token


def _set_session_cookie(
    response: web.StreamResponse,
    request: web.Request,
    raw_token: str,
) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        raw_token,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        secure=bool(request.app["secure_cookie"]),
        samesite="Lax",
        path="/",
    )


def _clear_session_cookies(response: web.StreamResponse, request: web.Request) -> None:
    response.del_cookie(
        SESSION_COOKIE,
        path="/",
        secure=bool(request.app["secure_cookie"]),
        httponly=True,
        samesite="Lax",
    )


def _selected_web_user(account_id: int) -> dict:
    from database.db_web import get_web_account_users

    users = get_web_account_users(account_id)
    if not users:
        raise web.HTTPForbidden(text="У аккаунта не найден пользователь.")
    return next((user for user in users if user["is_primary"]), users[0])


def _available_adapters(account_id: int, tariff: dict):
    from bot.services.payment_intents import (
        PURPOSE_KEY_PURCHASE,
        PaymentIntent,
        PaymentNavigation,
        PaymentTarget,
    )
    from bot.services.payment_provider_adapters import list_payment_provider_adapters
    from database.db_currency import get_base_currency

    user = _selected_web_user(account_id)
    user_id = int(user["user_id"])
    telegram_id = int(user.get("telegram_id") or 0)
    if bool(user.get("is_banned")):
        raise web.HTTPForbidden(text="Этот пользователь заблокирован.")
    base_currency = get_base_currency()
    price = int(tariff.get("price_minor") or 0)
    intent = PaymentIntent(
        order_id="storefront-preview",
        user_id=user_id,
        purpose=PURPOSE_KEY_PURCHASE,
        purpose_data=MappingProxyType({"tariff_id": int(tariff["id"])}),
        base_currency=base_currency,
        nominal_amount_minor=price,
        payable_amount_minor=price,
        balance_deduct_minor=0,
        description=str(tariff.get("name") or "VPN"),
        navigation=PaymentNavigation(
            PaymentTarget("page", "main"),
            PaymentTarget("page", "main"),
        ),
        status="pending",
        fulfillment_status="pending",
        tariff_id=int(tariff["id"]),
    )
    adapters = list_payment_provider_adapters(intent, telegram_id=telegram_id)
    adapters = [
        adapter for adapter in adapters
        if adapter.presentation != "telegram_invoice"
        or (adapter.provider_id in {"stars", "cards"} and telegram_id > 0)
    ]
    adapters = [
        adapter for adapter in adapters
        if adapter.presentation != "placeholder"
    ]
    return user, adapters


def _minor_to_major(amount_minor: int) -> float:
    return float(Decimal(int(amount_minor)) / Decimal(100))


def _safe_http_url(value, *, https_only: bool = False) -> str | None:
    raw = str(value or "").strip()
    parsed = urlparse(raw)
    allowed = {"https"} if https_only else {"http", "https"}
    if parsed.scheme.casefold() not in allowed or not parsed.netloc:
        return None
    return raw


@web.middleware
async def _error_middleware(request: web.Request, handler):
    try:
        response = await handler(request)
    except web.HTTPException as error:
        if request.path.startswith("/api/"):
            return web.json_response(
                {"error": error.text or "Запрос не выполнен."},
                status=error.status,
            )
        raise
    except Exception as error:
        logger.exception("Unhandled storefront request %s (%s)", request.path, type(error).__name__)
        if request.path.startswith("/api/"):
            return web.json_response(
                {"error": "Внутренняя ошибка сервера. Попробуйте позже."},
                status=500,
            )
        raise
    return response


async def index(request: web.Request) -> web.StreamResponse:
    return web.FileResponse(ROOT / "web" / "static" / "index.html")


async def catalog(request: web.Request) -> web.Response:
    from database.db_tariffs import get_all_tariffs
    from database.db_currency import get_base_currency

    tariffs = get_all_tariffs(include_hidden=False, include_system=False)
    return web.json_response({
        "currency": get_base_currency(),
        "tariffs": [
            {
                "id": int(item["id"]),
                "name": str(item.get("name") or "VPN"),
                "duration_days": int(item.get("duration_days") or 0),
                "price_minor": int(item.get("price_minor") or 0),
                "price": _minor_to_major(int(item.get("price_minor") or 0)),
                "currency": str(item.get("base_currency") or get_base_currency()),
                "traffic_limit_gb": int(item.get("traffic_limit_gb") or 0),
                "max_ips": int(item.get("max_ips") or 1),
                "group_id": int(item.get("group_id") or 0),
            }
            for item in tariffs
        ],
    })


async def session_info(request: web.Request) -> web.Response:
    session = _session_for(request)
    if not session:
        return web.json_response({"authenticated": False})
    return web.json_response({
        "authenticated": True,
        "csrf": session["csrf_token"],
        "account": _account_summary(int(session["account_id"])),
    })


async def register(request: web.Request) -> web.Response:
    _check_origin(request)
    auth_key = _check_auth_rate(request)
    data = await _json_body(request)
    password = str(data.get("password") or "")
    try:
        email = _email(data.get("email"))
    except web.HTTPException:
        _record_auth_failure(auth_key)
        raise
    if len(password) < 10 or len(password) > 128:
        _record_auth_failure(auth_key)
        raise web.HTTPBadRequest(
            text="Пароль должен содержать от 10 до 128 символов."
        )
    from database.db_web import create_web_account

    try:
        account = create_web_account(email, _password_hash(password))
    except Exception as error:
        import sqlite3

        if isinstance(error, sqlite3.IntegrityError):
            _record_auth_failure(auth_key)
            raise web.HTTPConflict(text="Аккаунт с такой почтой уже существует.")
        raise
    _AUTH_FAILURES.pop(auth_key, None)
    raw_token, csrf = _create_session(int(account["id"]))
    response = web.json_response({
        "authenticated": True,
        "csrf": csrf,
        "account": _account_summary(int(account["id"])),
    }, status=201)
    _set_session_cookie(response, request, raw_token)
    return response


async def login(request: web.Request) -> web.Response:
    _check_origin(request)
    auth_key = _check_auth_rate(request)
    data = await _json_body(request)
    password = str(data.get("password") or "")
    try:
        email = _email(data.get("email"))
    except web.HTTPException:
        _record_auth_failure(auth_key)
        raise
    if len(password) > 128:
        _record_auth_failure(auth_key)
        raise web.HTTPUnauthorized(text="Неверная почта или пароль.")
    from database.db_web import get_web_account_by_email

    account = get_web_account_by_email(email)
    if not account or not _verify_password(password, str(account["password_hash"])):
        _record_auth_failure(auth_key)
        raise web.HTTPUnauthorized(text="Неверная почта или пароль.")
    _AUTH_FAILURES.pop(auth_key, None)
    raw_token, csrf = _create_session(int(account["id"]))
    response = web.json_response({
        "authenticated": True,
        "csrf": csrf,
        "account": _account_summary(int(account["id"])),
    })
    _set_session_cookie(response, request, raw_token)
    return response


async def logout(request: web.Request) -> web.Response:
    _check_origin(request)
    session = _require_session(request)
    _require_csrf(request, session)
    from database.db_web import delete_web_session, hash_token

    raw_token = request.cookies.get(SESSION_COOKIE, "")
    if raw_token:
        delete_web_session(hash_token(raw_token))
    response = web.json_response({"ok": True})
    _clear_session_cookies(response, request)
    return response


async def payment_methods(request: web.Request) -> web.Response:
    session = _require_session(request)
    tariff_id = request.query.get("tariff_id", "")
    try:
        tariff_id = int(tariff_id)
    except (TypeError, ValueError):
        raise web.HTTPBadRequest(text="Не выбран тариф.")
    from database.db_tariffs import get_tariff_by_id

    tariff = get_tariff_by_id(tariff_id)
    if not tariff or not tariff.get("is_active") or tariff.get("system_type") is not None:
        raise web.HTTPNotFound(text="Тариф недоступен.")
    user, adapters = _available_adapters(int(session["account_id"]), tariff)
    return web.json_response({
        "telegram_linked": int(user.get("telegram_id") or 0) > 0,
        "methods": [
            {
                "id": adapter.provider_id,
                "title": adapter.title,
                "label": adapter.label,
                "presentation": adapter.presentation,
            }
            for adapter in adapters
        ],
    })


async def create_order(request: web.Request) -> web.Response:
    _check_origin(request)
    session = _require_session(request)
    _require_csrf(request, session)
    data = await _json_body(request)
    try:
        tariff_id = int(data.get("tariff_id"))
    except (TypeError, ValueError):
        raise web.HTTPBadRequest(text="Не выбран тариф.")
    provider_id = str(data.get("provider_id") or "").strip().casefold()
    if len(provider_id) > 80:
        raise web.HTTPBadRequest(text="Неизвестный способ оплаты.")

    from database.db_tariffs import get_tariff_by_id
    from bot.services.payment_intents import (
        PURPOSE_KEY_PURCHASE,
        cancel_payment_intent,
        create_payment_intent,
        load_payment_intent,
        quote_payment_intent,
    )
    from bot.services.payment_provider_adapters import (
        create_provider_invoice,
        provider_minimum_satisfied,
    )

    tariff = get_tariff_by_id(tariff_id)
    if not tariff or not tariff.get("is_active") or tariff.get("system_type") is not None:
        raise web.HTTPNotFound(text="Тариф недоступен.")
    user, adapters = _available_adapters(int(session["account_id"]), tariff)
    adapter_map = {adapter.provider_id: adapter for adapter in adapters}
    adapter = adapter_map.get(provider_id)
    if adapter is None:
        raise web.HTTPBadRequest(text="Этот способ оплаты сейчас недоступен.")
    telegram_id = int(user.get("telegram_id") or 0)
    if adapter.presentation == "telegram_invoice" and telegram_id <= 0:
        raise web.HTTPConflict(
            text="Для оплаты Telegram Stars сначала привяжите Telegram-аккаунт."
        )

    intent = create_payment_intent(
        user_id=int(user["user_id"]),
        purpose=PURPOSE_KEY_PURCHASE,
        purpose_data={"tariff_id": tariff_id},
    )
    try:
        from database.db_web import record_web_order

        record_web_order(int(session["account_id"]), intent.order_id)
        quote = quote_payment_intent(intent.order_id, adapter.payment_type)
        if quote.unavailable_reason or not quote.raw.get("ok", True):
            raise web.HTTPBadRequest(text="Для этого тарифа оплата недоступна.")
        if quote.is_free:
            raise web.HTTPBadRequest(
                text="Этот тариф оформляется через бесплатный сценарий в боте."
            )
        if not provider_minimum_satisfied(adapter, quote):
            raise web.HTTPBadRequest(
                text="Сумма тарифа ниже минимальной для выбранного способа оплаты."
            )

        bot_username = str(
            request.app.get("bot_username") or _config("BOT_USERNAME", "") or "VPN"
        )
        invoice = await create_provider_invoice(
            load_payment_intent(intent.order_id) or intent,
            quote,
            telegram_id=telegram_id,
            bot_username=bot_username,
        )
        if invoice.presentation == "telegram_invoice":
            bot = request.app.get("bot")
            if bot is None:
                raise web.HTTPServiceUnavailable(
                    text="Telegram Bot API не настроен на сервере."
                )
            from aiogram.types import LabeledPrice
            from bot.utils.payment_invoice import clamp_invoice_text

            amount = int(quote.raw.get("final_amount") or 0)
            if amount <= 0:
                raise web.HTTPBadRequest(text="Сумма счёта некорректна.")
            invoice_kwargs = {
                "chat_id": telegram_id,
                "title": clamp_invoice_text(bot_username, 32),
                "description": clamp_invoice_text(intent.description, 255),
                "payload": intent.order_id,
                "provider_token": "",
                "currency": quote.charge_currency,
                "prices": [
                    LabeledPrice(
                        label=clamp_invoice_text(intent.description, 80),
                        amount=amount,
                    )
                ],
            }
            if adapter.provider_id == "cards":
                from database.requests import get_setting

                card_token = str(get_setting("cards_provider_token", "") or "")
                if not card_token:
                    raise web.HTTPServiceUnavailable(
                        text="Платёжный токен Telegram Cards не настроен."
                    )
                invoice_kwargs["provider_token"] = card_token
                invoice_kwargs["provider_data"] = json.dumps({
                    "receipt": {
                        "customer": {"email": str(session.get("email") or "")},
                        "items": [{
                            "description": clamp_invoice_text(intent.description, 128),
                            "quantity": "1.00",
                            "amount": {
                                "value": f"{quote.charge_amount:.2f}",
                                "currency": "RUB",
                            },
                            "vat_code": 1,
                            "payment_mode": "full_prepayment",
                            "payment_subject": "service",
                        }],
                    }
                }, ensure_ascii=False)
            await bot.send_invoice(
                **invoice_kwargs,
            )
            return web.json_response({
                "order_id": intent.order_id,
                "provider": adapter.provider_id,
                "telegram_invoice_sent": True,
                "payment_url": None,
                "amount": str(quote.charge_amount),
                "currency": quote.charge_currency,
            }, status=201)

        payment_url = _safe_http_url(invoice.payment_url, https_only=True)
        if not payment_url:
            raise web.HTTPBadGateway(
                text="Платёжный провайдер не вернул безопасную ссылку."
            )
        return web.json_response({
            "order_id": intent.order_id,
            "provider": adapter.provider_id,
            "telegram_invoice_sent": False,
            "payment_url": payment_url,
            "amount": str(quote.charge_amount),
            "currency": quote.charge_currency,
        }, status=201)
    except web.HTTPException:
        cancel_payment_intent(intent.order_id, user_id=int(user["user_id"]))
        raise
    except Exception:
        cancel_payment_intent(intent.order_id, user_id=int(user["user_id"]))
        raise


async def create_telegram_link(request: web.Request) -> web.Response:
    _check_origin(request)
    session = _require_session(request)
    _require_csrf(request, session)
    bot_username = str(
        request.app.get("bot_username")
        or _config("BOT_USERNAME", "")
        or ""
    ).strip().lstrip("@")
    if not bot_username:
        raise web.HTTPServiceUnavailable(
            text="Не удалось определить имя бота. Проверьте BOT_USERNAME в config.py."
        )
    from database.db_web import create_web_link_token, hash_token

    token = secrets.token_urlsafe(18)
    create_web_link_token(int(session["account_id"]), hash_token(token))
    return web.json_response({
        "url": f"https://t.me/{bot_username}?start=web_{token}",
        "expires_in_seconds": 900,
    })


async def keys(request: web.Request) -> web.Response:
    session = _require_session(request)
    from database.db_web import get_web_account_users
    from database.db_keys import get_user_vpn_keys, get_vpn_key_by_id
    from bot.services.vpn_api import get_subscription_url_for_key

    result = []
    for user in get_web_account_users(int(session["account_id"])):
        for key in get_user_vpn_keys(int(user["user_id"])):
            key_id = int(key["id"])
            subscription_url = None
            try:
                complete_key = get_vpn_key_by_id(key_id)
                if complete_key:
                    subscription_url = _safe_http_url(
                        await get_subscription_url_for_key(complete_key)
                    )
            except Exception as error:
                logger.warning(
                    "Could not resolve subscription URL for web key %s (%s)",
                    key_id,
                    type(error).__name__,
                )
            result.append({
                "id": key_id,
                "name": str(key.get("custom_name") or key.get("tariff_name") or f"Ключ #{key_id}"),
                "tariff": str(key.get("tariff_name") or "VPN"),
                "server": str(key.get("server_name") or ""),
                "expires_at": key.get("expires_at"),
                "subscription_url": subscription_url,
            })
    result.sort(key=lambda item: str(item.get("expires_at") or ""), reverse=True)
    return web.json_response({"keys": result})


async def orders(request: web.Request) -> web.Response:
    session = _require_session(request)
    from database.connection import get_db

    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT p.order_id, p.status, p.fulfillment_status, p.payment_type,
                   p.nominal_amount_minor, p.base_currency, p.created_at,
                   t.name AS tariff_name
            FROM payments p
            JOIN web_account_users w ON w.user_id = p.user_id
            LEFT JOIN tariffs t ON t.id = p.tariff_id
            WHERE w.account_id = ? AND p.intent_version = 1
            GROUP BY p.id
            ORDER BY p.id DESC
            LIMIT 40
            """,
            (int(session["account_id"]),),
        ).fetchall()
    return web.json_response({
        "orders": [
            {
                "order_id": str(row["order_id"]),
                "status": str(row["status"] or "pending"),
                "fulfillment_status": str(row["fulfillment_status"] or "pending"),
                "payment_type": str(row["payment_type"] or ""),
                "amount": _minor_to_major(int(row["nominal_amount_minor"] or 0)),
                "currency": str(row["base_currency"] or "RUB"),
                "tariff": str(row["tariff_name"] or "VPN"),
                "created_at": row["created_at"],
            }
            for row in rows
        ]
    })


async def check_order(request: web.Request) -> web.Response:
    _check_origin(request)
    session = _require_session(request)
    _require_csrf(request, session)
    data = await _json_body(request)
    order_id = str(data.get("order_id") or "").strip()
    if not order_id or len(order_id) > 100:
        raise web.HTTPBadRequest(text="Заказ не найден.")

    from database.connection import get_db
    from database.db_web import get_web_account_users
    from database.requests import find_order_by_order_id, get_payment_provider_order
    from bot.services.payment_intents import load_payment_intent
    from bot.services.payment_provider_adapters import check_provider_invoice

    allowed_ids = {
        int(user["user_id"])
        for user in get_web_account_users(int(session["account_id"]))
    }
    order = find_order_by_order_id(order_id)
    if not order or int(order.get("user_id") or 0) not in allowed_ids:
        raise web.HTTPNotFound(text="Заказ не найден.")

    if str(order.get("status") or "") != "paid":
        intent = load_payment_intent(order_id)
        provider_order = get_payment_provider_order(order_id)
        if (
            intent
            and provider_order
            and str(provider_order.get("provider_id") or "") not in {"stars", "cards"}
        ):
            status = await check_provider_invoice(intent)
            if status == "succeeded":
                bot = request.app.get("bot")
                if bot is None:
                    raise web.HTTPServiceUnavailable(
                        text="Бот не запущен: оплата подтверждена, выдачу повторите позже."
                    )
                from bot.services.payment_completion import complete_confirmed_payment

                await complete_confirmed_payment(
                    order_id,
                    bot=bot,
                    telegram_id=None,
                    notify_user=False,
                    show_primary_result=False,
                )

    updated_order = find_order_by_order_id(order_id) or order
    return web.json_response({
        "order_id": order_id,
        "status": str(updated_order.get("status") or "pending"),
        "fulfillment_status": str(updated_order.get("fulfillment_status") or "pending"),
    })


async def payment_worker(app: web.Application) -> None:
    """Poll web-originated link payments and finish them even after the user leaves."""
    while True:
        await asyncio.sleep(PAYMENT_CHECK_INTERVAL)
        bot = app.get("bot")
        if bot is None:
            continue
        try:
            from database.connection import get_db
            from database.requests import get_payment_provider_order
            from bot.services.payment_intents import load_payment_intent
            from bot.services.payment_provider_adapters import check_provider_invoice
            from bot.services.payment_completion import complete_confirmed_payment

            with get_db() as conn:
                rows = conn.execute(
                    """
                    SELECT DISTINCT p.order_id
                    FROM payments p
                    JOIN web_orders w ON w.order_id = p.order_id
                    WHERE p.intent_version = 1
                      AND p.status = 'pending'
                      AND p.created_at >= datetime('now', '-3 days')
                      AND w.account_id IS NOT NULL
                    ORDER BY p.id DESC
                    LIMIT 25
                    """
                ).fetchall()
            for row in rows:
                order_id = str(row["order_id"])
                provider_order = get_payment_provider_order(order_id)
                if not provider_order or str(provider_order.get("provider_id") or "") in {
                    "stars", "cards"
                }:
                    continue
                try:
                    intent = load_payment_intent(order_id)
                    if not intent:
                        continue
                    if await check_provider_invoice(intent) != "succeeded":
                        continue
                    await complete_confirmed_payment(
                        order_id,
                        bot=bot,
                        telegram_id=None,
                        notify_user=False,
                        show_primary_result=False,
                    )
                except Exception as error:
                    logger.warning(
                        "Background web payment check failed for %s (%s)",
                        order_id,
                        type(error).__name__,
                    )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning("Web payment worker iteration failed (%s)", type(error).__name__)


async def _startup(app: web.Application) -> None:
    from database.migrations import run_migrations

    run_migrations()
    from bot.utils.user_ui_texts import load_user_ui_text_cache
    from bot.utils.page_renderer import validate_required_user_pages
    from bot.utils.telegram_links import load_telegram_link_domain

    load_user_ui_text_cache()
    validate_required_user_pages()
    load_telegram_link_domain()
    try:
        from bot.utils.custom_extensions import load_custom_extensions

        extensions = load_custom_extensions()
        if getattr(extensions, "failed", None):
            logger.warning("Some payment extensions could not be loaded by the web process")
    except Exception as error:
        logger.warning("Extension registry unavailable in web process (%s)", type(error).__name__)

    app["bot"] = None
    app["bot_username"] = str(_config("BOT_USERNAME", "") or "").strip().lstrip("@")
    token = _bot_token()
    if _valid_bot_token(token):
        try:
            from aiogram import Bot
            from bot.middlewares.parse_mode_fallback import SafeParseSession

            bot = Bot(token=token, session=SafeParseSession())
            app["bot"] = bot
            try:
                bot_info = await asyncio.wait_for(bot.get_me(), timeout=8)
                if bot_info.username:
                    app["bot_username"] = bot_info.username
            except Exception as error:
                logger.warning(
                    "Telegram API is not reachable from the web process (%s)",
                    type(error).__name__,
                )
        except Exception as error:
            logger.warning("Telegram bot client could not be initialized (%s)", type(error).__name__)
    else:
        logger.warning("BOT_TOKEN is not configured; Telegram invoices and linking are unavailable")

    app["payment_worker_task"] = asyncio.create_task(payment_worker(app))


async def _cleanup(app: web.Application) -> None:
    task = app.get("payment_worker_task")
    if task:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    bot = app.get("bot")
    if bot:
        await bot.session.close()


def create_app() -> web.Application:
    secure_cookie = str(
        os.environ.get("WEB_COOKIE_SECURE")
        or _config("WEB_COOKIE_SECURE", "")
        or ""
    ).casefold()
    if secure_cookie:
        secure = secure_cookie in {"1", "true", "yes", "on"}
    else:
        base_url = str(os.environ.get("WEB_BASE_URL") or _config("WEB_BASE_URL", "") or "")
        secure = base_url.casefold().startswith("https://")
    app = web.Application(middlewares=[_error_middleware], client_max_size=64 * 1024)
    app["secure_cookie"] = secure
    app.router.add_get("/", index)
    app.router.add_static("/static/", ROOT / "web" / "static", show_index=False)
    app.router.add_get("/api/catalog", catalog)
    app.router.add_get("/api/session", session_info)
    app.router.add_post("/api/auth/register", register)
    app.router.add_post("/api/auth/login", login)
    app.router.add_post("/api/auth/logout", logout)
    app.router.add_get("/api/payment-methods", payment_methods)
    app.router.add_post("/api/orders", create_order)
    app.router.add_get("/api/keys", keys)
    app.router.add_get("/api/orders", orders)
    app.router.add_post("/api/orders/check", check_order)
    app.router.add_post("/api/telegram/link", create_telegram_link)
    app.on_startup.append(_startup)
    app.on_cleanup.append(_cleanup)
    return app


if __name__ == "__main__":
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
    )
    web.run_app(
        create_app(),
        host=os.environ.get("WEB_HOST", "127.0.0.1"),
        port=int(os.environ.get("WEB_PORT", "8080")),
    )
