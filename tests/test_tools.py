"""agent/tools.py 的单元测试:外部 HTTP 和 subprocess 全部用 mock 隔离。

V1 统一返回协议: {"ok": bool, "data": dict | None, "error": str | None}
运行:
    python -m unittest discover -s tests -v
"""

import json
import subprocess
import unittest
from unittest.mock import MagicMock, patch

from agent import tools


def _resp(status_code=200, body=None, json_error=None):
    resp = MagicMock(status_code=status_code)
    if json_error:
        resp.json.side_effect = json_error
    else:
        resp.json.return_value = body or {}
    return resp


def _sub_result(returncode=0, stdout="", stderr=""):
    result = MagicMock()
    result.returncode = returncode
    result.stdout = stdout
    result.stderr = stderr
    return result


def _container_line(service, state):
    return json.dumps({"Service": service, "State": state, "Name": f"recoverbench-{service}-1"})


class TestGetApiHealth(unittest.TestCase):
    """get_api_health:健康、降级、网络失败、超时、非 200、非对象 JSON、字段缺失。"""

    @patch.object(tools.requests, "get")
    def test_healthy(self, mock_get):
        mock_get.return_value = _resp(body={"status": "healthy", "redis": "healthy"})
        h = tools.get_api_health()
        self.assertTrue(h["ok"])
        self.assertEqual(h["data"]["status"], "healthy")
        self.assertEqual(h["data"]["redis"], "healthy")
        self.assertIsNone(h["error"])

    @patch.object(tools.requests, "get")
    def test_degraded_is_query_success(self, mock_get):
        # ok=True 只表示"查询成功",不表示系统健康
        mock_get.return_value = _resp(body={"status": "degraded", "redis": "unavailable"})
        h = tools.get_api_health()
        self.assertTrue(h["ok"])
        self.assertEqual(h["data"]["status"], "degraded")

    @patch.object(tools.requests, "get")
    def test_network_error(self, mock_get):
        mock_get.side_effect = tools.requests.ConnectionError("refused")
        h = tools.get_api_health()
        self.assertFalse(h["ok"])
        self.assertIn("请求失败", h["error"])

    @patch.object(tools.requests, "get")
    def test_timeout(self, mock_get):
        mock_get.side_effect = tools.requests.Timeout("slow")
        h = tools.get_api_health()
        self.assertFalse(h["ok"])
        self.assertIn("请求失败", h["error"])

    @patch.object(tools.requests, "get")
    def test_http_503(self, mock_get):
        mock_get.return_value = _resp(status_code=503)
        h = tools.get_api_health()
        self.assertFalse(h["ok"])
        self.assertIn("HTTP 503", h["error"])

    @patch.object(tools.requests, "get")
    def test_non_json_body(self, mock_get):
        mock_get.return_value = _resp(json_error=ValueError("no json"))
        h = tools.get_api_health()
        self.assertFalse(h["ok"])
        self.assertIn("JSON 解析失败", h["error"])

    @patch.object(tools.requests, "get")
    def test_non_object_json(self, mock_get):
        # 合法 JSON 但是数组,不是对象 → 异常响应
        mock_get.return_value = _resp(body=["healthy", "healthy"])
        h = tools.get_api_health()
        self.assertFalse(h["ok"])
        self.assertIn("不是对象", h["error"])

    @patch.object(tools.requests, "get")
    def test_missing_fields(self, mock_get):
        mock_get.return_value = _resp(body={"status": "healthy"})  # 缺 redis 字段
        h = tools.get_api_health()
        self.assertFalse(h["ok"])
        self.assertIn("缺少字段", h["error"])


class TestGetServiceStatus(unittest.TestCase):
    """get_service_status:状态解析、服务名校验、docker 失败、超时、命令不存在。"""

    @patch.object(tools.subprocess, "run")
    def test_running(self, mock_run):
        mock_run.return_value = _sub_result(stdout=_container_line("redis", "running"))
        s = tools.get_service_status("redis")
        self.assertTrue(s["ok"])
        self.assertIs(s["data"]["running"], True)
        self.assertEqual(s["data"]["state"], "running")

    @patch.object(tools.subprocess, "run")
    def test_exited(self, mock_run):
        mock_run.return_value = _sub_result(stdout=_container_line("redis", "exited"))
        s = tools.get_service_status("redis")
        self.assertTrue(s["ok"])
        self.assertIs(s["data"]["running"], False)
        self.assertEqual(s["data"]["state"], "exited")

    @patch.object(tools.subprocess, "run")
    def test_unknown_state_not_treated_as_stopped(self, mock_run):
        # restarting / paused 等未知状态:running=None,不能当作"确认已停止"
        mock_run.return_value = _sub_result(stdout=_container_line("redis", "restarting"))
        s = tools.get_service_status("redis")
        self.assertTrue(s["ok"])
        self.assertIsNone(s["data"]["running"])
        self.assertEqual(s["data"]["state"], "restarting")

    @patch.object(tools.subprocess, "run")
    def test_no_such_container(self, mock_run):
        mock_run.return_value = _sub_result(stdout=_container_line("api", "running"))
        s = tools.get_service_status("redis")
        self.assertTrue(s["ok"])
        self.assertIs(s["data"]["running"], False)
        self.assertEqual(s["data"]["state"], "no such container")

    @patch.object(tools.subprocess, "run")
    def test_invalid_service_never_executes(self, mock_run):
        s = tools.get_service_status("mysql")
        self.assertFalse(s["ok"])
        self.assertIn("参数校验失败", s["error"])
        mock_run.assert_not_called()  # 校验失败 = 不执行任何命令

    @patch.object(tools.subprocess, "run")
    def test_command_failed(self, mock_run):
        mock_run.return_value = _sub_result(returncode=1, stderr="docker: error")
        s = tools.get_service_status("redis")
        self.assertFalse(s["ok"])
        self.assertIn("docker: error", s["error"])

    @patch.object(tools.subprocess, "run")
    def test_timeout(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="docker", timeout=10)
        s = tools.get_service_status("redis")
        self.assertFalse(s["ok"])
        self.assertIn("命令超时", s["error"])

    @patch.object(tools.subprocess, "run")
    def test_docker_missing(self, mock_run):
        mock_run.side_effect = FileNotFoundError()
        s = tools.get_service_status("redis")
        self.assertFalse(s["ok"])
        self.assertIn("找不到 docker", s["error"])


class TestGetServiceLogs(unittest.TestCase):
    """get_service_logs:tail 边界、字符数上限、服务名校验、失败路径。"""

    @patch.object(tools.subprocess, "run")
    def test_ok_and_tail_passed(self, mock_run):
        mock_run.return_value = _sub_result(stdout="log line 1\nlog line 2\n")
        logs = tools.get_service_logs("redis", tail=10)
        self.assertTrue(logs["ok"])
        self.assertIn("log line 1", logs["data"]["output"])
        cmd = mock_run.call_args[0][0]
        self.assertIn("--tail", cmd)
        self.assertIn("10", cmd)

    @patch.object(tools.subprocess, "run")
    def test_char_cap(self, mock_run):
        # 输出超过上限:截断并标记 truncated
        mock_run.return_value = _sub_result(stdout="x" * (tools.MAX_LOG_CHARS + 500))
        logs = tools.get_service_logs("redis")
        self.assertTrue(logs["ok"])
        self.assertTrue(logs["data"]["truncated"])
        self.assertLessEqual(len(logs["data"]["output"]), tools.MAX_LOG_CHARS)

    @patch.object(tools.subprocess, "run")
    def test_tail_too_small_never_executes(self, mock_run):
        logs = tools.get_service_logs("redis", tail=0)
        self.assertFalse(logs["ok"])
        self.assertIn("参数校验失败", logs["error"])
        mock_run.assert_not_called()

    @patch.object(tools.subprocess, "run")
    def test_tail_too_large_never_executes(self, mock_run):
        logs = tools.get_service_logs("redis", tail=101)
        self.assertFalse(logs["ok"])
        mock_run.assert_not_called()

    @patch.object(tools.subprocess, "run")
    def test_tail_wrong_type_never_executes(self, mock_run):
        logs = tools.get_service_logs("redis", tail="50")
        self.assertFalse(logs["ok"])
        mock_run.assert_not_called()

    @patch.object(tools.subprocess, "run")
    def test_invalid_service_never_executes(self, mock_run):
        logs = tools.get_service_logs("mysql")
        self.assertFalse(logs["ok"])
        mock_run.assert_not_called()

    @patch.object(tools.subprocess, "run")
    def test_timeout(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="docker", timeout=10)
        logs = tools.get_service_logs("redis")
        self.assertFalse(logs["ok"])
        self.assertIn("命令超时", logs["error"])

    @patch.object(tools.subprocess, "run")
    def test_docker_missing(self, mock_run):
        mock_run.side_effect = FileNotFoundError()
        logs = tools.get_service_logs("redis")
        self.assertFalse(logs["ok"])
        self.assertIn("找不到 docker", logs["error"])


class TestStartService(unittest.TestCase):
    """start_service:白名单,被拒绝时绝不执行任何命令。"""

    @patch.object(tools.subprocess, "run")
    def test_denied_service_never_executes(self, mock_run):
        result = tools.start_service("api")
        self.assertFalse(result["ok"])
        self.assertIn("参数校验失败", result["error"])
        mock_run.assert_not_called()

    @patch.object(tools.subprocess, "run")
    def test_denied_injection_attempt(self, mock_run):
        # 恶意字符串也只是当服务名比较,绝不执行
        result = tools.start_service("rm -rf /")
        self.assertFalse(result["ok"])
        mock_run.assert_not_called()

    @patch.object(tools.subprocess, "run")
    def test_allowed_service_runs_in_project_root(self, mock_run):
        mock_run.return_value = _sub_result(stdout="Started")
        r = tools.start_service("redis")
        self.assertTrue(r["ok"])
        self.assertEqual(r["data"]["service"], "redis")
        cmd = mock_run.call_args[0][0]
        self.assertEqual(cmd, ["docker", "compose", "start", "redis"])
        self.assertEqual(mock_run.call_args[1]["cwd"], tools.PROJECT_ROOT)

    @patch.object(tools.subprocess, "run")
    def test_allowed_service_failure_reported(self, mock_run):
        mock_run.return_value = _sub_result(returncode=1, stdout="", stderr="boom")
        r = tools.start_service("redis")
        self.assertFalse(r["ok"])
        self.assertIn("boom", r["error"])


class TestVerifyApi(unittest.TestCase):
    @patch.object(tools, "get_api_health")
    def test_healthy(self, mock_health):
        mock_health.return_value = {"ok": True, "data": {"status": "healthy", "redis": "healthy"}, "error": None}
        self.assertTrue(tools.verify_api())

    @patch.object(tools, "get_api_health")
    def test_degraded(self, mock_health):
        mock_health.return_value = {"ok": True, "data": {"status": "degraded", "redis": "unavailable"}, "error": None}
        self.assertFalse(tools.verify_api())

    @patch.object(tools, "get_api_health")
    def test_unreachable(self, mock_health):
        mock_health.return_value = {"ok": False, "data": None, "error": "boom"}
        self.assertFalse(tools.verify_api())


if __name__ == "__main__":
    unittest.main()
