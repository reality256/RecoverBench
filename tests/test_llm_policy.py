"""agent/policies/llm.py 的单元测试:上下文构造、决策解析、重试与终止原因。

模型客户端用假客户端替换,不联网。

运行:
    python -m unittest discover -s tests -v
"""

import json
import unittest

from agent.budget import Budget, BudgetExceeded
from agent.llm_client import LlmResponse, ToolCallRequest
from agent.policies.llm import (
    DEFAULT_TASK,
    FINISH_TOOL_NAME,
    LlmPolicy,
    build_system_prompt,
    build_tool_schemas,
)
from agent.state import Finish, HistoryEntry, ToolCall

ALLOWED = {"get_api_health", "get_service_status", "get_service_logs", "start_service"}


class FakeClient:
    """按脚本依次返回响应的假客户端。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def chat(self, messages, tools=None, timeout=None, max_tokens=None):
        self.calls.append({"messages": messages, "tools": tools, "timeout": timeout})
        if not self.responses:
            raise AssertionError("假客户端没有更多脚本响应了")
        return self.responses.pop(0)


def tool_response(name, arguments="{}", content=""):
    return LlmResponse(ok=True, content=content,
                       tool_calls=[ToolCallRequest(id="call_1", name=name, arguments=arguments)],
                       usage={"total_tokens": 10})


def text_response(text):
    return LlmResponse(ok=True, content=text, usage={"total_tokens": 5})


def fail(error="boom", retryable=False):
    return LlmResponse(ok=False, error=error, retryable=retryable)


def make_policy(responses, budget=None, config=None):
    client = FakeClient(responses)
    budget = budget or Budget()
    policy = LlmPolicy(client, budget=budget, config=config)
    return policy, client, budget


class TestContextVisibility(unittest.TestCase):
    """模型能看到什么、不能看到什么。"""

    def test_sees_task_and_topology(self):
        prompt = build_system_prompt()
        self.assertIn(DEFAULT_TASK, prompt)
        self.assertIn("api", prompt)
        self.assertIn("redis", prompt)     # 服务拓扑:允许
        self.assertIn("8000", prompt)

    def test_does_not_leak_answers(self):
        policy, _, _ = make_policy([])
        messages = policy._build_messages([])
        blob = json.dumps(messages, ensure_ascii=False).lower()
        for forbidden in ("root_cause", "redis_down", "inject_fault", "reset_env",
                          "scenarios", "expected", "yaml", "yaml"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, blob)

    def test_same_task_for_normal_and_fault_scenario(self):
        # 正常对照场景与故障场景使用同一段任务描述(策略不区分场景)
        policy_a, _, _ = make_policy([])
        policy_b, _, _ = make_policy([])
        self.assertEqual(policy_a.service_prompt if hasattr(policy_a, "service_prompt") else policy_a.system_prompt,
                         policy_b.system_prompt)

    def test_budget_is_visible(self):
        budget = Budget(max_tool_calls=10, max_modifications=2)
        budget.consume_decision()
        policy, _, _ = make_policy([], budget=budget)
        messages = policy._build_messages([])
        note = messages[-1]["content"]
        self.assertIn("1/10", note)
        self.assertIn("0/2", note)
        self.assertIn("剩余时间", note)

    def test_history_is_reconstructed_as_pairs(self):
        policy, _, _ = make_policy([])
        history = [HistoryEntry(step=1, decision=ToolCall("get_api_health", note="先看看"),
                                result={"ok": True, "data": {"status": "degraded"}, "error": None})]
        messages = policy._build_messages(history)
        roles = [m["role"] for m in messages]
        self.assertEqual(roles, ["system", "assistant", "tool", "user"])
        assistant = messages[1]
        self.assertEqual(assistant["tool_calls"][0]["function"]["name"], "get_api_health")
        self.assertEqual(assistant["content"], "先看看")
        self.assertEqual(messages[2]["tool_call_id"], assistant["tool_calls"][0]["id"])


class TestToolSchemas(unittest.TestCase):
    def test_schemas_include_env_tools_and_finish(self):
        policy, _, _ = make_policy([tool_response("get_api_health")])
        policy.decide([], {name: (lambda **kw: {}) for name in ALLOWED})
        tools = policy.client.calls[0]["tools"]
        names = [t["function"]["name"] for t in tools]
        self.assertEqual(set(names), ALLOWED | {FINISH_TOOL_NAME})

    def test_schema_reflects_arg_constraints(self):
        policy, _, _ = make_policy([tool_response("get_api_health")])
        policy.decide([], {"get_service_logs": lambda **kw: {}})
        schema = policy.client.calls[0]["tools"][0]["function"]["parameters"]
        self.assertEqual(schema["properties"]["service"]["enum"], ["api", "redis"])
        self.assertEqual(schema["properties"]["tail"]["minimum"], 1)
        self.assertEqual(schema["properties"]["tail"]["maximum"], 100)
        self.assertEqual(schema["required"], ["service"])


class TestDecisionParsing(unittest.TestCase):
    def test_tool_call_becomes_tool_call_decision(self):
        policy, _, _ = make_policy([tool_response("start_service", '{"service": "redis"}', content="我来启动它")])
        decision = policy.decide([], {"start_service": lambda **kw: {}})
        self.assertIsInstance(decision, ToolCall)
        self.assertEqual(decision.name, "start_service")
        self.assertEqual(decision.args, {"service": "redis"})
        self.assertEqual(decision.note, "我来启动它")

    def test_finish_tool_becomes_finish_decision(self):
        args = json.dumps({"conclusion": "recovered", "evidence": "启动后 /health 返回 healthy"})
        policy, _, _ = make_policy([tool_response(FINISH_TOOL_NAME, args)])
        decision = policy.decide([], {"get_api_health": lambda **kw: {}})
        self.assertIsInstance(decision, Finish)
        self.assertEqual(decision.conclusion, "recovered")
        self.assertIn("healthy", decision.evidence)

    def test_invalid_conclusion_is_agent_error(self):
        args = json.dumps({"conclusion": "all_good", "evidence": "x"})
        policy, _, _ = make_policy([tool_response(FINISH_TOOL_NAME, args)])
        decision = policy.decide([], {})
        self.assertIsInstance(decision, Finish)
        self.assertEqual(decision.conclusion, "agent_error")

    def test_multiple_tool_calls_only_first_used(self):
        response = LlmResponse(ok=True, tool_calls=[
            ToolCallRequest("call_1", "get_api_health", "{}"),
            ToolCallRequest("call_2", "start_service", '{"service": "redis"}'),
        ])
        policy, _, _ = make_policy([response])
        decision = policy.decide([], {})
        self.assertEqual(decision.name, "get_api_health")

    def test_broken_arguments_are_passed_to_dispatcher_for_rejection(self):
        policy, _, _ = make_policy([tool_response("start_service", "{not json")])
        decision = policy.decide([], {})
        self.assertIsInstance(decision, ToolCall)
        self.assertIn("_raw_arguments", decision.args)

    def test_unknown_tool_name_is_not_executed_anywhere(self):
        # 模型编出来的工具名照样只是"名字",由分发器拒绝
        policy, _client, budget = make_policy([tool_response("delete_everything")])
        decision = policy.decide([], {"get_api_health": lambda **kw: {}})
        self.assertEqual(decision.name, "delete_everything")
        # 分发器会拒绝它 —— 这里只验证策略没有做任何特殊处理
        from agent.dispatch import dispatch
        result = dispatch(decision, {"get_api_health": lambda **kw: {}}, budget)
        self.assertFalse(result["ok"])
        self.assertIn("未注册", result["error"])


class TestTextOnlyHandling(unittest.TestCase):
    def test_nudges_once_then_uses_tool(self):
        policy, client, _ = make_policy([text_response("看起来 redis 停了"),
                                         tool_response("start_service", '{"service": "redis"}')])
        decision = policy.decide([], {})
        self.assertIsInstance(decision, ToolCall)
        self.assertEqual(len(client.calls), 2)   # 第一次文本 + 提醒后第二次
        # 提醒消息确实带上了模型的原话
        roles = [m["role"] for m in client.calls[1]["messages"]]
        self.assertEqual(roles[-2:], ["assistant", "user"])

    def test_text_only_twice_is_agent_error(self):
        policy, _, _ = make_policy([text_response("嗯"), text_response("还是嗯")])
        decision = policy.decide([], {})
        self.assertIsInstance(decision, Finish)
        self.assertEqual(decision.conclusion, "agent_error")
        self.assertIn("未返回工具调用", decision.evidence)

    def test_empty_response_is_agent_error(self):
        policy, _, _ = make_policy([LlmResponse(ok=True, content="", tool_calls=[])])
        decision = policy.decide([], {})
        self.assertEqual(decision.conclusion, "agent_error")


class TestFailuresAndBudget(unittest.TestCase):
    def test_retryable_failure_retries_once_then_succeeds(self):
        budget = Budget()
        policy, client, _ = make_policy([fail("503 oops", retryable=True), tool_response("get_api_health")],
                                        budget=budget)
        decision = policy.decide([], {})
        self.assertIsInstance(decision, ToolCall)
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(budget.model_retries, 1)
        self.assertEqual(budget.model_calls, 2)

    def test_retry_exhausted_is_agent_error(self):
        policy, _, budget = make_policy([fail("503", retryable=True), fail("503 again", retryable=True)])
        decision = policy.decide([], {})
        self.assertEqual(decision.conclusion, "agent_error")
        self.assertIn("模型请求失败", decision.evidence)
        self.assertEqual(budget.model_retries, 1)

    def test_non_retryable_failure_stops_immediately(self):
        policy, client, budget = make_policy([fail("HTTP 401", retryable=False)])
        decision = policy.decide([], {})
        self.assertEqual(decision.conclusion, "agent_error")
        self.assertIn("HTTP 401", decision.evidence)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(budget.model_retries, 0)

    def test_model_calls_consume_budget(self):
        budget = Budget()
        policy, _, _ = make_policy([tool_response("get_api_health")], budget=budget)
        policy.decide([], {})
        self.assertEqual(budget.model_calls, 1)

    def test_retry_budget_exhaustion_propagates(self):
        budget = Budget(max_model_retries=0)
        policy, _, _ = make_policy([fail("503", retryable=True)], budget=budget)
        with self.assertRaises(BudgetExceeded):
            policy.decide([], {})

    def test_timeout_is_clamped_by_remaining_budget(self):
        budget = Budget(max_seconds=100)
        policy, client, _ = make_policy([tool_response("get_api_health")], budget=budget,
                                        config=type("C", (), {"timeout": 30, "model": "m"})())
        policy.decide([], {})
        self.assertLessEqual(client.calls[0]["timeout"], 100)


if __name__ == "__main__":
    unittest.main()
