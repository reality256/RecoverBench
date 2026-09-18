"""agent/budget.py 的单元测试:次数上限、时间预算(单调时钟)、超时夹取。

运行:
    python -m unittest discover -s tests -v
"""

import unittest
from unittest.mock import patch

from agent import budget as budget_mod
from agent.budget import Budget, BudgetExceeded


class FakeClock:
    """可控的单调时钟。"""

    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class TestBudgetCounters(unittest.TestCase):
    def test_tool_call_budget(self):
        b = Budget(max_tool_calls=2)
        b.consume_decision()
        b.consume_decision()
        self.assertEqual(b.tool_calls, 2)
        with self.assertRaises(BudgetExceeded) as ctx:
            b.consume_decision()
        self.assertIn("工具调用预算用完", ctx.exception.reason)

    def test_modification_budget(self):
        b = Budget(max_modifications=2)
        b.consume_modification()
        b.consume_modification()
        with self.assertRaises(BudgetExceeded) as ctx:
            b.consume_modification()
        self.assertIn("环境修改预算用完", ctx.exception.reason)

    def test_model_retry_budget_is_one(self):
        b = Budget(max_model_retries=1)
        b.consume_model_retry()
        with self.assertRaises(BudgetExceeded) as ctx:
            b.consume_model_retry()
        self.assertIn("模型重试预算用完", ctx.exception.reason)

    def test_model_call_counts(self):
        b = Budget()
        b.consume_model_call()
        b.consume_model_call()
        self.assertEqual(b.model_calls, 2)


class TestBudgetTime(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.patcher = patch.object(budget_mod.time, "monotonic", self.clock)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def test_remaining_decreases_with_clock(self):
        b = Budget(max_seconds=100)
        self.assertAlmostEqual(b.remaining_seconds(), 100, places=3)
        self.clock.advance(30)
        self.assertAlmostEqual(b.remaining_seconds(), 70, places=3)

    def test_remaining_never_negative(self):
        b = Budget(max_seconds=10)
        self.clock.advance(999)
        self.assertEqual(b.remaining_seconds(), 0.0)
        self.assertFalse(b.has_time_left())

    def test_tool_timeout_clamped_by_remaining(self):
        b = Budget(max_seconds=100)
        self.clock.advance(95)
        # 工具想要 30 秒,但只剩 5 秒 → 只能给 5 秒
        self.assertAlmostEqual(b.tool_timeout(30), 5, places=3)

    def test_tool_timeout_uses_requested_when_smaller(self):
        b = Budget(max_seconds=100)
        self.assertAlmostEqual(b.tool_timeout(3), 3, places=3)

    def test_tool_timeout_raises_when_time_up(self):
        b = Budget(max_seconds=10)
        self.clock.advance(10)
        with self.assertRaises(BudgetExceeded):
            b.tool_timeout(5)

    def test_model_timeout_clamped_by_remaining(self):
        b = Budget(max_seconds=100, model_timeout=30)
        self.assertAlmostEqual(b.model_timeout(), 30, places=3)
        self.clock.advance(85)
        self.assertAlmostEqual(b.model_timeout(), 15, places=3)

    def test_timeout_has_minimum(self):
        # 剩余时间几乎为零时,给一个极小正数而不是 0 或负数
        b = Budget(max_seconds=100)
        self.clock.advance(99.999)
        self.assertGreater(b.tool_timeout(10), 0)

    def test_check_time_raises_after_deadline(self):
        b = Budget(max_seconds=5)
        self.clock.advance(5)
        with self.assertRaises(BudgetExceeded) as ctx:
            b.check_time()
        self.assertIn("时间预算用完", ctx.exception.reason)


class TestSnapshot(unittest.TestCase):
    def test_snapshot_fields(self):
        b = Budget()
        b.consume_decision()
        b.consume_modification()
        snap = b.snapshot()
        self.assertEqual(snap["used_tool_calls"], 1)
        self.assertEqual(snap["used_modifications"], 1)
        self.assertEqual(snap["max_tool_calls"], budget_mod.DEFAULT_MAX_TOOL_CALLS)
        self.assertEqual(snap["max_modifications"], budget_mod.DEFAULT_MAX_MODIFICATIONS)
        self.assertEqual(snap["max_seconds"], budget_mod.DEFAULT_MAX_SECONDS)
        self.assertIn("elapsed_seconds", snap)


if __name__ == "__main__":
    unittest.main()
