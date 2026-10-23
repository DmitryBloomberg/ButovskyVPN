"""Database helpers for email accounts used by the web storefront."""
from __future__ import annotations

import hashlib
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

from .connection import get_db


def create_web_support_tables(conn: sqlite3.Connection) -> None:
    """Create web account, session, and Telegram-link tables."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS web_accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL COLLATE NOCASE UNIQUE,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS web_account_users (
            account_id INTEGER NOT NULL REFERENCES web_accounts(id) ON DELETE CASCADE,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            is_primary INTEGER NOT NULL DEFAULT 0 CHECK (is_primary IN (0, 1)),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (account_id, user_id),
            UNIQUE (user_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS web_sessions (
            token_hash TEXT PRIMARY KEY,
            account_id INTEGER NOT NULL REFERENCES web_accounts(id) ON DELETE CASCADE,
            csrf_token TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            expires_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS web_link_tokens (
            token_hash TEXT PRIMARY KEY,
            account_id INTEGER NOT NULL REFERENCES web_accounts(id) ON DELETE CASCADE,
            expires_at TEXT NOT NULL,
            consumed_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS web_orders (
            order_id TEXT PRIMARY KEY REFERENCES payments(order_id) ON DELETE CASCADE,
            account_id INTEGER NOT NULL REFERENCES web_accounts(id) ON DELETE CASCADE,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_web_sessions_expiry ON web_sessions(expires_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_web_link_tokens_expiry "
        "ON web_link_tokens(expires_at, consumed_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_web_orders_account_created "
        "ON web_orders(account_id, created_at)"
    )


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _new_synthetic_telegram_id(conn: sqlite3.Connection) -> int:
    """Reserve a negative Telegram ID for web-only users until they link a bot."""
    for _ in range(100):
        candidate = -(10**12 + secrets.randbelow(8 * 10**12))
        if not conn.execute(
            "SELECT 1 FROM users WHERE telegram_id = ?", (candidate,)
        ).fetchone():
            return candidate
    raise RuntimeError("Could not allocate a web-only user ID")


def _new_referral_code(conn: sqlite3.Connection) -> str:
    from .db_users import _generate_referral_code

    for _ in range(100):
        candidate = _generate_referral_code()
        if not conn.execute(
            "SELECT 1 FROM users WHERE referral_code = ?", (candidate,)
        ).fetchone():
            return candidate
    raise RuntimeError("Could not allocate a referral code")


def create_web_account(email: str, password_hash: str) -> dict:
    """Create the email account and its initial shared-business user atomically."""
    normalized_email = str(email or "").strip().casefold()
    if not normalized_email or not password_hash:
        raise ValueError("Email and password are required")

    with get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        telegram_id = _new_synthetic_telegram_id(conn)
        referral_code = _new_referral_code(conn)
        user_columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(users)").fetchall()
        }
        fields = ["telegram_id", "referral_code"]
        values: list[object] = [telegram_id, referral_code]
        if "username" in user_columns:
            fields.append("username")
            values.append(None)
        if "first_name" in user_columns:
            fields.append("first_name")
            values.append("Web")
        placeholders = ", ".join("?" for _ in fields)
        cursor = conn.execute(
            f"INSERT INTO users ({', '.join(fields)}) VALUES ({placeholders})",
            values,
        )
        user_id = int(cursor.lastrowid)
        account_cursor = conn.execute(
            "INSERT INTO web_accounts (email, password_hash) VALUES (?, ?)",
            (normalized_email, password_hash),
        )
        account_id = int(account_cursor.lastrowid)
        conn.execute(
            """
            INSERT INTO web_account_users (account_id, user_id, is_primary)
            VALUES (?, ?, 1)
            """,
            (account_id, user_id),
        )
        return {
            "id": account_id,
            "email": normalized_email,
            "created_at": None,
            "user_id": user_id,
            "telegram_id": telegram_id,
            "telegram_linked": False,
        }


def get_web_account_by_email(email: str) -> dict | None:
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT id, email, password_hash, created_at
            FROM web_accounts WHERE email = ? COLLATE NOCASE
            """,
            (str(email or "").strip().casefold(),),
        ).fetchone()
        return dict(row) if row else None


def get_web_account(account_id: int) -> dict | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT id, email, created_at FROM web_accounts WHERE id = ?",
            (int(account_id),),
        ).fetchone()
        return dict(row) if row else None


def get_web_account_users(account_id: int) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT u.id AS user_id, u.telegram_id, u.username,
                   w.is_primary, u.is_banned
            FROM web_account_users w
            JOIN users u ON u.id = w.user_id
            WHERE w.account_id = ?
            ORDER BY w.is_primary DESC, w.created_at, u.id
            """,
            (int(account_id),),
        ).fetchall()
        return [dict(row) for row in rows]


def create_web_session(
    account_id: int,
    token_hash: str,
    csrf_token: str,
    *,
    lifetime_days: int = 30,
) -> None:
    expires_at = _iso_utc(_now_utc() + timedelta(days=lifetime_days))
    with get_db() as conn:
        conn.execute(
            "DELETE FROM web_sessions WHERE expires_at <= ?",
            (_iso_utc(_now_utc()),),
        )
        conn.execute(
            """
            INSERT INTO web_sessions (token_hash, account_id, csrf_token, expires_at)
            VALUES (?, ?, ?, ?)
            """,
            (token_hash, int(account_id), csrf_token, expires_at),
        )


def get_web_session(token_hash: str) -> dict | None:
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT s.account_id, s.csrf_token, a.email
            FROM web_sessions s
            JOIN web_accounts a ON a.id = s.account_id
            WHERE s.token_hash = ? AND s.expires_at > ?
            """,
            (token_hash, _iso_utc(_now_utc())),
        ).fetchone()
        return dict(row) if row else None


def delete_web_session(token_hash: str) -> None:
    with get_db() as conn:
        conn.execute("DELETE FROM web_sessions WHERE token_hash = ?", (token_hash,))


def record_web_order(account_id: int, order_id: str) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT INTO web_orders (order_id, account_id) VALUES (?, ?)",
            (str(order_id), int(account_id)),
        )


def create_web_link_token(account_id: int, token_hash: str) -> None:
    expires_at = _iso_utc(_now_utc() + timedelta(minutes=15))
    with get_db() as conn:
        conn.execute(
            "DELETE FROM web_link_tokens WHERE expires_at <= ? OR consumed_at IS NOT NULL",
            (_iso_utc(_now_utc()),),
        )
        conn.execute(
            "INSERT INTO web_link_tokens (token_hash, account_id, expires_at) "
            "VALUES (?, ?, ?)",
            (token_hash, int(account_id), expires_at),
        )


def consume_web_link_token(token: str, telegram_user_id: int) -> str:
    """Consume one link token and make that Telegram-backed user primary."""
    token_hash = hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()
    now = _iso_utc(_now_utc())
    with get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT account_id FROM web_link_tokens
            WHERE token_hash = ? AND consumed_at IS NULL AND expires_at > ?
            """,
            (token_hash, now),
        ).fetchone()
        if not row:
            return "invalid_or_expired"
        account_id = int(row["account_id"])
        telegram_user = conn.execute(
            "SELECT id FROM users WHERE telegram_id = ?",
            (int(telegram_user_id),),
        ).fetchone()
        if not telegram_user:
            return "telegram_user_not_found"
        user_id = int(telegram_user["id"])
        other_account = conn.execute(
            "SELECT account_id FROM web_account_users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if other_account and int(other_account["account_id"]) != account_id:
            return "telegram_already_linked"

        conn.execute(
            "UPDATE web_link_tokens SET consumed_at = ? "
            "WHERE token_hash = ? AND consumed_at IS NULL",
            (now, token_hash),
        )
        conn.execute(
            "UPDATE web_account_users SET is_primary = 0 WHERE account_id = ?",
            (account_id,),
        )
        conn.execute(
            """
            INSERT INTO web_account_users (account_id, user_id, is_primary)
            VALUES (?, ?, 1)
            ON CONFLICT(account_id, user_id) DO UPDATE SET is_primary = 1
            """,
            (account_id, user_id),
        )
        return "linked"


def hash_token(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


__all__ = [
    "consume_web_link_token",
    "create_web_account",
    "create_web_link_token",
    "create_web_session",
    "create_web_support_tables",
    "delete_web_session",
    "get_web_account",
    "get_web_account_by_email",
    "get_web_account_users",
    "get_web_session",
    "hash_token",
    "record_web_order",
]
