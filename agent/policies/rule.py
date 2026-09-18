"""V0 规则策略:完全从 history 推导下一步,无内部可变状态。

行为(与 V0 一致):
- 健康时结束,不修改环境。
- Redis 确认停止时启动它(只启动一次)。
- Redis 正在运行但 API 异常时报告无法解决。
- 修复后最多验证 MAX_VERIFY 次,每次间隔 VERIFY_INTERVAL 秒。
"""

from typing import List

from agent.state import Finish, HistoryEntry, ToolCall

MAX_VERIFY = 5        # 修复后最多验证次数
VERIFY_INTERVAL = 1   # 每次验证前的等待秒数


def _is_healthy(result: dict) -> bool:
    """API 完全健康 = 查询成功且 status、redis 都是 healthy。"""
    if not result.get("ok"):
        return False
    data = result.get("data") or {}
    return data.get("status") == "healthy" and data.get("redis") == "healthy"


def _started(history: List[HistoryEntry]) -> bool:
    """是否已经成功执行过启动动作。"""
    return any(
        entry.decision.name == "start_service" and entry.result.get("ok")
        for entry in history
    )


def _verify_count(history: List[HistoryEntry]) -> int:
    """修复后已执行的验证次数(启动成功之后的 get_api_health 次数)。"""
    started = False
    count = 0
    for entry in history:
        if entry.decision.name == "start_service" and entry.result.get("ok"):
            started = True
        elif entry.decision.name == "get_api_health" and started:
            count += 1
    return count


class RulePolicy:
    """规则策略:观察 → 诊断 → 行动 → 验证。"""

    def decide(self, history, available_tools):
        if not history:
            return ToolCall("get_api_health")

        last = history[-1]
        result = last.result

        # ========== 修复前:观察与诊断 ==========
        if not _started(history):
            if last.decision.name == "get_api_health":
                if _is_healthy(result):
                    return Finish("healthy", "API 与 Redis 均健康,无需动作")
                # 降级或 API 不可访问:都先查 Redis 状态
                return ToolCall("get_service_status", {"service": "redis"})

            if last.decision.name == "get_service_status":
                if not result.get("ok"):
                    return Finish("agent_error", f"查询 Redis 状态失败: {result.get('error')}")
                data = result.get("data") or {}
                if data.get("running") is True:
                    return Finish("unresolved", "Redis 在运行,故障另有原因,超出 V0 能力范围")
                if data.get("running") is False:
                    return ToolCall("start_service", {"service": "redis"})
                # 状态未知:不贸然动手
                return Finish("unresolved", f"Redis 状态未知({data.get('state')}),不贸然动手")

            if last.decision.name == "start_service":
                if not result.get("ok"):
                    return Finish("agent_error", f"启动 Redis 失败: {result.get('error')}")
                return ToolCall("get_api_health", wait_seconds=VERIFY_INTERVAL)

            # 兜底:其它任何情况回到观察
            return ToolCall("get_api_health")

        # ========== 修复后:验证阶段 ==========
        if last.decision.name == "get_api_health":
            if _is_healthy(result):
                return Finish("recovered", f"启动后第 {_verify_count(history)} 次验证,API 恢复健康")
            if _verify_count(history) >= MAX_VERIFY:
                return Finish("failed", f"验证 {MAX_VERIFY} 次仍未恢复")
            return ToolCall("get_api_health", wait_seconds=VERIFY_INTERVAL)

        if last.decision.name == "start_service":
            return ToolCall("get_api_health", wait_seconds=VERIFY_INTERVAL)

        return ToolCall("get_api_health")
