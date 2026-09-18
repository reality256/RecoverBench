"""运行配置:环境变量优先,.env 兜底,默认值最后。

只支持一个供应商(DeepSeek,OpenAI 兼容接口),先把闭环跑稳。
API key 只存在内存和 .env 里,绝不进日志(.env 已在 .gitignore 中)。
"""

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / ".env"

DEFAULTS = {
    # 正式模型名(用 GET /models 向 API 确认过);deepseek-chat 是已弃用的兼容别名
    "LLM_MODEL": "deepseek-flash",
    "LLM_BASE_URL": "https://api.deepseek.com/v1",
    "LLM_TIMEOUT": "30",
    "LLM_MAX_TOKENS": "1024",
}

# key 的多个常见写法都认
API_KEY_NAMES = ("LLM_API_KEY", "DEEPSEEK_API_KEY")


def load_env_file(path=ENV_FILE) -> dict:
    """极简 .env 解析:KEY=VALUE,忽略注释与空行。不覆盖已存在的环境变量。"""
    values = {}
    path = Path(path)
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _lookup(name: str, file_values: dict):
    for source in (os.environ, file_values, DEFAULTS):
        value = source.get(name)
        if value:
            return value
    return None


@dataclass
class AppConfig:
    """模型配置:名称、地址、key、请求超时、输出 token 上限。"""

    model: str
    base_url: str
    api_key: str
    timeout: float
    max_tokens: int

    @classmethod
    def from_env(cls, env_file=ENV_FILE) -> "AppConfig":
        file_values = load_env_file(env_file)

        api_key = ""
        for name in API_KEY_NAMES:
            value = _lookup(name, file_values)
            if value:
                api_key = value
                break

        return cls(
            model=_lookup("LLM_MODEL", file_values),
            base_url=_lookup("LLM_BASE_URL", file_values),
            api_key=api_key,
            timeout=float(_lookup("LLM_TIMEOUT", file_values) or 30),
            max_tokens=int(_lookup("LLM_MAX_TOKENS", file_values) or 1024),
        )

    @property
    def has_api_key(self) -> bool:
        return bool(self.api_key)

    @property
    def endpoint(self) -> str:
        return self.base_url.rstrip("/") + "/chat/completions"

    def redacted(self) -> dict:
        """写进 config.json 的版本:不含 key 明文。"""
        return {
            "model": self.model,
            "base_url": self.base_url,
            "api_key": "***" if self.api_key else None,
            "timeout": self.timeout,
            "max_tokens": self.max_tokens,
        }
