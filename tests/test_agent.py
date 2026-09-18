"""agent/agent.py 决策循环的单元测试:工具全部 mock,验证退出码和修复动作次数。

运行:
    python -m unittest discover -s tests -v
"""

import contextlib
import io
import unittest
from unittest.mock import patch

from agent import agent as ag
from agent import tools


def run_quietly():
    """跑一遍 run_agent,吞掉打印,只拿退出码。"""
    with contextlib.redirect_stdout(io.StringIO()):
        return ag.run_agent()


def health(ok=True, status="healthy", redis="healthy", error=None):
    return tools.ApiHealth(ok=ok, status=status, redis=redis, error=error)


class TestAgentLoop(unittest.TestCase):
    @patch.object(tools, "restart_service")
    @patch.object(tools, "get_service_logs")
    @patch.object(tools, "get_service_status")
    @patch.object(tools, "get_api_health")
    def test_already_healthy_does_nothing(self, mock_health, mock_status, mock_logs, mock_restart):
        mock_health.return_value = health()
        self.assertEqual(run_quietly(), ag.EXIT_OK)
        mock_restart.assert_not_called()
        mock_status.assert_not_called()  # 健康时连状态都不查,直接收工

    @patch.object(ag.time, "sleep")
    @patch.object(tools, "restart_service")
    @patch.object(tools, "get_service_logs")
    @patch.object(tools, "get_service_status")
    @patch.object(tools, "get_api_health")
    def test_recovers(self, mock_health, mock_status, mock_logs, mock_restart, mock_sleep):
        mock_health.side_effect = [
            health(status="degraded", redis="unavailable"),  # 初始观察
            health(),                                          # 验证第 1 次
        ]
        mock_status.return_value = tools.ServiceStatus(service="redis", running=False, raw="exited")
        mock_logs.return_value = tools.LogsResult(ok=True, service="redis", output="bye bye")
        mock_restart.return_value = tools.ActionResult(ok=True, action="start redis", output="Started")
        self.assertEqual(run_quietly(), ag.EXIT_OK)
        mock_restart.assert_called_once_with("redis")

    @patch.object(tools, "restart_service")
    @patch.object(tools, "get_service_logs")
    @patch.object(tools, "get_service_status")
    @patch.object(tools, "get_api_health")
    def test_unresolved_when_redis_running(self, mock_health, mock_status, mock_logs, mock_restart):
        mock_health.return_value = health(status="degraded", redis="unavailable")
        mock_status.return_value = tools.ServiceStatus(service="redis", running=True, raw="running")
        self.assertEqual(run_quietly(), ag.EXIT_UNRESOLVED)
        mock_restart.assert_not_called()  # 没证据不动手

    @patch.object(ag.time, "sleep")
    @patch.object(tools, "restart_service")
    @patch.object(tools, "get_service_logs")
    @patch.object(tools, "get_service_status")
    @patch.object(tools, "get_api_health")
    def test_failed_after_max_verifies(self, mock_health, mock_status, mock_logs, mock_restart, mock_sleep):
        # 验证永远不通过:FAILED,且验证恰好 5 次、修复恰好 1 次
        mock_health.return_value = health(status="degraded", redis="unavailable")
        mock_status.return_value = tools.ServiceStatus(service="redis", running=False, raw="exited")
        mock_logs.return_value = tools.LogsResult(ok=True, service="redis", output="")
        mock_restart.return_value = tools.ActionResult(ok=True, action="start redis", output="Started")
        self.assertEqual(run_quietly(), ag.EXIT_UNRESOLVED)
        self.assertEqual(mock_health.call_count, 1 + ag.MAX_VERIFY_ATTEMPTS)  # 初始 1 次 + 验证 5 次
        mock_restart.assert_called_once_with("redis")  # 只修一次,不失控循环

    @patch.object(tools, "restart_service")
    @patch.object(tools, "get_service_logs")
    @patch.object(tools, "get_service_status")
    @patch.object(tools, "get_api_health")
    def test_agent_error_when_docker_unavailable(self, mock_health, mock_status, mock_logs, mock_restart):
        mock_health.return_value = health(status="degraded", redis="unavailable")
        mock_status.return_value = tools.ServiceStatus(service="redis", running=False, error="找不到 docker 命令")
        self.assertEqual(run_quietly(), ag.EXIT_AGENT_ERROR)
        mock_restart.assert_not_called()

    @patch.object(tools, "restart_service")
    @patch.object(tools, "get_service_logs")
    @patch.object(tools, "get_service_status")
    @patch.object(tools, "get_api_health")
    def test_agent_error_when_restart_fails(self, mock_health, mock_status, mock_logs, mock_restart):
        mock_health.return_value = health(status="degraded", redis="unavailable")
        mock_status.return_value = tools.ServiceStatus(service="redis", running=False, raw="exited")
        mock_logs.return_value = tools.LogsResult(ok=True, service="redis", output="")
        mock_restart.return_value = tools.ActionResult(ok=False, action="start redis", error="exit code 1")
        self.assertEqual(run_quietly(), ag.EXIT_AGENT_ERROR)


if __name__ == "__main__":
    unittest.main()
