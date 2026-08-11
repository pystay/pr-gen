# pr-gen 商业化服务（server/）

FastAPI 实现的商业化扩展模块，提供两条变现路径的自动化闭环：

1. **GitHub Marketplace 订阅（SaaS）**：Webhook 自动激活/降级 Free/Pro/Team 订阅
2. **企业私有部署报价**：询价表单 → PDF 报价单 → 邮件自动发送 → leads 入库

详细提案见 `../commercialization_plan.md`。

## 快速开始（本地模拟模式）

```bash
cd server
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

未配置任何外部服务密钥时自动进入本地模拟模式：
- 存储：SQLite（`server/data/prgen.db`），生产切换 Supabase 见 `supabase/schema.sql`
- 邮件：落盘到 `server/mail_out/`（配置 `RESEND_API_KEY` 后自动走真实 API）
- 通知：写入 `notifications` 表（配置 Telegram 密钥后真实发送）

## API 一览

| 端点 | 说明 |
| --- | --- |
| `POST /api/github/webhook` | GitHub Marketplace 事件（签名校验 + 幂等） |
| `POST /api/github/cron/downgrade` | 定时任务：到期订阅降级为 Free |
| `POST /api/enterprise/quote` | 企业询价：校验 → 报价 → PDF → 邮件 → leads |
| `POST /api/usage` | pr-gen CLI 生成后上报用量/成本 |
| `GET /api/admin/stats` | 看板数据（MRR/ARR/活跃用户/订单/leads/成本） |
| `GET /admin` | 轻量 HTML 运营看板 |
| `GET /healthz` | 健康检查 |
| `POST /api/auth/register` | 自营用户注册（自动激活 free 30 天试用） |
| `DELETE /api/auth/me/{user_id}` | GDPR 删除：用户/订阅删除 + 支付记录匿名化 |
| `GET /api/pricing` | 定价查询（层级/月付年付/CNY+USD/促销/功能列表） |
| `POST /api/payment/create` | 国内支付下单（支付宝/微信；本地模拟二维码） |
| `POST /api/payment/callback` | 国内支付网关异步回调（验签+幂等+激活） |
| `POST /api/payment/paypal/create` | PayPal 下单（返回 approval_url） |
| `POST /api/payment/paypal/webhook` | PayPal 事件（PAYMENT.CAPTURE.COMPLETED → 激活） |
| `POST /api/payment/alipay/create` | 支付宝 AI 网页应用收款下单（返回 HTML 支付表单，真实通道） |
| `POST /api/payment/alipay/notify` | 支付宝异步通知（RSA2 验签 + 业务校验 + 幂等 + 激活） |
| `GET /api/payment/alipay/return` | 支付完成同步回跳页（不信任回跳参数） |
| `POST /api/payment/alipay/query` / `refund` / `refund/query` / `close` | 支付宝交易查询 / 退款 / 退款查询 / 关闭 |
| `POST /api/github/cron/reset-usage` | 每月用量重置（仅 Free 用户） |

## 定价与收费

- 定价事实来源：`server/config/pricing_config.yaml`（修改即生效，无需重启）
  - Free / Pro(¥9.9/月, ¥99/年) / Team(¥39/月, ¥399/年)，海外 USD 独立定价
  - 促销窗口：配置 `promotion` 起止日期与价格，报价自动判断
  - 锁定老价格：首次付费记录 `price_locked` + `locked_price`，提价只影响新用户
- 支付渠道：国内（支付宝/微信，FAST易支付/YPay 风格免签网关）+ 海外 PayPal API v2
  - 未配置网关密钥时本地模拟（模拟二维码 / 模拟 approval URL），配置后自动切换真实服务
  - 回调验签（md5 签名）、金额一致性校验、原子幂等、原始回调留痕（对账）
- **支付宝真实通道（AI 网页应用收款，官方 alipay-sdk-python）**：`/api/payment/alipay/*`
  - 下单 `alipay.trade.page.pay`（`page_execute` 返回支付表单）、异步通知 RSA2 验签、
    交易查询/退款/退款查询/关闭、同步回跳页
  - 配置：`ALIPAY_APP_ID` / `ALIPAY_APP_PRIVATE_KEY`（PKCS#1）/ `ALIPAY_PUBLIC_KEY` /
    `ALIPAY_SANDBOX` / `ALIPAY_NOTIFY_URL`（公网 HTTPS，生产必配）
  - 未配置凭证时接口明确报错（不 fallback 占位密钥）；本地联调用交易查询兜底确认支付结果
- 订阅生命周期：注册 → free(+30 天) → 支付延长 end_date → 到期 cron 置 expired → 续费叠加

## 验收测试

```bash
cd server
python -m unittest discover -s tests -v
```

覆盖三条验收标准：
1. **Webhook**：模拟 `purchased` 激活 Pro（含订单与幂等重放）；模拟 `cancelled` 到期自动降级
2. **报价**：15 人 + 私有 IDC → 金额精确 198.75 美元/年，30s 内生成 PDF 并发送邮件（落盘可查）
3. **看板**：显示最近 Pro 订单详情与总 MRR

定价模块验收（59 个测试中的新增部分）：
- 国内支付回调 5 秒内激活 Pro（验签/幂等/金额校验/二维码）
- PayPal Webhook 10 秒内激活订阅（事件幂等）
- 注册自动插入 free 订阅（end_date = +30 天）
- 提价（9.9 → 19.9）后老用户按锁定价格续费、新用户按新价
- 每日任务把过期订阅置为 expired；Free 用量月度重置；GDPR 匿名化

## 部署

- **Railway**：`railway up`（根目录 `railway.json` 已配置，构建 `server/Dockerfile`）
- **Fly.io**：`fly launch`（`fly.toml` 已配置，数据卷挂载 `/app/data`）
- **Docker**：`docker build -f server/Dockerfile .`
- **CI**：`.github/workflows/sync-marketplace.yml` — Release 发布时同步 Marketplace 描述/定价

## 安全说明

- Webhook 请求必须通过 `X-Hub-Signature-256` 签名校验（HMAC-SHA256，支持多签名轮换）
- 管理看板：`/admin` 登录表单 → HttpOnly Cookie 鉴权；**非本地模式**下 `/api/admin/stats` 无 Cookie 返回 403（token 不进入页面源码）
- 内部 API（`/api/usage`、`/cron/downgrade`）：配置 `USAGE_API_KEY` 后必须携带 `X-API-Key` 头，防止伪造用量/成本数据
- 邮箱/公司名等敏感字段 AES-256（Fernet）加密存储，密钥 `DATA_ENCRYPTION_KEY`（未配置时自动生成于 `data/encryption.key` 并仅限所有者读取；生产请显式设置）
- 表单输入：邮箱白名单正则校验（防路径注入），公司名等字段 HTML 转义后进入邮件/PDF（防 XSS/注入）
- 支付由 GitHub 计费体系托管（PCI 合规简化）；如接入 Stripe/Paddle 见提案第 5 节
