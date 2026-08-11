"""存储层：SQLite（本地/测试）实现，表结构与 Supabase 同构。

生产切换 Supabase 时：执行 supabase/schema.sql 建表，并将本模块的数据访问
替换为 Supabase REST 客户端即可（接口不变，见各函数 docstring）。
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

CREATE TABLE IF NOT EXISTS subscriptions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id    TEXT NOT NULL,
    account_type  TEXT NOT NULL DEFAULT 'user',
    account_login TEXT NOT NULL DEFAULT '',
    plan          TEXT NOT NULL DEFAULT 'free',      -- free | pro | team
    status        TEXT NOT NULL DEFAULT 'active',    -- active | cancelled | expired
    seats         INTEGER NOT NULL DEFAULT 1,
    billing_model TEXT NOT NULL DEFAULT 'per_seat',  -- 预留：pay_as_you_go
    effective_date REAL NOT NULL,
    expiry_date   REAL,
    price_locked  INTEGER NOT NULL DEFAULT 0,        -- 锁定老价格（自营订阅）
    locked_price  REAL,
    locked_currency TEXT NOT NULL DEFAULT 'CNY',
    payment_channel TEXT NOT NULL DEFAULT '',        -- alipay | wechat | paypal | github
    billing_cycle TEXT NOT NULL DEFAULT 'monthly',   -- monthly | yearly
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL,
    UNIQUE (account_id)
);

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    email_hash    TEXT NOT NULL UNIQUE,               -- sha256 确定性哈希（查询键）
    email_enc     TEXT NOT NULL,                      -- Fernet 加密（存储原文）
    created_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS payment_logs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    order_no      TEXT NOT NULL UNIQUE,
    user_id       INTEGER NOT NULL,
    tier          TEXT NOT NULL,
    cycle         TEXT NOT NULL,
    amount        REAL NOT NULL,
    currency      TEXT NOT NULL DEFAULT 'CNY',
    channel       TEXT NOT NULL,                     -- alipay | wechat | paypal
    status        TEXT NOT NULL DEFAULT 'pending',   -- pending | success | failed
    raw_callback  TEXT NOT NULL DEFAULT '',          -- 原始回调留痕（对账）
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id   TEXT NOT NULL,
    account_id TEXT NOT NULL,
    plan       TEXT NOT NULL,
    seats      INTEGER NOT NULL,
    amount     REAL NOT NULL,
    currency   TEXT NOT NULL DEFAULT 'USD',
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS leads (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    company_enc      TEXT NOT NULL,
    contact_email_enc TEXT NOT NULL,
    dev_count        INTEGER NOT NULL,
    environment      TEXT NOT NULL,      -- aws | private_idc | hybrid
    needs_custom     INTEGER NOT NULL DEFAULT 0,
    special_notes_enc TEXT NOT NULL DEFAULT '',
    quote_amount     REAL,
    quote_currency   TEXT NOT NULL DEFAULT 'USD',
    quote_pdf_path   TEXT NOT NULL DEFAULT '',
    status           TEXT NOT NULL DEFAULT '待联系',  -- 待联系|已报价|已转化|已流失
    created_at       REAL NOT NULL
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
    channel    TEXT NOT NULL,             -- telegram | email | log
    message    TEXT NOT NULL,
    created_at REAL NOT NULL
);
"""


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
        """旧库迁移：subscriptions/users 新增列（CREATE IF NOT EXISTS 不会补列）。"""
        sub_cols = {r[1] for r in conn.execute("PRAGMA table_info(subscriptions)")}
        additions = {
            "price_locked": "INTEGER NOT NULL DEFAULT 0",
            "locked_price": "REAL",
            "locked_currency": "TEXT NOT NULL DEFAULT 'CNY'",
            "payment_channel": "TEXT NOT NULL DEFAULT ''",
            "billing_cycle": "TEXT NOT NULL DEFAULT 'monthly'",
        }
        for name, ddl in additions.items():
            if name not in sub_cols:
                conn.execute(f"ALTER TABLE subscriptions ADD COLUMN {name} {ddl}")
        # users.email_hash：老库（如有）补列
        user_cols = {r[1] for r in conn.execute("PRAGMA table_info(users)")}
        if "email_hash" not in user_cols:
            conn.execute("ALTER TABLE users ADD COLUMN email_hash TEXT")

    # ---------- events（幂等） ----------

    def event_exists(self, event_id: str) -> bool:
        with self._connect() as conn:
            return conn.execute(
                "SELECT 1 FROM events WHERE event_id=?", (event_id,)
            ).fetchone() is not None

    def record_event(self, event_id: str, event_type: str, payload: dict) -> bool:
        """记录事件；返回 True 表示新事件（幂等判断与写入原子完成）。"""
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO events(event_id, event_type, payload, received_at)"
                " VALUES(?,?,?,?)",
                (event_id, event_type, json.dumps(payload, ensure_ascii=False), now()),
            )
            return cur.rowcount > 0

    # ---------- subscriptions ----------

    def upsert_subscription(self, sub: dict[str, Any]) -> None:
        # 新列默认值合并：GitHub 渠道等旧调用方无需感知自营订阅字段
        merged = {
            "price_locked": 0,
            "locked_price": None,
            "locked_currency": "CNY",
            "payment_channel": "",
            "billing_cycle": "monthly",
            **sub,
        }
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO subscriptions(
                       account_id, account_type, account_login, plan, status, seats,
                       billing_model, effective_date, expiry_date, price_locked,
                       locked_price, locked_currency, payment_channel, billing_cycle,
                       created_at, updated_at)
                   VALUES(:account_id,:account_type,:account_login,:plan,:status,:seats,
                          :billing_model,:effective_date,:expiry_date,:price_locked,
                          :locked_price,:locked_currency,:payment_channel,:billing_cycle,
                          :created_at,:updated_at)
                   ON CONFLICT(account_id) DO UPDATE SET
                       account_type=excluded.account_type,
                       account_login=excluded.account_login,
                       plan=excluded.plan,
                       status=excluded.status,
                       seats=excluded.seats,
                       effective_date=excluded.effective_date,
                       expiry_date=excluded.expiry_date,
                       price_locked=excluded.price_locked,
                       locked_price=excluded.locked_price,
                       locked_currency=excluded.locked_currency,
                       payment_channel=excluded.payment_channel,
                       billing_cycle=excluded.billing_cycle,
                       updated_at=excluded.updated_at""",
                merged,
            )

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

    # ---------- orders ----------

    def create_order(self, order: dict[str, Any]) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                """INSERT INTO orders(event_id, account_id, plan, seats, amount,
                                      currency, created_at)
                   VALUES(:event_id,:account_id,:plan,:seats,:amount,:currency,:created_at)""",
                order,
            )
            return cur.lastrowid

    def list_orders(self, limit: int = 50) -> list[dict]:
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM orders ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()]

    def mrr(self, plans: dict[str, float]) -> float:
        """月经常性收入：active 订阅 × 席位 × 计划单价。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT plan, seats FROM subscriptions WHERE status='active'"
            ).fetchall()
        return round(sum(plans.get(r["plan"], 0.0) * r["seats"] for r in rows), 2)

    # ---------- leads ----------

    def create_lead(self, lead: dict[str, Any]) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                """INSERT INTO leads(company_enc, contact_email_enc, dev_count,
                                     environment, needs_custom, special_notes_enc,
                                     quote_amount, quote_currency, quote_pdf_path,
                                     status, created_at)
                   VALUES(:company_enc,:contact_email_enc,:dev_count,:environment,
                          :needs_custom,:special_notes_enc,:quote_amount,
                          :quote_currency,:quote_pdf_path,:status,:created_at)""",
                lead,
            )
            return cur.lastrowid

    def list_leads(self, limit: int = 100) -> list[dict]:
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM leads ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()]

    def update_lead_status(self, lead_id: int, status: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE leads SET status=? WHERE id=?", (status, lead_id)
            )

    # ---------- usage / 成本 ----------

    def record_usage(self, account_id: str, plan: str, calls: int = 1,
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

    # ---------- users（自营订阅用户） ----------

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
        """GDPR/个保法：删除用户并匿名化其支付记录。"""
        with self._connect() as conn:
            conn.execute("DELETE FROM users WHERE id=?", (user_id,))
            conn.execute("DELETE FROM subscriptions WHERE account_id=?", (str(user_id),))
            # 支付记录匿名化（保留对账所需金额/时间，清除用户关联与原始回调）
            conn.execute(
                "UPDATE payment_logs SET user_id=-1, raw_callback='' WHERE user_id=?",
                (user_id,),
            )

    # ---------- payment_logs ----------

    def create_payment(self, log: dict[str, Any]) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                """INSERT INTO payment_logs(order_no, user_id, tier, cycle, amount,
                                           currency, channel, status, raw_callback,
                                           created_at, updated_at)
                   VALUES(:order_no,:user_id,:tier,:cycle,:amount,:currency,
                          :channel,:status,:raw_callback,:created_at,:updated_at)""",
                log,
            )
            return cur.lastrowid

    def get_payment(self, order_no: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM payment_logs WHERE order_no=?", (order_no,)
            ).fetchone()
            return dict(row) if row else None

    def mark_payment(self, order_no: str, status: str, raw_callback: str = "") -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE payment_logs SET status=?, raw_callback=?, updated_at=? "
                "WHERE order_no=?",
                (status, raw_callback, now(), order_no),
            )

    def mark_payment_if_pending(self, order_no: str, status: str,
                                raw_callback: str = "") -> bool:
        """原子闸门：仅当订单仍为 pending 时更新为 success/failed。

        返回 True 表示本调用抢到了处理权（网关并发重试只允许一个生效），
        防止重复回调导致订阅到期日被多次延长。
        """
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE payment_logs SET status=?, raw_callback=?, updated_at=? "
                "WHERE order_no=? AND status='pending'",
                (status, raw_callback, now(), order_no),
            )
            return cur.rowcount > 0

    def cleanup_payments(self, keep_days: int = 90) -> int:
        """清理超过保留期的失败/挂起支付记录（success 保留供对账）。

        符合提案要求：支付日志保留 ≥90 天；到期后仅清理非成功记录，
        成功记录按对账需要留存（可另配归档任务）。
        """
        cutoff = now() - keep_days * 86400
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM payment_logs WHERE created_at < ? AND status != 'success'",
                (cutoff,),
            )
            return cur.rowcount

    def list_payments(self, limit: int = 50) -> list[dict]:
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM payment_logs ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()]

    def payment_revenue(self, days: int = 90) -> list[dict]:
        """近 N 天支付收入（按天汇总，status=success）。"""
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT date(created_at, 'unixepoch') AS day,"
                " SUM(amount) AS amount, COUNT(*) AS count"
                " FROM payment_logs WHERE status='success'"
                " AND created_at >= ? GROUP BY day ORDER BY day DESC",
                (now() - days * 86400,),
            ).fetchall()]

    def reset_free_usage(self) -> int:
        """每月 1 日用量重置：清空 Free 用户的 usage 记录。"""
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM usage WHERE plan='free'")
            return cur.rowcount
