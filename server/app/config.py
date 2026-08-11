"""server 配置：.env 加载 + 开源免费版配置项。

外部服务（Supabase/Telegram）未配置密钥时自动降级为本地模拟。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent  # .../server/app
SERVER_ROOT = SERVER_DIR.parent               # .../server
PROJECT_ROOT = SERVER_ROOT.parent             # .../pr-gen（项目根）

DEFAULT_ENV = {
    # 基础
    "APP_NAME": "pr-gen",
    "PORT": "8000",
    "DATA_DIR": str(SERVER_ROOT / "data"),
    # 外部服务
    "SUPABASE_URL": "",
    "SUPABASE_KEY": "",
    "TELEGRAM_BOT_TOKEN": "",
    "TELEGRAM_CHAT_ID": "",
    # 安全
    "DATA_ENCRYPTION_KEY": "",  # 空则自动生成并持久化到 data/ 下
    "USAGE_API_KEY": "",  # 非空时内部 API 要求 X-API-Key 头
    # 监控
    "COST_ALERT_THRESHOLD": "5",
}


def load_env_file(path: Path | None = None) -> dict[str, str]:
    """加载 .env 文件（KEY=VALUE，忽略注释），返回覆盖字典。"""
    path = path or SERVER_ROOT / ".env"  # 与 .env.example 同层（server/.env）
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        line = line.removeprefix("export ").strip()
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


@dataclass
class Settings:
    app_name: str
    data_dir: Path
    supabase_url: str = ""
    supabase_key: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    data_encryption_key: str = ""
    usage_api_key: str = ""
    cost_alert_threshold: float = 5.0

    @property
    def is_local_mode(self) -> bool:
        """未配置任何外部服务 → 本地模拟模式。"""
        return not (self.supabase_url or self.telegram_bot_token)


def _f(val: str, default: float) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def load_settings(env_overrides: dict[str, str] | None = None) -> Settings:
    """组装 Settings：env_overrides > 环境变量 > .env 文件 > 默认值。"""
    merged = dict(DEFAULT_ENV)
    merged.update(load_env_file())
    # 环境变量（仅采纳已知 key），支持 Railway/Fly.io 等平台注入
    known_env = {k: v for k, v in os.environ.items() if k in merged}
    merged.update(known_env)
    if env_overrides:
        merged.update(env_overrides)

    data_dir = Path(merged["DATA_DIR"])
    data_dir.mkdir(parents=True, exist_ok=True)

    enc_key = merged["DATA_ENCRYPTION_KEY"]
    if not enc_key:
        # 本地模式：自动生成 Fernet 密钥并持久化（生产应显式配置）
        from cryptography.fernet import Fernet

        key_file = data_dir / "encryption.key"
        if key_file.exists():
            enc_key = key_file.read_text(encoding="utf-8").strip()
        else:
            enc_key = Fernet.generate_key().decode("utf-8")
            key_file.write_text(enc_key, encoding="utf-8")
            try:
                os.chmod(key_file, 0o600)  # 仅所有者可读（Windows 上为 no-op）
            except OSError:
                pass

    return Settings(
        app_name=merged["APP_NAME"],
        data_dir=data_dir,
        supabase_url=merged["SUPABASE_URL"],
        supabase_key=merged["SUPABASE_KEY"],
        telegram_bot_token=merged["TELEGRAM_BOT_TOKEN"],
        telegram_chat_id=merged["TELEGRAM_CHAT_ID"],
        data_encryption_key=enc_key,
        usage_api_key=merged["USAGE_API_KEY"],
        cost_alert_threshold=_f(merged["COST_ALERT_THRESHOLD"], 5.0),
    )
