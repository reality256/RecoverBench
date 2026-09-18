"""策略与循环的单元测试:RulePolicy 的决策表 + run_loop 的公共机制。

运行:
    python -m unittest discover -s tests -v
"""

import contextlib
import io
import unittest
from unittest.mock import MagicMock, patch

from agent import budget as budget_mod
from agent import loop as loop_mod
from agent.policies.rule import MAX_VERIFY, VERIFY_INTERVAL, RulePolicy
from agent.state import Finish, HistoryEntry, ToolCall


# ---------- 造数据的小工具 ----------

def hres(status, redis, ok=True):
    """get_api_health 的结果。"""
    return {"ok": ok, "data": {"status": status, "redis": redis}, "error": None if ok else "boom"}


def sres(running, state="exited", ok=True):
    """get_service_status 的结果。"""
    return {"ok": ok, "data": {"service": "redis", "running": running, "state": state}, "error": None if ok else "boom"}


def start_res(ok=True):
    return {"ok": ok, "data": {"service": "redis", "action": "docker compose start redis"}, "error": None if ok else "boom"}


def entry(name, result, args=None, wait=0.0):
    return HistoryEntry(step=1, decision=ToolCall(name=name, args=args or {}, wait_seconds=wait), result=result)


def decide_quietly(policy, history):
    with contextlib.redirect_stdout(io.StringIO()):
        return policy.decide(history, {})


# ---------- RulePolicy 决策表 ----------

class TestRulePolicy(unittest.TestCase):
    def setUp(self):
        self.policy = RulePolicy()

    def test_empty_history_observes_api(self):
        d = decide_quietly(self.policy, [])
        self.assertIsInstance(d, ToolCall)
        self.assertEqual(d.name, "get_api_health")

    def test_healthy_finishes_without_action(self):
        history = [entry("get_api_health", hres("healthy", "healthy"))]
        d = decide_quietly(self.policy, history)
        self.assertIsInstance(d, Finish)
        self.assertEqual(d.conclusion, "healthy")

    def test_degraded_checks_redis(self):
        history = [entry("get_api_health", hres("degraded", "unavailable"))]
        d = decide_quietly(self.policy, history)
        self.assertIsInstance(d, ToolCall)
        self.assertEqual(d.name, "get_service_status")
        self.assertEqual(d.args, {"service": "redis"})

    def test_api_unreachable_still_checks_redis(self):
        history = [entry("get_api_health", hres(None, None, ok=False))]
        d = decide_quietly(self.policy, history)
        self.assertIsInstance(d, ToolCall)
        self.assertEqual(d.name, "get_service_status")

    def test_redis_running_means_unresolved(self):
        history = [
            entry("get_api_health", hres("degraded", "unavailable")),
            entry("get_service_status", sres(True, "running")),
        ]
        d = decide_quietly(self.policy, history)
        self.assertIsInstance(d, Finish)
        self.assertEqual(d.conclusion, "unresolved")

    def test_redis_exited_starts_it(self):
        history = [
            entry("get_api_health", hres("degraded", "unavailable")),
            entry("get_service_status", sres(False, "exited")),
        ]
        d = decide_quietly(self.policy, history)
        self.assertIsInstance(d, ToolCall)
        self.assertEqual(d.name, "start_service")
        self.assertEqual(d.args, {"service": "redis"})

    def test_redis_unknown_state_does_not_act(self):
        # 未知状态(running=None):不贸然动手,报 unresolved
        history = [
            entry("get_api_health", hres("degraded", "unavailable")),
            entry("get_service_status", sres(None, "restarting")),
        ]
        d = decide_quietly(self.policy, history)
        self.assertIsInstance(d, Finish)
        self.assertEqual(d.conclusion, "unresolved")

    def test_status_query_failed_is_agent_error(self):
        history = [
            entry("get_api_health", hres("degraded", "unavailable")),
            entry("get_service_status", sres(None, "exited", ok=False)),
        ]
        d = decide_quietly(self.policy, history)
        self.assertIsInstance(d, Finish)
        self.assertEqual(d.conclusion, "agent_error")

    def test_start_failed_is_agent_error(self):
        history = [
            entry("get_api_health", hres("degraded", "unavailable")),
            entry("get_service_status", sres(False, "exited")),
            entry("start_service", start_res(ok=False)),
        ]
        d = decide_quietly(self.policy, history)
        self.assertIsInstance(d, Finish)
        self.assertEqual(d.conclusion, "agent_error")

    def test_after_start_verifies_with_wait(self):
        history = [
            entry("get_api_health", hres("degraded", "unavailable")),
            entry("get_service_status", sres(False, "exited")),
            entry("start_service", start_res(ok=True)),
        ]
        d = decide_quietly(self.policy, history)
        self.assertIsInstance(d, ToolCall)
        self.assertEqual(d.name, "get_api_health")
        self.assertEqual(d.wait_seconds, VERIFY_INTERVAL)

    def test_verify_success_recovers(self):
        history = [
            entry("get_api_health", hres("degraded", "unavailable")),
            entry("get_service_status", sres(False, "exited")),
            entry("start_service", start_res(ok=True)),
            entry("get_api_health", hres("healthy", "healthy")),
        ]
        d = decide_quietly(self.policy, history)
        self.assertIsInstance(d, Finish)
        self.assertEqual(d.conclusion, "recovered")

    def test_verify_fails_exactly_max_times(self):
        history = [
            entry("get_api_health", hres("degraded", "unavailable")),
            entry("get_service_status", sres(False, "exited")),
            entry("start_service", start_res(ok=True)),
        ]
        # 前 MAX_VERIFY-1 次验证失败 → 继续验证
        for _ in range(MAX_VERIFY - 1):
            d = decide_quietly(self.policy, history)
            self.assertIsInstance(d, ToolCall, "前几次验证失败应继续验证")
            history.append(entry("get_api_health", hres("degraded", "unavailable")))
        # 第 MAX_VERIFY 次仍失败 → failed
        history.append(entry("get_api_health", hres("degraded", "unavailable")))
        d = decide_quietly(self.policy, history)
        self.assertIsInstance(d, Finish)
        self.assertEqual(d.conclusion, "failed")

    def test_only_one_repair_even_if_verify_fails(self):
        # 验证失败时策略绝不发起第二次 start_service
        history = [
            entry("get_api_health", hres("degraded", "unavailable")),
            entry("get_service_status", sres(False, "exited")),
            entry("start_service", start_res(ok=True)),
            entry("get_api_health", hres("degraded", "unavailable")),
        ]
        d = decide_quietly(self.policy, history)
        self.assertNotEqual(getattr(d, "name", None), "start_service")


# ---------- run_loop 公共机制 ----------

class FakePolicy:
    """按脚本吐出决策的假策略。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def decide(self, history, available_tools):
        if not self.script:
            return Finish("agent_error", "no script left")
        decision = self.script.pop(0)
        self.calls += 1
        if callable(decision):
            return decision(history)
        return decision


def run_quiet(policy, registry, max_steps=None, budget=None):
    with contextlib.redirect_stdout(io.StringIO()):
        return loop_mod.run_loop(policy, registry, budget=budget, max_steps=max_steps)


class TestRunLoop(unittest.TestCase):
    def test_executes_tool_calls_and_records_history(self):
        calls = []
        registry = {"say": lambda text: calls.append(text) or {"ok": True, "data": {"echo": text}, "error": None}}
        policy = FakePolicy([ToolCall("say", {"text": "hello"}), Finish("healthy", "done")])
        finish, history = run_quiet(policy, registry)
        self.assertEqual(finish.conclusion, "healthy")
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].decision.name, "say")
        self.assertEqual(history[0].result["data"]["echo"], "hello")
        self.assertEqual(calls, ["hello"])

    def test_finish_stops_immediately(self):
        registry = {"say": MagicMock()}
        policy = FakePolicy([Finish("unresolved", "stop")])
        finish, history = run_quiet(policy, registry)
        self.assertEqual(finish.conclusion, "unresolved")
        self.assertEqual(history, [])
        registry["say"].assert_not_called()

    def test_unknown_tool_recorded_as_failure(self):
        # 未注册工具不再是致命错误:记为失败结果,策略可以继续(预算照扣)
        policy = FakePolicy([ToolCall("not_registered"), Finish("unresolved", "放弃")])
        finish, history = run_quiet(policy, {})
        self.assertEqual(finish.conclusion, "unresolved")
        self.assertEqual(len(history), 1)
        self.assertFalse(history[0].result["ok"])
        self.assertIn("未注册", history[0].result["error"])

    def test_runaway_invalid_calls_end_by_budget(self):
        # 策略一直输出无效调用 → 被预算终止,而不是无限空转
        budget = budget_mod.Budget(max_tool_calls=3)
        policy = FakePolicy([ToolCall("nope")] * 100)
        finish, history = run_quiet(policy, {}, budget=budget)
        self.assertEqual(finish.conclusion, "budget_exceeded")
        self.assertEqual(len(history), 3)
        self.assertEqual(budget.tool_calls, 3)

    def test_runaway_valid_calls_end_by_budget(self):
        budget = budget_mod.Budget(max_tool_calls=4)
        registry = {"say": lambda: {"ok": True, "data": {}, "error": None}}
        policy = FakePolicy([ToolCall("say")] * 100)
        finish, history = run_quiet(policy, registry, budget=budget)
        self.assertEqual(finish.conclusion, "budget_exceeded")
        self.assertEqual(len(history), 4)

    def test_unknown_decision_type_is_agent_error(self):
        policy = FakePolicy(["garbage"])
        finish, _ = run_quiet(policy, {})
        self.assertEqual(finish.conclusion, "agent_error")
        self.assertIn("未知决策类型", finish.evidence)

    def test_tool_exception_becomes_structured_error(self):
        def boom():
            raise RuntimeError("kaboom")

        policy = FakePolicy([ToolCall("boom"), Finish("unresolved", "after boom")])
        finish, history = run_quiet(policy, {"boom": boom})
        self.assertEqual(finish.conclusion, "unresolved")
        self.assertEqual(len(history), 1)
        self.assertFalse(history[0].result["ok"])
        self.assertIn("kaboom", history[0].result["error"])

    def test_max_steps_caps_loop(self):
        # 策略永远要求调工具 → 循环在第 max_steps 步强制 agent_error
        policy = FakePolicy([ToolCall("say")] * 100)
        finish, history = run_quiet(policy, {"say": lambda: {"ok": True, "data": {}, "error": None}}, max_steps=3)
        self.assertEqual(finish.conclusion, "agent_error")
        self.assertIn("超过最大步数", finish.evidence)
        self.assertEqual(len(history), 3)

    def test_wait_seconds_is_honored(self):
        registry = {"say": lambda: {"ok": True, "data": {}, "error": None}}
        policy = FakePolicy([ToolCall("say", wait_seconds=1), Finish("healthy")])
        with patch.object(loop_mod.time, "sleep") as mock_sleep:
            run_quiet(policy, registry)
        mock_sleep.assert_called_once_with(1)


if __name__ == "__main__":
    unittest.main()
