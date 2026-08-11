"""路径与凭据定位：缓存目录、Reasonix 全局 .env。"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def home_dir() -> Path:
    return Path.home()


def reasonix_env_path() -> Path | None:
    """定位 Reasonix 全局 .env（保存 provider API key 的唯一运行时来源）。

    Windows: %APPDATA%\\reasonix\\.env
    其他平台: $XDG_CONFIG_HOME/reasonix/.env 或 ~/.config/reasonix/.env
    """
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if appdata:
            p = Path(appdata) / "reasonix" / ".env"
            if p.exists():
                return p
    xdg = os.environ.get("XDG_CONFIG_HOME")
    candidates = []
    if xdg:
        candidates.append(Path(xdg) / "reasonix" / ".env")
    candidates.append(home_dir() / ".config" / "reasonix" / ".env")
    for p in candidates:
        if p.exists():
            return p
    return None


def cache_dir() -> Path:
    """pr-gen 本地缓存目录。Windows: %LOCALAPPDATA%\\pr-gen；其他: ~/.cache/pr-gen。"""
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA")
        base = Path(local) if local else home_dir() / "AppData" / "Local"
    else:
        xdg = os.environ.get("XDG_CACHE_HOME")
        base = Path(xdg) if xdg else home_dir() / ".cache"
    d = base / "pr-gen"
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_reasonix_keys() -> dict[str, str]:
    """读取 Reasonix 全局 .env 中形如 KEY=value 的行（忽略注释与空行）。"""
    p = reasonix_env_path()
    if not p:
        return {}
    keys: dict[str, str] = {}
    try:
        for raw in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            line = line.removeprefix("export ").strip()
            if "=" not in line:
                continue
            k, _, v = line.partition("=")
            keys[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        return {}
    return keys


def resolve_api_key(env_name: str) -> str | None:
    """解析 API key：先环境变量，再 Reasonix 全局 .env。"""
    val = os.environ.get(env_name)
    if val and val.strip():
        return val.strip()
    keys = load_reasonix_keys()
    val = keys.get(env_name)
    return val if val else None
