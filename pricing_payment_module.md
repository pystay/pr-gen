# 定价与收费配置模块（V2 扩展）

> 本文档为定价/收费模块提案。实现位于 `server/`（FastAPI）：
> 自营订阅渠道（CNY 支付宝/微信 + 海外 PayPal），与 GitHub Marketplace 渠道并存。
> 定价事实来源为 `server/config/pricing_config.yaml`，支持促销窗口与锁定老价格。

## 1. 定价配置结构（server/config/pricing_config.yaml）

- 层级：free / pro / team，月付 + 年付
- 国内 CNY：pro ¥9.9/月、¥99/年；team ¥39/月、¥399/年
- 海外 USD：pro $2.99/月、$29.99/年；team $9.99/月、$99.99/年
- 促销窗口：`promotion`（起止日期 + 促销价），报价时自动判断
- 提价策略：首次订阅记录 `price_locked=true` + `locked_price`，后续提价不影响存量用户

## 2. 支付渠道

### 2.1 国内（支付宝/微信，FAST易支付/YPay 风格免签网关）

- 配置：`PAY_GATEWAY_URL` / `PAY_MERCHANT_ID` / `PAY_SECRET_KEY`
- 流程：创建订单 → 返回收款二维码（本地模拟）/ 网关跳转 → 异步回调
  `POST /api/payment/callback`（验签 → 幂等 → 激活订阅 → `{"code":0,"msg":"success"}`）

### 2.2 海外（PayPal API v2 / Orders API）

- 配置：`PAYPAL_CLIENT_ID` / `PAYPAL_SECRET` / `PAYPAL_WEBHOOK_ID` / `PAYPAL_SANDBOX`
- 流程：创建订单 → `approval_url` 跳转 → `PAYMENT.CAPTURE.COMPLETED` Webhook 激活

## 3. 订阅状态管理

- `users` 表（自营用户）+ `payment_logs` 表（支付流水，order_no 唯一，raw_callback 留痕）
- 订阅表复用现有 `subscriptions`，新增列：`price_locked` / `locked_price` /
  `payment_channel` / `billing_cycle`
- 状态流转：注册 → free 30 天；支付 → 激活 + 延长 end_date；每日任务 → 过期置 expired；
  续费 → 新订单成功延长 end_date

## 4. 验收标准

- [ ] 模拟国内支付回调：扫码支付后 5 秒内激活 Pro
- [ ] 模拟 PayPal Webhook：10 秒内接收并激活订阅
- [ ] 注册新用户自动插入 free 订阅（end_date = +30 天）
- [ ] 改配置 pro_monthly 9.9 → 19.9：老用户（price_locked）续费仍按 9.9，新用户按 19.9
- [ ] 每日定时任务：end_date < NOW() → expired

## 5. 安全与合规

- 支付回调必须验签（HMAC/MD5 签名），防伪造
- 支付流水匿名化支持（GDPR/个保法），日志保留 ≥90 天
- 支付渠道绑定信息 AES-256 加密存储（复用 Fernet）
