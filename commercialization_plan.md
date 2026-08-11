# AI 驱动的自动化 PR 描述生成器 —— 商业化扩展模块

> 本文档为商业化扩展提案（V1，MVP 范围）。实现位于 `server/`（FastAPI），
> 定价模型与外部服务均可在 `.env` 中配置。

## 1. 商业化目标

- **路径一（SaaS 规模化）**：GitHub Marketplace 标准化订阅（Free / Pro / Team），
  Webhook 自动激活与降级，人工成本趋近于零。
- **路径二（企业高客单价）**：企业私有部署询价 → 自动 PDF 报价单 → 邮件发送，
  自动化高客单价转化闭环。

## 2. 模块一：GitHub Marketplace 集成

### 2.1 功能需求

- `POST /api/github/webhook` 接收 `marketplace_purchase` 事件（签名校验 + 幂等）。
- `purchased` → 创建/更新订阅并激活对应计划（Free/Pro/Team，per-seat 计费）。
- `cancelled` → 记录到期日（`effective_date`），到期后自动降级 Free。
- `pending_change` / `changed` / `refunded` → 计划变更与退款处理。
- `.github/workflows/sync-marketplace.yml`：Release 发布时同步 Marketplace 描述/截图/定价。

### 2.2 技术规格

- Webhook 鉴权：`X-Hub-Signature-256`（HMAC-SHA256，webhook secret）。
- GitHub App API 调用：JWT（RS256，私钥从 `GITHUB_APP_PRIVATE_KEY` 读取）。
- 存储：SQLite（本地/测试）/ Supabase PostgreSQL（生产，见 `server/supabase/schema.sql`）。
- 幂等：`events` 表按 `event_id` 唯一约束。

### 2.3 定价策略（每席位/月）

| 层级 | 价格 (美元/月/席) | 功能范围 |
| --- | --- | --- |
| Free | $0 | 每月 10 次 PR 生成，仅 public 仓库 |
| Pro | $9 | 无限次数，private 仓库，自定义模板 |
| Team | $29 | Pro + 团队审批流 + 用量分析仪表盘 |

## 3. 模块二：企业私有部署报价与咨询

### 3.1 流程

1. `POST /api/enterprise/quote` 提交询价表单（公司名、开发者数、部署环境、定制需求）。
2. 后端 Pydantic 双重校验 → 价格模型计算 → reportlab 生成 PDF 报价单。
3. 邮件发送（Resend/SendGrid，可配置；本地模式落盘）。
4. leads 入库（AES-256-GCM 加密邮箱/公司信息），状态：待联系/已报价/已转化/已流失。
5. 开发者数量 > 50 → Telegram/企业微信通知人工介入（本地模式落盘日志）。

### 3.2 报价公式（可配置，美元/年）

```text
基础价格     = 99 + max(0, 开发者数量 - 10) * 12
部署附加费   = 基础价格 * 0.25   （私有 IDC 部署）
定制附加费   = 基础价格 * 0.40   （勾选定制开发）
最终报价     = 基础价格 + 部署附加费 + 定制附加费
```

示例（15 人 + 私有 IDC）：159 + 39.75 = **198.75 美元/年**（验收用例）。

### 3.3 技术规格

- PDF：reportlab（含公司 Logo、30 天有效期、条款）。
- 邮件：Resend API（`RESEND_API_KEY`），未配置时落盘到 `mail_out/`。
- 安全：`cryptography` AES-256-GCM 加密敏感字段；密钥 `DATA_ENCRYPTION_KEY`。

## 4. 数据看板与监控

- `GET /api/admin/stats` + `/admin` 轻量 HTML 管理页：
  今日/本月 MRR、活跃用户（Free vs Pro）、待处理询价列表、API 用量与模型成本趋势。
- 成本告警：单日 API 成本 > `COST_ALERT_THRESHOLD`（默认 $5）触发通知（Telegram/落盘）。

## 5. 非功能性要求

- 幂等与签名校验保证 Webhook 安全；敏感字段加密存储（GDPR）。
- 数据库预留 `billing_model` 字段支持未来按量付费。
- Dockerfile + railway.json + fly.toml 一键部署；Webhook 端点幂等支持重试。
- 报价邮件发送延迟 < 30s（本地生成毫秒级）。

## 6. 验收标准

- [x] 模拟 `marketplace_purchase`（purchased）→ 创建用户并激活 Pro；模拟 `cancelled` → 到期自动降级。
- [x] 提交 15 人 + 私有 IDC 表单 → 30s 内 PDF 报价单 + 邮件，金额 = 198.75 美元/年。
- [x] 管理后台显示最近 Pro 订单详情与总 MRR。

## 7. 开发约束

- MVP 仅实现：Marketplace 订阅激活/降级 + 报价单邮件自动生成（不含完整 CRM 界面）。
- 技术栈：Python FastAPI + SQLite/Supabase；Railway/Fly.io 一键部署。
- 外部服务（Supabase/Resend/Telegram）默认本地模拟，配置密钥后自动切换真实服务。
