"""运营看板：统计 API + 轻量 HTML 管理页（/admin）。

指标：MRR/ARR、活跃用户（Free vs Pro/Team）、最近订单、待处理询价、
API 用量与成本趋势、成本告警（单日成本超阈值触发通知）。

鉴权：/admin 登录表单 → 服务端校验 admin token → HttpOnly Cookie；
/api/admin/stats 校验 Cookie（本地模拟模式放行）。token 不进入 HTML 源码。
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Cookie, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .config import Settings
from .db import Database, today
from .security import Encryptor, admin_token
from .webhook import downgrade_expired

router = APIRouter(tags=["admin"])

ADMIN_COOKIE = "prgen_admin"
PLAN_NAMES = {"free": "Free", "pro": "Pro", "team": "Team"}


def _is_admin(request: Request, settings: Settings) -> bool:
    """校验管理身份：本地模式放行；否则检查 Cookie 中的 admin token。"""
    if settings.is_local_mode:
        return True
    cookie = request.cookies.get(ADMIN_COOKIE)
    query = request.query_params.get("token", "")
    expected = admin_token(settings.github_webhook_secret)
    return cookie == expected or query == expected


def _stats(settings: Settings, db: Database, enc: Encryptor) -> dict:
    downgrade_expired(db)  # 先处理到期降级，保证统计口径一致
    subs = db.list_subscriptions()
    orders = db.list_orders(limit=20)
    leads = db.list_leads(limit=50)

    active = [s for s in subs if s["status"] == "active"]
    counts = {"free": 0, "pro": 0, "team": 0}
    for s in active:
        counts[s["plan"]] = counts.get(s["plan"], 0) + 1

    mrr = db.mrr(settings.plan_price)

    # 今日/本月收入（按订单金额汇总）
    today_str = today()
    month_str = today_str[:7]
    today_revenue = sum(o["amount"] for o in orders
                        if datetime.fromtimestamp(o["created_at"], tz=timezone.utc)
                        .strftime("%Y-%m-%d") == today_str)
    month_revenue = sum(o["amount"] for o in orders
                        if datetime.fromtimestamp(o["created_at"], tz=timezone.utc)
                        .strftime("%Y-%m") == month_str)

    # 询价列表（解密敏感字段）
    lead_rows = []
    for lead in leads:
        lead_rows.append({
            "id": lead["id"],
            "company": enc.decrypt(lead["company_enc"]),
            "contact_email": enc.decrypt(lead["contact_email_enc"]),
            "dev_count": lead["dev_count"],
            "environment": lead["environment"],
            "needs_custom": bool(lead["needs_custom"]),
            "quote_amount": lead["quote_amount"],
            "status": lead["status"],
            "created_at": lead["created_at"],
        })

    # 最近订单（按订阅信息补全账户名）
    order_rows = []
    sub_by_id = {s["account_id"]: s for s in subs}
    for o in orders:
        sub = sub_by_id.get(o["account_id"], {})
        order_rows.append({
            **o,
            "account_login": sub.get("account_login", ""),
            "plan_name": PLAN_NAMES.get(o["plan"], o["plan"]),
        })

    # 自营支付流水（payment_logs）与近 90 天收入
    payments = db.list_payments(limit=20)
    payment_rows = []
    for p in payments:
        payment_rows.append({
            "order_no": p["order_no"],
            "tier": p["tier"],
            "cycle": p["cycle"],
            "amount": p["amount"],
            "currency": p["currency"],
            "channel": p["channel"],
            "status": p["status"],
            "created_at": p["created_at"],
        })

    return {
        "mrr": mrr,
        "arr": round(mrr * 12, 2),
        "today_revenue": round(today_revenue, 2),
        "month_revenue": round(month_revenue, 2),
        "active_users": counts,
        "active_total": len(active),
        "orders": order_rows,
        "payments": payment_rows,
        "payment_revenue_90d": db.payment_revenue(days=90),
        "leads": lead_rows,
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
            from .leads import notify_human_intervention

            return notify_human_intervention(settings, db, message)
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
    expected = admin_token(settings.github_webhook_secret)
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
  .badge { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 12px; background: #e8f0fe; }
  #login { max-width: 320px; margin: 80px auto; background: #fff; padding: 24px; border-radius: 10px; box-shadow: 0 1px 4px rgba(0,0,0,.08); }
  #login input { width: 100%; box-sizing: border-box; padding: 8px; margin: 8px 0; }
  #login button { width: 100%; padding: 8px; background: #1f3a5f; color: #fff; border: 0; border-radius: 6px; cursor: pointer; }
  .hidden { display: none; }
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
<h1>📊 pr-gen 运营看板 <span class="muted" id="gen"></span></h1>
<div class="cards" id="cards"></div>
<h2>最近订单</h2>
<table><thead><tr><th>时间</th><th>账户</th><th>计划</th><th>席位</th><th>金额 (USD)</th></tr></thead>
<tbody id="orders"></tbody></table>
<h2>企业询价（leads）</h2>
<table><thead><tr><th>公司</th><th>邮箱</th><th>开发者数</th><th>环境</th><th>定制</th><th>报价 (USD/年)</th><th>状态</th></tr></thead>
<tbody id="leads"></tbody></table>
<h2>API 用量与成本（近 14 天）</h2>
<table><thead><tr><th>日期</th><th>调用次数</th><th>成本 (USD)</th></tr></thead>
<tbody id="usage"></tbody></table>
</div>
<script>
// 所有动态数据一律经 escapeHtml 转义后插入，防存储型 XSS
const esc = s => String(s == null ? "" : s)
  .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
  .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
const fmt = n => n == null ? "-" : Number(n).toLocaleString("en-US", {maximumFractionDigits: 2});
const envName = {aws: "AWS", private_idc: "私有 IDC", hybrid: "混合"};

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
    ["MRR", "$" + fmt(d.mrr)], ["ARR", "$" + fmt(d.arr)],
    ["本月收入", "$" + fmt(d.month_revenue)], ["今日收入", "$" + fmt(d.today_revenue)],
    ["活跃用户", d.active_total + "（Free " + d.active_users.free + " / Pro " + d.active_users.pro + " / Team " + d.active_users.team + "）"],
    ["今日 API 成本", "$" + fmt(d.today_cost)],
  ];
  document.getElementById("cards").innerHTML = cards.map(([l, v]) =>
    `<div class="card"><div class="label">${esc(l)}</div><div class="value">${esc(v)}</div></div>`).join("");
  document.getElementById("orders").innerHTML = d.orders.map(o =>
    `<tr><td>${esc(new Date(o.created_at*1000).toLocaleString())}</td><td>${esc(o.account_login || o.account_id)}</td><td><span class="badge">${esc(o.plan_name)}</span></td><td>${esc(o.seats)}</td><td>$${esc(fmt(o.amount))}</td></tr>`).join("");
  document.getElementById("leads").innerHTML = d.leads.map(l =>
    `<tr><td>${esc(l.company)}</td><td>${esc(l.contact_email)}</td><td>${esc(l.dev_count)}</td><td>${esc(envName[l.environment] || l.environment)}</td><td>${esc(l.needs_custom ? "是" : "否")}</td><td>$${esc(fmt(l.quote_amount))}</td><td>${esc(l.status)}</td></tr>`).join("");
  document.getElementById("usage").innerHTML = d.usage.map(u =>
    `<tr><td>${esc(u.day)}</td><td>${esc(u.calls)}</td><td>$${esc(fmt(u.cost))}</td></tr>`).join("");
}
// 页面加载时尝试直接拉取（本地模式或已有 Cookie）
load();
</script>
</body>
</html>
"""
