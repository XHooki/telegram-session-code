import os
import sqlite3
from datetime import datetime, timezone
from typing import Any
from app.config import get_settings


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect() -> sqlite3.Connection:
    settings = get_settings()
    parent = os.path.dirname(settings.db_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS pending_auth (
                id TEXT PRIMARY KEY,
                phone TEXT NOT NULL,
                phone_code_hash TEXT NOT NULL,
                temp_session_enc TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'code_sent',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone TEXT NOT NULL UNIQUE,
                telegram_user_id INTEGER,
                username TEXT,
                first_name TEXT,
                session_enc TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                last_result TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS bot_targets (
                username TEXT PRIMARY KEY,
                expected_user_id INTEGER,
                title TEXT,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone TEXT,
                action TEXT NOT NULL,
                level TEXT NOT NULL,
                message TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row else None


def add_log(phone: str | None, action: str, level: str, message: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO logs(phone, action, level, message, created_at) VALUES (?, ?, ?, ?, ?)",
            (phone, action, level, message, utc_now()),
        )


def create_pending_auth(auth_id: str, phone: str, phone_code_hash: str, temp_session_enc: str) -> None:
    now = utc_now()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO pending_auth(id, phone, phone_code_hash, temp_session_enc, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'code_sent', ?, ?)
            """,
            (auth_id, phone, phone_code_hash, temp_session_enc, now, now),
        )


def get_pending_auth(auth_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        return row_to_dict(conn.execute("SELECT * FROM pending_auth WHERE id = ?", (auth_id,)).fetchone())


def update_pending_auth(auth_id: str, **fields: Any) -> None:
    if not fields:
        return
    fields['updated_at'] = utc_now()
    assignments = ', '.join(f'{key} = ?' for key in fields)
    values = list(fields.values()) + [auth_id]
    with connect() as conn:
        conn.execute(f"UPDATE pending_auth SET {assignments} WHERE id = ?", values)


def upsert_account(
    phone: str,
    telegram_user_id: int | None,
    username: str | None,
    first_name: str | None,
    session_enc: str,
    last_result: str,
) -> None:
    now = utc_now()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO accounts(phone, telegram_user_id, username, first_name, session_enc, status, last_result, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?)
            ON CONFLICT(phone) DO UPDATE SET
                telegram_user_id = excluded.telegram_user_id,
                username = excluded.username,
                first_name = excluded.first_name,
                session_enc = excluded.session_enc,
                status = 'active',
                last_result = excluded.last_result,
                updated_at = excluded.updated_at
            """,
            (phone, telegram_user_id, username, first_name, session_enc, last_result, now, now),
        )


def list_accounts() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id, phone, telegram_user_id, username, first_name, status, last_result, created_at, updated_at
            FROM accounts
            ORDER BY id DESC
            """
        ).fetchall()
        return [dict(r) for r in rows]


def get_account(account_id: int) -> dict[str, Any] | None:
    with connect() as conn:
        return row_to_dict(conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone())


def mark_account_offboarded(account_id: int, last_result: str) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE accounts SET status = 'offboarded', session_enc = '', last_result = ?, updated_at = ? WHERE id = ?",
            (last_result, utc_now(), account_id),
        )
