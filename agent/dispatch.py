"""工具分发器:把结构化的工具调用映射到真实函数。

执行前四道检查:
1. 工具是否存在(注册表)
2. 参数类型与范围
3. 服务是否在允许范围内(参数表里就是白名单,工具内部还会再校验一次,双保险)
4. 预算:工具调用次数、环境修改次数、剩余时间

顺序上先在预算里扣掉一次决策(非法调用也扣),再做其余检查 ——
这样策略持续输出无效参数时,循环会被预算终止而不是无限空转。

分发器只负责"能不能执行、怎么执行",不负责记账和打印(那是 loop.py 的事)。
"""

import inspect
from typing import Optional

from agent import tools as tools_module
from agent.budget import Budget
from agent.state import ToolCall

# 会修改环境的工具(消耗"环境修改"预算)
MODIFYING_TOOLS = {"start_service"}

# 各工具的默认超时;分发器会用"剩余时间"再夹一次
DEFAULT_TOOL_TIMEOUTS = {
    "get_api_health": tools_module.API_TIMEOUT,
    "get_service_status": tools_module.DOCKER_STATUS_TIMEOUT,
    "get_service_logs": tools_module.DOCKER_LOGS_TIMEOUT,
    "start_service": tools_module.DOCKER_START_TIMEOUT,
}

# 参数表:{工具名: {参数名: 规则}}。required 缺省为 True。
ARG_SPECS = {
    "get_api_health": {},
    "get_service_status": {
        "service": {"type": str, "choices": tools_module.ALLOWED_SERVICES},
    },
    "get_service_logs": {
        "service": {"type": str, "choices": tools_module.ALLOWED_SERVICES},
        "tail": {
            "type": int,
            "min": tools_module.MIN_LOG_TAIL,
            "max": tools_module.MAX_LOG_TAIL,
            "required": False,
        },
    },
    "start_service": {
        "service": {"type": str, "choices": tools_module.STARTABLE_SERVICES},
    },
}

# timeout 是分发器注入的内部参数:策略可以传,但一定会被剩余时间夹住
INJECTED_ARGS = {"timeout"}

# 给模型看的工具说明;新增工具必须同时登记参数表和说明(有测试守着)
TOOL_DESCRIPTIONS = {
    "get_api_health": "查看 API 的健康状态:接口是否可访问、依赖的 redis 是否可用。无参数。",
    "get_service_status": "查看某个服务的运行状态(running / exited 等)。service 只能是 api 或 redis。",
    "get_service_logs": "查看某个服务最近的容器日志,用于诊断。tail 为读取的行数(1~100)。",
    "start_service": "启动一个已停止的服务。V1 只允许 redis。",
}


def build_tool_schemas(registry: dict) -> list:
    """按注册表和参数表生成模型可用的工具 schema(单一事实来源,不会与校验逻辑脱节)。"""
    schemas = []
    for name in registry:
        spec = ARG_SPECS.get(name, {})
        properties = {}
        required = []
        for arg, rule in spec.items():
            prop = {"type": "integer" if rule.get("type") is int else "string"}
            if "choices" in rule:
                prop["enum"] = sorted(rule["choices"])
            if "min" in rule:
                prop["minimum"] = rule["min"]
            if "max" in rule:
                prop["maximum"] = rule["max"]
            properties[arg] = prop
            if rule.get("required", True):
                required.append(arg)

        schemas.append({
            "type": "function",
            "function": {
                "name": name,
                "description": TOOL_DESCRIPTIONS.get(name, ""),
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                    "additionalProperties": False,
                },
            },
        })
    return schemas


def _ok(data: dict) -> dict:
    return {"ok": True, "data": data, "error": None}


def _err(message: str) -> dict:
    return {"ok": False, "data": None, "error": message}


def _check_value(name: str, rule: dict, value) -> Optional[str]:
    """校验单个参数值,返回错误信息或 None。"""
    expected = rule.get("type")
    if expected is int:
        # 注意:bool 是 int 的子类,True/False 不能当行数用
        if isinstance(value, bool) or not isinstance(value, int):
            return f"{name} 必须是整数,收到 {type(value).__name__}"
    elif expected is not None and not isinstance(value, expected):
        return f"{name} 必须是 {expected.__name__},收到 {type(value).__name__}"

    if "choices" in rule and value not in rule["choices"]:
        return f"{name} 只能是 {sorted(rule['choices'])} 之一,收到 {value!r}"
    if "min" in rule and value < rule["min"]:
        return f"{name} 不能小于 {rule['min']},收到 {value}"
    if "max" in rule and value > rule["max"]:
        return f"{name} 不能大于 {rule['max']},收到 {value}"
    return None


def validate_arguments(tool_name: str, args: dict) -> Optional[str]:
    """校验参数,返回错误信息或 None。

    只有登记了参数表的工具(ARG_SPECS)才做严格的类型/范围校验 ——
    新增工具必须登记参数表,否则视为配置错误(有单元测试守着这一点)。
    """
    if not isinstance(args, dict):
        return f"参数必须是对象(dict),收到 {type(args).__name__}"

    if tool_name not in ARG_SPECS:
        return None

    spec = ARG_SPECS[tool_name]
    allowed = set(spec) | INJECTED_ARGS

    unknown = set(args) - allowed
    if unknown:
        return f"不认识的参数: {sorted(unknown)}"

    for name, rule in spec.items():
        if name not in args:
            if rule.get("required", True):
                return f"缺少必需参数: {name}"
            continue
        problem = _check_value(name, rule, args[name])
        if problem:
            return problem

    if "timeout" in args:
        problem = _check_value("timeout", {"type": (int, float), "min": 0}, args["timeout"])
        if problem:
            return problem
        if isinstance(args["timeout"], bool):
            return "timeout 必须是数字"
    return None


def _accepts_timeout(func) -> bool:
    """工具是否接受 timeout 参数(不接受就不注入,保持注册表通用)。"""
    try:
        params = inspect.signature(func).parameters
    except (TypeError, ValueError):
        return False
    return "timeout" in params or any(p.kind == p.VAR_KEYWORD for p in params.values())


def dispatch(decision: ToolCall, registry: dict, budget: Budget) -> dict:
    """执行一次工具调用,永远返回结构化结果(不抛异常,预算耗尽除外)。

    budget 抛出的 BudgetExceeded 由调用方(loop)捕获并结束这一轮。
    """
    # 1) 先扣决策预算:非法调用也消耗,防止策略无限输出无效参数
    budget.consume_decision()

    # 2) 工具是否存在
    if decision.name not in registry:
        return _err(f"未注册的工具: {decision.name}")

    # 3) 参数校验
    problem = validate_arguments(decision.name, decision.args)
    if problem:
        return _err(f"参数校验失败: {problem}")

    # 4) 修改预算(只对真正要执行的修改动作扣)
    if decision.name in MODIFYING_TOOLS:
        budget.consume_modification()

    # 5) 执行:超时取"工具默认值"和"剩余时间"的较小者
    args = dict(decision.args)
    requested = args.pop("timeout", None)
    if requested is None:
        requested = DEFAULT_TOOL_TIMEOUTS.get(decision.name, budget.remaining_seconds())
    try:
        effective_timeout = budget.tool_timeout(requested)
    except Exception as e:  # BudgetExceeded:交给 loop 结束这一轮
        raise e

    func = registry[decision.name]
    if _accepts_timeout(func):
        args["timeout"] = effective_timeout

    try:
        result = func(**args)
    except Exception as e:
        return _err(f"工具异常: {type(e).__name__}: {e}")

    if not isinstance(result, dict):
        return _err(f"工具返回了非 dict 结果: {type(result).__name__}")
    return result
