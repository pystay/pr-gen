"""本地缓存：以 diff 指纹为 key，缓存静态分析与 LLM 生成结果。

SQLite 实现，支持并发读写；命中缓存时生成可在毫秒级完成，
这正是提案中「利用缓存优势、降低 API 消耗」的落地方式。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path

from .paths import cache_dir

SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    key        TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,
    payload    TEXT NOT NULL,
    created_at REAL NOT NULL,
    hit_count  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_kind ON entries(kind);
"""


def _db_path() -> Path:
    return cache_dir() / "cache.db"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_db_path()), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    # executescript：一次性执行建表 + 建索引（execute 只允许单语句）
    conn.executescript(SCHEMA)
    return conn


def fingerprint(parts: dict) -> str:
    """对任意结构化片段做稳定指纹。"""
    blob = json.dumps(parts, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def get(key: str) -> str | None:
    try:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT payload FROM entries WHERE key=?", (key,)
            ).fetchone()
            if row:
                conn.execute(
                    "UPDATE entries SET hit_count=hit_count+1 WHERE key=?", (key,)
                )
                conn.commit()
                return row[0]
            return None
        finally:
            conn.close()
    except sqlite3.Error:
        return None


def put(key: str, kind: str, payload: str, max_entries: int = 2000) -> None:
    try:
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO entries(key, kind, payload, created_at) VALUES(?,?,?,?)"
                " ON CONFLICT(key) DO UPDATE SET payload=excluded.payload,"
                " created_at=excluded.created_at",
                (key, kind, payload, time.time()),
            )
            # 超出上限时清理最旧的 10%（按创建时间）
            total = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
            if total > max_entries:
                trim = total - int(max_entries * 0.9)
                conn.execute(
                    "DELETE FROM entries WHERE key IN ("
                    "  SELECT key FROM entries ORDER BY created_at ASC LIMIT ?)",
                    (trim,),
                )
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error:
        pass


def stats() -> dict:
    try:
        conn = _connect()
        try:
            total = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
            hits = conn.execute("SELECT SUM(hit_count) FROM entries").fetchone()[0]
            kinds = conn.execute(
                "SELECT kind, COUNT(*) FROM entries GROUP BY kind"
            ).fetchall()
            return {
                "entries": total,
                "total_hits": hits or 0,
                "by_kind": {k: c for k, c in kinds},
                "db_path": str(_db_path()),
            }
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return {"error": str(exc)}


def clear() -> int:
    try:
        conn = _connect()
        try:
            cur = conn.execute("DELETE FROM entries")
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()
    except sqlite3.Error:
        return 0
