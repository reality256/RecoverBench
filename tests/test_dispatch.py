"""agent/dispatch.py 的单元测试:四道检查 + 预算扣减 + 超时夹取。

重点验证:非法参数绝不执行命令;非法调用照样消耗决策预算。

运行:
    python -m unittest discover -s tests -v
"""

import unittest
from unittest.mock import MagicMock

from agent import tools
from agent.budget import Budget, BudgetExceeded
from agent.dispatch import ARG_SPECS, dispatch
from agent.state import ToolCall


def registry_with(**funcs):
    return dict(funcs)


class TestRegistryHygiene(unittest.TestCase):
    def test_every_registered_tool_has_arg_spec(self):
        # 新增工具必须登记参数表,否则参数校验会被跳过 —— 有测试守着
        missing = set(tools.TOOL_REGISTRY) - set(ARG_SPECS)
        self.assertEqual(missing, set(), f"这些工具缺参数表: {missing}")


class TestExistenceCheck(unittest.TestCase):
    def test_unknown_tool_rejected(self):
        b = Budget()
        result = dispatch(ToolCall("not_registered"), {}, b)
        self.assertFalse(result["ok"])
        self.assertIn("未注册", result["error"])

    def test_unknown_tool_still_consumes_budget(self):
        # 非法调用也要消耗决策预算
        b = Budget()
        dispatch(ToolCall("not_registered"), {}, b)
        self.assertEqual(b.tool_calls, 1)


class TestArgumentValidation(unittest.TestCase):
    def setUp(self):
        self.budget = Budget()
        self.func = MagicMock(return_value={"ok": True, "data": {}, "error": None})
        self.registry = {"start_service": self.func}

    def _dispatch(self, **kwargs):
        return dispatch(ToolCall("start_service", kwargs), self.registry, self.budget)

    def test_argument_validation_never_executes(self):
        cases = [
            {},                                   # 缺必需参数
            {"service": "api"},                   # 不在白名单
            {"service": 123},                     # 类型错
            {"service": "redis", "extra": 1},     # 多余参数
            {"service": "redis", "timeout": -1},  # 超时非法
            {"service": "redis", "timeout": True},
        ]
        for args in cases:
            with self.subTest(args=args):
                self.func.reset_mock()
                result = dispatch(ToolCall("start_service", args), self.registry, self.budget)
                self.assertFalse(result["ok"], f"{args} 应被拒绝")
                self.func.assert_not_called()

    def test_non_dict_args_rejected(self):
        result = dispatch(ToolCall("start_service", "redis"), self.registry, self.budget)
        self.assertFalse(result["ok"])
        self.assertIn("必须是对象", result["error"])
        self.func.assert_not_called()

    def test_valid_args_execute(self):
        result = self._dispatch(service="redis")
        self.assertTrue(result["ok"])
        self.func.assert_called_once()

    def test_logs_tail_range(self):
        func = MagicMock(return_value={"ok": True, "data": {}, "error": None})
        registry = {"get_service_logs": func}
        for tail, expected_ok in [(1, True), (100, True), (0, False), (101, False), (True, False), ("50", False)]:
            with self.subTest(tail=tail):
                func.reset_mock()
                result = dispatch(ToolCall("get_service_logs", {"service": "redis", "tail": tail}), registry, Budget())
                self.assertEqual(result["ok"], expected_ok)
                if not expected_ok:
                    func.assert_not_called()


class TestModificationBudget(unittest.TestCase):
    def test_modification_consumed_on_execution(self):
        func = MagicMock(return_value={"ok": True, "data": {}, "error": None})
        b = Budget(max_modifications=2)
        for _ in range(2):
            dispatch(ToolCall("start_service", {"service": "redis"}), {"start_service": func}, b)
        self.assertEqual(b.modifications, 2)
        with self.assertRaises(BudgetExceeded):
            dispatch(ToolCall("start_service", {"service": "redis"}), {"start_service": func}, b)

    def test_readonly_tools_do_not_consume_modification_budget(self):
        func = MagicMock(return_value={"ok": True, "data": {}, "error": None})
        b = Budget()
        dispatch(ToolCall("get_service_status", {"service": "redis"}), {"get_service_status": func}, b)
        self.assertEqual(b.modifications, 0)

    def test_rejected_modification_does_not_consume_modification_budget(self):
        func = MagicMock()
        b = Budget()
        dispatch(ToolCall("start_service", {"service": "api"}), {"start_service": func}, b)
        self.assertEqual(b.modifications, 0)  # 被拒绝 = 没动环境
        self.assertEqual(b.tool_calls, 1)     # 但决策预算照扣


class TestTimeoutInjection(unittest.TestCase):
    def test_timeout_injected_and_clamped(self):
        seen = {}

        def fake(service, timeout=None):
            seen["timeout"] = timeout
            return {"ok": True, "data": {}, "error": None}

        b = Budget(max_seconds=100)
        dispatch(ToolCall("get_service_status", {"service": "redis"}), {"get_service_status": fake}, b)
        # 工具默认超时是 10 秒,预算充足 → 传 10
        self.assertEqual(seen["timeout"], tools.DOCKER_STATUS_TIMEOUT)

    def test_policy_supplied_timeout_is_clamped_by_remaining(self):
        seen = {}

        def fake(service, timeout=None):
            seen["timeout"] = timeout
            return {"ok": True, "data": {}, "error": None}

        b = Budget(max_seconds=100)
        # 策略想要 9999 秒,但整轮只剩 100 秒
        dispatch(ToolCall("get_service_status", {"service": "redis", "timeout": 9999}),
                 {"get_service_status": fake}, b)
        self.assertLessEqual(seen["timeout"], 100)

    def test_tool_without_timeout_param_still_works(self):
        # 自定义工具不接受 timeout → 不注入,保持注册表通用
        def plain(service):
            return {"ok": True, "data": {"service": service}, "error": None}

        result = dispatch(ToolCall("get_service_status", {"service": "redis"}), {"get_service_status": plain}, Budget())
        self.assertTrue(result["ok"])


class TestExecutionResult(unittest.TestCase):
    def test_tool_exception_becomes_error(self):
        def boom():
            raise RuntimeError("kaboom")

        result = dispatch(ToolCall("get_api_health"), {"get_api_health": boom}, Budget())
        self.assertFalse(result["ok"])
        self.assertIn("工具异常", result["error"])
        self.assertIn("kaboom", result["error"])

    def test_non_dict_result_becomes_error(self):
        result = dispatch(ToolCall("get_api_health"), {"get_api_health": lambda: "oops"}, Budget())
        self.assertFalse(result["ok"])
        self.assertIn("非 dict", result["error"])


if __name__ == "__main__":
    unittest.main()
