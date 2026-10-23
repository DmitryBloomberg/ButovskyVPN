# -*- coding: utf-8 -*-
"""Слой работы с базой данных бота + таблицы сайта (web_orders, web_users).

Используется то же VPN-ядро (sqlite файл), что и у бота. WAL-режим позволяет
боту и сайту работать с БД одновременно.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from .settings import get_db_path, get_setting

logger = logging.getLogger("butovsky.db")

WEB_SCHEMA = """
CREATE TABLE IF NOT EXISTS web_users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    telegram_id INTEGER UNIQUE,
    link_token TEXT UNIQUE,
    username TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS web_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id TEXT UNIQUE NOT NULL,
    web_user_id INTEGER REFERENCES web_users(id),
    telegram_id INTEGER,
    server_id INTEGER,
    tariff_id INTEGER NOT NULL,
    amount_minor INTEGER NOT NULL,
    currency TEXT DEFAULT 'RUB',
    method TEXT NOT NULL,               -- platega | stars | balance
    status TEXT NOT NULL DEFAULT 'pending',  -- pending|paid|fulfilled|failed|expired
    panel_email TEXT,
    sub_id TEXT,
    vpn_key_id INTEGER,
    transaction_id TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    paid_at TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_web_orders_status ON web_orders(status);
CREATE INDEX IF NOT EXISTS idx_web_orders_user ON web_orders(web_user_id);

-- Ключи, выданные сайтом (зеркалируются в меню бота через эту же таблицу)
CREATE TABLE IF NOT EXISTS web_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    web_order_id INTEGER NOT NULL REFERENCES web_orders(id),
    telegram_id INTEGER,
    panel_email TEXT NOT NULL,
    sub_id TEXT,
    subscription_url TEXT,
    server_name TEXT,
    tariff_name TEXT,
    expires_at DATETIME,
    source TEXT NOT NULL DEFAULT 'site',   -- site | bot
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_web_keys_tg ON web_keys(telegram_id);
CREATE INDEX IF NOT EXISTS idx_web_keys_order ON web_keys(web_order_id);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(get_db_path(), timeout=15, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=15000")
        conn.execute("PRAGMA synchronous=NORMAL")
    except sqlite3.Error:
        pass
    return conn


@contextmanager
def get_conn():
    conn = _connect()
    try:
        conn.execute("SELECT 1 FROM web_users LIMIT 0")
    except sqlite3.Error:
        conn.executescript(WEB_SCHEMA)
    # совместимость со "свежей" базой бота без vpn_keys
    try:
        conn.execute("SELECT 1 FROM vpn_keys LIMIT 0")
    except sqlite3.Error:
        try:
            conn.execute("SELECT 1 FROM users LIMIT 0")  # это база бота — создаём таблицу в её формате
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS vpn_keys (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                server_id INTEGER,
                tariff_id INTEGER,
                panel_email TEXT,
                sub_id TEXT,
                custom_name TEXT,
                expires_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                traffic_limit INTEGER DEFAULT 0,
                traffic_limit_override INTEGER,
                max_ips_override INTEGER
            );""")
        except sqlite3.Error:
            pass  # таблица будет создана миграциями ядра бота
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """Создаёт таблицы сайта (если их ещё нет) поверх базы бота."""
    with get_conn() as conn:
        conn.executescript(WEB_SCHEMA)


# ---------------------------------------------------------------- passwords
def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode(), salt=salt.encode(), n=2**14, r=8, p=1).hex()
    return f"scrypt${salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt, digest = stored.split("$", 2)
        candidate = hashlib.scrypt(password.encode(), salt=salt.encode(), n=2**14, r=8, p=1).hex()
        return hmac.compare_digest(candidate, digest)
    except Exception:
        return False


# ---------------------------------------------------------------- web users
def create_web_user(email: str, password: str) -> Optional[Dict[str, Any]]:
    token = secrets.token_urlsafe(24)
    with get_conn() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO web_users (email, password_hash, link_token) VALUES (?, ?, ?)",
                (email.lower().strip(), hash_password(password), token),
            )
        except sqlite3.IntegrityError:
            return None
        uid = cur.lastrowid
    return get_web_user(uid)


def get_web_user(user_id: int) -> Optional[Dict[str, Any]]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM web_users WHERE id = ?", (user_id,)).fetchone()
    return dict(row) if row else None


def get_web_user_by_email(email: str) -> Optional[Dict[str, Any]]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM web_users WHERE email = ?", (email.lower().strip(),)).fetchone()
    return dict(row) if row else None


def get_web_user_by_link_token(token: str) -> Optional[Dict[str, Any]]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM web_users WHERE link_token = ?", (token,)).fetchone()
    return dict(row) if row else None


def bind_telegram(web_user_id: int, telegram_id: int, username: str = "") -> bool:
    """Привязка Telegram ID к аккаунту сайта. Повторная привязка переносит аккаунт."""
    with get_conn() as conn:
        existing = conn.execute("SELECT id FROM web_users WHERE telegram_id = ?", (telegram_id,)).fetchone()
        if existing and existing["id"] != web_user_id:
            conn.execute("UPDATE web_users SET telegram_id = NULL WHERE id = ?", (existing["id"],))
        conn.execute(
            "UPDATE web_users SET telegram_id = ?, username = COALESCE(?, username) WHERE id = ?",
            (telegram_id, username or None, web_user_id),
        )
    return True


# ---------------------------------------------------------------- bot core sync
def get_or_create_bot_user(telegram_id: int, username: str = "") -> Optional[int]:
    """Возвращает внутренний id пользователя бота (таблица users) по telegram_id.

    Если пользователя нет — создаёт минимальную запись, чтобы ключ, выданный
    сайтом, появился в меню бота («Мои ключи») сразу после оплаты.
    """
    if not telegram_id:
        return None
    with get_conn() as conn:
        row = conn.execute("SELECT id FROM users WHERE telegram_id = ?", (telegram_id,)).fetchone()
        if row:
            return int(row["id"])
        try:
            cur = conn.execute(
                "INSERT INTO users (telegram_id, username, created_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
                (telegram_id, username or f"user_{telegram_id}"),
            )
            return int(cur.lastrowid)
        except sqlite3.Error as exc:
            logger.warning("get_or_create_bot_user failed: %s", exc)
            return None


def mirror_key_to_bot_table(*, bot_user_id: int, server_id: Optional[int], tariff_id: int,
                            panel_email: str, sub_id: str, days: int,
                            traffic_limit_bytes: int = 0) -> Optional[int]:
    """Дублирование ключа сайта в таблицу vpn_keys — тот же формат, что у бота."""
    with get_conn() as conn:
        try:
            cur = conn.execute(
                """INSERT INTO vpn_keys
                       (user_id, server_id, tariff_id, panel_email, sub_id,
                        expires_at, created_at, traffic_limit)
                   VALUES (?, ?, ?, ?, ?,
                           CASE WHEN ? = 0 THEN NULL
                                ELSE datetime('now', '+' || ? || ' days') END,
                           CURRENT_TIMESTAMP, ?)""",
                (bot_user_id, server_id, tariff_id, panel_email, sub_id,
                 int(days), int(days), int(traffic_limit_bytes or 0)),
            )
            return int(cur.lastrowid)
        except sqlite3.Error as exc:
            logger.warning("mirror_key_to_bot_table failed: %s", exc)
            return None


# ---------------------------------------------------------------- tariffs (из ядра бота)
def _rub_rate(conn) -> float:
    base = (get_setting(conn, "base_currency", "RUB") or "RUB").upper()
    if base == "RUB":
        return 1.0
    row = conn.execute(
        "SELECT units_per_base FROM currency_rates WHERE base_currency = ? AND target_currency = 'RUB'",
        (base,),
    ).fetchone()
    return float(row["units_per_base"]) if row else 1.0


def get_active_tariffs() -> List[Dict[str, Any]]:
    """Тарифы ровно как в боте: активные, не системные, с ценой в рублях."""
    with get_conn() as conn:
        rate = _rub_rate(conn)
        rows = conn.execute(
            """
            SELECT t.id, t.name, t.duration_days, t.price_minor, t.traffic_limit_gb,
                   t.max_ips, t.group_id, g.name AS group_name
            FROM tariffs t
            LEFT JOIN tariff_groups g ON g.id = t.group_id
            WHERE t.is_active = 1 AND t.system_type IS NULL
            ORDER BY t.display_order, t.id
            """
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        minor = int(d.get("price_minor") or 0)
        d["price_rub"] = round(minor / 100 * rate, 2)
        out.append(d)
    return out


def get_tariff(tariff_id: int) -> Optional[Dict[str, Any]]:
    tariffs = {t["id"]: t for t in get_active_tariffs()}
    return tariffs.get(tariff_id)


def get_active_servers() -> List[Dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, name, host, port, protocol, web_base_path, login, password, is_active "
            "FROM servers WHERE is_active = 1 ORDER BY id"
        ).fetchall()
    return [dict(r) for r in rows]


def get_server(server_id: int) -> Optional[Dict[str, Any]]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM servers WHERE id = ?", (server_id,)).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------- orders
def new_order_id(prefix: str = "W") -> str:
    return f"{prefix}{datetime.now().strftime('%y%m%d%H%M%S')}{secrets.token_hex(3).upper()}"


def create_web_order(*, web_user_id: Optional[int], telegram_id: Optional[int], server_id: Optional[int],
                     tariff_id: int, amount_minor: int, method: str) -> Dict[str, Any]:
    oid = new_order_id()
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO web_orders (order_id, web_user_id, telegram_id, server_id, tariff_id,
                                       amount_minor, method, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')""",
            (oid, web_user_id, telegram_id, server_id, tariff_id, amount_minor, method),
        )
        row = conn.execute("SELECT * FROM web_orders WHERE order_id = ?", (oid,)).fetchone()
    return dict(row)


def get_order(order_id: str) -> Optional[Dict[str, Any]]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM web_orders WHERE order_id = ?", (order_id,)).fetchone()
    return dict(row) if row else None


def update_order(order_id: str, **fields) -> None:
    if not fields:
        return
    keys = ", ".join(f"{k} = ?" for k in fields)
    vals = list(fields.values()) + [order_id]
    with get_conn() as conn:
        conn.execute(f"UPDATE web_orders SET {keys} WHERE order_id = ?", vals)


def mark_order_paid(order_id: str, transaction_id: str = "") -> bool:
    """Атомарный переход pending -> paid. Возвращает True если заказ был закрыт этим вызовом."""
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE web_orders SET status = 'paid', paid_at = CURRENT_TIMESTAMP, transaction_id = ? "
            "WHERE order_id = ? AND status = 'pending'",
            (transaction_id or None, order_id),
        )
        return cur.rowcount == 1


# ---------------------------------------------------------------- keys
def save_web_key(*, web_order_id: int, telegram_id: Optional[int], panel_email: str, sub_id: str,
                 subscription_url: str, server_name: str, tariff_name: str,
                 expires_at: Optional[datetime], source: str = "site") -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO web_keys (web_order_id, telegram_id, panel_email, sub_id, subscription_url,
                                     server_name, tariff_name, expires_at, source)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (web_order_id, telegram_id, panel_email, sub_id, subscription_url,
             server_name, tariff_name, expires_at.isoformat(sep=" ") if expires_at else None, source),
        )


def get_user_keys(web_user: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Ключи пользователя: и купленные на сайте, и купленные в боте (по telegram_id)."""
    keys: List[Dict[str, Any]] = []
    tg_id = web_user.get("telegram_id")
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM web_keys WHERE web_order_id IN "
            "(SELECT id FROM web_orders WHERE web_user_id = ?)",
            (web_user["id"],),
        ).fetchall()
        keys += [dict(r) for r in rows]
        if tg_id:
            # ключи из бота: vpn_keys joined users по telegram_id
            try:
                bot_rows = conn.execute(
                """
                SELECT k.id, k.panel_email, k.sub_id, k.expires_at, k.created_at,
                       s.name AS server_name, t.name AS tariff_name,
                       p.protocol, p.host, p.port, p.web_base_path
                FROM vpn_keys k
                JOIN users u ON u.id = k.user_id
                LEFT JOIN servers s ON s.id = k.server_id
                LEFT JOIN tariffs t ON t.id = k.tariff_id
                WHERE u.telegram_id = ?
                  AND (k.expires_at IS NULL OR k.expires_at > CURRENT_TIMESTAMP)
                ORDER BY k.created_at DESC
                    """,
                    (tg_id,),
                ).fetchall()
            except sqlite3.Error:
                bot_rows = []
            for r in bot_rows:
                d = dict(r)
                proto = d.pop("protocol", "https") or "https"
                host = d.pop("host", "")
                port = d.pop("port", "")
                path = d.pop("web_base_path", "") or ""
                sub = d.get("sub_id") or ""
                url = f"{proto}://{host}:{port}/{path.strip('/')}/{sub}" if host and sub else ""
                keys.append({
                    "id": None,
                    "panel_email": d.get("panel_email"),
                    "sub_id": sub,
                    "subscription_url": url,
                    "server_name": d.get("server_name"),
                    "tariff_name": d.get("tariff_name"),
                    "expires_at": d.get("expires_at"),
                    "created_at": d.get("created_at"),
                    "source": "bot",
                })
    keys.sort(key=lambda k: k.get("created_at") or "", reverse=True)
    return keys


def count_site_keys_for_telegram(telegram_id: int) -> int:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) c FROM web_keys WHERE telegram_id = ? AND source = 'site'",
            (telegram_id,),
        ).fetchone()
    return int(row["c"]) if row else 0


def register_bot_key(payload: Dict[str, Any]) -> bool:
    """Веб-хук от бота: дублирование ключа, выданного ботом, на сайт.

    payload: {telegram_id, panel_email, sub_id, subscription_url, server_name,
              tariff_name, expires_at, sync_sign}
    """
    from .settings import sync_secret

    sign = payload.get("sync_sign", "")
    expected = hmac.new(sync_secret().encode(),
                        f'{payload.get("telegram_id")}:{payload.get("panel_email")}'.encode(),
                        hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sign, expected):
        logger.warning("Bot key sync: bad signature for %s", payload.get("panel_email"))
        return False

    tg_id = int(payload["telegram_id"])
    with get_conn() as conn:
        user = conn.execute("SELECT id FROM web_users WHERE telegram_id = ?", (tg_id,)).fetchone()
        if not user:
            return False
        exists = conn.execute(
            "SELECT id FROM web_keys WHERE panel_email = ? AND source = 'bot'",
            (payload.get("panel_email"),),
        ).fetchone()
        if exists:
            return True
        conn.execute(
            """INSERT INTO web_keys (web_order_id, telegram_id, panel_email, sub_id, subscription_url,
                                     server_name, tariff_name, expires_at, source)
               VALUES (0, ?, ?, ?, ?, ?, ?, ?, 'bot')""",
            (tg_id, payload.get("panel_email"), payload.get("sub_id"), payload.get("subscription_url"),
             payload.get("server_name"), payload.get("tariff_name"), payload.get("expires_at")),
        )
    return True


def make_sync_signature(telegram_id: int, panel_email: str) -> str:
    from .settings import sync_secret
    return hmac.new(sync_secret().encode(), f"{telegram_id}:{panel_email}".encode(),
                    hashlib.sha256).hexdigest()
