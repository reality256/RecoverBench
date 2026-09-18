"""LLM 策略:构造上下文 → 请求模型 → 解析工具调用或结束结论。

模型每轮能看到:故障症状(任务描述)、必要的服务拓扑、工具描述、
之前的工具调用与结果、剩余预算。

模型看不到:场景 YAML、注入/恢复脚本、expected.root_cause、裁判内部答案、
任何包含答案的场景名称。任务描述对正常环境和故障环境完全相同。

安全:模型输出只被解析成"工具调用"或"结束结论",绝不会被当作 Python/shell 执行;
工具名必须在注册表内、参数必须过分发器校验。
"""

import hashlib
import json
import time
from typing import List, Optional

from agent.dispatch import build_tool_schemas
from agent.state import Finish, HistoryEntry, ToolCall

FINISH_TOOL_NAME = "finish"

# 默认任务描述:正常对照场景与故障场景使用同一段文字,不含任何答案
DEFAULT_TASK = (
    "请检查该服务是否正常。如存在故障,请根据工具观测进行调查并尝试恢复。"
    "修改后验证服务状态,证据不足或无法处理时如实结束。"
)

ALLOWED_CONCLUSIONS = ("healthy", "recovered", "unresolved", "failed")

FINISH_TOOL = {
    "type": "function",
    "function": {
        "name": FINISH_TOOL_NAME,
        "description": "结束本次排查,给出结论与证据。不再需要观测或动作时调用。",
        "parameters": {
            "type": "object",
            "properties": {
                "conclusion": {
                    "type": "string",
                    "enum": list(ALLOWED_CONCLUSIONS),
                    "description": (
                        "healthy=检查后一切正常,未做任何修改;"
                        "recovered=发现故障并已恢复;"
                        "unresolved=证据不足或超出能力范围,未解决;"
                        "failed=尝试修复但未成功。"
                    ),
                },
                "evidence": {
                    "type": "string",
                    "description": "一句话证据,引用你实际观测到的工具结果。",
                },
            },
            "required": ["conclusion", "evidence"],
            "additionalProperties": False,
        },
    },
}


def build_system_prompt(task: str = DEFAULT_TASK) -> str:
    return f"""你是运维排查助手,在一个隔离的演练环境中工作。

# 任务
{task}

# 环境拓扑
- api:HTTP 服务,监听本机 8000 端口,提供 /health(健康探测)与 /cache/{{key}} 接口。
- redis:api 依赖的数据服务。api 的接口能否正常工作,取决于 redis 是否可用。

# 工具
- 观测工具:查看 API 健康状态、查看服务运行状态、查看服务日志。
- 行动工具:启动已停止的服务(有权限限制,超出范围会被拒绝)。
- 结束时必须调用 finish 工具给出结论和证据。

# 规则
- 每次只调用一个工具,拿到结果后再决定下一步。
- 结论必须有你实际观测到的证据支撑,不要凭猜测下结论。
- 证据不足或超出能力范围时,如实选择 unresolved 或 failed,不要谎报成功。
- 只能使用上面注册过的工具;不要输出命令、脚本或代码要求执行。
"""


def prompt_version(task: str = DEFAULT_TASK) -> str:
    """prompt 版本号:内容哈希前 8 位,便于比较不同 prompt 的运行结果。"""
    digest = hashlib.sha256(build_system_prompt(task).encode("utf-8")).hexdigest()[:8]
    return f"llm-v1-{digest}"


def _short(text: str, limit: int = 300) -> str:
    text = (text or "").strip().replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + "..."


class LlmPolicy:
    """模型策略:每次 decide 请求模型一次(必要时重试/提醒一次)。"""

    def __init__(self, client, budget, recorder=None, task: str = DEFAULT_TASK, config=None):
        self.client = client
        self.budget = budget
        self.recorder = recorder
        self.task = task
        self.config = config
        self.system_prompt = build_system_prompt(task)
        self._current_step = 1  # 供轨迹记录用:当前是循环的第几步

    # ---------- 上下文构造 ----------
    def _budget_note(self) -> str:
        return (
            f"当前状态:已用工具调用 {self.budget.tool_calls}/{self.budget.max_tool_calls},"
            f"已用环境修改 {self.budget.modifications}/{self.budget.max_modifications},"
            f"剩余时间约 {self.budget.remaining_seconds():.0f} 秒。请继续。"
        )

    def _build_messages(self, history: List[HistoryEntry]) -> List[dict]:
        messages = [{"role": "system", "content": self.system_prompt}]
        for entry in history:
            call_id = f"call_{entry.step}"
            messages.append({
                "role": "assistant",
                "content": entry.decision.note or "",
                "tool_calls": [{
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": entry.decision.name,
                        "arguments": json.dumps(entry.decision.args, ensure_ascii=False),
                    },
                }],
            })
            messages.append({
                "role": "tool",
                "tool_call_id": call_id,
                "content": json.dumps(entry.result, ensure_ascii=False),
            })
        messages.append({"role": "user", "content": self._budget_note()})
        return messages

    # ---------- 请求模型 ----------
    def _ask(self, messages, tools) -> tuple:
        """请求模型,失败时最多重试一次(计入预算)。返回 (response, finish_or_none)。"""
        retried = False
        while True:
            self.budget.consume_model_call()
            started = time.monotonic()
            timeout = self.budget.model_timeout(self.config.timeout if self.config else None)
            response = self.client.chat(messages, tools=tools, timeout=timeout)
            duration_ms = int((time.monotonic() - started) * 1000)

            if self.recorder is not None:
                self.recorder.log_model_call(
                    step=self._current_step, model=getattr(self.config, "model", None),
                    duration_ms=duration_ms, usage=response.usage, retry=retried,
                )

            if response.ok:
                return response, None

            if response.retryable and not retried:
                self.budget.consume_model_retry()   # 预算用尽会抛 BudgetExceeded,由 loop 结束
                retried = True
                continue
            return None, Finish("agent_error", f"模型请求失败: {response.error}")

    # ---------- 决策 ----------
    def decide(self, history: List[HistoryEntry], available_tools: dict):
        self._current_step = len(history) + 1
        messages = self._build_messages(history)
        tools = build_tool_schemas(available_tools) + [FINISH_TOOL]

        response, failure = self._ask(messages, tools)
        if failure is not None:
            return failure

        # 只回了文本、没有工具调用 → 提醒一次
        if not response.tool_calls and response.content:
            messages.append({"role": "assistant", "content": response.content})
            messages.append({
                "role": "user",
                "content": "请使用工具调用回复:需要信息就调用观测工具,结束时调用 finish 工具。",
            })
            response, failure = self._ask(messages, tools)
            if failure is not None:
                return failure

        if not response.tool_calls:
            return Finish("agent_error", f"模型未返回工具调用: {_short(response.content) or '内容为空'}")

        return self._parse(response)

    def _parse(self, response):
        """把模型的第一个工具调用翻译成 Decision。"""
        call = response.tool_calls[0]
        raw_arguments = call.arguments or ""
        note = _short(response.content, 200)

        try:
            arguments = json.loads(raw_arguments) if raw_arguments.strip() else {}
        except ValueError:
            arguments = None

        if call.name == FINISH_TOOL_NAME:
            if not isinstance(arguments, dict):
                return Finish("agent_error", f"finish 参数无法解析: {_short(raw_arguments)}")
            conclusion = arguments.get("conclusion")
            if conclusion not in ALLOWED_CONCLUSIONS:
                return Finish("agent_error", f"finish 的 conclusion 非法: {conclusion!r}")
            return Finish(conclusion, f"[模型] {_short(arguments.get('evidence', ''))}")

        if not isinstance(arguments, dict):
            # 参数不是合法 JSON:交给分发器拒绝,错误信息里带上原文让模型自我修正
            return ToolCall(call.name, {"_raw_arguments": _short(raw_arguments)}, note=note)

        return ToolCall(call.name, arguments, note=note)
