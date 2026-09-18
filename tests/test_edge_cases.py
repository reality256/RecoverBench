"""边界与异常场景测试(Step 9)。

覆盖真正容易出问题的地方:
- 模型输出未知工具 / 非法参数 → 不执行真实动作
- 模型反复请求操作 → 预算终止循环
- 模型超时 / 接口异常 → 明确报错且保留轨迹
- 模型直接宣称成功 → 裁判仍独立验证
- 环境准备或注入失败 → 标记无效实验,不混入恢复失败
- 正常场景 → 不发生无必要修改
- RulePolicy 回归 → V0 能力没被重构破坏

模型异常全部用模拟响应,不调用真实模型。

运行:
    python -m unittest discover -s tests -v
"""

import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from agent import tools as real_tools
from agent.budget import Budget
from agent.llm_client import LlmResponse, ToolCallRequest
from agent.loop import run_loop
from agent.policies.llm import FINISH_TOOL_NAME, LlmPolicy
from agent.policies.rule import RulePolicy
from agent.trace import RunRecorder

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 动态加载 runner(scripts/ 不是包)
_spec = importlib.util.spec_from_file_location("run_benchmark", PROJECT_ROOT / "scripts" / "run_benchmark.py")
rb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rb)


# ---------- 工具 ----------

class ScriptedClient:
    """按脚本返回响应的假模型。"""

    def __init__(self, responses):
        self.responses = list(responses)

    def chat(self, messages, tools=None, timeout=None, max_tokens=None):
        if not self.responses:
            raise AssertionError("脚本用完了")
        return self.responses.pop(0)


def tool_response(name, arguments="{}"):
    return LlmResponse(ok=True, tool_calls=[ToolCallRequest("call_1", name, arguments)], usage={})


def finish_response(conclusion="recovered", evidence="我认为修好了"):
    return LlmResponse(ok=True, tool_calls=[ToolCallRequest(
        "call_1", FINISH_TOOL_NAME, json.dumps({"conclusion": conclusion, "evidence": evidence}))], usage={})


def run_quiet(policy, registry, budget=None, recorder=None):
    with contextlib.redirect_stdout(io.StringIO()):
        return run_loop(policy, registry, budget=budget, recorder=recorder)


def spy_registry():
    """四个真实工具名的假实现,用来观察"有没有被执行"。"""
    spies = {name: MagicMock(return_value={"ok": True, "data": {}, "error": None})
             for name in real_tools.TOOL_REGISTRY}
    return spies, spies["start_service"]


# ---------- 模型输出未知工具/非法参数 ----------

class TestModelAnomalies(unittest.TestCase):
    def test_unknown_tool_never_executes_real_action(self):
        spies, start_spy = spy_registry()
        policy = LlmPolicy(ScriptedClient([tool_response("rm_rf_slash"), finish_response("failed", "放弃")]),
                           budget=Budget(), config=None)
        finish, history = run_quiet(policy, spies)
        self.assertFalse(history[0].result["ok"])
        self.assertIn("未注册", history[0].result["error"])
        for spy in spies.values():
            spy.assert_not_called()          # 一个真实动作都没执行
        self.assertEqual(finish.conclusion, "failed")

    def test_invalid_arguments_never_execute_real_action(self):
        spies, start_spy = spy_registry()
        # service 不在白名单 → 分发器拒绝,绝不执行
        policy = LlmPolicy(ScriptedClient([tool_response("start_service", '{"service": "api"}'),
                                           finish_response("failed", "放弃")]),
                           budget=Budget(), config=None)
        _, history = run_quiet(policy, spies)
        self.assertFalse(history[0].result["ok"])
        self.assertIn("参数校验失败", history[0].result["error"])
        start_spy.assert_not_called()

    def test_broken_json_arguments_are_rejected_not_executed(self):
        spies, start_spy = spy_registry()
        policy = LlmPolicy(ScriptedClient([tool_response("start_service", "{不是 JSON"),
                                           finish_response("failed", "放弃")]),
                           budget=Budget(), config=None)
        _, history = run_quiet(policy, spies)
        self.assertFalse(history[0].result["ok"])
        start_spy.assert_not_called()

    def test_real_tools_registry_is_never_touched(self):
        """用真注册表:模型乱来的时候,连 docker 命令都不该发出。"""
        policy = LlmPolicy(ScriptedClient([tool_response("get_service_status", '{"service": "mysql"}'),
                                           finish_response("failed", "放弃")]),
                           budget=Budget(), config=None)
        with patch.object(real_tools.subprocess, "run") as mock_run, \
             patch.object(real_tools.requests, "get") as mock_get:
            _, history = run_quiet(policy, real_tools.TOOL_REGISTRY)
        self.assertFalse(history[0].result["ok"])
        mock_run.assert_not_called()
        mock_get.assert_not_called()


# ---------- 模型反复请求操作 ----------

class TestRunawayTermination(unittest.TestCase):
    def test_repeated_repairs_stop_at_modification_budget(self):
        spies, start_spy = spy_registry()
        policy = LlmPolicy(ScriptedClient([tool_response("start_service", '{"service": "redis"}')] * 20),
                           budget=Budget(max_tool_calls=10, max_modifications=2), config=None)
        finish, history = run_quiet(policy, spies, budget=policy.budget)
        self.assertEqual(finish.conclusion, "budget_exceeded")
        self.assertIn("环境修改预算", finish.evidence)
        self.assertEqual(start_spy.call_count, 2)   # 最多两次,一次都不多

    def test_repeated_invalid_calls_stop_at_tool_budget(self):
        spies, start_spy = spy_registry()
        budget = Budget(max_tool_calls=4)
        policy = LlmPolicy(ScriptedClient([tool_response("nonsense")] * 20), budget=budget, config=None)
        finish, history = run_quiet(policy, spies, budget=budget)
        self.assertEqual(finish.conclusion, "budget_exceeded")
        self.assertEqual(len(history), 4)
        for spy in spies.values():
            spy.assert_not_called()

    def test_budget_snapshot_reflects_reality(self):
        spies, _ = spy_registry()
        budget = Budget(max_tool_calls=3)
        policy = LlmPolicy(ScriptedClient([tool_response("nonsense")] * 10), budget=budget, config=None)
        run_quiet(policy, spies, budget=budget)
        snapshot = budget.snapshot()
        self.assertEqual(snapshot["used_tool_calls"], 3)
        self.assertEqual(snapshot["used_modifications"], 0)


# ---------- 模型超时/接口异常 ----------

class TestModelFailures(unittest.TestCase):
    def test_timeout_reports_clearly_and_keeps_trace(self):
        with tempfile.TemporaryDirectory() as tmp:
            recorder = RunRecorder(run_id="t1", runs_root=Path(tmp))
            budget = Budget()
            client = ScriptedClient([LlmResponse(ok=False, error="请求超时(30s)", retryable=True),
                                     LlmResponse(ok=False, error="请求超时(30s)", retryable=True)])
            policy = LlmPolicy(client, budget=budget, recorder=recorder, config=None)
            recorder.write_config(policy="LlmPolicy", budget=budget.snapshot())
            finish, _ = run_quiet(policy, real_tools.TOOL_REGISTRY, budget=budget, recorder=recorder)
            recorder.write_result(conclusion=finish.conclusion, label="AGENT_ERROR",
                                  evidence=finish.evidence, budget=budget.snapshot())

            self.assertEqual(finish.conclusion, "agent_error")
            self.assertIn("超时", finish.evidence)
            self.assertEqual(budget.model_retries, 1)      # 只重试一次

            events = [json.loads(line) for line in
                      (Path(tmp) / "t1" / "trace.jsonl").read_text(encoding="utf-8").splitlines()]
            kinds = [e["event"] for e in events]
            self.assertIn("model_call", kinds)
            self.assertIn("finish", kinds)
            finish_event = next(e for e in events if e["event"] == "finish")
            self.assertEqual(finish_event["conclusion"], "agent_error")
            self.assertIn("超时", finish_event["evidence"])   # 错误原因留在轨迹里

    def test_non_retryable_error_stops_immediately(self):
        budget = Budget()
        client = ScriptedClient([LlmResponse(ok=False, error="HTTP 401: unauthorized", retryable=False)])
        policy = LlmPolicy(client, budget=budget, config=None)
        finish, history = run_quiet(policy, real_tools.TOOL_REGISTRY, budget=budget)
        self.assertEqual(finish.conclusion, "agent_error")
        self.assertIn("401", finish.evidence)
        self.assertEqual(budget.model_retries, 0)
        self.assertEqual(history, [])   # 没执行任何工具

    def test_infrastructure_failure_never_touches_environment(self):
        with patch.object(real_tools.subprocess, "run") as mock_run, \
             patch.object(real_tools.requests, "get") as mock_get:
            policy = LlmPolicy(ScriptedClient([LlmResponse(ok=False, error="连接失败", retryable=False)]),
                               budget=Budget(), config=None)
            run_quiet(policy, real_tools.TOOL_REGISTRY)
        mock_run.assert_not_called()
        mock_get.assert_not_called()


# ---------- 模型直接宣称成功 ----------

class TestLyingModel(unittest.TestCase):
    def test_agent_claim_recorded_without_touching_environment(self):
        spies, start_spy = spy_registry()
        policy = LlmPolicy(ScriptedClient([finish_response("recovered", "我觉得没问题")]),
                           budget=Budget(), config=None)
        finish, history = run_quiet(policy, spies)
        self.assertEqual(finish.conclusion, "recovered")
        self.assertEqual(history, [])            # 一次工具都没调
        start_spy.assert_not_called()

    @patch("evaluator.evaluate.requests.get")
    def test_judge_fails_despite_agent_claiming_success(self, mock_get):
        """完成标准:模型说成功但业务仍异常 → 裁判明确判失败。"""
        from evaluator import evaluate as ev

        resp = MagicMock(status_code=200)
        resp.json.return_value = {"status": "degraded", "redis": "unavailable"}
        mock_get.return_value = resp

        with patch.object(ev.time, "sleep"):
            verdict = ev.evaluate("race:key", "expected-value", deadline=0)

        self.assertEqual(verdict["verdict"], ev.FAIL)
        self.assertIn("未完成连续", verdict["reason"])

    @patch("evaluator.evaluate.requests.get")
    def test_judge_fails_on_wrong_business_value(self, mock_get):
        from evaluator import evaluate as ev

        def side_effect(url, timeout=None):
            resp = MagicMock(status_code=200)
            if url.endswith("/health"):
                resp.json.return_value = {"status": "healthy", "redis": "healthy"}
            else:
                resp.json.return_value = {"key": "k", "value": "残留的旧值"}
            return resp

        mock_get.side_effect = side_effect
        with patch.object(ev.time, "sleep"):
            verdict = ev.evaluate("k", "本轮应写入的值", max_checks=2)
        self.assertEqual(verdict["verdict"], ev.FAIL)
        self.assertIn("业务读取不一致", verdict["reason"])


# ---------- 环境准备/注入失败 ----------

class TestRunnerInvalidExperiment(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runs_root = Path(self.tmp.name)
        self.scenario = {"id": "test", "fault": {"type": "service_down", "target": "redis"}}

    def _run_round(self):
        return rb.run_round("bench-test", 1, "rule", self.scenario, self.runs_root, 1)

    def test_baseline_failure_skips_agent(self):
        with patch.object(rb, "reset_baseline", return_value=(False, "docker 起不来")), \
             patch.object(rb, "run_agent") as mock_agent:
            record, abort = self._run_round()
        self.assertEqual(record["status"], "setup_failed")
        self.assertEqual(record["stage"], "基线重置")
        mock_agent.assert_not_called()

    def test_data_preset_failure_skips_agent(self):
        with patch.object(rb, "reset_baseline", return_value=(True, "ok")), \
             patch.object(rb, "preset_data", return_value=(False, "redis 拒绝写入")), \
             patch.object(rb, "run_agent") as mock_agent:
            record, _ = self._run_round()
        self.assertEqual(record["status"], "setup_failed")
        self.assertEqual(record["stage"], "预置数据")
        mock_agent.assert_not_called()

    def test_injection_failure_skips_agent(self):
        with patch.object(rb, "reset_baseline", return_value=(True, "ok")), \
             patch.object(rb, "preset_data", return_value=(True, "ok")), \
             patch.object(rb, "probe", return_value={"ok": True, "reasons": []}), \
             patch.object(rb, "inject", return_value=(False, "stop 失败")), \
             patch.object(rb, "run_agent") as mock_agent:
            record, _ = self._run_round()
        self.assertEqual(record["status"], "setup_failed")
        self.assertEqual(record["stage"], "注入故障")
        mock_agent.assert_not_called()

    def test_scenario_verify_failure_skips_agent(self):
        with patch.object(rb, "reset_baseline", return_value=(True, "ok")), \
             patch.object(rb, "preset_data", return_value=(True, "ok")), \
             patch.object(rb, "probe", return_value={"ok": True, "reasons": []}), \
             patch.object(rb, "inject", return_value=(True, "已停止")), \
             patch.object(rb, "verify_scenario_state", return_value=(False, "注入未生效")), \
             patch.object(rb, "run_agent") as mock_agent:
            record, _ = self._run_round()
        self.assertEqual(record["status"], "setup_failed")
        self.assertEqual(record["stage"], "场景验证")
        mock_agent.assert_not_called()

    def test_invalid_round_not_counted_in_pass_rate(self):
        """无效实验不能混进恢复失败(通过率只按有效轮次算)。"""
        records = [
            {"status": "pass", "verdict": "PASS", "agent_ms": 100},
            {"status": "fail", "verdict": "FAIL", "agent_ms": 200},
            {"status": "setup_failed", "stage": "基线重置"},
        ]
        valid = [r for r in records if r.get("status") in ("pass", "fail")]
        totals = {"pass": 1, "fail": 1, "setup_failed": 1, "infra_error": 0}
        pass_rate = totals["pass"] / len(valid)
        self.assertEqual(len(valid), 2)
        self.assertEqual(pass_rate, 0.5)


# ---------- 正常场景:不发生无必要修改 ----------

class TestHealthyScenario(unittest.TestCase):
    def test_rule_policy_makes_no_modification_when_healthy(self):
        spies, start_spy = spy_registry()
        spies["get_api_health"].return_value = {
            "ok": True, "data": {"status": "healthy", "redis": "healthy"}, "error": None}
        budget = Budget()
        finish, history = run_quiet(RulePolicy(), spies, budget=budget)
        self.assertEqual(finish.conclusion, "healthy")
        self.assertEqual(budget.modifications, 0)
        start_spy.assert_not_called()

    def test_llm_policy_makes_no_modification_when_healthy(self):
        spies, start_spy = spy_registry()
        spies["get_api_health"].return_value = {
            "ok": True, "data": {"status": "healthy", "redis": "healthy"}, "error": None}
        policy = LlmPolicy(ScriptedClient([tool_response("get_api_health"),
                                           finish_response("healthy", "检查后一切正常")]),
                           budget=Budget(), config=None)
        finish, _ = run_quiet(policy, spies)
        self.assertEqual(finish.conclusion, "healthy")
        self.assertEqual(policy.budget.modifications, 0)
        start_spy.assert_not_called()


# ---------- RulePolicy 回归:V0 能力没被重构破坏 ----------

class TestRulePolicyRegression(unittest.TestCase):
    def test_full_v0_sequence_for_redis_down(self):
        """V0 的完整序列:观察 → 查状态 → 启动 → 验证 → recovered。"""
        spies, start_spy = spy_registry()
        state = {"restarted": False}

        # 注意:分发器会注入 timeout 参数,所以假工具必须接受 **kwargs
        def health(**kwargs):
            return {"ok": True, "data": {"status": "healthy" if state["restarted"] else "degraded",
                                         "redis": "healthy" if state["restarted"] else "unavailable"},
                    "error": None}

        def status(**kwargs):
            return {"ok": True, "data": {"service": "redis",
                                         "running": state["restarted"], "state": "running" if state["restarted"] else "exited"},
                    "error": None}

        def start(**kwargs):
            state["restarted"] = True
            return {"ok": True, "data": {"action": "docker compose start redis"}, "error": None}

        spies["get_api_health"].side_effect = health
        spies["get_service_status"].side_effect = status
        spies["start_service"].side_effect = start

        budget = Budget()
        with patch("agent.loop.time.sleep"):   # 跳过验证间隔,保持测试快速
            finish, history = run_quiet(RulePolicy(), spies, budget=budget)

        self.assertEqual(finish.conclusion, "recovered")
        self.assertEqual([e.decision.name for e in history],
                         ["get_api_health", "get_service_status", "start_service", "get_api_health"])
        self.assertEqual(budget.modifications, 1)   # 只修一次

    def test_redis_running_but_api_broken_is_unresolved(self):
        spies, start_spy = spy_registry()
        spies["get_api_health"].return_value = {"ok": True, "data": {"status": "degraded", "redis": "unavailable"}, "error": None}
        spies["get_service_status"].return_value = {"ok": True, "data": {"service": "redis", "running": True, "state": "running"}, "error": None}
        finish, _ = run_quiet(RulePolicy(), spies)
        self.assertEqual(finish.conclusion, "unresolved")
        start_spy.assert_not_called()

    def test_unknown_state_does_not_act(self):
        spies, start_spy = spy_registry()
        spies["get_api_health"].return_value = {"ok": True, "data": {"status": "degraded", "redis": "unavailable"}, "error": None}
        spies["get_service_status"].return_value = {"ok": True, "data": {"service": "redis", "running": None, "state": "restarting"}, "error": None}
        finish, _ = run_quiet(RulePolicy(), spies)
        self.assertEqual(finish.conclusion, "unresolved")
        start_spy.assert_not_called()

    def test_gives_up_after_max_verifications(self):
        spies, _ = spy_registry()
        spies["get_api_health"].return_value = {"ok": True, "data": {"status": "degraded", "redis": "unavailable"}, "error": None}
        spies["get_service_status"].return_value = {"ok": True, "data": {"service": "redis", "running": False, "state": "exited"}, "error": None}
        spies["start_service"].return_value = {"ok": True, "data": {}, "error": None}
        budget = Budget()
        finish, _ = run_quiet(RulePolicy(), spies, budget=budget)
        self.assertEqual(finish.conclusion, "failed")
        self.assertIn("5 次", finish.evidence)
        self.assertEqual(budget.modifications, 1)


if __name__ == "__main__":
    unittest.main()
