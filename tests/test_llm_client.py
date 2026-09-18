"""agent/llm_client.py 的单元测试:请求构造、响应解析、用量、接口错误分类。

所有 HTTP 都被 mock 隔离,不联网。

运行:
    python -m unittest discover -s tests -v
"""

import json
import unittest
from unittest.mock import MagicMock, patch

from agent import llm_client as llm_client_mod
from agent.config import AppConfig
from agent.llm_client import LlmClient


def make_config(**overrides):
    values = dict(model="deepseek-chat", base_url="https://api.deepseek.com/v1",
                  api_key="sk-test-key", timeout=30, max_tokens=1024)
    values.update(overrides)
    return AppConfig(**values)


def http_response(status_code=200, payload=None, text=None, json_error=None):
    resp = MagicMock(status_code=status_code)
    if json_error:
        resp.json.side_effect = json_error
    else:
        resp.json.return_value = payload if payload is not None else {}
    resp.text = text if text is not None else json.dumps(payload or {})
    return resp


def tool_call_response(name="get_api_health", arguments="{}", call_id="call_1", content=""):
    return {
        "choices": [{"message": {
            "content": content,
            "tool_calls": [{"id": call_id, "type": "function",
                            "function": {"name": name, "arguments": arguments}}],
        }}],
        "usage": {"prompt_tokens": 120, "completion_tokens": 18, "total_tokens": 138},
    }


class TestSuccessfulCall(unittest.TestCase):
    @patch.object(llm_client_mod.requests, "post")
    def test_parses_tool_call_and_usage(self, mock_post):
        mock_post.return_value = http_response(payload=tool_call_response())
        client = LlmClient(make_config())
        result = client.chat([{"role": "user", "content": "hi"}], tools=[{"type": "function"}])

        self.assertTrue(result.ok)
        self.assertEqual(len(result.tool_calls), 1)
        self.assertEqual(result.tool_calls[0].name, "get_api_health")
        self.assertEqual(result.tool_calls[0].arguments, "{}")
        self.assertEqual(result.usage["total_tokens"], 138)

    @patch.object(llm_client_mod.requests, "post")
    def test_request_shape(self, mock_post):
        mock_post.return_value = http_response(payload=tool_call_response())
        client = LlmClient(make_config())
        client.chat([{"role": "user", "content": "hi"}], tools=[{"type": "function"}], timeout=7)

        url = mock_post.call_args[0][0]
        kwargs = mock_post.call_args[1]
        self.assertEqual(url, "https://api.deepseek.com/v1/chat/completions")
        self.assertEqual(kwargs["timeout"], 7)
        self.assertEqual(kwargs["json"]["model"], "deepseek-chat")
        self.assertEqual(kwargs["json"]["temperature"], 0)
        self.assertEqual(kwargs["json"]["max_tokens"], 1024)
        self.assertEqual(kwargs["json"]["tool_choice"], "auto")
        # key 只在请求头里,不进请求体
        self.assertIn("Bearer sk-test-key", kwargs["headers"]["Authorization"])
        self.assertNotIn("sk-test-key", json.dumps(kwargs["json"]))

    @patch.object(llm_client_mod.requests, "post")
    def test_text_only_response(self, mock_post):
        mock_post.return_value = http_response(payload={
            "choices": [{"message": {"content": "我觉得没问题"}}],
            "usage": {},
        })
        result = LlmClient(make_config()).chat([])
        self.assertTrue(result.ok)
        self.assertEqual(result.content, "我觉得没问题")
        self.assertEqual(result.tool_calls, [])

    @patch.object(llm_client_mod.requests, "post")
    def test_no_tools_means_no_tool_fields(self, mock_post):
        mock_post.return_value = http_response(payload=tool_call_response())
        LlmClient(make_config()).chat([])
        body = mock_post.call_args[1]["json"]
        self.assertNotIn("tools", body)
        self.assertNotIn("tool_choice", body)


class TestErrorClassification(unittest.TestCase):
    @patch.object(llm_client_mod.requests, "post")
    def test_timeout_is_retryable(self, mock_post):
        mock_post.side_effect = llm_client_mod.requests.Timeout()
        result = LlmClient(make_config()).chat([])
        self.assertFalse(result.ok)
        self.assertTrue(result.retryable)
        self.assertIn("超时", result.error)

    @patch.object(llm_client_mod.requests, "post")
    def test_connection_error_is_retryable(self, mock_post):
        mock_post.side_effect = llm_client_mod.requests.ConnectionError("refused")
        result = LlmClient(make_config()).chat([])
        self.assertFalse(result.ok)
        self.assertTrue(result.retryable)

    @patch.object(llm_client_mod.requests, "post")
    def test_401_is_not_retryable(self, mock_post):
        mock_post.return_value = http_response(status_code=401, text="unauthorized")
        result = LlmClient(make_config()).chat([])
        self.assertFalse(result.ok)
        self.assertFalse(result.retryable)
        self.assertIn("HTTP 401", result.error)

    @patch.object(llm_client_mod.requests, "post")
    def test_429_is_retryable(self, mock_post):
        mock_post.return_value = http_response(status_code=429, text="slow down")
        result = LlmClient(make_config()).chat([])
        self.assertTrue(result.retryable)

    @patch.object(llm_client_mod.requests, "post")
    def test_500_is_retryable(self, mock_post):
        mock_post.return_value = http_response(status_code=503, text="oops")
        result = LlmClient(make_config()).chat([])
        self.assertTrue(result.retryable)

    @patch.object(llm_client_mod.requests, "post")
    def test_error_body_is_truncated(self, mock_post):
        mock_post.return_value = http_response(status_code=400, text="x" * 5000)
        result = LlmClient(make_config()).chat([])
        self.assertLessEqual(len(result.error), 300)

    @patch.object(llm_client_mod.requests, "post")
    def test_malformed_json_body(self, mock_post):
        mock_post.return_value = http_response(json_error=ValueError("no json"))
        result = LlmClient(make_config()).chat([])
        self.assertFalse(result.ok)
        self.assertIn("不是合法 JSON", result.error)

    @patch.object(llm_client_mod.requests, "post")
    def test_missing_choices(self, mock_post):
        mock_post.return_value = http_response(payload={"id": "x"})
        result = LlmClient(make_config()).chat([])
        self.assertFalse(result.ok)
        self.assertIn("缺少 choices", result.error)


if __name__ == "__main__":
    unittest.main()
