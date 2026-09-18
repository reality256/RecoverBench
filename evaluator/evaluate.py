"""独立裁判:探测环境状态,输出结构化评测结果。

判定内容(V1):
1. /health:API 与 Redis 都健康。
2. /cache/{key}:返回预置的正确值(证明业务链路真的通了,而不是只看健康探针)。

成功条件:健康检查与业务读取**连续通过 3 次**,每次间隔 1 秒,且在截止时间内完成。
任何一次失败都会把连续计数清零。

判定结果:
    PASS  —— 达到恢复条件
    FAIL  —— 有效实验中未达到恢复条件
    ERROR —— 裁判或实验基础设施异常(缺少预置键值、裁判自身出错)

退出码:0 = PASS,1 = FAIL,2 = ERROR。

裁判不信任 Agent 的任何汇报 —— 环境状态才是 ground truth。
Agent 也不调用裁判;由 runner 在 Agent 结束后调用本模块。
"""

import argparse
import json
import sys
import time
import urllib.parse
from datetime import datetime
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

API_BASE = "http://localhost:8000"
HEALTH_URL = f"{API_BASE}/health"
CACHE_URL = f"{API_BASE}/cache"

PASS = "PASS"
FAIL = "FAIL"
ERROR = "ERROR"

EXIT_CODES = {PASS: 0, FAIL: 1, ERROR: 2}

# 初始工程标准:连续 3 次通过,间隔 1 秒
DEFAULT_ATTEMPTS = 3
DEFAULT_INTERVAL = 1.0
DEFAULT_DEADLINE = 60.0
PROBE_TIMEOUT = 6.0   # 降级时 /health 因 DNS 确认约需 4 秒


def _get_json(url: str, timeout: float):
    """返回 (payload, error)。payload 非 dict 也算错误。"""
    try:
        response = requests.get(url, timeout=timeout)
    except requests.RequestException as e:
        return None, f"{type(e).__name__}: {e}"

    try:
        payload = response.json()
    except ValueError:
        return None, f"响应不是合法 JSON(HTTP {response.status_code})"

    if not isinstance(payload, dict):
        return None, f"响应 JSON 不是对象(HTTP {response.status_code})"
    if response.status_code != 200:
        detail = payload.get("detail") or payload
        return None, f"HTTP {response.status_code}: {str(detail)[:150]}"
    return payload, None


def probe(key: str = None, expected_value: str = None, timeout: float = PROBE_TIMEOUT) -> dict:
    """单次检查:健康 + 业务读取。返回结构化明细(供 judge 和 runner 复用)。"""
    detail = {
        "ok": False,
        "health": None,
        "cache": None,
        "reasons": [],
        "checked_at": datetime.now().isoformat(timespec="milliseconds"),
    }

    payload, error = _get_json(HEALTH_URL, timeout)
    if error:
        detail["reasons"].append(f"健康检查失败: {error}")
    else:
        detail["health"] = {
            "status": payload.get("status"),
            "redis": payload.get("redis"),
        }
        if payload.get("status") != "healthy" or payload.get("redis") != "healthy":
            detail["reasons"].append(
                f"健康检查未通过: status={payload.get('status')}, redis={payload.get('redis')}")

    if key is not None:
        url = f"{CACHE_URL}/{urllib.parse.quote(key, safe='')}"
        payload, error = _get_json(url, timeout)
        if error:
            detail["reasons"].append(f"业务读取失败: {error}")
        else:
            value = payload.get("value")
            detail["cache"] = {"key": payload.get("key", key), "value": value}
            if value != expected_value:
                detail["reasons"].append(f"业务读取不一致: 期望 {expected_value!r}, 实际 {value!r}")

    detail["ok"] = not detail["reasons"]
    return detail


def evaluate(
    key: str = None,
    expected_value: str = None,
    attempts: int = DEFAULT_ATTEMPTS,
    interval: float = DEFAULT_INTERVAL,
    deadline: float = DEFAULT_DEADLINE,
    max_checks: int = None,
) -> dict:
    """连续 attempts 次通过才判 PASS;返回结构化结果。

    循环由截止时间兜底:中途失败会把连续计数清零,然后继续等到截止时间为止 ——
    这样"偶发一次抖动"不会直接判死,而"一直没恢复"也不会无限等下去。
    """
    started = time.monotonic()

    if key is not None and expected_value is None:
        return _result(ERROR, "裁判输入不完整:提供了 key 但没有预置期望值", attempts, [], started)

    max_checks = max_checks if max_checks is not None else max(attempts * 5, attempts)
    checks = []
    consecutive = 0

    while len(checks) < max_checks:
        detail = probe(key, expected_value)
        checks.append(detail)
        consecutive = consecutive + 1 if detail["ok"] else 0

        if consecutive >= attempts:
            return _result(PASS, f"健康检查与业务读取连续通过 {attempts} 次", attempts, checks, started)

        elapsed = time.monotonic() - started
        if elapsed + interval > deadline:
            last = "; ".join(detail["reasons"]) or "未通过"
            return _result(
                FAIL,
                f"超过评测截止时间({deadline:.0f}s),未完成连续 {attempts} 次通过(最后失败原因: {last})",
                attempts, checks, started)
        time.sleep(interval)

    last = "; ".join(checks[-1]["reasons"]) if checks else "没有执行任何检查"
    return _result(FAIL, f"达到最大检查次数({max_checks}),连续 {attempts} 次通过未达成(最后失败原因: {last})",
                   attempts, checks, started)


def _result(verdict: str, reason: str, attempts: int, checks: list, started: float) -> dict:
    return {
        "verdict": verdict,
        "reason": reason,
        "required_attempts": attempts,
        "checks_run": len(checks),
        "checks": checks,
        "duration_ms": int((time.monotonic() - started) * 1000),
        "finished_at": datetime.now().isoformat(timespec="seconds"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="RecoverBench 独立裁判")
    parser.add_argument("--key", default=None, help="预置的业务测试键(省略则只做健康检查)")
    parser.add_argument("--value", default=None, help="该键的期望值")
    parser.add_argument("--attempts", type=int, default=DEFAULT_ATTEMPTS)
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL)
    parser.add_argument("--deadline", type=float, default=DEFAULT_DEADLINE)
    parser.add_argument("--attach", default=None, help="把判定写进某次运行的 result.json")
    parser.add_argument("--json", action="store_true", help="输出完整结构化结果")
    args = parser.parse_args()

    result = evaluate(args.key, args.value, args.attempts, args.interval, args.deadline)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(result["verdict"])
        print(f"  {result['reason']}")
        if args.key is None:
            print("  注意:未提供 --key,本次只检查了健康探针,没有验证业务读取。")

    if args.attach:
        from agent.trace import attach_evaluator_result

        attach_evaluator_result(args.attach, result)
        print(f"  已附到: {args.attach}")

    return EXIT_CODES[result["verdict"]]


if __name__ == "__main__":
    sys.exit(main())
