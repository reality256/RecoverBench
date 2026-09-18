"""evaluator/evaluate.py 的单元测试:判定逻辑、连续通过、截止时间、退出码。

网络请求被 mock 隔离,不联网。

运行:
    python -m unittest discover -s tests -v
"""

import unittest
from unittest.mock import MagicMock, patch

from evaluator import evaluate as ev


def http_ok(payload):
    resp = MagicMock(status_code=200)
    resp.json.return_value = payload
    return resp


def http_status(status_code, payload):
    resp = MagicMock(status_code=status_code)
    resp.json.return_value = payload
    return resp


HEALTHY = {"status": "healthy", "redis": "healthy"}
DEGRADED = {"status": "degraded", "redis": "unavailable"}


def healthy_side_effect(key="k", value="v"):
    """健康 + 业务值正确。"""
    def side_effect(url, timeout=None):
        if url.endswith("/health"):
            return http_ok(HEALTHY)
        return http_ok({"key": key, "value": value})
    return side_effect


class TestProbe(unittest.TestCase):
    @patch.object(ev.requests, "get")
    def test_all_good(self, mock_get):
        mock_get.side_effect = healthy_side_effect()
        detail = ev.probe("k", "v")
        self.assertTrue(detail["ok"])
        self.assertEqual(detail["health"]["status"], "healthy")
        self.assertEqual(detail["cache"]["value"], "v")

    @patch.object(ev.requests, "get")
    def test_degraded(self, mock_get):
        mock_get.return_value = http_ok(DEGRADED)
        detail = ev.probe("k", "v")
        self.assertFalse(detail["ok"])
        self.assertTrue(any("健康检查未通过" in r for r in detail["reasons"]))

    @patch.object(ev.requests, "get")
    def test_value_mismatch(self, mock_get):
        mock_get.side_effect = healthy_side_effect(value="old")
        detail = ev.probe("k", "v")
        self.assertFalse(detail["ok"])
        self.assertTrue(any("业务读取不一致" in r for r in detail["reasons"]))

    @patch.object(ev.requests, "get")
    def test_cache_503_counts_as_failure(self, mock_get):
        def side_effect(url, timeout=None):
            if url.endswith("/health"):
                return http_ok(HEALTHY)
            return http_status(503, {"detail": "Redis unavailable"})
        mock_get.side_effect = side_effect
        detail = ev.probe("k", "v")
        self.assertFalse(detail["ok"])
        self.assertTrue(any("业务读取失败" in r for r in detail["reasons"]))

    @patch.object(ev.requests, "get")
    def test_connection_error_is_failure_not_crash(self, mock_get):
        mock_get.side_effect = ev.requests.ConnectionError("refused")
        detail = ev.probe("k", "v")
        self.assertFalse(detail["ok"])
        self.assertEqual(len(detail["reasons"]), 2)  # 健康 + 业务都失败

    @patch.object(ev.requests, "get")
    def test_non_json_body(self, mock_get):
        resp = MagicMock(status_code=200)
        resp.json.side_effect = ValueError("no json")
        mock_get.return_value = resp
        detail = ev.probe("k", "v")
        self.assertFalse(detail["ok"])
        self.assertTrue(any("不是合法 JSON" in r for r in detail["reasons"]))

    @patch.object(ev.requests, "get")
    def test_health_only_mode_skips_business(self, mock_get):
        mock_get.return_value = http_ok(HEALTHY)
        detail = ev.probe()
        self.assertTrue(detail["ok"])
        self.assertIsNone(detail["cache"])


class TestEvaluate(unittest.TestCase):
    @patch.object(ev.time, "sleep")
    @patch.object(ev, "probe")
    def test_pass_after_consecutive_successes(self, mock_probe, mock_sleep):
        mock_probe.return_value = {"ok": True, "reasons": [], "health": HEALTHY, "cache": {}}
        result = ev.evaluate("k", "v")
        self.assertEqual(result["verdict"], ev.PASS)
        self.assertEqual(result["checks_run"], 3)
        mock_sleep.assert_called()  # 每次间隔 1 秒

    @patch.object(ev.time, "sleep")
    @patch.object(ev, "probe")
    def test_fail_when_never_healthy(self, mock_probe, mock_sleep):
        mock_probe.return_value = {"ok": False, "reasons": ["健康检查未通过: degraded"],
                                   "health": DEGRADED, "cache": None}
        result = ev.evaluate("k", "v")
        self.assertEqual(result["verdict"], ev.FAIL)
        self.assertIn("未达成", result["reason"])

    @patch.object(ev.time, "sleep")
    @patch.object(ev, "probe")
    def test_streak_resets_on_failure(self, mock_probe, mock_sleep):
        # 成功 2 次 → 失败 1 次 → 又成功 3 次:必须在第 6 次检查才 PASS
        sequence = [True, True, False, True, True, True]
        mock_probe.side_effect = [
            {"ok": flag, "reasons": [] if flag else ["失败"], "health": None, "cache": None}
            for flag in sequence
        ]
        result = ev.evaluate("k", "v", attempts=3, max_checks=10)
        self.assertEqual(result["verdict"], ev.PASS)
        self.assertEqual(result["checks_run"], 6)

    @patch.object(ev.time, "sleep")
    @patch.object(ev, "probe")
    def test_single_transient_failure_still_passes(self, mock_probe, mock_sleep):
        # 抖动一次不算失败 —— 这正是"连续"的意义
        sequence = [False, True, True, True]
        mock_probe.side_effect = [
            {"ok": flag, "reasons": [] if flag else ["抖动"], "health": None, "cache": None}
            for flag in sequence
        ]
        result = ev.evaluate("k", "v", attempts=3, max_checks=10)
        self.assertEqual(result["verdict"], ev.PASS)

    @patch.object(ev.time, "sleep")
    @patch.object(ev, "probe")
    def test_max_checks_backstop(self, mock_probe, mock_sleep):
        # deadline 很大时也不能无限轮询
        mock_probe.return_value = {"ok": False, "reasons": ["一直坏"], "health": None, "cache": None}
        result = ev.evaluate("k", "v", attempts=3, deadline=9999, max_checks=4)
        self.assertEqual(result["verdict"], ev.FAIL)
        self.assertEqual(result["checks_run"], 4)
        self.assertIn("最大检查次数", result["reason"])

    @patch.object(ev, "probe")
    def test_missing_expected_value_is_error(self, mock_probe):
        result = ev.evaluate("k", None)
        self.assertEqual(result["verdict"], ev.ERROR)
        self.assertIn("裁判输入不完整", result["reason"])
        mock_probe.assert_not_called()   # 输入不完整就不该开始探测

    @patch.object(ev.time, "sleep")
    @patch.object(ev, "probe")
    def test_deadline_exceeded_is_fail(self, mock_probe, mock_sleep):
        mock_probe.return_value = {"ok": False, "reasons": ["失败"], "health": None, "cache": None}
        result = ev.evaluate("k", "v", attempts=3, interval=1.0, deadline=0)
        self.assertEqual(result["verdict"], ev.FAIL)
        self.assertIn("截止时间", result["reason"])

    @patch.object(ev.time, "sleep")
    @patch.object(ev, "probe")
    def test_result_is_json_serializable(self, mock_probe, mock_sleep):
        import json
        mock_probe.return_value = {"ok": True, "reasons": [], "health": HEALTHY, "cache": {"value": "v"}}
        result = ev.evaluate("k", "v")
        json.dumps(result)  # 序列化失败会抛异常
        self.assertIn("duration_ms", result)
        self.assertEqual(result["required_attempts"], 3)


class TestExitCodes(unittest.TestCase):
    def test_mapping(self):
        self.assertEqual(ev.EXIT_CODES[ev.PASS], 0)
        self.assertEqual(ev.EXIT_CODES[ev.FAIL], 1)
        self.assertEqual(ev.EXIT_CODES[ev.ERROR], 2)


if __name__ == "__main__":
    unittest.main()
