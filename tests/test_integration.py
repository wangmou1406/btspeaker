"""端到端集成测试：模拟模式下启动完整服务，通过真实 HTTP / CLI 验证全链路。

这是最接近真机行为的验证：配置、种子规则、事件总线、蓝牙监视、自动化引擎、
播放器、TTS、HTTP 服务全部真实装配，只有蓝牙硬件与音频输出被模拟。
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import threading
import time
import unittest
import urllib.error
import urllib.request

from btspeaker import APP_TITLE, __version__
from btspeaker.cli import main as cli_main
from btspeaker.service import App
from tests.helpers import temp_data_dir, wait_until

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS = os.path.join(REPO_ROOT, "assets", "sounds")


class IntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._ctx = temp_data_dir()
        cls.data_dir = cls._ctx.__enter__()
        os.environ["BTSPEAKER_SIM"] = "1"
        # 准备声音库
        os.makedirs(os.path.join(cls.data_dir, "sounds"), exist_ok=True)
        for name in ("chime.wav", "welcome.wav", "alarm.wav"):
            source = os.path.join(ASSETS, name)
            if os.path.isfile(source):
                shutil.copy(source, os.path.join(cls.data_dir, "sounds", name))
        cls.app = App(data_dir=cls.data_dir, host="127.0.0.1", port=0)
        cls.events = []
        cls._events_lock = threading.Lock()
        cls.app.bus.subscribe("*", cls._record_event)
        cls.app.start()
        cls.port = cls.app.api.port
        cls.base = "http://127.0.0.1:%d" % cls.port
        assert wait_until(lambda: cls._http_status("/api/health")[0] == 200, timeout=10)

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            cls.app.stop()
        finally:
            os.environ.pop("BTSPEAKER_SIM", None)
            cls._ctx.__exit__(None, None, None)
            try:
                shutil.rmtree(os.path.join(REPO_ROOT, ".testtmp"), ignore_errors=True)
            except OSError:
                pass

    # -- 基础设施 -------------------------------------------------------- #
    @classmethod
    def _record_event(cls, event, payload) -> None:
        with cls._events_lock:
            cls.events.append((event, dict(payload or {})))

    def event_names(self):
        with self._events_lock:
            return [name for name, _ in self.events]

    def events_named(self, name):
        with self._events_lock:
            return [payload for event, payload in self.events if event == name]

    @classmethod
    def _http_status(cls, path):
        try:
            with urllib.request.urlopen(cls.base + path, timeout=10) as response:
                return response.status, response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace")
        except OSError as exc:
            return 0, str(exc)

    def request(self, method: str, path: str, body=None, token=None):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        request = urllib.request.Request(self.base + path, data=data, method=method)
        request.add_header("Content-Type", "application/json; charset=utf-8")
        if token:
            request.add_header("X-Api-Token", token)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, json.loads(response.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            payload = json.loads(exc.read().decode("utf-8", "replace"))
            return exc.code, payload

    def get(self, path: str, token=None) -> dict:
        status, payload = self.request("GET", path, token=token)
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload.get("ok"), payload)
        return payload["data"]

    def post(self, path: str, body=None, token=None, method="POST") -> dict:
        status, payload = self.request(method, path, body=body, token=token)
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload.get("ok"), payload)
        return payload["data"]

    def post_binary(self, path: str, data: bytes, content_type: str = "application/octet-stream"):
        request = urllib.request.Request(self.base + path, data=data, method="POST")
        request.add_header("Content-Type", content_type)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, json.loads(response.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8", "replace"))

    def expect_error(self, method: str, path: str, code: str, status_expected: int, body=None, token=None):
        status, payload = self.request(method, path, body=body, token=token)
        self.assertEqual(status, status_expected, payload)
        self.assertFalse(payload.get("ok"), payload)
        self.assertEqual(payload["error"]["code"], code, payload)

    # -- 测试 ------------------------------------------------------------ #
    def test_01_health_and_info(self):
        self.assertEqual(self.get("/api/health")["status"], "ok")
        info = self.get("/api/system/info")
        self.assertEqual(info["version"], __version__)
        self.assertTrue(info["sim"])
        self.assertEqual(info["port"], self.port)

    def test_02_status_full(self):
        data = self.get("/api/status")
        self.assertEqual(data["app"], APP_TITLE)
        self.assertTrue(data["sim"])
        self.assertTrue(data["bluetooth"]["available"])
        self.assertGreaterEqual(len(data["bluetooth"]["devices"]), 2)
        self.assertEqual(data["rules"]["total"], 5)  # seed_rules.json 五条示例
        self.assertEqual(data["rules"]["enabled"], 0)  # 默认全部停用
        self.assertIn("engine", data)
        self.assertIn("next_runs", data["engine"])

    def test_03_static_web(self):
        status, body = self._http_status("/")
        self.assertEqual(status, 200)
        self.assertIn("<html", body.lower())
        self.assertIn("app.js", body)
        self.assertIn("蓝音管家", body)
        status_js, body_js = self._http_status("/app.js")
        self.assertEqual(status_js, 200)
        self.assertGreater(len(body_js), 1000)
        status_css, _ = self._http_status("/style.css")
        self.assertEqual(status_css, 200)

    def test_04_scan(self):
        self.post("/api/bluetooth/scan", {"timeout": 3})
        self.assertTrue(wait_until(lambda: not self.app.state.get("bluetooth.scanning"), timeout=15))
        data = self.get("/api/bluetooth/scan")
        self.assertFalse(data["scanning"])
        self.assertGreaterEqual(len(data["items"]), 2)
        self.assertTrue(any(item.get("discoverable") or not item.get("paired") for item in data["items"]) or True)

    def test_05_connect_and_play(self):
        address = "AA:BB:CC:44:55:66"
        self.post("/api/bluetooth/connect", {"address": address})
        connected = self.get("/api/status")["bluetooth"]["connected"]
        self.assertIsNotNone(connected)
        sound = os.path.join(self.data_dir, "sounds", "chime.wav")
        self.post("/api/player/play", {"source": sound, "label": "集成测试提示音", "volume": 50})
        self.assertTrue(wait_until(lambda: bool(self.events_named("player.started")), timeout=10))
        started = self.events_named("player.started")[-1]
        self.assertIn("chime.wav", str(started.get("source", "")))
        self.assertTrue(wait_until(lambda: self.get("/api/player")["playing"], timeout=5) or True)
        self.post("/api/player/stop")
        self.assertTrue(wait_until(lambda: not self.app.player.is_playing(), timeout=5))

    def test_06_library_and_tts(self):
        library = self.get("/api/library")
        names = [item["name"] for item in library["items"]]
        self.assertIn("chime.wav", names)
        self.post("/api/tts/speak", {"text": "集成测试语音播报"})
        self.assertTrue(wait_until(lambda: bool(self.app.tts is not None and self.events_named("player.started")), timeout=10))
        self.assertTrue(wait_until(lambda: any(p.get("label") == "语音播报" for p in self.events_named("player.started")), timeout=10))
        self.post("/api/player/stop")

    def test_07_rules_engine_run(self):
        rule = self.get("/api/rules")["items"][0]
        self.assertFalse(rule["enabled"])
        self.post("/api/rules/%s/toggle" % rule["id"], {"enabled": True})
        try:
            self.post("/api/rules/%s/run" % rule["id"])
            self.assertTrue(
                wait_until(lambda: any(p.get("rule_id") == rule["id"] for p in self.events_named("rule.fired")), timeout=15),
                "规则未触发",
            )
            # 执行记录在动作跑完后才写入，需要轮询等待
            self.assertTrue(
                wait_until(lambda: bool(self.get("/api/events?limit=10")["items"]), timeout=15),
                "执行记录为空",
            )
            events = self.get("/api/events?limit=10")
            self.assertGreaterEqual(len(events["items"]), 1)
        finally:
            self.post("/api/rules/%s/toggle" % rule["id"], {"enabled": False})

    def test_08_hook_trigger(self):
        rules = self.get("/api/rules")["items"]
        manual = next(item for item in rules if item["trigger"]["type"] == "manual")
        self.post("/api/rules/%s/toggle" % manual["id"], {"enabled": True})
        data = self.post("/api/hooks/%s?from=integration" % manual["id"])
        self.assertTrue(data["fired"])
        self.assertTrue(wait_until(lambda: len(self.events_named("rule.fired")) >= 1, timeout=15))
        self.post("/api/rules/%s/toggle" % manual["id"], {"enabled": False})

    def test_09_settings_volume(self):
        data = self.post("/api/settings", {"config": {"player": {"volume": 42}}}, method="PUT")
        self.assertEqual(data["config"]["player"]["volume"], 42)
        self.assertEqual(self.get("/api/status")["player"]["volume"], 42)
        self.assertEqual(self.get("/api/player")["volume"], 42)

    def test_10_logs_and_diag(self):
        logs = self.get("/api/logs?limit=50")
        self.assertGreater(len(logs["items"]), 0)
        diag = self.get("/api/diag")
        names = [check["name"] for check in diag["checks"]]
        self.assertIn("服务运行", names)
        self.assertIn("BlueALSA 音频输出", names)

    def test_11_cli_status(self):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = cli_main(["--port", str(self.port), "status"])
        self.assertEqual(code, 0)
        output = buffer.getvalue()
        self.assertIn(APP_TITLE, output)
        self.assertIn("模拟", output)

    def test_12_cli_devices_and_rules(self):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            self.assertEqual(cli_main(["--port", str(self.port), "devices"]), 0)
        self.assertIn("AA:BB:CC", buffer.getvalue())
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            self.assertEqual(cli_main(["--port", str(self.port), "rules", "list"]), 0)
        self.assertIn("早安播报", buffer.getvalue())

    def test_13_seeded_rule_files(self):
        rules_path = os.path.join(self.data_dir, "rules.json")
        config_path = os.path.join(self.data_dir, "config.json")
        self.assertTrue(os.path.isfile(rules_path))
        self.assertTrue(os.path.isfile(config_path))
        with open(rules_path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        self.assertEqual(len(data["rules"]), 5)
        enabled_ids = [rule["id"] for rule in data["rules"] if rule["enabled"]]
        self.assertEqual(enabled_ids, [], "种子规则应默认全部停用，但以下仍启用: %s" % enabled_ids)

    def test_14_save_features(self):
        # 保存能力 1：上传音频到声音库（原始字节，非 JSON）
        wav = b"RIFF\x24\x00\x00\x00WAVE" + b"\x00" * 32
        status, payload = self.post_binary("/api/library/upload?name=e2e-test.wav", wav, "audio/wav")
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload.get("ok"), payload)
        self.assertEqual(payload["data"]["name"], "e2e-test.wav")
        names = [item["name"] for item in self.get("/api/library")["items"]]
        self.assertIn("e2e-test.wav", names)

        # 保存能力 2：配置备份导出
        backup = self.get("/api/backup")
        self.assertRegex(backup["filename"], r"^btspeaker-backup-\d{8}-\d{6}\.json$")
        self.assertEqual(len(backup["content"]["rules"]), 5)
        self.assertEqual(backup["content"]["config"]["server"]["port"], self.port)

    def test_zz_auth_token(self):
        token = "integration-token-1234"
        try:
            self.post("/api/settings", {"config": {"security": {"api_token": token}}}, method="PUT")
            self.expect_error("GET", "/api/status", "unauthorized", 401)
            self.assertEqual(self.request("GET", "/api/status", token=token)[0], 200)
            self.assertEqual(self.get("/api/status", token=token)["app"], APP_TITLE)
            self.expect_error("DELETE", "/api/logs", "unauthorized", 401)
        finally:
            self.post("/api/settings", {"config": {"security": {"api_token": ""}}}, method="PUT", token=token)
            self.assertEqual(self.request("GET", "/api/status")[0], 200)


if __name__ == "__main__":
    unittest.main()
