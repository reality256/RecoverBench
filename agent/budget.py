"""预算控制:工具调用次数、环境修改次数、整轮时间。

三个要点:
- 整轮时间用 time.monotonic()(单调时钟)计算,不受系统时钟调整影响。
- 所有超时都必须经过剩余时间约束(tool_timeout / model_timeout),
  否则一次阻塞调用就能突破整轮预算。
- 任何请求工具的决策都消耗工具调用预算,非法调用同样消耗 ——
  这样策略持续输出无效参数时,循环仍会被预算终止。
"""

import time

# 建议初始预算
DEFAULT_MAX_TOOL_CALLS = 10
DEFAULT_MAX_MODIFICATIONS = 2
DEFAULT_MAX_SECONDS = 120.0
DEFAULT_MODEL_TIMEOUT = 30.0
DEFAULT_MAX_MODEL_RETRIES = 1

# 超时下限:哪怕剩余时间很少,也留一点执行余量而不是传 0 或负数
MIN_TIMEOUT = 0.1


class BudgetExceeded(Exception):
    """预算用尽(次数或时间)。"""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class Budget:
    """一轮运行的预算账本。"""

    def __init__(
        self,
        max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS,
        max_modifications: int = DEFAULT_MAX_MODIFICATIONS,
        max_seconds: float = DEFAULT_MAX_SECONDS,
        model_timeout: float = DEFAULT_MODEL_TIMEOUT,
        max_model_retries: int = DEFAULT_MAX_MODEL_RETRIES,
    ):
        self.max_tool_calls = max_tool_calls
        self.max_modifications = max_modifications
        self.max_seconds = max_seconds
        self.model_timeout_limit = model_timeout
        self.max_model_retries = max_model_retries

        self.tool_calls = 0
        self.modifications = 0
        self.model_calls = 0
        self.model_retries = 0

        self._start = time.monotonic()

    # ---------- 时间(单调时钟)----------
    def elapsed_seconds(self) -> float:
        return time.monotonic() - self._start

    def remaining_seconds(self) -> float:
        return max(0.0, self.max_seconds - self.elapsed_seconds())

    def has_time_left(self) -> bool:
        return self.elapsed_seconds() < self.max_seconds

    def check_time(self) -> None:
        if not self.has_time_left():
            raise BudgetExceeded(f"整轮时间预算用完({self.max_seconds:.0f}s)")

    # ---------- 计数 ----------
    def consume_decision(self) -> None:
        """每次请求工具的决策消耗一次工具调用预算(非法调用也消耗)。"""
        if self.tool_calls >= self.max_tool_calls:
            raise BudgetExceeded(f"工具调用预算用完({self.max_tool_calls} 次)")
        self.tool_calls += 1

    def consume_modification(self) -> None:
        """每次真正要执行的修改动作消耗一次修改预算。"""
        if self.modifications >= self.max_modifications:
            raise BudgetExceeded(f"环境修改预算用完({self.max_modifications} 次)")
        self.modifications += 1

    def consume_model_call(self) -> None:
        """一次模型调用。"""
        self.check_time()
        self.model_calls += 1

    def consume_model_retry(self) -> None:
        """一次模型重试(最多 1 次,计入整轮预算)。"""
        if self.model_retries >= self.max_model_retries:
            raise BudgetExceeded(f"模型重试预算用完({self.max_model_retries} 次)")
        self.model_retries += 1

    # ---------- 超时(一律受剩余时间约束)----------
    def tool_timeout(self, requested: float) -> float:
        self.check_time()
        return max(MIN_TIMEOUT, min(float(requested), self.remaining_seconds()))

    def model_timeout(self, requested: float = None) -> float:
        self.check_time()
        if requested is None:
            requested = self.model_timeout_limit
        return max(MIN_TIMEOUT, min(float(requested), self.remaining_seconds()))

    # ---------- 快照(写进 config.json / result.json)----------
    def snapshot(self) -> dict:
        return {
            "max_tool_calls": self.max_tool_calls,
            "max_modifications": self.max_modifications,
            "max_seconds": self.max_seconds,
            "model_timeout": self.model_timeout_limit,
            "max_model_retries": self.max_model_retries,
            "used_tool_calls": self.tool_calls,
            "used_modifications": self.modifications,
            "used_model_calls": self.model_calls,
            "used_model_retries": self.model_retries,
            "elapsed_seconds": round(self.elapsed_seconds(), 3),
        }
