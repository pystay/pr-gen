"""运营看板（开源免费版）：统计 API + 轻量 HTML 管理页（/admin）。

指标：注册用户数、API 用量与成本趋势、成本告警、通知记录。
（无任何收入/订阅计费数据——项目已完全免费开源。）

鉴权：/admin 登录表单 → 服务端校验 admin token → HttpOnly Cookie；
/api/admin/stats 校验 Cookie（本地模拟模式放行）。token 不进入 HTML 源码。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Cookie, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .config import Settings
from .db import Database, today
from .security import Encryptor, admin_token

logger = logging.getLogger("prgen.admin")

router = APIRouter(tags=["admin"])

ADMIN_COOKIE = "prgen_admin"


def _is_admin(request: Request, settings: Settings) -> bool:
    """校验管理身份：本地模式放行；否则检查 Cookie 中的 admin token。"""
    if settings.is_local_mode:
        return True
    cookie = request.cookies.get(ADMIN_COOKIE)
    query = request.query_params.get("token", "")
    expected = admin_token(settings.data_encryption_key)
    return cookie == expected or query == expected


def _stats(settings: Settings, db: Database, enc: Encryptor) -> dict:
    users = db.list_subscriptions()
    return {
        "users_total": len(users),
        "users_active": db.active_user_count(),
        "usage": db.usage_series(days=14),
        "today_cost": db.daily_cost(),
        "cost_alert_threshold": settings.cost_alert_threshold,
        "notifications": db.list_notifications(limit=20),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def check_cost_alert(settings: Settings, db: Database) -> dict | None:
    """单日 API 成本超阈值时告警（Telegram 或落盘）。返回告警结果或 None。"""
    cost = db.daily_cost()
    if cost > settings.cost_alert_threshold:
        message = (f"[成本告警] 今日 API 调用成本 ${cost:.2f} 已超过阈值 "
                   f"${settings.cost_alert_threshold:.2f}")
        if settings.telegram_bot_token and settings.telegram_chat_id:
            from urllib import error as urlerror
            from urllib import request as urlreq

            import json as _json

            body = _json.dumps({
                "chat_id": settings.telegram_chat_id, "text": message,
            }).encode("utf-8")
            try:
                req = urlreq.Request(
                    f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
                    data=body, method="POST",
                    headers={"Content-Type": "application/json"},
                )
                with urlreq.urlopen(req, timeout=15) as resp:
                    resp.read()
                db.add_notification("telegram", message)
                return {"channel": "telegram"}
            except (urlerror.URLError, OSError) as exc:
                logger.warning("Telegram 通知失败，落盘: %s", exc)
        db.add_notification("log", message)
        return {"channel": "log", "message": message}
    return None


@router.get("/api/admin/stats")
async def admin_stats(request: Request):
    """看板数据 API（需登录 Cookie；本地模式直接访问）。"""
    settings: Settings = request.app.state.settings
    db: Database = request.app.state.db
    enc: Encryptor = request.app.state.encryptor
    if not _is_admin(request, settings):
        return JSONResponse(status_code=403, content={"error": "forbidden"})
    check_cost_alert(settings, db)
    return _stats(settings, db, enc)


@router.post("/api/admin/login")
async def admin_login(request: Request):
    """登录：提交 admin token，成功则写入 HttpOnly Cookie。"""
    settings: Settings = request.app.state.settings
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "invalid json"})
    token = str(payload.get("token", ""))
    expected = admin_token(settings.data_encryption_key)
    if token != expected:
        return JSONResponse(status_code=401, content={"error": "invalid token"})
    resp = JSONResponse(content={"ok": True})
    resp.set_cookie(
        ADMIN_COOKIE, expected, httponly=True, samesite="lax", max_age=3600 * 8,
        secure=not settings.is_local_mode,
    )
    return resp


@router.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    """轻量 HTML 管理页（登录表单 + 内嵌 JS 拉取 /api/admin/stats）。"""
    return HTMLResponse(_ADMIN_HTML)


_ADMIN_HTML = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>pr-gen 运营看板</title>
<style>
  body { font-family: "Segoe UI", "Microsoft YaHei", sans-serif; margin: 24px; background: #f5f7fa; color: #222; }
  h1 { font-size: 20px; }
  .cards { display: flex; gap: 16px; flex-wrap: wrap; margin: 16px 0; }
  .card { background: #fff; border-radius: 10px; padding: 16px 20px; box-shadow: 0 1px 4px rgba(0,0,0,.08); min-width: 150px; }
  .card .label { color: #888; font-size: 12px; }
  .card .value { font-size: 24px; font-weight: 600; margin-top: 4px; }
  table { border-collapse: collapse; width: 100%; background: #fff; border-radius: 8px; overflow: hidden; box-shadow: 0 1px 4px rgba(0,0,0,.08); }
  th, td { padding: 8px 12px; text-align: left; border-bottom: 1px solid #eee; font-size: 13px; }
  th { background: #1f3a5f; color: #fff; }
  h2 { font-size: 15px; margin: 24px 0 8px; }
  .muted { color: #999; font-size: 12px; }
  #login { max-width: 320px; margin: 80px auto; background: #fff; padding: 24px; border-radius: 10px; box-shadow: 0 1px 4px rgba(0,0,0,.08); }
  #login input { width: 100%; box-sizing: border-box; padding: 8px; margin: 8px 0; }
  #login button { width: 100%; padding: 8px; background: #1f3a5f; color: #fff; border: 0; border-radius: 6px; cursor: pointer; }
  .hidden { display: none; }
  .free-badge { display:inline-block; padding:4px 12px; border-radius:12px; background:#e6f7e6; color:#1a7f37; font-size:13px; font-weight:600; }
</style>
</head>
<body>
<div id="login">
  <h1 style="font-size:16px">🔐 运营看板登录</h1>
  <p class="muted">输入管理令牌（部署时由 GITHUB_WEBHOOK_SECRET 派生）</p>
  <input type="password" id="token" placeholder="管理令牌">
  <button onclick="login()">登录</button>
  <p id="err" class="muted" style="color:#c0392b"></p>
</div>
<div id="dashboard" class="hidden">
<h1>📊 pr-gen 运营看板 <span class="free-badge">完全免费开源</span> <span class="muted" id="gen"></span></h1>
<div class="cards" id="cards"></div>
<h2>API 用量与成本（近 14 天）</h2>
<table><thead><tr><th>日期</th><th>调用次数</th><th>成本 (USD)</th></tr></thead>
<tbody id="usage"></tbody></table>
<h2>通知记录</h2>
<table><thead><tr><th>渠道</th><th>消息</th><th>时间</th></tr></thead>
<tbody id="notices"></tbody></table>
</div>
<script>
const esc = s => String(s == null ? "" : s)
  .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
  .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
const fmt = n => n == null ? "-" : Number(n).toLocaleString("en-US", {maximumFractionDigits: 2});

async function login() {
  const r = await fetch("/api/admin/login", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({token: document.getElementById("token").value}),
  });
  if (r.ok) { document.getElementById("err").textContent = ""; load(); }
  else document.getElementById("err").textContent = "令牌无效";
}

async function load() {
  const r = await fetch("/api/admin/stats");
  if (r.status === 403) { document.getElementById("login").classList.remove("hidden"); return; }
  document.getElementById("login").classList.add("hidden");
  document.getElementById("dashboard").classList.remove("hidden");
  const d = await r.json();
  document.getElementById("gen").textContent = "生成于 " + d.generated_at;
  const cards = [
    ["注册用户", d.users_total],
    ["活跃用户", d.users_active],
    ["今日 API 成本", "$" + fmt(d.today_cost)],
    ["成本告警阈值", "$" + fmt(d.cost_alert_threshold) + "/天"],
  ];
  document.getElementById("cards").innerHTML = cards.map(([l, v]) =>
    `<div class="card"><div class="label">${esc(l)}</div><div class="value">${esc(v)}</div></div>`).join("");
  document.getElementById("usage").innerHTML = d.usage.map(u =>
    `<tr><td>${esc(u.day)}</td><td>${esc(u.calls)}</td><td>$${esc(fmt(u.cost))}</td></tr>`).join("");
  document.getElementById("notices").innerHTML = d.notifications.map(n =>
    `<tr><td>${esc(n.channel)}</td><td>${esc(n.message)}</td><td>${esc(new Date(n.created_at*1000).toLocaleString())}</td></tr>`).join("");
}
load();
</script>
</body>
</html>
"""
