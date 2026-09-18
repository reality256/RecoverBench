"""LLM 策略的"干跑"验收:用脚本化的假模型驱动完整闭环,不需要 API key、不花钱。

它跑的是真实的 LlmPolicy + 真实工具 + 真实环境,只有"模型本身"是假的 ——
用来在没有 key 的情况下验证整条链路(上下文构造、工具分发、预算、轨迹)是否正常。

用法:
    python scripts/run_llm_dryrun.py
"""

import contextlib
import io
import json
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

from agent import tools  # noqa: E402
from agent.budget import Budget  # noqa: E402
from agent.llm_client import LlmResponse, ToolCallRequest  # noqa: E402
from agent.loop import run_loop  # noqa: E402
from agent.policies.llm import FINISH_TOOL_NAME, LlmPolicy  # noqa: E402
from agent.trace import RunRecorder  # noqa: E402


class ScriptedClient:
    """假模型:按剧本返回工具调用,模拟一个称职的排查者。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def chat(self, messages, tools=None, timeout=None, max_tokens=None):
        self.calls.append(messages)
        name, args = self.script.pop(0)
        return LlmResponse(
            ok=True,
            content="",
            tool_calls=[ToolCallRequest(id="call_1", name=name, arguments=json.dumps(args))],
            usage={"prompt_tokens": 500, "completion_tokens": 30, "total_tokens": 530},
        )


def call(name, **args):
    return (name, args)


NORMAL_SCRIPT = [call("get_api_health"), call(FINISH_TOOL_NAME, conclusion="healthy",
                                              evidence="get_api_health 返回 healthy/healthy")]
FAULT_SCRIPT = [
    call("get_api_health"),
    call("get_service_status", service="redis"),
    call("start_service", service="redis"),
    call("get_api_health"),
    call(FINISH_TOOL_NAME, conclusion="recovered", evidence="启动 redis 后 /health 恢复 healthy"),
]


def docker(*args):
    subprocess.run(["docker", "compose", *args], cwd=PROJECT_ROOT,
                   capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)


def run_once(label, script, run_id):
    recorder = RunRecorder(run_id=run_id)
    budget = Budget()
    policy = LlmPolicy(ScriptedClient(script), budget=budget, recorder=recorder, config=None)
    recorder.write_config(policy="LlmPolicy(dry-run)", model="scripted-fake",
                          prompt_version="dryrun", budget=budget.snapshot())

    print(f"\n===== {label} =====")
    with contextlib.redirect_stdout(io.StringIO()) as captured:
        finish, history = run_loop(policy, tools.TOOL_REGISTRY, budget=budget, recorder=recorder)
    for line in captured.getvalue().splitlines():
        print(f"  {line}")

    recorder.write_result(conclusion=finish.conclusion, label=finish.conclusion.upper(),
                          evidence=finish.evidence, exit_code=0, steps=len(history),
                          budget=budget.snapshot(),
                          tools_used=[entry.decision.name for entry in history])
    print(f"  结论: {finish.conclusion} —— {finish.evidence}")
    print(f"  工具序列: {[e.decision.name for e in history]}")
    print(f"  环境修改次数: {budget.modifications}")
    print(f"  轨迹目录: {recorder.run_dir.name}")
    return finish, history, budget


def main():
    print("===== LLM 策略干跑(假模型 + 真实环境)=====")
    ok = True

    # 场景 1:正常环境 —— 模型检查后结束,不做任何修改
    docker("up", "-d")
    finish, history, budget = run_once("场景 1:正常环境", NORMAL_SCRIPT, "dryrun-normal")
    if finish.conclusion != "healthy":
        print("  ! 期望 healthy")
        ok = False
    if budget.modifications != 0:
        print(f"  ! 正常环境不应有任何修改,实际 {budget.modifications} 次")
        ok = False

    # 场景 2:Redis 停止 —— 模型自主观测、启动、验证
    docker("stop", "redis")
    finish, history, budget = run_once("场景 2:Redis 停止", FAULT_SCRIPT, "dryrun-fault")
    if finish.conclusion != "recovered":
        print("  ! 期望 recovered")
        ok = False
    if budget.modifications != 1:
        print(f"  ! 期望恰好 1 次修改,实际 {budget.modifications} 次")
        ok = False

    # 场景 3:独立裁判判定恢复结果
    print("\n===== 场景 3:独立裁判 =====")
    result = subprocess.run([sys.executable, "evaluator/evaluate.py", "--attach",
                             str(Path("runs/dryrun-fault"))],
                            cwd=PROJECT_ROOT, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=60)
    print("  " + result.stdout.strip().replace("\n", "\n  "))
    if "PASS" not in result.stdout:
        print("  ! 裁判未通过")
        ok = False

    print("\n===== 结果: {} =====".format("全部通过" if ok else "有步骤失败"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
