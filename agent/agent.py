"""命令行入口:选策略 → 建预算 → 跑公共循环 → 写运行轨迹 → 打印结论与退出码。

用法:
    python agent/agent.py                  # 规则策略(不需要 API key)
    python agent/agent.py --policy llm     # 模型策略(需要 .env 里的 API key)

退出码:
    0 = 已健康或恢复成功
    1 = 诊断后仍无法恢复
    2 = Agent 自身出错(工具调用失败、超出预算、配置错误)

注意:退出码反映的是 Agent 自己的结论(模型策略下即模型的判断)。
真正的 ground truth 是裁判(evaluator)的判定,两者都记在 runs/<id>/result.json 里。
"""

import argparse
import sys
from pathlib import Path

# 保证无论从哪个目录、以哪种方式启动,都能 import 到 agent 包
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import tools  # noqa: E402
from agent.budget import Budget  # noqa: E402
from agent.config import AppConfig  # noqa: E402
from agent.loop import run_loop  # noqa: E402
from agent.policies.rule import RulePolicy  # noqa: E402
from agent.trace import RunRecorder  # noqa: E402

# 结论 → (展示名, 退出码)
CONCLUSION_LABELS = {
    "healthy": "ALREADY_HEALTHY",
    "recovered": "RECOVERED",
    "unresolved": "UNRESOLVED",
    "failed": "FAILED",
    "budget_exceeded": "BUDGET_EXCEEDED",
    "agent_error": "AGENT_ERROR",
}

EXIT_CODES = {
    "healthy": 0,
    "recovered": 0,
    "unresolved": 1,
    "failed": 1,
    "budget_exceeded": 2,
    "agent_error": 2,
}


def build_policy(name: str, budget: Budget, recorder: RunRecorder, task: str = None):
    """按名字构造策略,并返回 (policy, 用于 config.json 的元信息)。"""
    if name == "rule":
        policy = RulePolicy()
        return policy, {
            "policy": type(policy).__name__,
            "policy_version": "rule-v1",
            "model": None,
            "prompt_version": None,
            "llm": None,
        }

    # llm
    from agent.llm_client import LlmClient
    from agent.policies.llm import DEFAULT_TASK, LlmPolicy, prompt_version

    config = AppConfig.from_env()
    if not config.has_api_key:
        print("缺少 API key:请在项目根目录的 .env 里设置 LLM_API_KEY(可参考 .env.example)")
        print("退出码: 2")
        sys.exit(2)

    task_text = task or DEFAULT_TASK
    client = LlmClient(config)
    policy = LlmPolicy(client, budget=budget, recorder=recorder, task=task_text, config=config)
    return policy, {
        "policy": type(policy).__name__,
        "policy_version": prompt_version(task_text),
        "model": config.model,
        "prompt_version": prompt_version(task_text),
        "llm": config.redacted(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="RecoverBench Agent")
    parser.add_argument("--policy", choices=["rule", "llm"], default="rule",
                        help="决策策略:rule(默认,确定性规则)或 llm(模型)")
    parser.add_argument("--task", default=None, help="覆盖默认任务描述(正常/故障场景应使用同一段)")
    parser.add_argument("--run-id", default=None, help="指定轨迹目录名(实验 runner 用来归档每轮)")
    parser.add_argument("--runs-root", default=None, help="轨迹根目录(默认 runs/)")
    args = parser.parse_args()

    budget = Budget()
    recorder = RunRecorder(run_id=args.run_id, runs_root=Path(args.runs_root) if args.runs_root else None)
    policy, meta = build_policy(args.policy, budget, recorder, args.task)

    recorder.write_config(budget=budget.snapshot(), task=args.task, **meta)

    finish, history = run_loop(policy, tools.TOOL_REGISTRY, budget=budget, recorder=recorder)

    label = CONCLUSION_LABELS.get(finish.conclusion, finish.conclusion.upper())
    exit_code = EXIT_CODES.get(finish.conclusion, 2)

    recorder.write_result(
        conclusion=finish.conclusion,
        label=label,
        evidence=finish.evidence,
        exit_code=exit_code,
        steps=len(history),
        budget=budget.snapshot(),
        tools_used=[entry.decision.name for entry in history],
        error=finish.evidence if finish.conclusion in ("agent_error", "budget_exceeded") else None,
    )

    print(f"结论: {label}" if not finish.evidence else f"结论: {label}({finish.evidence})")
    print(f"运行记录: {recorder.run_dir}")
    print("\n提示:这是 Agent 自己的结论;最终以裁判为准 ——")
    print(f"      python evaluator/evaluate.py --attach {recorder.run_dir}")
    print(f"\n退出码: {exit_code}")
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
