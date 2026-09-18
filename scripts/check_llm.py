"""模型连通性体检:只发一次最小请求,验证 配置/认证/工具调用 是否可用。

排查配置问题时先跑它 —— 把"配置不对"和"Agent 逻辑不对"分开。
成本:一次调用,几百 token。

用法:
    python scripts/check_llm.py
"""

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

from agent import tools  # noqa: E402
from agent.config import AppConfig  # noqa: E402
from agent.dispatch import build_tool_schemas  # noqa: E402
from agent.llm_client import LlmClient  # noqa: E402

PROBE_PROMPT = "请调用工具查看当前 API 的健康状态,然后用一句话说明你看到了什么。"


def main() -> int:
    config = AppConfig.from_env()
    print("===== 模型连通性体检 =====")
    print(f"model     : {config.model}")
    print(f"endpoint  : {config.endpoint}")
    print(f"api_key   : {'已设置' if config.has_api_key else '未设置'}")
    print(f"timeout   : {config.timeout}s | max_tokens: {config.max_tokens}")

    if not config.has_api_key:
        print("\n结论:缺少 API key。请复制 .env.example 为 .env 并填入 LLM_API_KEY。")
        return 2

    client = LlmClient(config)
    schemas = build_tool_schemas(tools.TOOL_REGISTRY)
    print(f"\n发送一次请求(附带 {len(schemas)} 个工具定义)...")

    response = client.chat(
        [{"role": "user", "content": PROBE_PROMPT}],
        tools=schemas,
        timeout=config.timeout,
    )

    if not response.ok:
        print(f"\n结论:请求失败 —— {response.error}")
        print("可重试:" + ("是(网络/限流类问题,稍后再试)" if response.retryable else "否(多半是 key、地址或模型名不对)"))
        return 1

    print("\n第 1 轮请求成功 ✅")
    print(f"  模型回复文本: {response.content[:200] or '(空)'}")
    print(f"  用量: {response.usage}")

    if not response.tool_calls:
        print("  工具调用: 无")
        print("\n结论:请求通了,但模型没有返回工具调用 —— Agent 大概率跑不动,建议换模型。")
        return 1

    call = response.tool_calls[0]
    print(f"  工具调用: {call.name}({call.arguments})")

    # 第 2 轮:完全复刻策略的做法(从历史重建 assistant 消息,再回传工具结果),
    # 验证多轮工具调用链路 —— 单轮能通不代表多轮能通。
    messages = [
        {"role": "user", "content": PROBE_PROMPT},
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "call_1", "type": "function",
                         "function": {"name": call.name, "arguments": call.arguments or "{}"}}]},
        {"role": "tool", "tool_call_id": "call_1",
         "content": '{"ok": true, "data": {"service": "redis", "running": true, "state": "running"}, "error": null}'},
        {"role": "user", "content": "当前状态:已用工具调用 1/10,已用环境修改 0/2,剩余时间约 120 秒。请继续。"},
    ]
    print("\n第 2 轮(复刻策略的消息重建方式)...")
    second = client.chat(messages, tools=schemas, timeout=config.timeout)

    if not second.ok:
        print(f"  ❌ 多轮失败 —— {second.error}")
        print("\n结论:单轮可用但多轮不可用,Agent 循环会中途失败。")
        return 1

    print(f"  第 2 轮成功 ✅ 用量: {second.usage}")
    print(f"  回复: {(second.content or '')[:120] or '(空)'}")
    print(f"  工具调用: {[c.name for c in second.tool_calls] or '无(纯文本)'}")
    print("\n结论:配置正常,模型支持多轮结构化工具调用 —— 可以跑 python agent/agent.py --policy llm")
    return 0


if __name__ == "__main__":
    sys.exit(main())
