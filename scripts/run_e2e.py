"""冒烟级端到端验收:环境启动 → 预置数据 → 基线判定 → 注入故障 → Agent → 独立判定 → 轨迹完整。

比 run_benchmark.py 更轻,适合改完代码快速确认链路没断。
重复实验、多轮统计请用:
    python scripts/run_benchmark.py --policy rule --scenario redis_down --repeat 5

用法:
    python scripts/run_e2e.py
"""

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

from agent.trace import latest_run_dir  # noqa: E402

ENCODING = "utf-8"
CHILD_ENV = {**os.environ, "PYTHONIOENCODING": "utf-8"}
TEST_KEY = "race:e2e_key"


def run_cmd(args, timeout=180):
    result = subprocess.run(
        args, cwd=PROJECT_ROOT, capture_output=True, text=True,
        encoding=ENCODING, errors="replace", env=CHILD_ENV, timeout=timeout,
    )
    return result.returncode, result.stdout, result.stderr


def run_py(script, *extra, timeout=180):
    return run_cmd([sys.executable, script, *extra], timeout=timeout)


def step(name, fn, expect=None):
    """expect: (期望退出码, stdout 必须包含的字符串)。"""
    start = time.time()
    code, stdout, stderr = fn()
    elapsed = time.time() - start

    ok = code == 0
    if expect is not None:
        want_code, want_text = expect
        ok = (code == want_code) and (want_text in stdout if want_text else True)

    print(f"[{name}] {'PASS' if ok else 'FAIL'} (exit={code}, {elapsed:.1f}s)")
    for line in stdout.strip().splitlines()[-10:]:
        print(f"    | {line}")
    if not ok and stderr.strip():
        for line in stderr.strip().splitlines()[-10:]:
            print(f"    ! {line}")
    return ok


def check_run_dir():
    """轨迹目录:三个文件齐全、trace.jsonl 可解析、裁判结果已附上。"""
    run_dir = latest_run_dir()
    if run_dir is None:
        print("    ! 没有找到任何运行记录")
        return 1, "", "no runs"

    lines_out = [f"运行目录: {run_dir}"]
    missing = [n for n in ("config.json", "trace.jsonl", "result.json") if not (run_dir / n).exists()]
    if missing:
        print(f"    ! 缺少文件: {missing}")
        return 1, "\n".join(lines_out), f"missing {missing}"

    trace_lines = (run_dir / "trace.jsonl").read_text(encoding="utf-8").strip().splitlines()
    for line in trace_lines:
        json.loads(line)
    result = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
    evaluator = result.get("evaluator") or {}
    if not evaluator:
        print("    ! result.json 里没有裁判结果")
        return 1, "\n".join(lines_out), "no evaluator verdict"

    lines_out.append(f"trace 事件: {len(trace_lines)} 条")
    lines_out.append(f"Agent 自述: {result.get('label')} | 裁判: {evaluator.get('verdict')}")
    return 0, "\n".join(lines_out), ""


def main():
    print("===== RecoverBench 端到端冒烟验收 =====")
    ok = True

    # 每轮随机值:证明裁判读到的是本次写入的数据
    value = f"v-{uuid.uuid4().hex[:12]}"

    ok &= step("环境启动", lambda: run_cmd(["docker", "compose", "up", "-d"]))
    ok &= step("预置业务数据", lambda: run_cmd(
        ["docker", "compose", "exec", "-T", "redis", "redis-cli", "set", TEST_KEY, value]))
    ok &= step("基线判定", lambda: run_py("evaluator/evaluate.py", "--key", TEST_KEY, "--value", value),
               expect=(0, "PASS"))
    ok &= step("注入故障", lambda: run_py("scripts/inject_fault.py"))
    # 裁判退出码:0=PASS 1=FAIL 2=ERROR
    ok &= step("故障判定", lambda: run_py("evaluator/evaluate.py", "--key", TEST_KEY, "--value", value),
               expect=(1, "FAIL"))
    ok &= step("Agent 恢复", lambda: run_py("agent/agent.py"), expect=(0, "RECOVERED"))

    run_dir = latest_run_dir()
    if run_dir is None:
        print("[恢复判定] FAIL (没有运行目录可供附加)")
        ok = False
    else:
        ok &= step("恢复判定", lambda: run_py("evaluator/evaluate.py", "--key", TEST_KEY,
                                              "--value", value, "--attach", str(run_dir)),
                   expect=(0, "PASS"))
        ok &= step("轨迹完整", check_run_dir)

    print("===== 结果: {} =====".format("全部通过" if ok else "有步骤失败"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
