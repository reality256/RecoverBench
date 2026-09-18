"""端到端验收脚本:环境启动 → 基线判定 → 注入故障 → Agent 恢复 → 独立判定。

用法:
    python scripts/run_e2e.py

任何一步失败都会如实报告,整体退出码:0 = 全部通过,1 = 有步骤失败。
"""

import os
import subprocess
import sys
import time
from pathlib import Path

# 打印端防御:遇到控制台/管道编不出的字符,替换而不是崩溃
try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 编码策略:显式要求子进程用 UTF-8 写管道(Windows 上默认按 GBK 写),
# 解码端同样用 UTF-8,两端对齐,杜绝乱码和替换字符。
ENCODING = "utf-8"
CHILD_ENV = {**os.environ, "PYTHONIOENCODING": "utf-8"}


def run_cmd(args, timeout=180):
    """执行任意命令,返回 (returncode, stdout, stderr)。"""
    result = subprocess.run(
        args,
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        encoding=ENCODING,
        errors="replace",
        env=CHILD_ENV,
        timeout=timeout,
    )
    return result.returncode, result.stdout, result.stderr


def run_py(script, timeout=180):
    return run_cmd([sys.executable, script], timeout)


def step(name, fn, expect=None):
    """执行一个验收步骤。

    expect: (期望退出码, stdout 必须包含的字符串),None 表示只要求退出码 0。
    """
    start = time.time()
    code, stdout, stderr = fn()
    elapsed = time.time() - start

    ok = code == 0
    if expect is not None:
        want_code, want_text = expect
        ok = (code == want_code) and (want_text in stdout if want_text else True)

    print(f"[{name}] {'PASS' if ok else 'FAIL'} (exit={code}, {elapsed:.1f}s)")
    for line in stdout.strip().splitlines():
        print(f"    | {line}")
    if not ok and stderr.strip():
        for line in stderr.strip().splitlines()[-10:]:
            print(f"    ! {line}")
    return ok


def main():
    print("===== RecoverBench 端到端验收 =====")
    ok = True

    # 1. 环境启动
    ok &= step("环境启动", lambda: run_cmd(["docker", "compose", "up", "-d"]))
    # 2. 基线判定:健康 → PASS
    ok &= step("基线判定", lambda: run_py("evaluator/evaluate.py"), expect=(0, "PASS"))
    # 3. 注入故障
    ok &= step("注入故障", lambda: run_py("scripts/inject_fault.py"))
    # 4. 故障判定:必须 FAIL —— 证明故障真的注入成功了
    ok &= step("故障判定", lambda: run_py("evaluator/evaluate.py"), expect=(0, "FAIL"))
    # 5. Agent 恢复:必须 RECOVERED,退出码 0
    ok &= step("Agent 恢复", lambda: run_py("agent/agent.py"), expect=(0, "RECOVERED"))
    # 6. 恢复判定:独立裁判必须 PASS
    ok &= step("恢复判定", lambda: run_py("evaluator/evaluate.py"), expect=(0, "PASS"))

    print("===== 结果: {} =====".format("全部通过" if ok else "有步骤失败"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
