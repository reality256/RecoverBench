"""agent/trace.py 的单元测试:三个文件、轨迹可解析、凭据不落盘。

运行:
    python -m unittest discover -s tests -v
"""

import json
import os
import tempfile
import unittest
from pathlib import Path

from agent import trace as trace_mod
from agent.trace import RunRecorder, attach_evaluator_result, latest_run_dir, sanitize


class TestSanitize(unittest.TestCase):
    def test_keys_with_secrets_are_redacted(self):
        payload = {
            "api_key": "sk-real-key",
            "Authorization": "Bearer abc",
            "nested": {"token": "t0ken", "ok": 1},
            "list": [{"password": "pw"}],
            "safe": "keep me",
        }
        cleaned = sanitize(payload)
        self.assertEqual(cleaned["api_key"], "***")
        self.assertEqual(cleaned["Authorization"], "***")
        self.assertEqual(cleaned["nested"]["token"], "***")
        self.assertEqual(cleaned["nested"]["ok"], 1)
        self.assertEqual(cleaned["list"][0]["password"], "***")
        self.assertEqual(cleaned["safe"], "keep me")

    def test_scalars_pass_through(self):
        self.assertEqual(sanitize("x"), "x")
        self.assertEqual(sanitize(3), 3)

    def test_token_usage_is_not_redacted(self):
        # 回归:用量字段含 "token" 但必须保留(prompt_tokens / max_tokens 不是凭据)
        payload = {
            "usage": {"prompt_tokens": 500, "completion_tokens": 30, "total_tokens": 530},
            "max_tokens": 1024,
            "tokens": 7,
        }
        cleaned = sanitize(payload)
        self.assertEqual(cleaned["usage"]["prompt_tokens"], 500)
        self.assertEqual(cleaned["usage"]["completion_tokens"], 30)
        self.assertEqual(cleaned["usage"]["total_tokens"], 530)
        self.assertEqual(cleaned["max_tokens"], 1024)
        self.assertEqual(cleaned["tokens"], 7)

    def test_various_credential_key_names(self):
        for key in ("api-key", "API_KEY", "access_token", "refresh_token", "private_key",
                    "db_password", "client_secret", "authorization", "bearer", "token"):
            with self.subTest(key=key):
                self.assertEqual(sanitize({key: "leak"})[key], "***")

    def test_innocent_keys_containing_sensitive_words(self):
        for key in ("monkey", "keyword", "tokenizer", "keyboard"):
            with self.subTest(key=key):
                self.assertEqual(sanitize({key: "keep"})[key], "keep")


class TestRunRecorder(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.runs_root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.recorder = RunRecorder(run_id="test-run", runs_root=self.runs_root)

    def test_three_files_written(self):
        self.recorder.write_config(policy="RulePolicy", model=None, prompt_version=None, budget={"max_seconds": 120})
        self.recorder.log_decision(1, _tool_call())
        self.recorder.log_tool_result(1, "get_api_health", {}, {"ok": True, "data": {}, "error": None}, 42)
        self.recorder.log_finish("recovered", "验证通过", 1, {"used_tool_calls": 1})
        self.recorder.write_result(conclusion="recovered", label="RECOVERED", exit_code=0, steps=1)

        run_dir = self.runs_root / "test-run"
        self.assertTrue((run_dir / "config.json").exists())
        self.assertTrue((run_dir / "trace.jsonl").exists())
        self.assertTrue((run_dir / "result.json").exists())

    def test_trace_lines_are_valid_json_with_ts(self):
        self.recorder.log_decision(1, _tool_call())
        self.recorder.log_tool_result(1, "get_api_health", {}, {"ok": True, "data": {}, "error": None}, 42)
        lines = (self.runs_root / "test-run" / "trace.jsonl").read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 2)
        events = [json.loads(line) for line in lines]
        self.assertEqual(events[0]["event"], "decision")
        self.assertEqual(events[1]["event"], "tool_result")
        self.assertEqual(events[1]["tool"], "get_api_health")
        self.assertEqual(events[1]["duration_ms"], 42)
        for event in events:
            self.assertIn("ts", event)

    def test_config_records_code_version_and_budget(self):
        self.recorder.write_config(policy="RulePolicy", budget={"max_tool_calls": 10})
        payload = json.loads((self.runs_root / "test-run" / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["run_id"], "test-run")
        self.assertIn("code_version", payload)
        self.assertEqual(payload["budget"]["max_tool_calls"], 10)

    def test_secrets_never_reach_disk(self):
        self.recorder.write_config(api_key="sk-should-not-be-here", model="x")
        self.recorder.log({"event": "model_call", "authorization": "Bearer leak"})
        config_text = (self.runs_root / "test-run" / "config.json").read_text(encoding="utf-8")
        trace_text = (self.runs_root / "test-run" / "trace.jsonl").read_text(encoding="utf-8")
        self.assertNotIn("sk-should-not-be-here", config_text)
        self.assertNotIn("Bearer leak", trace_text)

    def test_result_has_evaluator_slot_and_independence(self):
        self.recorder.write_result(conclusion="recovered", exit_code=0)
        payload = json.loads((self.runs_root / "test-run" / "result.json").read_text(encoding="utf-8"))
        self.assertIsNone(payload["evaluator"])  # Agent 不自己判定
        self.assertIn("duration_ms", payload)

    def test_attach_evaluator_result(self):
        self.recorder.write_result(conclusion="recovered", exit_code=0)
        attach_evaluator_result(self.runs_root / "test-run", "PASS")
        payload = json.loads((self.runs_root / "test-run" / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["evaluator"]["verdict"], "PASS")
        self.assertEqual(payload["conclusion"], "recovered")  # 原有内容不丢


class TestLatestRunDir(unittest.TestCase):
    def test_returns_newest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            RunRecorder(run_id="older", runs_root=root).write_result(x=1)
            RunRecorder(run_id="newer", runs_root=root).write_result(x=1)
            # 显式设定修改时间,避免同秒创建导致顺序不确定
            os.utime(root / "older", (1_000_000, 1_000_000))
            os.utime(root / "newer", (2_000_000, 2_000_000))
            self.assertEqual(latest_run_dir(root).name, "newer")

    def test_none_when_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(latest_run_dir(Path(tmp) / "nope"))


def _tool_call():
    from agent.state import ToolCall
    return ToolCall("get_api_health")


if __name__ == "__main__":
    unittest.main()
