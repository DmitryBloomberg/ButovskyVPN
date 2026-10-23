# -*- coding: utf-8 -*-
"""Загрузка конфигурации сайта и параметров из таблицы settings базы бота."""
from __future__ import annotations

import os
import secrets
from functools import lru_cache
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


class _DefaultConfig:
    BOT_DB_PATH = ""
    SECRET_KEY = secrets.token_hex(16)
    BOT_TOKEN = ""
    BOT_USERNAME = "ButovskyVPN_bot"
    SITE_URL = "http://localhost:8090"
    WEB_PORT = 8090
    PLATEGA_MERCHANT_ID = ""
    PLATEGA_SECRET = ""
    PANEL_URL = ""
    PANEL_LOGIN = ""
    PANEL_PASSWORD = ""
    BOT_SYNC_SECRET = "CHANGE_ME_SYNC_SECRET"


try:
    import config as _user_config  # type: ignore
except Exception:
    _user_config = _DefaultConfig()


def _get(name: str, default=None):
    return getattr(_user_config, name, getattr(_DefaultConfig, name, default))


CONFIG = _user_config


def get_db_path() -> str:
    """Путь к базе данных бота. Ищем в стандартных местах."""
    explicit = _get("BOT_DB_PATH", "")
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    # типичные расположения относительно репозитория бота
    candidates += [
        BASE_DIR / "database" / "vpn_bot.db",
        BASE_DIR.parent / "bot" / "database" / "vpn_bot.db",
        BASE_DIR.parent / "ButovskyVPN" / "database" / "vpn_bot.db",
        Path("/opt/butovsky-bot/database/vpn_bot.db"),
        Path("/root/ButovskyVPN/database/vpn_bot.db"),
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    # если ничего не найдено — создаём по первому существующему parent или в ./database
    fallback = BASE_DIR / "database" / "vpn_bot.db"
    fallback.parent.mkdir(parents=True, exist_ok=True)
    return str(fallback)


def get_setting(conn, key: str, default: str = "") -> str:
    try:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return (row["value"] or default) if row else default
    except Exception:
        return default


def platega_credentials(conn):
    merchant = _get("PLATEGA_MERCHANT_ID", "") or get_setting(conn, "platega_merchant_id")
    secret = _get("PLATEGA_SECRET", "") or get_setting(conn, "platega_secret")
    return str(merchant), str(secret)


def bot_token() -> str:
    return _get("BOT_TOKEN", "") or os.getenv("BOT_TOKEN", "")


def site_url() -> str:
    return (_get("SITE_URL", "http://localhost:8090") or "").rstrip("/")


def web_port() -> int:
    return int(_get("WEB_PORT", 8090))


def secret_key() -> str:
    return _get("SECRET_KEY", "insecure-dev-key")


def bot_username() -> str:
    return _get("BOT_USERNAME", "ButovskyVPN_bot")


def sync_secret() -> str:
    return _get("BOT_SYNC_SECRET", "CHANGE_ME_SYNC_SECRET")
