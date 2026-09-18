"""统一实验入口:一条命令重复跑"注入故障 → Agent 恢复 → 独立裁判"。

用法:
    python scripts/run_benchmark.py --policy rule --scenario redis_down --repeat 5
    python scripts/run_benchmark.py --policy llm  --scenario redis_down --repeat 5
    python scripts/run_benchmark.py --policy llm  --scenario healthy    --repeat 5

每轮严格按顺序执行:
    恢复到干净基线 → 等待就绪 → 预置业务测试数据 → 验证基线
    → 注入故障(或保持正常) → 验证场景状态 → 启动 Agent → 独立裁判评测
    → 保存结果 → 清理环境

原则:
- 每轮独立重置,绝不沿用上一轮修复后的状态。
- 基线或注入失败时不启动 Agent(记为 setup_failed)。
- 清理放在 finally 里;清理失败立即中止整个实验,避免污染后续轮次。
- runner 可以调用重置脚本/命令,Agent 不可以。
- Agent 自述结果与裁判结果分别保存。
- 准备耗时、Agent 耗时、裁判耗时分别记录。
- V1 顺序执行,避免多轮共用同一套 Docker 服务互相干扰。

退出码:0 = 所有轮次裁判 PASS;1 = 有轮次判 FAIL;2 = 实验基础设施出错。
"""

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

import yaml  # noqa: E402

from agent.trace import RUNS_ROOT, attach_evaluator_result, summarize_run  # noqa: E402
from evaluator.evaluate import ERROR, FAIL, PASS  # noqa: E402
from evaluator.evaluate import evaluate as evaluate_env  # noqa: E402
from evaluator.evaluate import probe  # noqa: E402

SCENARIOS_DIR = PROJECT_ROOT / "scenarios"
ENCODING = "utf-8"
CHILD_ENV = {**os.environ, "PYTHONIOENCODING": "utf-8"}

DOCKER_TIMEOUT = 120
AGENT_TIMEOUT = 300
READY_TIMEOUT = 60
SCENARIO_VERIFY_TIMEOUT = 25

# 每轮预置的业务测试键(值每轮随机,证明数据确实是本轮写入的)
TEST_KEY = "race:bench_key"


# ---------- 基础设施动作(runner 专用,Agent 没有这些能力)----------

def run_cmd(args, timeout=DOCKER_TIMEOUT):
    result = subprocess.run(
        args, cwd=PROJECT_ROOT, capture_output=True, text=True,
        encoding=ENCODING, errors="replace", env=CHILD_ENV, timeout=timeout,
    )
    return result.returncode, result.stdout, result.stderr


def docker(*args, timeout=DOCKER_TIMEOUT):
    return run_cmd(["docker", "compose", *args], timeout=timeout)


def load_scenario(name: str) -> dict:
    path = SCENARIOS_DIR / f"{name}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"找不到场景文件: {path}")
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def wait_ready(timeout: float = READY_TIMEOUT):
    """等到 API 能响应健康探针(不管健康与否,能响应就算就绪)。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if probe()["health"] is not None:
            return True, "服务已就绪"
        time.sleep(1)
    return False, f"服务在 {timeout:.0f}s 内未就绪"


def reset_baseline():
    """恢复到干净基线:所有服务启动并等待就绪。"""
    code, out, err = docker("up", "-d")
    if code != 0:
        return False, f"docker compose up 失败: {(err or out).strip()[:200]}"
    return wait_ready()


def preset_data(key: str, value: str):
    """预置业务测试数据,并回读确认写入成功。"""
    code, out, err = docker("exec", "-T", "redis", "redis-cli", "set", key, value)
    if code != 0:
        return False, f"预置数据失败: {(err or out).strip()[:200]}"

    code, out, err = docker("exec", "-T", "redis", "redis-cli", "get", key)
    if code != 0 or out.strip() != value:
        return False, f"预置数据回读不一致: 期望 {value!r}, 实际 {out.strip()!r}"
    return True, f"已预置 {key}={value}"


def inject(scenario: dict):
    """按场景注入故障。"""
    fault = scenario.get("fault") or {}
    kind = fault.get("type")

    if kind == "none":
        return True, "对照场景:不注入故障"

    if kind == "service_down":
        target = fault.get("target")
        code, out, err = docker("stop", target)
        if code != 0:
            return False, f"停止 {target} 失败: {(err or out).strip()[:200]}"
        return True, f"已停止服务: {target}"

    return False, f"未知故障类型: {kind!r}"


def verify_scenario_state(scenario: dict, key: str, expected: str):
    """注入后确认场景真的生效(否则不启动 Agent)。"""
    fault = scenario.get("fault") or {}
    kind = fault.get("type")

    if kind == "none":
        detail = probe(key, expected)
        if not detail["ok"]:
            return False, f"对照场景基线异常: {'; '.join(detail['reasons'])}"
        return True, "对照场景:健康且业务读取正常"

    if kind == "service_down":
        target = fault.get("target")
        code, out, _ = docker("ps", "--format", "json")
        if code == 0 and target:
            running = any(
                json.loads(line).get("Service") == target and json.loads(line).get("State") == "running"
                for line in out.splitlines() if line.strip().startswith("{")
            )
            if running:
                return False, f"注入未生效: {target} 仍在运行"

        # 等服务"察觉"到故障(API 需要几秒才会变成 degraded/不可达)
        deadline = time.monotonic() + SCENARIO_VERIFY_TIMEOUT
        while time.monotonic() < deadline:
            health = probe()["health"]
            if health is None or health.get("status") != "healthy":
                return True, f"注入已生效: {target} 已停止,健康探针返回 {health}"
            time.sleep(1)
        return False, f"注入后健康探针仍报告健康(等待 {SCENARIO_VERIFY_TIMEOUT}s)"

    return False, f"未知故障类型: {kind!r}"


def run_agent(policy: str, run_id: str, runs_root: Path):
    cmd = [sys.executable, "agent/agent.py", "--policy", policy,
           "--run-id", run_id, "--runs-root", str(runs_root)]
    return run_cmd(cmd, timeout=AGENT_TIMEOUT)


# ---------- 单轮实验 ----------

def run_round(bench_id: str, index: int, policy: str, scenario: dict,
              runs_root: Path, total: int):
    """跑一轮,返回 (轮次记录, 是否需要中止整个实验)。"""
    run_id = f"r{index}"
    run_dir = runs_root / run_id
    key = TEST_KEY
    value = f"v-{uuid.uuid4().hex[:12]}"

    record = {
        "round": index,
        "total": total,
        "run_id": run_id,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "test_key": key,
        "expected_value": value,
    }
    print(f"\n----- 第 {index}/{total} 轮 -----")

    def setup_failed(stage, detail):
        record.update({"status": "setup_failed", "stage": stage, "detail": detail})
        print(f"  [{stage}] 失败: {detail} → 不启动 Agent")
        return record, False

    # 1-4 恢复到干净基线 → 就绪 → 预置数据 → 验证基线
    prep_start = time.monotonic()

    ok, detail = reset_baseline()
    print(f"  [基线重置] {detail}")
    if not ok:
        return setup_failed("基线重置", detail)

    ok, detail = preset_data(key, value)
    print(f"  [预置数据] {detail}")
    if not ok:
        return setup_failed("预置数据", detail)

    baseline = probe(key, value)
    if not baseline["ok"]:
        return setup_failed("基线验证", "; ".join(baseline["reasons"]))
    print("  [基线验证] 健康且业务读取正常")

    # 5-6 注入故障 → 验证场景状态
    ok, detail = inject(scenario)
    print(f"  [注入故障] {detail}")
    if not ok:
        return setup_failed("注入故障", detail)

    ok, detail = verify_scenario_state(scenario, key, value)
    print(f"  [场景验证] {detail}")
    if not ok:
        return setup_failed("场景验证", detail)

    record["prep_ms"] = int((time.monotonic() - prep_start) * 1000)

    # 7 启动 Agent
    agent_start = time.monotonic()
    code, out, err = run_agent(policy, run_id, runs_root)
    record["agent_ms"] = int((time.monotonic() - agent_start) * 1000)
    record["agent_exit_code"] = code
    record["agent_output"] = out[-4000:]
    if err.strip():
        record["agent_stderr"] = err[-1500:]

    # 8 独立裁判评测(在 Agent 结束后;Agent 自己不会调用裁判)
    judge_start = time.monotonic()
    try:
        verdict = evaluate_env(key=key, expected_value=value)
    except Exception as e:
        verdict = {"verdict": ERROR, "reason": f"裁判执行异常: {type(e).__name__}: {e}",
                   "checks": [], "checks_run": 0}
    record["judge_ms"] = int((time.monotonic() - judge_start) * 1000)
    record["verdict"] = verdict["verdict"]
    record["verdict_reason"] = verdict["reason"]
    record["judge_checks_run"] = verdict.get("checks_run")

    if run_dir.exists():
        attach_evaluator_result(run_dir, verdict)

    # Agent 自述与裁判结果分别保存
    usage = summarize_run(run_dir)
    record["agent_claim"] = usage["label"] or "unknown"
    record["tool_calls"] = usage["tool_calls"]
    record["modifications"] = usage["modifications"]
    record["model_calls"] = usage["model_calls"]
    record["tokens"] = usage["tokens"]

    record["status"] = "pass" if verdict["verdict"] == PASS else (
        "fail" if verdict["verdict"] == FAIL else "infra_error")
    print(f"  [Agent] 退出码 {code},自述 {record['agent_claim']},耗时 {record['agent_ms']}ms")
    print(f"  [裁判] {verdict['verdict']} —— {verdict['reason']}")

    return record, False


def cleanup():
    """清理:恢复到干净基线。"""
    ok, detail = reset_baseline()
    return ok, detail


# ---------- 主流程 ----------

def main() -> int:
    parser = argparse.ArgumentParser(description="RecoverBench 统一实验入口")
    parser.add_argument("--policy", choices=["rule", "llm"], default="rule")
    parser.add_argument("--scenario", default="redis_down")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--benchmark-id", default=None)
    args = parser.parse_args()

    scenario = load_scenario(args.scenario)
    bench_id = args.benchmark_id or f"bench-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    runs_root = RUNS_ROOT / bench_id
    runs_root.mkdir(parents=True, exist_ok=True)
    rounds_path = runs_root / "rounds.jsonl"

    print("===== RecoverBench 实验 =====")
    print(f"  实验 ID : {bench_id}")
    print(f"  策略    : {args.policy}")
    print(f"  场景    : {args.scenario} ({scenario.get('id')})")
    print(f"  轮数    : {args.repeat}")
    print(f"  轨迹    : {runs_root}")

    started = time.monotonic()
    records = []
    abort_reason = None

    for index in range(1, args.repeat + 1):
        record = None
        try:
            record, abort = run_round(bench_id, index, args.policy, scenario, runs_root, args.repeat)
        except Exception as e:
            record = {"round": index, "run_id": f"r{index}", "status": "infra_error",
                      "started_at": datetime.now().isoformat(timespec="seconds"),
                      "detail": f"轮次执行异常: {type(e).__name__}: {e}"}
            print(f"  轮次异常: {record['detail']}")
        finally:
            # 清理必须在 finally:即使轮次异常也要恢复环境
            cleanup_start = time.monotonic()
            ok, detail = cleanup()
            if record is not None:
                record["cleanup_ms"] = int((time.monotonic() - cleanup_start) * 1000)
                record["cleanup_ok"] = ok
            if not ok:
                abort_reason = f"清理失败,已中止后续实验(避免污染): {detail}"
                print(f"  [清理] 失败: {detail}")

        if record is not None:
            records.append(record)
            with rounds_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")

        if abort_reason:
            break

    # 汇总
    totals = {"pass": 0, "fail": 0, "setup_failed": 0, "infra_error": 0}
    for record in records:
        totals[record.get("status", "infra_error")] = totals.get(record.get("status", "infra_error"), 0) + 1

    def average(field):
        values = [record[field] for record in records if isinstance(record.get(field), int)]
        return int(sum(values) / len(values)) if values else None

    # 有效轮次 = Agent 真的跑过并出了裁判结论的轮次(不含 setup_failed / infra_error)
    valid = [record for record in records if record.get("status") in ("pass", "fail")]

    summary = {
        "benchmark_id": bench_id,
        "policy": args.policy,
        "scenario": args.scenario,
        "scenario_id": scenario.get("id"),
        "repeat": args.repeat,
        "started_at": records[0].get("started_at") if records else None,
        "finished_at": datetime.now().isoformat(timespec="seconds"),
        "duration_ms": int((time.monotonic() - started) * 1000),
        "totals": totals,
        "valid_rounds": len(valid),
        "pass_rate": round(totals["pass"] / len(valid), 3) if valid else None,
        "timing_avg_ms": {
            "prep": average("prep_ms"),
            "agent": average("agent_ms"),
            "judge": average("judge_ms"),
            "cleanup": average("cleanup_ms"),
        },
        "usage_avg": {
            "tool_calls": average("tool_calls"),
            "modifications": average("modifications"),
            "tokens": average("tokens"),
        },
        "abort_reason": abort_reason,
        "rounds": records,
    }
    (runs_root / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n===== 汇总 =====")
    print(f"  PASS {totals['pass']} / FAIL {totals['fail']} / "
          f"setup_failed {totals['setup_failed']} / infra_error {totals['infra_error']}")
    print(f"  平均耗时: 准备 {summary['timing_avg_ms']['prep']}ms | "
          f"Agent {summary['timing_avg_ms']['agent']}ms | 裁判 {summary['timing_avg_ms']['judge']}ms")
    if abort_reason:
        print(f"  ⚠ {abort_reason}")
    print(f"  汇总文件: {runs_root / 'summary.json'}")

    if totals["infra_error"] or totals["setup_failed"] or abort_reason:
        return 2
    return 0 if totals["fail"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
