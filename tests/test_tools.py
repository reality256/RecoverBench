"""agent/tools.py 的单元测试:外部 HTTP 和 subprocess 全部用 mock 隔离。

运行:
    python -m unittest discover -s tests -v
"""

import json
import subprocess
import unittest
from unittest.mock import MagicMock, patch

from agent import tools


class TestGetApiHealth(unittest.TestCase):
    """get_api_health:健康、降级、网络失败、超时、非 200、非 JSON。"""

    def _mock_response(self, status_code=200, body=None, json_error=None):
        resp = MagicMock(status_code=status_code)
        if json_error:
            resp.json.side_effect = json_error
        else:
            resp.json.return_value = body or {}
        return resp

    @patch.object(tools.requests, "get")
    def test_healthy(self, mock_get):
        mock_get.return_value = self._mock_response(body={"status": "healthy", "redis": "healthy"})
        h = tools.get_api_health()
        self.assertTrue(h.ok)
        self.assertEqual(h.status, "healthy")
        self.assertEqual(h.redis, "healthy")
        self.assertIsNone(h.error)

    @patch.object(tools.requests, "get")
    def test_degraded_still_reachable(self, mock_get):
        # 降级时 API 是可访问的:ok=True,status 如实报告
        mock_get.return_value = self._mock_response(body={"status": "degraded", "redis": "unavailable"})
        h = tools.get_api_health()
        self.assertTrue(h.ok)
        self.assertEqual(h.status, "degraded")

    @patch.object(tools.requests, "get")
    def test_network_error(self, mock_get):
        mock_get.side_effect = tools.requests.ConnectionError("refused")
        h = tools.get_api_health()
        self.assertFalse(h.ok)
        self.assertIn("请求失败", h.error)

    @patch.object(tools.requests, "get")
    def test_timeout(self, mock_get):
        mock_get.side_effect = tools.requests.Timeout("slow")
        h = tools.get_api_health()
        self.assertFalse(h.ok)
        self.assertIn("请求失败", h.error)

    @patch.object(tools.requests, "get")
    def test_http_503(self, mock_get):
        mock_get.return_value = self._mock_response(status_code=503)
        h = tools.get_api_health()
        self.assertFalse(h.ok)
        self.assertEqual(h.http_status, 503)

    @patch.object(tools.requests, "get")
    def test_non_json_body(self, mock_get):
        mock_get.return_value = self._mock_response(json_error=ValueError("no json"))
        h = tools.get_api_health()
        self.assertFalse(h.ok)
        self.assertIn("JSON 解析失败", h.error)


class TestGetServiceStatus(unittest.TestCase):
    """get_service_status:JSONL 解析、docker 失败、超时、命令不存在。"""

    def _mock_result(self, returncode=0, stdout="", stderr=""):
        result = MagicMock()
        result.returncode = returncode
        result.stdout = stdout
        result.stderr = stderr
        return result

    def _container_line(self, service, state):
        return json.dumps({"Service": service, "State": state, "Name": f"recoverbench-{service}-1"})

    @patch.object(tools.subprocess, "run")
    def test_running(self, mock_run):
        mock_run.return_value = self._mock_result(stdout=self._container_line("redis", "running"))
        s = tools.get_service_status("redis")
        self.assertTrue(s.running)
        self.assertEqual(s.raw, "running")

    @patch.object(tools.subprocess, "run")
    def test_exited(self, mock_run):
        mock_run.return_value = self._mock_result(stdout=self._container_line("redis", "exited"))
        s = tools.get_service_status("redis")
        self.assertFalse(s.running)
        self.assertEqual(s.raw, "exited")

    @patch.object(tools.subprocess, "run")
    def test_no_such_container(self, mock_run):
        mock_run.return_value = self._mock_result(stdout=self._container_line("api", "running"))
        s = tools.get_service_status("redis")
        self.assertFalse(s.running)
        self.assertEqual(s.raw, "no such container")

    @patch.object(tools.subprocess, "run")
    def test_junk_line_skipped(self, mock_run):
        # 输出里混进一行非 JSON 内容,不能被拖垮
        stdout = "not json at all\n" + self._container_line("redis", "running")
        mock_run.return_value = self._mock_result(stdout=stdout)
        s = tools.get_service_status("redis")
        self.assertTrue(s.running)

    @patch.object(tools.subprocess, "run")
    def test_command_failed(self, mock_run):
        mock_run.return_value = self._mock_result(returncode=1, stderr="docker: error")
        s = tools.get_service_status("redis")
        self.assertFalse(s.running)
        self.assertIn("docker: error", s.error)

    @patch.object(tools.subprocess, "run")
    def test_timeout(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="docker", timeout=10)
        s = tools.get_service_status("redis")
        self.assertFalse(s.running)
        self.assertIn("命令超时", s.error)

    @patch.object(tools.subprocess, "run")
    def test_docker_missing(self, mock_run):
        mock_run.side_effect = FileNotFoundError()
        s = tools.get_service_status("redis")
        self.assertFalse(s.running)
        self.assertIn("找不到 docker", s.error)


class TestGetServiceLogs(unittest.TestCase):
    """get_service_logs:正常、超时、docker 不存在。"""

    @patch.object(tools.subprocess, "run")
    def test_ok_and_tail_passed(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="log line 1\nlog line 2\n", stderr="")
        logs = tools.get_service_logs("redis", tail=10)
        self.assertTrue(logs.ok)
        self.assertIn("log line 1", logs.output)
        cmd = mock_run.call_args[0][0]
        self.assertIn("--tail", cmd)
        self.assertIn("10", cmd)

    @patch.object(tools.subprocess, "run")
    def test_timeout(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="docker", timeout=10)
        logs = tools.get_service_logs("redis")
        self.assertFalse(logs.ok)
        self.assertIn("命令超时", logs.error)

    @patch.object(tools.subprocess, "run")
    def test_docker_missing(self, mock_run):
        mock_run.side_effect = FileNotFoundError()
        logs = tools.get_service_logs("redis")
        self.assertFalse(logs.ok)
        self.assertIn("找不到 docker", logs.error)


class TestRestartService(unittest.TestCase):
    """restart_service:白名单,被拒绝时绝不执行任何命令。"""

    @patch.object(tools.subprocess, "run")
    def test_denied_service_never_executes(self, mock_run):
        result = tools.restart_service("api")
        self.assertFalse(result.ok)
        self.assertIn("权限拒绝", result.error)
        mock_run.assert_not_called()  # 关键:拒绝 = 不执行

    @patch.object(tools.subprocess, "run")
    def test_denied_injection_attempt(self, mock_run):
        # 就算传进来的是恶意字符串,也只是当服务名比较,绝不执行
        result = tools.restart_service("rm -rf /")
        self.assertFalse(result.ok)
        mock_run.assert_not_called()

    @patch.object(tools.subprocess, "run")
    def test_allowed_service_runs_in_project_root(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="Started", stderr="")
        r = tools.restart_service("redis")
        self.assertTrue(r.ok)
        cmd = mock_run.call_args[0][0]
        self.assertEqual(cmd, ["docker", "compose", "start", "redis"])
        self.assertEqual(mock_run.call_args[1]["cwd"], tools.PROJECT_ROOT)

    @patch.object(tools.subprocess, "run")
    def test_allowed_service_failure_reported(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="boom")
        r = tools.restart_service("redis")
        self.assertFalse(r.ok)
        self.assertIn("exit code 1", r.error)


class TestVerifyApi(unittest.TestCase):
    @patch.object(tools, "get_api_health")
    def test_healthy(self, mock_health):
        mock_health.return_value = tools.ApiHealth(ok=True, status="healthy", redis="healthy")
        self.assertTrue(tools.verify_api())

    @patch.object(tools, "get_api_health")
    def test_degraded(self, mock_health):
        mock_health.return_value = tools.ApiHealth(ok=True, status="degraded", redis="unavailable")
        self.assertFalse(tools.verify_api())

    @patch.object(tools, "get_api_health")
    def test_unreachable(self, mock_health):
        mock_health.return_value = tools.ApiHealth(ok=False, error="boom")
        self.assertFalse(tools.verify_api())


if __name__ == "__main__":
    unittest.main()
