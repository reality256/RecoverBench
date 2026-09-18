"""Agent 的受限工具层。

设计约束(见 steps.md Step 6):
- 不给 Agent shell,只提供有限的结构化工具:观察 / 诊断 / 行动 / 验证。
- 所有 Docker 命令用 subprocess.run 的参数列表形式,绝不使用 shell=True。
- 所有命令都在项目根目录执行,不依赖用户从哪个目录启动脚本。
- 任何失败(网络错误、超时、权限拒绝)都转成结构化返回值,不抛异常。
"""

import json
import subprocess
from pathlib import Path

import requests

from agent.state import ActionResult, ApiHealth, LogsResult, ServiceStatus

# 项目根目录 = 本文件的上级的上级,不依赖启动目录
PROJECT_ROOT = Path(__file__).resolve().parent.parent

API_URL = "http://localhost:8000"

# V0 只允许重启 redis,白名单之外的任何服务一律拒绝
ALLOWED_RESTART_SERVICES = {"redis"}

# Windows 上 subprocess 默认按 GBK 解码命令输出,而 Docker 输出 UTF-8,
# 必须显式指定,否则遇到非 ASCII 字符会抛 UnicodeDecodeError
SUB_ENCODING = "utf-8"

# 超时预算:降级状态的 /health 因 DNS 查找约需 4 秒,所以 API 超时取 6 秒
API_TIMEOUT = 6
DOCKER_STATUS_TIMEOUT = 10
DOCKER_LOGS_TIMEOUT = 10
DOCKER_RESTART_TIMEOUT = 30


def _run_docker(cmd: list, action: str, timeout: int = DOCKER_STATUS_TIMEOUT) -> ActionResult:
    """在项目根目录执行一条 docker 命令,把所有失败情况转成 ActionResult。"""
    try:
        result = subprocess.run(
            cmd,
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            encoding=SUB_ENCODING,
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return ActionResult(ok=False, action=action, error=f"命令超时({timeout}s)")
    except FileNotFoundError:
        return ActionResult(ok=False, action=action, error="找不到 docker 命令,请确认 Docker 已安装")

    output = "\n".join(part.strip() for part in (result.stdout, result.stderr) if part.strip())
    if result.returncode == 0:
        return ActionResult(ok=True, action=action, output=output)
    return ActionResult(ok=False, action=action, output=output, error=f"exit code {result.returncode}")


def get_api_health() -> ApiHealth:
    """请求 /health,返回结构化健康状态。"""
    try:
        response = requests.get(f"{API_URL}/health", timeout=API_TIMEOUT)
    except requests.RequestException as e:
        return ApiHealth(ok=False, error=f"请求失败: {type(e).__name__}: {e}")

    if response.status_code != 200:
        return ApiHealth(
            ok=False,
            http_status=response.status_code,
            error=f"HTTP {response.status_code}",
        )

    try:
        data = response.json()
    except ValueError as e:
        return ApiHealth(ok=False, http_status=response.status_code, error=f"JSON 解析失败: {e}")

    return ApiHealth(
        ok=True,
        status=data.get("status"),
        redis=data.get("redis"),
        http_status=response.status_code,
    )


def get_service_status(service: str) -> ServiceStatus:
    """通过 docker compose ps 查看服务状态。"""
    # 注意 -a:不加的话停掉的容器不显示,就分不清"不存在"和"已停止"
    try:
        result = subprocess.run(
            ["docker", "compose", "ps", "-a", "--format", "json"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            encoding=SUB_ENCODING,
            errors="replace",
            timeout=DOCKER_STATUS_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return ServiceStatus(service=service, running=False, error=f"命令超时({DOCKER_STATUS_TIMEOUT}s)")
    except FileNotFoundError:
        return ServiceStatus(service=service, running=False, error="找不到 docker 命令,请确认 Docker 已安装")

    if result.returncode != 0:
        return ServiceStatus(
            service=service,
            running=False,
            error=(result.stderr or result.stdout).strip() or f"exit code {result.returncode}",
        )

    for line in result.stdout.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if entry.get("Service") == service:
            state = entry.get("State", "")
            return ServiceStatus(service=service, running=(state == "running"), raw=state)

    return ServiceStatus(service=service, running=False, raw="no such container")


def get_service_logs(service: str, tail: int = 50) -> LogsResult:
    """获取服务最近的容器日志,只用于诊断。限制行数,避免无限输出。"""
    cmd = ["docker", "compose", "logs", "--no-color", "--tail", str(tail), service]
    try:
        result = subprocess.run(
            cmd,
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            encoding=SUB_ENCODING,
            errors="replace",
            timeout=DOCKER_LOGS_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return LogsResult(ok=False, service=service, error=f"命令超时({DOCKER_LOGS_TIMEOUT}s)")
    except FileNotFoundError:
        return LogsResult(ok=False, service=service, error="找不到 docker 命令,请确认 Docker 已安装")

    output = (result.stdout or "") + (result.stderr or "")
    if result.returncode != 0:
        return LogsResult(ok=False, service=service, output=output, error=f"exit code {result.returncode}")
    return LogsResult(ok=True, service=service, output=output.strip())


def restart_service(service: str) -> ActionResult:
    """重启指定服务。V0 只允许白名单内的服务(redis)。"""
    if service not in ALLOWED_RESTART_SERVICES:
        return ActionResult(
            ok=False,
            action=f"restart {service}",
            error=f"权限拒绝:V0 只允许操作 {sorted(ALLOWED_RESTART_SERVICES)}",
        )

    action = f"docker compose start {service}"
    return _run_docker(["docker", "compose", "start", service], action, timeout=DOCKER_RESTART_TIMEOUT)


def verify_api() -> bool:
    """验证 API 是否完全恢复:只有 status 和 redis 都是 healthy 才算成功。"""
    health = get_api_health()
    return health.ok and health.status == "healthy" and health.redis == "healthy"
