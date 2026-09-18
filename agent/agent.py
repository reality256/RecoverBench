"""无 LLM 的确定性 Agent 循环(V0)。

流程(见 steps.md Step 7):
    观察 API
      ├─ 健康 ──────────────▶ ALREADY_HEALTHY(退出码 0)
      └─ 不健康
           ↓
    查看 Redis 状态和最近日志
           ↓
     Redis 在运行? ──是──▶ UNRESOLVED(退出码 1)
           │否
           ↓
     启动 Redis(整个循环只允许这一次修复动作)
           ↓
     等待并验证(最多 5 次,每次间隔 1 秒)
      ├─ 健康 ──────────────▶ RECOVERED(退出码 0)
      └─ 仍不健康 ──────────▶ FAILED(退出码 1)

约束:
- 只在证据表明 Redis 未运行时才启动它。
- 不使用 scripts/reset_env.py,不调用 evaluator —— Agent 和裁判必须相互独立。
- 退出码:0 = 已健康/恢复成功,1 = 诊断后仍无法恢复,2 = Agent 自身工具调用出错。
"""

import sys
import time
from pathlib import Path

# 保证无论从哪个目录、以哪种方式启动,都能 import 到 agent 包
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import tools  # noqa: E402
from agent.state import ApiHealth  # noqa: E402

# 退出码
EXIT_OK = 0            # 已健康或恢复成功
EXIT_UNRESOLVED = 1    # 诊断后仍无法恢复
EXIT_AGENT_ERROR = 2   # Agent 自身配置或工具调用出错

# 修复后最多验证 5 次,每次间隔 1 秒
MAX_VERIFY_ATTEMPTS = 5
VERIFY_INTERVAL_SECONDS = 1


def _is_healthy(health: ApiHealth) -> bool:
    """API 完全健康 = 请求成功且 status、redis 都是 healthy。"""
    return health.ok and health.status == "healthy" and health.redis == "healthy"


def run_agent() -> int:
    """执行一次 观察→诊断→行动→验证 闭环,返回退出码。"""
    step = 0

    # ---- 观察 1:API 健康吗 ----
    step += 1
    health = tools.get_api_health()
    print(f"[{step}] get_api_health → {health}")
    if _is_healthy(health):
        print("结论: ALREADY_HEALTHY")
        return EXIT_OK

    # ---- 观察 2:Redis 状态和最近日志 ----
    step += 1
    redis_status = tools.get_service_status("redis")
    print(f"[{step}] get_service_status(redis) → {redis_status}")
    if redis_status.error:
        print("结论: AGENT_ERROR(查不到 Redis 状态,Docker 可能不可用)")
        return EXIT_AGENT_ERROR

    logs = tools.get_service_logs("redis", tail=20)
    if logs.ok and logs.output:
        print(f"[{step}] get_service_logs(redis, 20) → {logs.output[-300:]}")
    elif not logs.ok:
        print(f"[{step}] get_service_logs(redis, 20) → 查询失败: {logs.error}")

    # ---- 决策(V0 规则):只有证据表明 Redis 未运行时才动手 ----
    if redis_status.running:
        print("结论: UNRESOLVED(Redis 在运行,故障另有原因,超出 V0 能力范围)")
        return EXIT_UNRESOLVED

    # ---- 行动:启动 Redis(整个循环中唯一一次修复动作) ----
    step += 1
    result = tools.restart_service("redis")
    print(f"[{step}] restart_service(redis) → {result}")
    if not result.ok:
        print("结论: AGENT_ERROR(修复动作执行失败)")
        return EXIT_AGENT_ERROR

    # ---- 验证:等待并重复验证,最多 5 次 ----
    for attempt in range(1, MAX_VERIFY_ATTEMPTS + 1):
        time.sleep(VERIFY_INTERVAL_SECONDS)
        health = tools.get_api_health()
        print(f"[验证 {attempt}/{MAX_VERIFY_ATTEMPTS}] → status={health.status}, redis={health.redis}")
        if _is_healthy(health):
            print("结论: RECOVERED")
            return EXIT_OK

    print(f"结论: FAILED(验证 {MAX_VERIFY_ATTEMPTS} 次仍未恢复)")
    return EXIT_UNRESOLVED


def main() -> None:
    code = run_agent()
    print(f"\n退出码: {code}")
    sys.exit(code)


if __name__ == "__main__":
    main()
