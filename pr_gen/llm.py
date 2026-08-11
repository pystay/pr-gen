"""LLM 客户端：调用 DeepSeek Anthropic 兼容 Messages API。

- 端点与模型与 Reasonix 配置一致（deepseek-v4-flash）
- API key 复用 Reasonix 全局 .env（DEEPSEEK_API_KEY），无需重复配置
- 纯标准库实现（urllib），零第三方依赖
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from .paths import resolve_api_key

DEFAULT_BASE_URL = "https://api.deepseek.com/anthropic"
DEFAULT_MODEL = "deepseek-v4-flash"
DEFAULT_TIMEOUT = 60


class LLMError(RuntimeError):
    pass


class LLMClient:
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = DEFAULT_BASE_URL,
        model: str = DEFAULT_MODEL,
        timeout: int = DEFAULT_TIMEOUT,
        api_key_env: str = "DEEPSEEK_API_KEY",
    ):
        self.api_key = api_key or resolve_api_key(api_key_env)
        if not self.api_key:
            raise LLMError(
                f"未找到 {api_key_env}。请设置环境变量，"
                "或确保 Reasonix 全局 .env 中存在该 key。"
            )
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout

    def messages(
        self,
        system: str,
        user: str,
        max_tokens: int = 8000,
        temperature: float = 0.2,
    ) -> str:
        """发送一次 Messages 请求，返回首个文本块内容。"""
        url = f"{self.base_url}/v1/messages"
        body = {
            "model": self.model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
                "anthropic-version": "2023-06-01",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            if exc.code == 401:
                raise LLMError("API key 无效（401），请检查 DEEPSEEK_API_KEY。") from exc
            if exc.code == 429:
                raise LLMError("请求过于频繁（429），请稍后重试或检查账户余额。") from exc
            raise LLMError(f"模型服务返回错误 {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise LLMError(f"无法连接模型服务（{exc.reason}），请检查网络。") from exc
        except (TimeoutError, OSError) as exc:
            raise LLMError(f"读取模型响应失败或超时: {exc}") from exc

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LLMError(f"模型服务响应无法解析: {raw[:200]}") from exc
        if parsed.get("type") == "error":
            raise LLMError(f"模型服务错误: {parsed.get('error')}")
        texts = [
            block.get("text", "")
            for block in parsed.get("content", [])
            if block.get("type") == "text"
        ]
        if not texts:
            raise LLMError("模型未返回文本内容。")
        return texts[0].strip()
