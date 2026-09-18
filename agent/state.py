"""Agent 的状态、决策与历史类型(V1)。

V1 起,工具返回值统一为可序列化 dict(见 tools.py 的约定):
    {"ok": bool, "data": dict | None, "error": str | None}
其中 ok=True 只表示"查询/动作成功",不表示系统健康。

这里的类型只服务于循环和策略:决策(Decision)、历史(HistoryEntry)。
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Union


@dataclass
class ToolCall:
    """调用一个工具。"""
    name: str
    args: Dict[str, object] = field(default_factory=dict)
    # 执行前等待秒数(策略用它控制节奏,如验证间隔;V0 规则策略使用)
    wait_seconds: float = 0.0
    # 决策者的一句话说明(模型策略填模型可见输出,规则策略留空);只记摘要,不进"思考过程"
    note: str = ""


@dataclass
class Finish:
    """结束循环。

    conclusion 取值:healthy / recovered / unresolved / failed / agent_error
    evidence    :一句话说明依据(供复盘)。
    """
    conclusion: str
    evidence: str = ""


# 决策只有两种:调工具,或结束
Decision = Union[ToolCall, Finish]


@dataclass
class HistoryEntry:
    """循环中一步的完整记录:决策 + 工具结果,可序列化、可回放。"""
    step: int
    decision: ToolCall
    result: Dict[str, object]
