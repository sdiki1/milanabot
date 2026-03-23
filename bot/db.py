from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from threading import Lock


class Database:
    def __init__(self, path: str) -> None:
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._lock = Lock()

    def init(self) -> None:
        with self._lock:
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    is_paid INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    paid_at TEXT
                )
                """
            )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS reminder_log (
                    user_id INTEGER NOT NULL,
                    reminder_id TEXT NOT NULL,
                    sent_at TEXT NOT NULL,
                    PRIMARY KEY (user_id, reminder_id)
                )
                """
            )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS tbank_orders (
                    order_id TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    amount INTEGER NOT NULL,
                    payment_id TEXT,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            self._connection.commit()

    def upsert_user(self, user_id: int, username: str | None, first_name: str | None) -> None:
        created_at_utc = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO users (user_id, username, first_name, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username = excluded.username,
                    first_name = excluded.first_name
                """,
                (user_id, username, first_name, created_at_utc),
            )
            self._connection.commit()

    def set_paid(self, user_id: int, value: bool) -> None:
        paid_at = datetime.now(timezone.utc).isoformat() if value else None
        created_at_utc = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO users (user_id, created_at, is_paid, paid_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    is_paid = excluded.is_paid,
                    paid_at = excluded.paid_at
                """,
                (user_id, created_at_utc, 1 if value else 0, paid_at),
            )
            self._connection.commit()

    def is_paid(self, user_id: int) -> bool:
        with self._lock:
            cursor = self._connection.execute(
                "SELECT is_paid FROM users WHERE user_id = ?",
                (user_id,),
            )
            row = cursor.fetchone()
        return bool(row and row[0])

    def get_unpaid_user_ids_for_reminder(
        self, reminder_id: str, reminder_due_iso_utc: str
    ) -> list[int]:
        with self._lock:
            cursor = self._connection.execute(
                """
                SELECT u.user_id
                FROM users u
                LEFT JOIN reminder_log l
                    ON l.user_id = u.user_id
                   AND l.reminder_id = ?
                WHERE u.is_paid = 0
                  AND u.created_at <= ?
                  AND l.user_id IS NULL
                """,
                (reminder_id, reminder_due_iso_utc),
            )
            rows = cursor.fetchall()
        return [int(row[0]) for row in rows]

    def mark_reminder_sent(self, user_id: int, reminder_id: str) -> None:
        sent_at_utc = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._connection.execute(
                """
                INSERT OR IGNORE INTO reminder_log (user_id, reminder_id, sent_at)
                VALUES (?, ?, ?)
                """,
                (user_id, reminder_id, sent_at_utc),
            )
            self._connection.commit()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def create_tbank_order(
        self,
        order_id: str,
        user_id: int,
        amount: int,
        payment_id: str,
        status: str = "NEW",
    ) -> None:
        now_utc = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO tbank_orders (
                    order_id, user_id, amount, payment_id, status, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(order_id) DO UPDATE SET
                    user_id = excluded.user_id,
                    amount = excluded.amount,
                    payment_id = excluded.payment_id,
                    status = excluded.status,
                    updated_at = excluded.updated_at
                """,
                (order_id, user_id, amount, payment_id, status, now_utc, now_utc),
            )
            self._connection.commit()

    def update_tbank_order_status(
        self,
        order_id: str,
        status: str,
        payment_id: str = "",
    ) -> None:
        now_utc = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._connection.execute(
                """
                UPDATE tbank_orders
                SET status = ?, payment_id = COALESCE(NULLIF(?, ''), payment_id), updated_at = ?
                WHERE order_id = ?
                """,
                (status, payment_id, now_utc, order_id),
            )
            self._connection.commit()

    def get_tbank_order_user_id(self, order_id: str) -> int | None:
        with self._lock:
            cursor = self._connection.execute(
                "SELECT user_id FROM tbank_orders WHERE order_id = ?",
                (order_id,),
            )
            row = cursor.fetchone()
        return int(row[0]) if row else None
