# pr-gen 运营服务（server/）— 开源免费版

FastAPI 实现的轻量运营服务：免费用户注册、用量统计、运营看板与成本监控。
**本项目完全免费开源，无任何付费渠道。**

## 快速开始

```bash
cd server
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

未配置外部服务密钥时自动进入本地模拟模式（SQLite + 日志通知）。

## API 一览

| 端点 | 说明 |
| --- | --- |
| `POST /api/auth/register` | 免费用户注册（全部功能开放，邮箱加密存储） |
| `DELETE /api/auth/me/{user_id}` | GDPR 删除：用户/订阅/用量记录 |
| `POST /api/usage` | pr-gen CLI 生成后上报用量/成本 |
| `GET /api/admin/stats` | 看板数据（注册用户/用量/成本/通知） |
| `GET /admin` | 轻量 HTML 运营看板 |
| `POST /api/github/cron/reset-usage` | 每月用量重置（cron 调度） |
| `GET /healthz` | 健康检查 |

## 配置（server/.env，模板见 .env.example）

```dotenv
# 外部服务（留空 = 本地模拟）
SUPABASE_URL=
SUPABASE_KEY=
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
# 安全
DATA_ENCRYPTION_KEY=            # 留空自动生成于 data/encryption.key
USAGE_API_KEY=                  # 非空时内部 API 要求 X-API-Key
# 监控
COST_ALERT_THRESHOLD=5          # 单日 API 成本告警（美元）
```

## 验收测试

```bash
cd server
python -m unittest discover -s tests -v
```

覆盖：免费注册（邮箱幂等/加密）、看板统计、用量上报、成本告警、cron 鉴权、生产 fail-fast。

## 部署

- **Railway**：`railway up`（根目录 `railway.json`，构建 `server/Dockerfile`）
- **Fly.io**：`fly launch`（`fly.toml`，数据卷挂载 `/app/data`）
- **Docker**：`docker build -f server/Dockerfile .`

## 安全说明

- 邮箱 Fernet 加密 + pepper 哈希（防枚举），密钥文件 0600 权限
- 内部 API / cron 端点 X-API-Key 鉴权（生产 fail-closed）
- 生产模式缺少必要密钥直接拒绝启动（fail-fast）
- GDPR：删除用户即删除订阅与用量记录
