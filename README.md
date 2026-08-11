# pr-gen — AI 驱动的自动化 PR 描述生成器

> **完全开源 · 完全免费 · 所有功能对所有用户开放** 🎉
>
> 在任意 git 仓库运行一条命令，自动分析分支差异并生成结构严谨、内容详实的 Pull Request 描述；
> 附带轻量运营看板（用户数 / 用量 / 成本监控）。

---

## ✨ 功能总览

### 核心：自动化 PR 描述生成（CLI）

- **深度语义分析**：本地静态分析识别变更类型（bugfix / feature / refactor / performance / schema…），
  提取新增/删除符号、影响面与 Issue 引用（`#42`、`JIRA-107`），不依赖模型即可正确区分
  "修复缓存击穿"与"新增用户认证 API"的语义差异
- **结构化输出**：五章节 Markdown —— Summary / Changes / Test Plan / Impact / Related Issues，
  中英文双语（按提交信息自动检测），杜绝"优化了性能"这类空洞措辞
- **交互式微调**：`pr-gen edit "将测试方法部分改为侧重于集成测试"`，自然语言指令逐轮修订
- **本地缓存**：diff 指纹级缓存（SQLite），重复分析毫秒级返回，显著降低 API 消耗
- **Git Hook 集成**：`pr-gen install-hooks`，每次 commit 自动刷新 PR 描述草稿
- **隐私可控**：默认调用 DeepSeek 官方 API，`--local` 模式完全离线

### 轻量运营服务（FastAPI，`server/`）

- **免费用户注册**：注册即拥有全部功能（邮箱加密存储，GDPR 删除支持）
- **运营看板**：`/admin` 页面展示注册用户数、API 用量与模型成本趋势、成本超支告警
- **成本监控**：单日 API 调用成本超过阈值自动告警（Telegram / 日志）
- **用量统计**：pr-gen CLI 生成后上报，看板实时可见

**本地模拟优先**：未配置 Supabase/Telegram 密钥时自动降级为 SQLite + 日志通知，开箱即用。

---

## 🚀 快速开始

### PR 描述生成（CLI，零第三方依赖）

```bash
cd 你的git仓库
python pr-gen/pr_gen/__main__.py generate          # 或使用 pr-gen.bat / pr-gen
# 输出五章节 PR 描述；--lang zh|en 强制语言；--file pr.md 写入文件
```

### 运营服务（FastAPI）

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

# 运营服务（19 个测试）
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
├── server/                  # 轻量运营服务（FastAPI）
│   ├── app/
│   │   ├── auth.py          #   免费用户注册（加密存储/GDPR 删除）
│   │   ├── admin.py         #   运营看板（用户数/用量/成本告警）
│   │   ├── db.py            #   SQLite 存储（Supabase 可切换）
│   │   ├── security.py      #   HMAC 验签 / Fernet 加密 / JWT
│   │   └── webhook.py       #   运维 cron 端点（用量重置）
│   ├── supabase/schema.sql  #   Supabase 生产建表脚本
│   └── tests/               #   19 个测试
├── examples/demo-repo/      # 验收测试仓库（运行 make_demo_repo.py 重建 git 历史）
├── scripts/                 # 性能基准 / 演示仓库生成
└── server/README.md         # 运营服务详细文档
```

---

## 🛠 技术栈

- **核心 CLI**：Python 3.10+（纯标准库）、DeepSeek `deepseek-v4-flash`（Anthropic 兼容端点）
- **运营服务**：FastAPI、SQLite（生产 Supabase PostgreSQL）、cryptography（Fernet）、PyJWT
- **部署**：Dockerfile、Railway、Fly.io 一键部署

## 🔒 安全设计

- 敏感字段（邮箱）AES-256（Fernet）加密存储 + pepper 哈希查询键
- 生产模式安全基线检查：缺少必要密钥直接拒绝启动（fail-fast）
- 内部 API 与 cron 端点鉴权（X-API-Key，生产 fail-closed）
- GDPR/个保法：用户数据删除端点

---

## 👤 作者与贡献者

- **pystay** — 项目设计、开发与维护（唯一贡献者）

---

## 📄 License

本项目**完全开源免费**，所有功能对所有用户开放，无任何付费渠道。
