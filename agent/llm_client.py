"""模型客户端:只负责"把消息发出去、把响应和用量收回来、把接口错误分类"。

不含任何策略逻辑(构造上下文、解析决策在 policies/llm.py)。
只用 requests 发 HTTP,不引入额外 SDK;请求用的 key 只放在请求头里,绝不进日志。
"""

import json
from dataclasses import dataclass, field
from typing import List, Optional

import requests

from agent.config import AppConfig


@dataclass
class ToolCallRequest:
    """模型请求的一次工具调用(arguments 是原始字符串,尚未解析)。"""
    id: str
    name: str
    arguments: str = ""


@dataclass
class LlmResponse:
    ok: bool
    content: str = ""
    tool_calls: List[ToolCallRequest] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    error: Optional[str] = None
    retryable: bool = False


class LlmClient:
    """DeepSeek(OpenAI 兼容)chat/completions 客户端。"""

    def __init__(self, config: AppConfig, session=None):
        self.config = config
        self.session = session or requests

    def chat(self, messages, tools=None, timeout=None, max_tokens=None) -> LlmResponse:
        payload = {
            "model": self.config.model,
            "messages": messages,
            "temperature": 0,
            "max_tokens": max_tokens or self.config.max_tokens,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }

        try:
            response = self.session.post(
                self.config.endpoint,
                json=payload,
                headers=headers,
                timeout=timeout or self.config.timeout,
            )
        except requests.Timeout:
            return LlmResponse(ok=False, error=f"请求超时({timeout or self.config.timeout}s)", retryable=True)
        except requests.ConnectionError as e:
            return LlmResponse(ok=False, error=f"连接失败: {e}", retryable=True)
        except requests.RequestException as e:
            return LlmResponse(ok=False, error=f"请求失败: {type(e).__name__}: {e}", retryable=True)

        if response.status_code != 200:
            # 只保留一小段响应体用于诊断,避免把整页 HTML 灌进日志
            detail = (response.text or "")[:200].replace("\n", " ")
            retryable = response.status_code == 429 or response.status_code >= 500
            return LlmResponse(
                ok=False,
                error=f"HTTP {response.status_code}: {detail}",
                retryable=retryable,
            )

        try:
            data = response.json()
        except ValueError as e:
            return LlmResponse(ok=False, error=f"响应不是合法 JSON: {e}", retryable=True)

        if not isinstance(data, dict):
            return LlmResponse(ok=False, error=f"响应 JSON 不是对象: {type(data).__name__}")

        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            return LlmResponse(ok=False, error=f"响应缺少 choices: {str(data)[:200]}")

        message = choices[0].get("message") or {}
        content = message.get("content") or ""
        tool_calls = []
        for raw in message.get("tool_calls") or []:
            function = raw.get("function") or {}
            tool_calls.append(ToolCallRequest(
                id=raw.get("id") or "",
                name=function.get("name") or "",
                arguments=function.get("arguments") or "",
            ))

        usage = data.get("usage") or {}
        return LlmResponse(ok=True, content=content, tool_calls=tool_calls, usage={
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "total_tokens": usage.get("total_tokens"),
        })
