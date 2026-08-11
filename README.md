# pr-gen — AI 驱动的自动化 PR 描述生成器

> 在任意 git 仓库运行一条命令，自动分析分支差异并生成结构严谨、内容详实的 Pull Request 描述；
> 内置完整的商业化闭环：GitHub Marketplace 订阅、企业私有部署自动报价、多渠道支付与运营看板。

---

## ✨ 功能总览

### 一、核心：自动化 PR 描述生成（CLI）

- **深度语义分析**：本地静态分析识别变更类型（bugfix / feature / refactor / performance / schema…），
  提取新增/删除符号、影响面与 Issue 引用（`#42`、`JIRA-107`），不依赖模型即可正确区分
  "修复缓存击穿"与"新增用户认证 API"的语义差异
- **结构化输出**：五章节 Markdown —— Summary / Changes / Test Plan / Impact / Related Issues，
  中英文双语（按提交信息自动检测），杜绝"优化了性能"这类空洞措辞
- **交互式微调**：`pr-gen edit "将测试方法部分改为侧重于集成测试"`，自然语言指令逐轮修订
- **本地缓存**：diff 指纹级缓存（SQLite），重复分析毫秒级返回，显著降低 API 消耗
- **Git Hook 集成**：`pr-gen install-hooks`，每次 commit 自动刷新 PR 描述草稿
- **隐私可控**：默认调用 DeepSeek 官方 API（复用 Reasonix 凭据），`--local` 模式完全离线

### 二、商业化闭环（FastAPI 服务，`server/`）

| 模块 | 能力 |
| --- | --- |
| GitHub Marketplace 订阅 | Webhook 接收 `marketplace_purchase` 事件：签名校验、原子幂等、Free/Pro/Team 三级订阅激活与到期自动降级 |
| 企业报价系统 | 询价表单 → 可配置价格模型 → reportlab PDF 报价单 → 邮件自动发送 → leads 加密入库，>50 人自动触发人工介入通知 |
| 定价与收费 | `pricing_config.yaml` 动态定价（月付/年付、CNY/USD）、限时促销窗口、**锁定老价格**（提价不影响存量用户） |
| 多渠道支付 | 国内支付宝/微信（FAST易支付风格免签网关）+ 海外 PayPal（API v2 + Webhook），回调验签、金额校验、并发幂等 |
| 运营看板 | 轻量 HTML 管理页 + stats API：MRR/ARR、活跃用户、订单、询价、API 成本趋势与超预算告警 |

**本地模拟优先**：未配置 Supabase/Resend/Telegram/支付网关密钥时自动降级为
SQLite + 邮件落盘 + 日志通知 + 模拟二维码/approval URL，开箱即用跑通全流程；
配置密钥后无缝切换真实服务。

---

## 🚀 快速开始

### PR 描述生成（CLI，零第三方依赖）

```bash
cd 你的git仓库
python pr-gen/pr_gen/__main__.py generate          # 或使用 pr-gen.bat / pr-gen
# 输出五章节 PR 描述；--lang zh|en 强制语言；--file pr.md 写入文件
```

### 商业化服务（FastAPI）

```bash
cd pr-gen/server
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000

# 浏览器打开：
#   http://127.0.0.1:8000/admin   运营看板
#   http://127.0.0.1:8000/docs    Swagger 交互式 API 文档
```

### 验收测试

```bash
# 核心生成器（40 个测试）
cd pr-gen && python -m unittest discover -s tests

# 商业化服务（62 个测试：验收 5 条 + 安全加固）
cd pr-gen/server && python -m unittest discover -s tests

# 200 文件极端场景性能基准（本地分析 < 0.3s，目标 15s）
cd pr-gen && python scripts/bench_200files.py

# 重建验收测试仓库（demo-repo，含完整 git 历史）
cd pr-gen && python scripts/make_demo_repo.py
```

---

## 🧭 目录结构

```
pr-gen/
├── pr_gen/                  # CLI 核心（纯标准库，零依赖）
│   ├── git_ops.py           #   git diff 捕获、Issue 提取
│   ├── diff_analyzer.py     #   本地静态分析（变更类型/符号/影响面）
│   ├── llm.py               #   DeepSeek Anthropic 兼容端点客户端
│   ├── prompt.py            #   中英文提示词与章节校验
│   ├── cache.py             #   SQLite 缓存（diff 指纹）
│   └── generator.py         #   生成主流程
├── server/                  # 商业化服务（FastAPI）
│   ├── app/
│   │   ├── webhook.py       #   GitHub Marketplace Webhook + cron 任务
│   │   ├── leads.py         #   企业报价（PDF/邮件/通知）
│   │   ├── pricing.py       #   定价模型（促销/锁定老价格）
│   │   ├── payment.py       #   国内支付（下单/验签回调）
│   │   ├── paypal.py        #   PayPal（Orders API v2 / Webhook）
│   │   ├── auth.py          #   自营用户注册（free 30 天试用/GDPR 删除）
│   │   ├── admin.py         #   运营看板（stats API + HTML 管理页）
│   │   └── security.py      #   HMAC 验签 / Fernet 加密 / JWT
│   ├── config/pricing_config.yaml   # 定价配置（改配置即生效）
│   ├── supabase/schema.sql  #   Supabase 生产建表脚本
│   └── tests/               #   62 个测试
├── examples/demo-repo/      # 验收测试仓库（运行 make_demo_repo.py 重建 git 历史）
├── scripts/                 # 性能基准 / 演示仓库生成
├── commercialization_plan.md    # 商业化提案
├── pricing_payment_module.md    # 定价支付模块提案
└── .github/workflows/sync-marketplace.yml   # Release → Marketplace 同步
```

---

## 🛠 技术栈

- **核心 CLI**：Python 3.10+（纯标准库）、DeepSeek `deepseek-v4-flash`（Anthropic 兼容端点）
- **商业化服务**：FastAPI、SQLite（生产 Supabase PostgreSQL）、reportlab、cryptography（Fernet）、PyJWT、PyYAML
- **部署**：Dockerfile、Railway、Fly.io 一键部署

## 🔒 安全设计

- 所有 Webhook/支付回调强制验签（HMAC-SHA256 / md5 网关签名 / PayPal transmission 验签）
- 敏感字段（邮箱、公司名、支付信息）AES-256（Fernet）加密存储 + pepper 哈希查询键
- 生产模式安全基线检查：默认密钥直接拒绝启动（fail-fast）
- 支付回调原子幂等（并发重试不重复生效）、金额一致性校验、日志 90 天保留
- GDPR/个保法：用户数据删除与支付记录匿名化端点

## 📝 项目文档

- [`commercialization_plan.md`](commercialization_plan.md) — 商业化扩展提案（Marketplace + 企业报价）
- [`pricing_payment_module.md`](pricing_payment_module.md) — 定价与收费模块提案
- [`server/README.md`](server/README.md) — 商业化服务详细文档

---

## 👤 作者与贡献者

- **pystay** — 项目设计、开发与维护（唯一贡献者）

---

## 📄 License

本项目保留所有权利。如需商业使用或合作，请通过 GitHub Issues 联系作者。
