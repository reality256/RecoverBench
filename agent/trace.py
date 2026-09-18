"""运行轨迹记录:runs/<run_id>/{config.json,trace.jsonl,result.json}。

- config.json  代码版本、策略、模型、prompt 版本、预算
- trace.jsonl  每次决策、工具参数、工具结果、时间、模型用量(一行一个事件)
- result.json  Agent 结论、裁判结果、耗时、调用次数、错误

凭据安全:任何键名含 api_key / token / secret / password / authorization 的字段,
写盘前一律替换为 "***",保证 API key 不会进入日志。
"""

import json
import subprocess
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RUNS_ROOT = PROJECT_ROOT / "runs"

# 精确命中的键名一律脱敏
SENSITIVE_EXACT = {
    "token", "key", "apikey", "api_key", "authorization", "auth", "bearer",
    "password", "passwd", "secret", "credentials", "private_key", "api_secret",
    "access_key", "access_token", "auth_token", "refresh_token",
}

# 后缀命中的键名(注意用单数 _token:prompt_tokens / max_tokens 是用量,不能误杀)
SENSITIVE_SUFFIXES = ("_key", "_secret", "_password", "_token", "_credentials", "_auth")

REDACTED = "***"


def _is_sensitive(key: str) -> bool:
    normalized = key.strip().lower().replace("-", "_")
    return normalized in SENSITIVE_EXACT or normalized.endswith(SENSITIVE_SUFFIXES)


def sanitize(value):
    """递归剔除凭据:键名命中敏感词的字段值一律替换。"""
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            if isinstance(key, str) and _is_sensitive(key):
                cleaned[key] = REDACTED
            else:
                cleaned[key] = sanitize(item)
        return cleaned
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    return value


def new_run_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]


def code_version() -> str:
    """当前代码版本(git 短 hash);拿不到就返回 unknown。"""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass
    return "unknown"


class RunRecorder:
    """一次运行的记录器。"""

    def __init__(self, run_id: Optional[str] = None, runs_root: Path = RUNS_ROOT):
        self.run_id = run_id or new_run_id()
        self.run_dir = Path(runs_root) / self.run_id
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.trace_path = self.run_dir / "trace.jsonl"
        self.config_path = self.run_dir / "config.json"
        self.result_path = self.run_dir / "result.json"
        self._t0 = time.monotonic()

    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self._t0) * 1000)

    # ---------- 三个文件 ----------
    def write_config(self, **fields) -> None:
        payload = {
            "run_id": self.run_id,
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "code_version": code_version(),
        }
        payload.update(fields)
        self._write_json(self.config_path, payload)

    def write_result(self, **fields) -> None:
        payload = {
            "run_id": self.run_id,
            "finished_at": datetime.now().isoformat(timespec="seconds"),
            "duration_ms": self.elapsed_ms(),
            "evaluator": None,   # 裁判结果由 evaluator 独立写入(Agent 不调裁判)
        }
        payload.update(fields)
        self._write_json(self.result_path, payload)

    # ---------- 事件 ----------
    def log(self, event: dict) -> None:
        payload = {"ts": datetime.now().isoformat(timespec="milliseconds")}
        payload.update(sanitize(event))
        with self.trace_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def log_decision(self, step: int, decision) -> None:
        self.log({
            "event": "decision",
            "step": step,
            "kind": type(decision).__name__,
            "tool": getattr(decision, "name", None),
            "arguments": getattr(decision, "args", None),
            "wait_seconds": getattr(decision, "wait_seconds", None),
        })

    def log_tool_result(self, step: int, tool: str, arguments: dict, result: dict, duration_ms: int) -> None:
        self.log({
            "event": "tool_result",
            "step": step,
            "tool": tool,
            "arguments": arguments,
            "result": result,
            "duration_ms": duration_ms,
        })

    def log_finish(self, conclusion: str, evidence: str, steps: int, budget_snapshot: dict) -> None:
        self.log({
            "event": "finish",
            "conclusion": conclusion,
            "evidence": evidence,
            "steps": steps,
            "budget": budget_snapshot,
        })

    def log_model_call(self, step: int, model: str, duration_ms: int, usage: dict = None, retry: bool = False) -> None:
        """模型用量(接 LLM 后由 LlmPolicy 调用)。只记摘要,不记内部思考。"""
        self.log({
            "event": "model_call",
            "step": step,
            "model": model,
            "duration_ms": duration_ms,
            "usage": usage or {},
            "retry": retry,
        })

    # ---------- 内部 ----------
    def _write_json(self, path: Path, payload: dict) -> None:
        with path.open("w", encoding="utf-8") as fh:
            json.dump(sanitize(payload), fh, ensure_ascii=False, indent=2)


def latest_run_dir(runs_root: Path = RUNS_ROOT) -> Optional[Path]:
    """最近一次运行目录(按修改时间)。"""
    root = Path(runs_root)
    if not root.exists():
        return None
    dirs = [d for d in root.iterdir() if d.is_dir()]
    if not dirs:
        return None
    return max(dirs, key=lambda d: d.stat().st_mtime)


def summarize_run(run_dir) -> dict:
    """从运行目录汇总一次运行的用量:工具次数、修改次数、模型调用、token、结论。

    纯读取,任何文件缺失或损坏都不抛异常(缺就记 0 / None)。
    """
    run_dir = Path(run_dir)
    summary = {
        "tool_calls": 0,
        "modifications": 0,
        "model_calls": 0,
        "tokens": 0,
        "conclusion": None,
        "label": None,
    }

    result_path = run_dir / "result.json"
    if result_path.exists():
        try:
            payload = json.loads(result_path.read_text(encoding="utf-8"))
            budget = payload.get("budget") or {}
            summary["tool_calls"] = budget.get("used_tool_calls", 0) or 0
            summary["modifications"] = budget.get("used_modifications", 0) or 0
            summary["model_calls"] = budget.get("used_model_calls", 0) or 0
            summary["conclusion"] = payload.get("conclusion")
            summary["label"] = payload.get("label")
        except (ValueError, OSError):
            pass

    trace_path = run_dir / "trace.jsonl"
    if trace_path.exists():
        tokens = 0
        try:
            for line in trace_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get("event") == "model_call":
                    value = (event.get("usage") or {}).get("total_tokens")
                    if isinstance(value, int):
                        tokens += value
        except OSError:
            pass
        summary["tokens"] = tokens

    return summary


def attach_evaluator_result(run_dir, result) -> None:
    """把裁判结果写进运行目录的 result.json(由 evaluator 独立执行后调用)。

    result 可以是结构化的评测结果 dict,也可以只是 "PASS"/"FAIL"/"ERROR" 字符串。
    """
    path = Path(run_dir) / "result.json"
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as fh:
        payload = json.load(fh)

    if isinstance(result, dict):
        payload["evaluator"] = result
    else:
        payload["evaluator"] = {
            "verdict": result,
            "checked_at": datetime.now().isoformat(timespec="seconds"),
        }

    with path.open("w", encoding="utf-8") as fh:
        json.dump(sanitize(payload), fh, ensure_ascii=False, indent=2)
