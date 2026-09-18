"""Agent 的受限工具层(V1)。

统一返回协议(可序列化 dict):
    {"ok": bool, "data": dict | None, "error": str | None}
    ok=True 只表示"查询/动作成功",不表示系统健康。

约束:
- 不给 Agent shell,只暴露四个结构化工具(verify_api 是内部辅助,不进注册表)。
- 所有 Docker 命令用参数列表形式,绝不使用 shell=True,统一在项目根目录执行。
- 服务名、参数一律先校验,校验失败绝不执行任何命令。
- 网络错误、超时、权限拒绝、异常响应全部转成结构化返回,不抛异常。
"""

import json
import subprocess
from pathlib import Path

import requests

# 项目根目录 = 本文件的上级的上级,不依赖启动目录
PROJECT_ROOT = Path(__file__).resolve().parent.parent

API_URL = "http://localhost:8000"

# 服务名白名单:状态/日志只允许查这两个服务
ALLOWED_SERVICES = {"api", "redis"}

# V1 只允许启动 redis
STARTABLE_SERVICES = {"redis"}

# 日志行数限制与总字符数上限
MIN_LOG_TAIL = 1
MAX_LOG_TAIL = 100
MAX_LOG_CHARS = 2000

# 可以确认为"已停止"的 docker 状态;其它未知状态一律如实上报,不当停止处理
CONFIRMED_STOPPED_STATES = {"exited", "dead"}

# 超时预算:降级状态的 /health 因 DNS 查找约需 4 秒,所以 API 超时取 6 秒
API_TIMEOUT = 6
DOCKER_STATUS_TIMEOUT = 10
DOCKER_LOGS_TIMEOUT = 10
DOCKER_START_TIMEOUT = 30

# Windows 上 subprocess 默认按 GBK 解码命令输出,而 Docker 输出 UTF-8,
# 必须显式指定,否则遇到非 ASCII 字符会抛 UnicodeDecodeError
SUB_ENCODING = "utf-8"


def _ok(data: dict) -> dict:
    return {"ok": True, "data": data, "error": None}


def _err(message: str) -> dict:
    return {"ok": False, "data": None, "error": message}


def _run_docker(cmd: list, timeout: float):
    """执行一条 docker 命令,返回 (output, error);失败情况全部转成 error。"""
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
        return None, f"超时参数非法: {timeout!r}"
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
        return None, f"命令超时({timeout}s)"
    except FileNotFoundError:
        return None, "找不到 docker 命令,请确认 Docker 已安装"

    output = "\n".join(part.strip() for part in (result.stdout, result.stderr) if part.strip())
    if result.returncode != 0:
        return None, output or f"exit code {result.returncode}"
    return output, None


def get_api_health(timeout: float = API_TIMEOUT) -> dict:
    """查看 API 健康状态。timeout 由分发器用"剩余时间"夹住(见 dispatch.py)。"""
    try:
        response = requests.get(f"{API_URL}/health", timeout=timeout)
    except requests.RequestException as e:
        return _err(f"请求失败: {type(e).__name__}: {e}")

    if response.status_code != 200:
        return _err(f"HTTP {response.status_code}")

    try:
        data = response.json()
    except ValueError as e:
        return _err(f"JSON 解析失败: {e}")

    # 非对象 JSON(如数组、字符串)与字段缺失都属于异常响应,如实报错
    if not isinstance(data, dict):
        return _err(f"响应 JSON 不是对象,而是 {type(data).__name__}")
    missing = [key for key in ("status", "redis") if key not in data]
    if missing:
        return _err(f"响应缺少字段: {', '.join(missing)}")

    return _ok({"status": data["status"], "redis": data["redis"], "http_status": response.status_code})


def get_service_status(service: str, timeout: float = DOCKER_STATUS_TIMEOUT) -> dict:
    """查看服务状态。service 只能是 api 或 redis。"""
    if service not in ALLOWED_SERVICES:
        return _err(f"参数校验失败: service 只能是 {sorted(ALLOWED_SERVICES)} 之一")

    # 注意 -a:不加的话停掉的容器不显示,就分不清"不存在"和"已停止"
    output, error = _run_docker(
        ["docker", "compose", "ps", "-a", "--format", "json"],
        timeout,
    )
    if error:
        return _err(error)

    for line in output.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        if entry.get("Service") == service:
            state = entry.get("State", "")
            if state == "running":
                return _ok({"service": service, "running": True, "state": state})
            if state in CONFIRMED_STOPPED_STATES or state == "created":
                return _ok({"service": service, "running": False, "state": state})
            # 未知状态(restarting / paused / ...)不能当作"确认已停止"
            return _ok({"service": service, "running": None, "state": state or "unknown"})

    return _ok({"service": service, "running": False, "state": "no such container"})


def get_service_logs(service: str, tail: int = 50, timeout: float = DOCKER_LOGS_TIMEOUT) -> dict:
    """查看服务最近日志,只用于诊断。tail 限 1~100,输出限总字符数。"""
    if service not in ALLOWED_SERVICES:
        return _err(f"参数校验失败: service 只能是 {sorted(ALLOWED_SERVICES)} 之一")
    if isinstance(tail, bool) or not isinstance(tail, int) or not (MIN_LOG_TAIL <= tail <= MAX_LOG_TAIL):
        return _err(f"参数校验失败: tail 必须是 {MIN_LOG_TAIL}~{MAX_LOG_TAIL} 的整数")

    output, error = _run_docker(
        ["docker", "compose", "logs", "--no-color", "--tail", str(tail), service],
        timeout,
    )
    if error:
        return _err(error)

    return _ok({
        "service": service,
        "lines_requested": tail,
        "output": output[-MAX_LOG_CHARS:],
        "truncated": len(output) > MAX_LOG_CHARS,
    })


def start_service(service: str, timeout: float = DOCKER_START_TIMEOUT) -> dict:
    """启动已停止的服务。V1 只允许 redis(白名单之外拒绝且绝不执行)。"""
    if service not in STARTABLE_SERVICES:
        return _err(f"参数校验失败: V1 只允许启动 {sorted(STARTABLE_SERVICES)} 之一")

    action = f"docker compose start {service}"
    output, error = _run_docker(["docker", "compose", "start", service], timeout)
    if error:
        return _err(error)
    return _ok({"service": service, "action": action, "output": output})


def verify_api() -> bool:
    """内部辅助函数(不进工具注册表):API 完全健康才算恢复。"""
    result = get_api_health()
    data = result.get("data") or {}
    return bool(result["ok"]) and data.get("status") == "healthy" and data.get("redis") == "healthy"


# 暴露给模型/策略的工具注册表(名称 → 函数)
TOOL_REGISTRY = {
    "get_api_health": get_api_health,
    "get_service_status": get_service_status,
    "get_service_logs": get_service_logs,
    "start_service": start_service,
}
