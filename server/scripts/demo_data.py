"""一次性演示脚本：为运营看板生成完整的多渠道业务数据。"""

import hashlib
import json
import urllib.request

BASE = "http://127.0.0.1:8000"
PAY_SECRET = "dev-pay-secret"


def post(path, body, headers=None):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"__error__": e.code, "body": e.read().decode("utf-8", "replace")}


def domestic_callback(order_no, amount):
    params = {"pid": "1001", "out_trade_no": order_no,
              "trade_status": "success", "money": f"{amount:.2f}",
              "trade_no": "TRADE-" + order_no[-8:]}
    params["sign"] = hashlib.md5(
        ("&".join(f"{k}={params[k]}" for k in sorted(params)) + PAY_SECRET).encode()
    ).hexdigest()
    return post("/api/payment/callback", params)


def paypal_webhook(order_no, amount, event_id):
    event = {"id": event_id, "event_type": "PAYMENT.CAPTURE.COMPLETED",
             "resource": {"custom_id": order_no,
                          "amount": {"value": f"{amount:.2f}", "currency_code": "USD"}}}
    sig = hashlib.sha256(event_id.encode()).hexdigest()  # 与模拟验签一致（HMAC over event_id）
    import hmac
    sig = hmac.new(PAY_SECRET.encode(), event_id.encode(), hashlib.sha256).hexdigest()
    return post("/api/payment/paypal/webhook", event,
                headers={"X-Sim-Signature": sig})


import sys; sys.stdout.reconfigure(encoding="utf-8")
print("== 1) 注册 3 位用户 ==")
u1 = post("/api/auth/register", {"email": "zhangsan@example.com"})
u2 = post("/api/auth/register", {"email": "wangwu@example.com"})
u3 = post("/api/auth/register", {"email": "lisi@example.com"})
print(f"   用户 A(张三) id={u1['user_id']} free订阅 [OK]")
print(f"   用户 B(王五) id={u2['user_id']} free订阅 [OK]")
print(f"   用户 C(李四) id={u3['user_id']} free订阅 [OK]")

import sys; sys.stdout.reconfigure(encoding="utf-8")
print("== 2) 用户 A：支付宝支付 Pro 月付 ¥9.9 ==")
o1 = post("/api/payment/create", {"user_id": u1["user_id"], "tier": "pro",
                                  "cycle": "monthly", "channel": "alipay"})
print(f"   订单 {o1['order_no']} 金额 ¥{o1['amount']} 二维码={o1['payment']['mode']}")
cb1 = domestic_callback(o1["order_no"], o1["amount"])
print(f"   回调结果: {cb1}")

import sys; sys.stdout.reconfigure(encoding="utf-8")
print("== 3) 用户 B：PayPal 支付 Team 年付 $99.99 ==")
o2 = post("/api/payment/paypal/create", {"user_id": u2["user_id"], "tier": "team",
                                         "cycle": "yearly"})
print(f"   订单 {o2['order_no']} 金额 ${o2['amount']} approval_url={o2['approval_url'][:48]}...")
wb2 = paypal_webhook(o2["order_no"], o2["amount"], "WH-DEMO-001")
print(f"   Webhook 结果: {wb2}")

import sys; sys.stdout.reconfigure(encoding="utf-8")
print("== 4) 企业询价：星辰科技 15 人 + 私有 IDC ==")
q = post("/api/enterprise/quote", {
    "company": "星辰科技有限公司", "contact_email": "cto@xingchen.cn",
    "dev_count": 15, "environment": "private_idc", "needs_custom": False,
    "special_notes": "需要等保三级合规支持",
})
print(f"   报价: ${q['quote']['total']}/年 (base {q['quote']['base_price']} + 部署 {q['quote']['deployment_fee']})")

import sys; sys.stdout.reconfigure(encoding="utf-8")
print("== 5) 上报 API 用量（成本数据）==")
print("   ", post("/api/usage", {"account_id": str(u1["user_id"]), "plan": "pro",
                                  "calls": 3, "cost": 0.42}))

print("\n全部演示数据已生成，看板数据:")
stats = json.loads(urllib.request.urlopen(BASE + "/api/admin/stats").read())
print(f"   MRR: ${stats['mrr']} | 活跃用户: {stats['active_total']} | 订单: {len(stats['orders'])}")
print(f"   支付流水: {len(stats['payments'])} 笔 | 询价: {len(stats['leads'])} 条 | 今日成本: ${stats['today_cost']}")
