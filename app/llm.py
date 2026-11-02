"""调用 OpenAI 兼容接口。

只用 httpx，不依赖官方 SDK，这样 base_url / 超时 / 温度都能按条配置。
"""

from __future__ import annotations

import json
from typing import Any

import httpx


class ProviderError(Exception):
    """单个 AI 调用失败（会触发切换到下一个 AI）。"""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


def _short(text: str, limit: int = 200) -> str:
    text = " ".join(str(text).split())
    return text[:limit] + ("..." if len(text) > limit else "")


def _extract_error(response: httpx.Response) -> str:
    try:
        data = response.json()
    except (json.JSONDecodeError, ValueError):
        return f"HTTP {response.status_code}: {_short(response.text)}"

    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict):
            message = error.get("message") or error.get("code") or error
            return f"HTTP {response.status_code}: {_short(message)}"
        if isinstance(error, str):
            return f"HTTP {response.status_code}: {_short(error)}"
        if data.get("message"):
            return f"HTTP {response.status_code}: {_short(data['message'])}"
    return f"HTTP {response.status_code}: {_short(response.text)}"


def _endpoint(base_url: str, api_type: str) -> str:
    base = (base_url or "").strip().rstrip("/")
    if not base:
        raise ProviderError("Base URL 为空，请在管理台填写")
    if not base.startswith(("http://", "https://")):
        base = "https://" + base
    return base + ("/responses" if api_type == "responses" else "/chat/completions")


def _payload(provider: dict[str, Any], messages: list[dict], api_type: str) -> dict[str, Any]:
    model = (provider.get("model") or "").strip()
    if not model:
        raise ProviderError("模型名称为空，请在管理台填写")

    payload: dict[str, Any] = {"model": model}
    if api_type == "responses":
        merged = "\n\n".join(item["content"] for item in messages)
        payload["input"] = [
            {"role": "user", "content": [{"type": "input_text", "text": merged}]}
        ]
    else:
        payload["messages"] = messages
        payload["stream"] = False

    if provider.get("temperature") is not None:
        payload["temperature"] = provider["temperature"]
    return payload


def _extract_text(data: dict[str, Any], api_type: str) -> str:
    if api_type == "responses":
        chunks: list[str] = []
        for item in data.get("output", []) or []:
            for content in item.get("content", []) or []:
                if content.get("type") in ("output_text", "text") and content.get("text"):
                    chunks.append(content["text"])
        return "".join(chunks).strip()

    choices = data.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, list):
        # 兼容 content 为分段数组的实现
        return "".join(
            part.get("text", "") for part in content if isinstance(part, dict)
        ).strip()
    return (content or "").strip()


async def call_provider(provider: dict[str, Any], messages: list[dict]) -> str:
    api_type = provider.get("api_type") or "chat_completions"
    url = _endpoint(provider.get("base_url", ""), api_type)
    payload = _payload(provider, messages, api_type)

    headers = {"Content-Type": "application/json"}
    api_key = (provider.get("api_key") or "").strip()
    if api_key:
        try:
            api_key.encode("ascii")
        except UnicodeEncodeError:
            raise ProviderError("API Key 含有中文或全角字符，请重新复制粘贴") from None
        headers["Authorization"] = f"Bearer {api_key}"

    timeout = httpx.Timeout(float(provider.get("timeout") or 30))

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(url, json=payload, headers=headers)
    except httpx.TimeoutException:
        raise ProviderError(f"请求超时（>{provider.get('timeout')}s）") from None
    except httpx.HTTPError as exc:
        raise ProviderError(f"网络错误: {_short(type(exc).__name__ + ': ' + str(exc))}") from None
    except ProviderError:
        raise
    except Exception as exc:  # noqa: BLE001 - 兜底，避免单个 AI 的问题把整个请求打挂
        raise ProviderError(f"请求失败: {_short(type(exc).__name__ + ': ' + str(exc))}") from None

    if response.status_code >= 400:
        raise ProviderError(_extract_error(response), response.status_code)

    try:
        data = response.json()
    except (json.JSONDecodeError, ValueError):
        raise ProviderError("返回内容不是 JSON，可能 Base URL 填错了") from None

    text = _extract_text(data, api_type)
    if not text:
        raise ProviderError("模型返回空内容")
    return text
