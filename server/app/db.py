"""存储层：SQLite（本地/测试）实现，表结构与 Supabase 同构。

仅保留用户、订阅、用量统计与通知。
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    event_id    TEXT PRIMARY KEY,
    event_type  TEXT NOT NULL,
    payload     TEXT NOT NULL,
    received_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    email_hash    TEXT NOT NULL UNIQUE,               -- sha256+pepper 确定性哈希（查询键）
    email_enc     TEXT NOT NULL,                      -- Fernet 加密（存储原文）
    created_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS subscriptions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id    TEXT NOT NULL UNIQUE,
    plan          TEXT NOT NULL DEFAULT 'free',       -- 订阅计划
    status        TEXT NOT NULL DEFAULT 'active',
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS usage (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL,
    plan       TEXT NOT NULL DEFAULT 'free',
    day        TEXT NOT NULL,             -- YYYY-MM-DD
    calls      INTEGER NOT NULL DEFAULT 0,
    cost       REAL NOT NULL DEFAULT 0,
    UNIQUE (account_id, day)
);

CREATE TABLE IF NOT EXISTS notifications (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    channel    TEXT NOT NULL,             -- telegram | log
    message    TEXT NOT NULL,
    created_at REAL NOT NULL
);
"""

# 旧版遗留表与列，迁移时清理
_LEGACY_TABLES = ("payment_logs", "leads")
_LEGACY_SUBSCRIPTION_COLS = (
    "account_type", "account_login", "seats", "billing_model",
    "effective_date", "expiry_date", "price_locked", "locked_price",
    "locked_currency", "payment_channel", "billing_cycle",
)


def now() -> float:
    return time.time()


def today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class Database:
    """SQLite 数据库封装。每个操作独立连接（WAL，线程安全）。"""

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            self._migrate(conn)

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """清理旧版遗留：废弃表与订阅多余列。"""
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for legacy in _LEGACY_TABLES:
            if legacy in tables:
                conn.execute(f"DROP TABLE IF EXISTS {legacy}")
        cols = {r[1] for r in conn.execute("PRAGMA table_info(subscriptions)")}
        for legacy in _LEGACY_SUBSCRIPTION_COLS:
            if legacy in cols:
                conn.execute(f"ALTER TABLE subscriptions DROP COLUMN {legacy}")

    # ---------- events（幂等） ----------

    def record_event(self, event_id: str, event_type: str, payload: dict) -> bool:
        """记录事件；返回 True 表示新事件（原子去重）。"""
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO events(event_id, event_type, payload, received_at)"
                " VALUES(?,?,?,?)",
                (event_id, event_type, json.dumps(payload, ensure_ascii=False), now()),
            )
            return cur.rowcount > 0

    def event_exists(self, event_id: str) -> bool:
        with self._connect() as conn:
            return conn.execute(
                "SELECT 1 FROM events WHERE event_id=?", (event_id,)
            ).fetchone() is not None

    # ---------- users ----------

    def create_user(self, email_hash: str, email_enc: str) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO users(email_hash, email_enc, created_at) VALUES(?,?,?)",
                (email_hash, email_enc, now()),
            )
            return cur.lastrowid

    def get_user(self, user_id: int) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE id=?", (user_id,)
            ).fetchone()
            return dict(row) if row else None

    def get_user_by_email(self, email_hash: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE email_hash=?", (email_hash,)
            ).fetchone()
            return dict(row) if row else None

    def delete_user(self, user_id: int) -> None:
        """GDPR/个保法：删除用户及其订阅记录。"""
        with self._connect() as conn:
            conn.execute("DELETE FROM users WHERE id=?", (user_id,))
            conn.execute("DELETE FROM subscriptions WHERE account_id=?", (str(user_id),))
            conn.execute("DELETE FROM usage WHERE account_id=?", (str(user_id),))

    # ---------- subscriptions（订阅） ----------

    def ensure_free_subscription(self, user_id: int) -> dict:
        """为用户创建/确认订阅（幂等）。"""
        with self._connect() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO subscriptions(account_id, plan, status,
                                                       created_at, updated_at)
                   VALUES(?, 'free', 'active', ?, ?)""",
                (str(user_id), now(), now()),
            )
        return self.get_subscription(str(user_id))

    def get_subscription(self, account_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM subscriptions WHERE account_id=?", (account_id,)
            ).fetchone()
            return dict(row) if row else None

    def list_subscriptions(self) -> list[dict]:
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM subscriptions ORDER BY created_at DESC"
            ).fetchall()]

    def active_user_count(self) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM subscriptions WHERE status='active'"
            ).fetchone()
            return row["c"]

    # ---------- usage / 成本 ----------

    def record_usage(self, account_id: str, plan: str = "free", calls: int = 1,
                     cost: float = 0.0) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO usage(account_id, plan, day, calls, cost)
                   VALUES(?,?,?,?,?)
                   ON CONFLICT(account_id, day) DO UPDATE SET
                       calls=calls+excluded.calls, cost=cost+excluded.cost""",
                (account_id, plan, today(), calls, cost),
            )

    def daily_cost(self, day: str | None = None) -> float:
        day = day or today()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT SUM(cost) AS c FROM usage WHERE day=?", (day,)
            ).fetchone()
        return round(row["c"] or 0.0, 4)

    def usage_series(self, days: int = 14) -> list[dict]:
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT day, SUM(calls) AS calls, SUM(cost) AS cost"
                " FROM usage GROUP BY day ORDER BY day DESC LIMIT ?", (days,)
            ).fetchall()]

    def reset_free_usage(self) -> int:
        """每月 1 日用量重置：清空 Free 用户的 usage 记录。"""
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM usage WHERE plan='free'")
            return cur.rowcount

    # ---------- notifications ----------

    def add_notification(self, channel: str, message: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO notifications(channel, message, created_at) VALUES(?,?,?)",
                (channel, message, now()),
            )

    def list_notifications(self, limit: int = 50) -> list[dict]:
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM notifications ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()]
