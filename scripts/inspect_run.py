"""查看一次运行的轨迹:只读 runs/<run_id>/,还原"看到了什么、做了什么、为什么结束"。

用法:
    python scripts/inspect_run.py              # 看最近一次运行
    python scripts/inspect_run.py <run_id>     # 看指定运行
"""

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

from agent.trace import RUNS_ROOT, latest_run_dir  # noqa: E402


def load_json(path):
    if not path.exists():
        print(f"(缺少 {path.name})")
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def main():
    if len(sys.argv) > 1:
        run_dir = RUNS_ROOT / sys.argv[1]
    else:
        run_dir = latest_run_dir()
        if run_dir is None:
            print(f"还没有任何运行记录({RUNS_ROOT})")
            return 1

    print(f"===== 运行 {run_dir.name} =====")

    config = load_json(run_dir / "config.json") or {}
    print("\n--- config.json ---")
    print(f"  策略      : {config.get('policy')} ({config.get('policy_version')})")
    print(f"  模型      : {config.get('model') or '未接入'}")
    print(f"  代码版本  : {config.get('code_version')}")
    budget = config.get("budget") or {}
    print(f"  预算      : 工具 {budget.get('max_tool_calls')} 次 / "
          f"修改 {budget.get('max_modifications')} 次 / {budget.get('max_seconds')} 秒")

    trace_path = run_dir / "trace.jsonl"
    print("\n--- trace.jsonl(时间线)---")
    if not trace_path.exists():
        print("  (缺少 trace.jsonl)")
    else:
        for line in trace_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            kind = event.get("event")
            if kind == "tool_result":
                data = (event.get("result") or {}).get("data")
                error = (event.get("result") or {}).get("error")
                summary = json.dumps(data, ensure_ascii=False) if data is not None else f"错误: {error}"
                print(f"  [{event['step']}] {event['tool']}({json.dumps(event['arguments'], ensure_ascii=False)})"
                      f" → {summary}  ({event.get('duration_ms')}ms)")
            elif kind == "finish":
                print(f"  结束: {event.get('conclusion')} —— {event.get('evidence')}")
            elif kind == "model_call":
                print(f"  [{event['step']}] 模型调用 {event.get('model')} "
                      f"({event.get('duration_ms')}ms, retry={event.get('retry')})")

    result = load_json(run_dir / "result.json") or {}
    print("\n--- result.json ---")
    print(f"  Agent 结论: {result.get('label')} ({result.get('conclusion')})")
    print(f"  证据      : {result.get('evidence')}")
    print(f"  耗时      : {result.get('duration_ms')}ms | 步骤: {result.get('steps')}")
    b = result.get("budget") or {}
    print(f"  用量      : 工具 {b.get('used_tool_calls')} 次 / 修改 {b.get('used_modifications')} 次 / "
          f"模型 {b.get('used_model_calls')} 次")
    evaluator = result.get("evaluator")
    print(f"  裁判      : {evaluator.get('verdict') if evaluator else '未判定'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
