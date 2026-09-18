"""公共运行循环:策略负责"选下一步",循环负责"分发、记账、预算、记录轨迹"。

以后接 LLM 时,工具分发、历史记录、预算控制、轨迹记录全部复用这一套代码,
策略只是被替换的零件。
"""

import time
from typing import Dict, List, Optional, Tuple

from agent.budget import Budget, BudgetExceeded
from agent.dispatch import dispatch
from agent.state import Finish, HistoryEntry, ToolCall

MAX_STEPS = 10


def run_loop(
    policy,
    tools_registry: Dict,
    budget: Optional[Budget] = None,
    recorder=None,
    max_steps: Optional[int] = None,
) -> Tuple[Finish, List[HistoryEntry]]:
    """跑一轮 决策→分发→记录 循环,返回 (最终结论, 完整历史)。

    - 策略返回 ToolCall → 经分发器执行,结果记入历史,进入下一轮
    - 策略返回 Finish  → 立即结束
    - 策略返回未知类型 / 策略抛异常 → agent_error
    - 分发器拒绝(未注册/参数非法) → 记为失败结果,策略可继续(预算照扣)
    - 预算耗尽(次数或时间) → budget_exceeded
    """
    budget = budget if budget is not None else Budget()
    hard_cap = max_steps if max_steps is not None else budget.max_tool_calls
    history: List[HistoryEntry] = []

    def finish(conclusion: str, evidence: str) -> Tuple[Finish, List[HistoryEntry]]:
        # 结论的展示留给入口(agent.py),这里只负责记录轨迹
        decision = Finish(conclusion=conclusion, evidence=evidence)
        if recorder is not None:
            recorder.log_finish(conclusion, evidence, len(history), budget.snapshot())
        return decision, history

    for step in range(1, hard_cap + 1):
        # 时间预算:轮到决策前先看一眼,别让一次阻塞调用悄悄突破总预算
        if not budget.has_time_left():
            return finish("budget_exceeded", f"整轮时间预算用完({budget.max_seconds:.0f}s)")

        try:
            decision = policy.decide(history, tools_registry)
        except Exception as e:
            return finish("agent_error", f"策略异常: {type(e).__name__}: {e}")

        if isinstance(decision, Finish):
            print(f"[{step}] 策略结束 → {decision.conclusion}: {decision.evidence}")
            if recorder is not None:
                recorder.log_decision(step, decision)
            return finish(decision.conclusion, decision.evidence)

        if not isinstance(decision, ToolCall):
            return finish("agent_error", f"策略返回了未知决策类型: {type(decision).__name__}")

        if recorder is not None:
            recorder.log_decision(step, decision)

        if decision.wait_seconds > 0:
            time.sleep(decision.wait_seconds)

        started = time.monotonic()
        try:
            result = dispatch(decision, tools_registry, budget)
        except BudgetExceeded as e:
            if recorder is not None:
                recorder.log_tool_result(step, decision.name, decision.args, {"ok": False, "data": None, "error": e.reason},
                                         int((time.monotonic() - started) * 1000))
            return finish("budget_exceeded", e.reason)
        duration_ms = int((time.monotonic() - started) * 1000)

        history.append(HistoryEntry(step=step, decision=decision, result=result))
        print(f"[{step}] {decision.name}({decision.args}) → {result}")
        if recorder is not None:
            recorder.log_tool_result(step, decision.name, decision.args, result, duration_ms)

    if max_steps is not None:
        return finish("agent_error", f"超过最大步数 {max_steps}")
    return finish("budget_exceeded", f"工具调用预算用完({budget.max_tool_calls} 次)")
