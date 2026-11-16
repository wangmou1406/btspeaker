"""AI 播报客户端测试：配置探测、地址归一化、请求构造、错误处理与密钥脱敏。"""

from __future__ import annotations

import io
import json
import unittest
import urllib.error
from unittest import mock

from btspeaker.ai import AIClient, AIError
from tests.helpers import make_config, temp_data_dir


class _FakeResponse:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload
        self.closed = False

    def read(self, size: int = -1) -> bytes:  # noqa: ARG002 - 与 urlopen 接口一致
        return self._payload

    def close(self) -> None:
        self.closed = True


class AIClientTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = temp_data_dir()
        self.data_dir = self._tmp.__enter__()
        self.cfg = make_config(self.data_dir, sim=True)
        self.client = AIClient(self.cfg)

    def tearDown(self) -> None:
        self._tmp.__exit__(None, None, None)

    def configure(self, **overrides) -> None:
        base = {
            "enabled": True,
            "base_url": "https://api.deepseek.com/v1",
            "api_key": "sk-test-123",
            "model": "deepseek-chat",
        }
        base.update(overrides)
        self.cfg.update({"ai": base})

    # -- 探测 ------------------------------------------------------------ #
    def test_probe_unconfigured(self) -> None:
        probe = self.client.probe()
        self.assertFalse(probe["ready"])
        self.assertEqual(probe["backend"], "none")
        self.assertIn("未配置 AI", probe["hint"])

    def test_probe_missing_key(self) -> None:
        self.cfg.update({"ai": {"enabled": True, "base_url": "https://api.deepseek.com/v1", "api_key": ""}})
        probe = self.client.probe()
        self.assertFalse(probe["ready"])
        self.assertIn("API Key", probe["hint"])

    def test_probe_disabled(self) -> None:
        self.configure(enabled=False)
        probe = self.client.probe()
        self.assertFalse(probe["ready"])
        self.assertIn("关闭", probe["hint"])

    # -- 地址归一化 ------------------------------------------------------ #
    def test_endpoint_normalization(self) -> None:
        cases = [
            ("https://api.deepseek.com/v1", "https://api.deepseek.com/v1/chat/completions"),
            ("https://api.deepseek.com/v1/", "https://api.deepseek.com/v1/chat/completions"),
            ("https://api.openai.com/v1/chat/completions", "https://api.openai.com/v1/chat/completions"),
            ("http://127.0.0.1:8000", "http://127.0.0.1:8000/v1/chat/completions"),
            ("https://open.bigmodel.cn/api/paas/v4", "https://open.bigmodel.cn/api/paas/v4/chat/completions"),
        ]
        for base, expected in cases:
            self.cfg.set("ai.base_url", base)
            self.assertEqual(expected, AIClient(self.cfg).endpoint())

    # -- 调用 ------------------------------------------------------------ #
    def test_complete_success(self) -> None:
        self.configure()
        response = {"choices": [{"message": {"role": "assistant", "content": "你好，我是 AI。"}}]}
        fake = _FakeResponse(json.dumps(response).encode("utf-8"))
        captured = {}

        def fake_urlopen(request, timeout=None):  # noqa: ARG001
            captured["request"] = request
            return fake

        with mock.patch("btspeaker.ai.urllib.request.urlopen", side_effect=fake_urlopen):
            text = self.client.complete("介绍一下你自己", system="你是播报员")
        self.assertEqual("你好，我是 AI。", text)
        request = captured["request"]
        self.assertEqual("https://api.deepseek.com/v1/chat/completions", request.full_url)
        self.assertEqual("Bearer sk-test-123", request.get_header("Authorization"))
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual("deepseek-chat", body["model"])
        self.assertEqual("你是播报员", body["messages"][0]["content"])
        self.assertEqual("介绍一下你自己", body["messages"][1]["content"])
        self.assertTrue(fake.closed)

    def test_complete_disabled(self) -> None:
        self.configure(enabled=False)
        with self.assertRaises(AIError) as ctx:
            self.client.complete("你好")
        self.assertIn("未启用", str(ctx.exception))

    def test_complete_missing_config(self) -> None:
        with self.assertRaises(AIError) as ctx:
            self.client.complete("你好")
        self.assertIn("未启用", str(ctx.exception))

    def test_complete_empty_prompt(self) -> None:
        self.configure()
        with self.assertRaises(AIError) as ctx:
            self.client.complete("   ")
        self.assertIn("不能为空", str(ctx.exception))

    def test_complete_http_error(self) -> None:
        self.configure()
        error = urllib.error.HTTPError(
            "https://api.deepseek.com/v1/chat/completions",
            401,
            "Unauthorized",
            {},
            io.BytesIO(b'{"error": {"message": "invalid api key"}}'),
        )
        with mock.patch("btspeaker.ai.urllib.request.urlopen", side_effect=error):
            with self.assertRaises(AIError) as ctx:
                self.client.complete("你好")
        self.assertIn("401", str(ctx.exception))
        self.assertIn("invalid api key", str(ctx.exception))
        self.assertIn("API Key", str(ctx.exception))

    def test_complete_empty_choices(self) -> None:
        self.configure()
        fake = _FakeResponse(json.dumps({"choices": []}).encode("utf-8"))
        with mock.patch("btspeaker.ai.urllib.request.urlopen", return_value=fake):
            with self.assertRaises(AIError) as ctx:
                self.client.complete("你好")
        self.assertIn("空内容", str(ctx.exception))

    # -- 脱敏 ------------------------------------------------------------ #
    def test_public_dict_masks_key(self) -> None:
        self.configure(api_key="sk-super-secret")
        public = self.cfg.as_public_dict()
        self.assertEqual("******", public["ai"]["api_key"])
        self.assertNotIn("sk-super-secret", json.dumps(public, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
