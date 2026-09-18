"""对照实验报告:把多次 benchmark 的结果汇成一张表。

用法:
    python scripts/report_benchmark.py                 # 汇总 runs/ 下所有 benchmark
    python scripts/report_benchmark.py bench-xxx-1 bench-xxx-2
    python scripts/report_benchmark.py --latest 4

输出:
1. 结果表(策略 × 场景):有效轮数、裁判通过率、Agent 耗时、工具数、修改数、Token
2. 无效实验(环境准备/注入失败,不计入通过率)
3. 错报成功(Agent 自称成功但裁判判失败)
4. 失败原因清单

注意:正常场景(healthy)的通过率代表"保持健康",与故障场景的"恢复成功率"
含义不同,必须分开解读。
"""

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

from agent.trace import RUNS_ROOT  # noqa: E402

SUCCESS_CLAIMS = {"RECOVERED", "ALREADY_HEALTHY"}


def load_summaries(bench_ids=None, latest=None):
    if bench_ids:
        dirs = [RUNS_ROOT / name for name in bench_ids]
    else:
        dirs = sorted([d for d in RUNS_ROOT.iterdir() if (d / "summary.json").exists()],
                      key=lambda d: d.stat().st_mtime)
        if latest:
            dirs = dirs[-latest:]

    summaries = []
    for path in dirs:
        summary_path = path / "summary.json"
        if not summary_path.exists():
            print(f"(跳过 {path.name}:没有 summary.json)")
            continue
        summaries.append(json.loads(summary_path.read_text(encoding="utf-8")))
    return summaries


def average(values):
    values = [v for v in values if isinstance(v, (int, float))]
    return sum(values) / len(values) if values else None


def collect_rounds(summaries):
    rows = {}
    invalid, false_success, failures = [], [], []

    for summary in summaries:
        for record in summary.get("rounds", []):
            status = record.get("status")
            claim = record.get("agent_claim")
            verdict = record.get("verdict")

            if status in ("pass", "fail"):
                key = (summary["policy"], summary["scenario"])
                rows.setdefault(key, []).append(record)

            if status in ("setup_failed", "infra_error"):
                invalid.append((summary["benchmark_id"], record))
            elif verdict == "FAIL":
                if claim in SUCCESS_CLAIMS:
                    false_success.append((summary["benchmark_id"], record))
                failures.append((summary["benchmark_id"], record))

    return rows, invalid, false_success, failures


def main():
    parser = argparse.ArgumentParser(description="RecoverBench 对照实验报告")
    parser.add_argument("bench_ids", nargs="*", help="指定 benchmark ID(默认汇总全部)")
    parser.add_argument("--latest", type=int, default=None, help="只看最近 N 次 benchmark")
    args = parser.parse_args()

    summaries = load_summaries(args.bench_ids or None, args.latest)
    if not summaries:
        print(f"{RUNS_ROOT} 下还没有 benchmark 结果。先跑:")
        print("  python scripts/run_benchmark.py --policy rule --scenario redis_down --repeat 5")
        return 1

    rows, invalid, false_success, failures = collect_rounds(summaries)

    print("===== 对照实验结果 =====")
    print(f"汇总 {len(summaries)} 次 benchmark,共 "
          f"{sum(len(s.get('rounds', [])) for s in summaries)} 轮\n")

    header = f"{'策略':<6} {'场景':<12} {'有效轮数':>8} {'裁判通过率':>10} {'Agent耗时':>10} {'工具数':>7} {'修改数':>7} {'Token':>8}"
    print(header)
    print("-" * len(header))

    for (policy, scenario) in sorted(rows, key=lambda k: (k[1], k[0])):
        records = rows[(policy, scenario)]
        passed = sum(1 for r in records if r.get("verdict") == "PASS")
        rate = f"{passed / len(records) * 100:.0f}%" if records else "-"
        agent_ms = average([r.get("agent_ms") for r in records])
        tools = average([r.get("tool_calls") for r in records])
        mods = average([r.get("modifications") for r in records])
        tokens = average([r.get("tokens") for r in records])

        def fmt(value, unit="", digits=0):
            if value is None:
                return "-"
            return f"{value:.{digits}f}{unit}"

        print(f"{policy:<6} {scenario:<12} {len(records):>8} {rate:>10} "
              f"{fmt(agent_ms, 'ms'):>10} {fmt(tools, '', 1):>7} {fmt(mods, '', 1):>7} {fmt(tokens, '', 0):>8}")

    print("\n提示:healthy 场景的通过率代表\"保持健康\"(不做多余修改),"
          "与 redis_down 的\"恢复成功率\"含义不同,需分开解读。")

    if invalid:
        print(f"\n===== 无效实验({len(invalid)} 轮,未计入通过率)=====")
        for bench_id, record in invalid:
            print(f"  [{bench_id} 第{record.get('round')}轮] {record.get('status')}: "
                  f"{record.get('stage', '')} —— {str(record.get('detail', ''))[:150]}")

    if false_success:
        print(f"\n===== 错报成功({len(false_success)} 轮:Agent 自称成功,裁判判失败)=====")
        for bench_id, record in false_success:
            print(f"  [{bench_id} 第{record.get('round')}轮] Agent 自述 {record.get('agent_claim')},"
                  f"裁判 {record.get('verdict')} —— {str(record.get('verdict_reason', ''))[:150]}")

    if failures:
        print(f"\n===== 失败原因({len(failures)} 轮)=====")
        for bench_id, record in failures:
            print(f"  [{bench_id} 第{record.get('round')}轮] {str(record.get('verdict_reason', ''))[:150]}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
