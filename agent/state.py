"""Agent 的结构化状态定义。

原则:Agent 的所有观察结果都用 dataclass 表达,不用随意拼接的字符串。
网络失败、命令超时等都通过返回值里的 ok/error 字段表达,而不是抛异常让程序崩溃。
"""

from dataclasses import dataclass
from typing import Optional


@dataclass
class ApiHealth:
    """API 健康状态的观察结果。

    ok     : API 是否可访问(请求成功并解析出 JSON)
    status : /health 返回的 status 字段(healthy / degraded)
    redis  : /health 返回的 redis 字段(healthy / unavailable)
    error  : 请求失败(网络错误、超时、JSON 解析失败等)时的错误信息
    """
    ok: bool
    status: Optional[str] = None
    redis: Optional[str] = None
    error: Optional[str] = None
    http_status: Optional[int] = None


@dataclass
class ServiceStatus:
    """Docker 服务的运行状态。

    running : 服务是否在运行
    raw     : docker compose ps 返回的原始状态(如 running / exited)
    error   : 查询失败时的错误信息
    """
    service: str
    running: bool
    raw: str = ""
    error: Optional[str] = None


@dataclass
class ActionResult:
    """一次动作(重启服务等)的执行结果。

    ok     : 动作是否成功
    action : 执行了什么动作(便于打印时看清 Agent 做了什么)
    output : 命令的标准输出 + 标准错误
    error  : 失败原因(命令报错、超时、权限拒绝等)
    """
    ok: bool
    action: str
    output: str = ""
    error: Optional[str] = None


@dataclass
class LogsResult:
    """容器日志的查询结果(只用于诊断)。"""
    ok: bool
    service: str
    output: str = ""
    error: Optional[str] = None
