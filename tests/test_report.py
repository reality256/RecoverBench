"""scripts/report_benchmark.py 的单元测试:分类与统计口径。

重点验证"无效实验不混入恢复失败"这个统计原则。

运行:
    python -m unittest discover -s tests -v
"""

import importlib.util
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location("report_benchmark",
                                               PROJECT_ROOT / "scripts" / "report_benchmark.py")
rep = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rep)


def make_summary(bench_id, policy, scenario, rounds):
    return {"benchmark_id": bench_id, "policy": policy, "scenario": scenario, "rounds": rounds}


def valid_round(verdict="PASS", claim="RECOVERED", agent_ms=100, tools=3, mods=1, tokens=500):
    return {"status": "pass" if verdict == "PASS" else "fail", "verdict": verdict,
            "agent_claim": claim, "agent_ms": agent_ms, "tool_calls": tools,
            "modifications": mods, "tokens": tokens, "verdict_reason": "x"}


class TestClassification(unittest.TestCase):
    def setUp(self):
        self.summaries = [
            make_summary("b1", "llm", "redis_down", [
                valid_round("PASS"),
                valid_round("PASS", agent_ms=200),
                valid_round("FAIL", claim="RECOVERED"),          # 错报成功
                {"status": "setup_failed", "stage": "注入故障", "detail": "stop 失败"},   # 无效实验
                {"status": "infra_error", "detail": "轮次执行异常"},                      # 无效实验
            ]),
            make_summary("b2", "rule", "healthy", [
                valid_round("PASS", claim="ALREADY_HEALTHY", agent_ms=300, tools=1, mods=0, tokens=0),
            ]),
        ]

    def test_rows_group_by_policy_and_scenario(self):
        rows, _, _, _ = rep.collect_rounds(self.summaries)
        self.assertIn(("llm", "redis_down"), rows)
        self.assertIn(("rule", "healthy"), rows)
        # 无效实验不进结果表
        self.assertEqual(len(rows[("llm", "redis_down")]), 3)
        self.assertEqual(len(rows[("rule", "healthy")]), 1)

    def test_invalid_experiments_listed_separately(self):
        _, invalid, _, _ = rep.collect_rounds(self.summaries)
        self.assertEqual(len(invalid), 2)
        stages = [record.get("stage") for _, record in invalid]
        self.assertIn("注入故障", stages)

    def test_false_success_detected(self):
        _, _, false_success, failures = rep.collect_rounds(self.summaries)
        self.assertEqual(len(false_success), 1)
        self.assertEqual(len(failures), 1)
        bench_id, record = false_success[0]
        self.assertEqual(bench_id, "b1")
        self.assertEqual(record["agent_claim"], "RECOVERED")

    def test_claim_matching_verdict_is_not_false_success(self):
        # Agent 自述失败、裁判也判失败 → 不是错报
        summaries = [make_summary("b", "llm", "redis_down", [valid_round("FAIL", claim="FAILED")])]
        _, _, false_success, failures = rep.collect_rounds(summaries)
        self.assertEqual(false_success, [])
        self.assertEqual(len(failures), 1)


class TestAggregation(unittest.TestCase):
    def test_pass_rate_excludes_invalid_rounds(self):
        """关键口径:无效实验不进分母。"""
        rounds = [valid_round("PASS"), valid_round("FAIL"),
                  {"status": "setup_failed"}, {"status": "infra_error"}]
        rows, _, _, _ = rep.collect_rounds([make_summary("b", "llm", "redis_down", rounds)])
        records = rows[("llm", "redis_down")]
        passed = sum(1 for r in records if r.get("verdict") == "PASS")
        self.assertEqual(len(records), 2)          # 分母只有 2
        self.assertEqual(passed / len(records), 0.5)   # 而不是 0.25

    def test_average_ignores_missing_values(self):
        self.assertEqual(rep.average([100, 200, None]), 150)
        self.assertEqual(rep.average([None, None]), None)
        self.assertEqual(rep.average([]), None)

    def test_average_zero_is_kept(self):
        # rule 策略 token 恒为 0,不能被当成"缺失"
        self.assertEqual(rep.average([0, 0, 0]), 0)


if __name__ == "__main__":
    unittest.main()
