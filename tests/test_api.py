"""REST API 测试：直接调用 Api.dispatch（不经网络），用假实现驱动业务。

覆盖：鉴权、状态、设置、蓝牙、扫描、播放、TTS、规则 CRUD/导入导出/手动执行、
webhook 触发、声音库、日志、诊断、错误处理。
"""

from __future__ import annotations

import json
import logging
import os
import unittest
from types import SimpleNamespace

from btspeaker.api import Api, ApiError
from btspeaker.bus import EventBus
from btspeaker.rules import RuleStore
from btspeaker.store import RuntimeState
from btspeaker.util import LogRingHandler, iso_now, write_json
from tests.helpers import FakeBluetooth, FakePlayer, FakeTts, make_config, temp_data_dir, wait_until


class FakeEngine:
    """假自动化引擎：只记录调用，返回固定结构。"""

    def __init__(self, rules: RuleStore, player: FakePlayer) -> None:
        self.rules = rules
        self.player = player
        self.calls = []
        self.reloads = 0

    def status(self) -> dict:
        return {
            "running": 0,
            "queued": len(self.calls),
            "scheduler_running": True,
            "next_runs": [{"rule_id": "r_x", "name": "测试", "at": iso_now(), "in": 60}],
            "recent": [],
        }

    def run_rule(self, rule_id: str, context=None) -> dict:
        self.calls.append((rule_id, dict(context or {})))
        return {"accepted": True, "job_id": "job_test"}

    def recent(self, limit: int = 20):
        return [{"rule_id": rule_id, "ok": True, "at": iso_now()} for rule_id, _ in reversed(self.calls[-limit:])]

    def reload_rules(self) -> None:
        self.reloads += 1


class ApiTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._ctx = temp_data_dir()
        self.data_dir = self._ctx.__enter__()
        self.cfg = make_config(self.data_dir, sim=True)
        self.bus = EventBus()
        self.state = RuntimeState(
            {
                "hostname": "iStoreOS",
                "bluetooth.available": True,
                "bluetooth.powered": True,
                "bluetooth.adapter": {"address": "00:11:22:33:44:55", "name": "iStoreOS", "hci": "hci0"},
                "bluetooth.connected": None,
                "bluetooth.devices": [],
                "bluetooth.scanning": False,
                "bluetooth.discovered": [],
                "player.playing": False,
                "player.volume": 70,
                "last_event": None,
            }
        )
        self.bt = FakeBluetooth()
        self.player = FakePlayer()
        self.tts = FakeTts(self.data_dir)
        self.rules = RuleStore(self.cfg.rules_path)
        self.engine = FakeEngine(self.rules, self.player)
        self.logs = LogRingHandler(50)
        self.app = SimpleNamespace(
            cfg=self.cfg,
            bus=self.bus,
            state=self.state,
            bt=self.bt,
            player=self.player,
            tts=self.tts,
            rules=self.rules,
            engine=self.engine,
            logs=self.logs,
            started_at=iso_now(),
        )
        self.api = Api(self.app)

    def tearDown(self) -> None:
        self._ctx.__exit__(None, None, None)

    # -- 断言助手 -------------------------------------------------------- #
    def call(self, method: str, path: str, body=None, query=None, headers=None, raw=None):
        try:
            return self.api.dispatch(method, path, query or {}, body or {}, headers or {}, raw_body=raw)
        except ApiError as exc:
            return exc.status, {"ok": False, "error": {"code": exc.code, "message": exc.message}}

    def ok(self, response) -> dict:
        status, payload = response
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload.get("ok"), payload)
        return payload["data"]

    def err(self, response, code: str, status: int = None) -> dict:
        actual_status, payload = response
        self.assertFalse(payload.get("ok"), payload)
        self.assertEqual(payload["error"]["code"], code, payload)
        if status is not None:
            self.assertEqual(actual_status, status)
        return payload

    def add_rule(self, name: str = "测试规则") -> dict:
        return self.rules.add(
            {
                "name": name,
                "trigger": {"type": "schedule", "at": "08:00"},
                "actions": [{"type": "tts", "text": "你好"}],
            }
        )


class ApiBasicsTests(ApiTestBase):
    def test_health(self):
        data = self.ok(self.call("GET", "/api/health"))
        self.assertEqual(data["status"], "ok")
        self.assertTrue(data["version"])

    def test_status_shape(self):
        data = self.ok(self.call("GET", "/api/status"))
        self.assertEqual(data["app"], "蓝音管家")
        self.assertTrue(data["sim"])
        self.assertIn("bluetooth", data)
        self.assertIn("player", data)
        self.assertIn("rules", data)
        self.assertIn("engine", data)
        self.assertIn("tts", data)
        self.assertTrue(data["bluetooth"]["available"])
        self.assertEqual(data["player"]["volume"], 70)
        self.assertEqual(len(data["bluetooth"]["devices"]), 3)  # 来自 FakeBluetooth

    def test_not_found_route(self):
        self.err(self.call("GET", "/api/not-exist"), "not_found", 404)

    def test_system_info(self):
        data = self.ok(self.call("GET", "/api/system/info"))
        self.assertTrue(data["version"])
        self.assertTrue(data["data_dir"])
        self.assertTrue(data["sim"])

    def test_auth_required(self):
        self.cfg.set("security.api_token", "tok-123456")
        # health 免鉴权
        self.ok(self.call("GET", "/api/health"))
        # 无 token 被拒
        self.err(self.call("GET", "/api/status"), "unauthorized", 401)
        # header 正确
        self.ok(self.call("GET", "/api/status", headers={"X-Api-Token": "tok-123456"}))
        # query token 正确
        self.ok(self.call("GET", "/api/status", query={"token": ["tok-123456"]}))
        # 错误 token
        self.err(self.call("GET", "/api/status", headers={"X-Api-Token": "wrong"}), "unauthorized", 401)

    def test_settings_masked_token_not_overwritten(self):
        self.cfg.set("security.api_token", "secret-token")
        self.cfg.save()
        auth = {"X-Api-Token": "secret-token"}
        data = self.ok(self.call("GET", "/api/settings", headers=auth))
        self.assertEqual(data["config"]["security"]["api_token"], "******")
        # 把脱敏值原样提交回来，不应覆盖真实 token
        self.ok(
            self.call(
                "PUT",
                "/api/settings",
                {
                    "config": {
                        "security": {"api_token": "******"},
                        "player": {"volume": 55},
                    }
                },
                headers=auth,
            )
        )
        self.assertEqual(self.cfg.get("security.api_token"), "secret-token")
        self.assertEqual(self.cfg.get("player.volume"), 55)
        self.assertEqual(self.player.get_volume(), 55)

    def test_settings_port_change_requires_restart(self):
        data = self.ok(self.call("PUT", "/api/settings", {"config": {"server": {"port": 9999}}}))
        self.assertTrue(data["restart_required"])
        self.assertEqual(data["config"]["server"]["port"], 9999)

    def test_settings_invalid_body(self):
        self.err(self.call("PUT", "/api/settings", {"wrong": 1}), "bad_request", 400)


class ApiBluetoothTests(ApiTestBase):
    def test_devices(self):
        data = self.ok(self.call("GET", "/api/bluetooth/devices"))
        self.assertEqual(len(data["items"]), 3)
        self.assertEqual(data["adapter"]["hci"], "hci0")
        self.assertTrue(any(item["connected"] for item in data["items"]))

    def test_connect_disconnect_pair_remove(self):
        address = "AA:BB:CC:44:55:66"
        self.assertEqual(self.ok(self.call("POST", "/api/bluetooth/connect", {"address": address}))["connected"], True)
        device = self.bt.device_info(address)
        self.assertTrue(device.connected)
        # 单连接互斥：另一台被断开
        other = "AA:BB:CC:11:22:33"
        self.assertFalse(self.bt.device_info(other).connected)

        self.assertEqual(self.ok(self.call("POST", "/api/bluetooth/disconnect", {"address": address}))["connected"], False)
        self.assertEqual(self.ok(self.call("POST", "/api/bluetooth/pair", {"address": "AA:BB:CC:77:88:99"}))["paired"], True)
        self.assertEqual(self.ok(self.call("POST", "/api/bluetooth/default", {"address": address}))["default_device"], address)
        self.assertEqual(self.cfg.get("bluetooth.default_device"), address)
        self.assertEqual(self.ok(self.call("DELETE", "/api/bluetooth/devices/AA:BB:CC:77:88:99"))["removed"], True)
        self.assertIsNone(self.bt.device_info("AA:BB:CC:77:88:99"))

    def test_pair_publishes_event(self):
        received = []
        self.bus.subscribe("bluetooth.paired", lambda event, payload: received.append(payload))
        self.ok(self.call("POST", "/api/bluetooth/pair", {"address": "AA:BB:CC:77:88:99"}))
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]["address"], "AA:BB:CC:77:88:99")

    def test_invalid_address(self):
        self.err(self.call("POST", "/api/bluetooth/connect", {"address": "not-a-mac"}), "bad_request", 400)
        self.err(self.call("POST", "/api/bluetooth/pair", {}), "bad_request", 400)

    def test_scan_lifecycle(self):
        data = self.ok(self.call("POST", "/api/bluetooth/scan", {"timeout": 5}))
        self.assertTrue(data["scanning"])
        self.assertTrue(data["job_id"])
        self.assertTrue(wait_until(lambda: not self.state.get("bluetooth.scanning"), timeout=5))
        status = self.ok(self.call("GET", "/api/bluetooth/scan"))
        self.assertFalse(status["scanning"])
        self.assertEqual(len(status["items"]), 3)
        self.assertEqual(status["error"], "")


class ApiPlayerTests(ApiTestBase):
    def test_play_and_stop(self):
        data = self.ok(self.call("POST", "/api/player/play", {"source": "chime.wav", "label": "提示音", "volume": 40}))
        self.assertTrue(data["accepted"])
        self.assertTrue(wait_until(lambda: bool(self.player.played), timeout=5))
        request = self.player.played[0]
        self.assertEqual(request.source, "chime.wav")
        self.assertEqual(request.volume, 40)
        self.assertEqual(self.ok(self.call("POST", "/api/player/stop"))["stopped"], True)

    def test_play_requires_source(self):
        self.err(self.call("POST", "/api/player/play", {}), "bad_request", 400)

    def test_volume_bounds_and_persist(self):
        self.assertEqual(self.ok(self.call("POST", "/api/player/volume", {"level": 130}))["volume"], 100)
        self.assertEqual(self.cfg.get("player.volume"), 100)
        self.assertEqual(self.state.get("player.volume"), 100)
        self.assertEqual(self.ok(self.call("POST", "/api/player/volume", {"level": -5}))["volume"], 0)
        self.err(self.call("POST", "/api/player/volume", {}), "bad_request", 400)

    def test_player_get(self):
        data = self.ok(self.call("GET", "/api/player"))
        self.assertFalse(data["playing"])
        self.assertIn("probe", data)
        self.assertEqual(data["probe"]["backend"], "mock")

    def test_tts_speak(self):
        data = self.ok(self.call("POST", "/api/tts/speak", {"text": "你好世界"}))
        self.assertTrue(data["accepted"])
        self.assertTrue(wait_until(lambda: bool(self.tts.texts), timeout=5))
        self.assertEqual(self.tts.texts[0], "你好世界")
        self.err(self.call("POST", "/api/tts/speak", {"text": ""}), "bad_request", 400)
        self.err(self.call("POST", "/api/tts/speak", {"text": "x" * 1001}), "bad_request", 400)


class ApiRulesTests(ApiTestBase):
    def test_crud_flow(self):
        rule = self.ok(
            self.call(
                "POST",
                "/api/rules",
                {
                    "name": "早上好",
                    "trigger": {"type": "schedule", "at": "07:30", "days": [0, 1]},
                    "actions": [{"type": "tts", "text": "早上好"}],
                },
            )
        )
        rule_id = rule["id"]
        self.assertTrue(rule_id.startswith("r_"))
        self.assertGreaterEqual(self.engine.reloads, 1)

        listing = self.ok(self.call("GET", "/api/rules"))
        self.assertEqual(listing["counts"]["total"], 1)
        self.assertEqual(len(listing["items"]), 1)

        detail = self.ok(self.call("GET", "/api/rules/" + rule_id))
        self.assertEqual(detail["name"], "早上好")

        updated = self.ok(self.call("PUT", "/api/rules/" + rule_id, {"name": "新的名字"}))
        self.assertEqual(updated["name"], "新的名字")

        toggled = self.ok(self.call("POST", "/api/rules/%s/toggle" % rule_id, {"enabled": False}))
        self.assertFalse(toggled["enabled"])

        run_result = self.ok(self.call("POST", "/api/rules/%s/run" % rule_id, {}))
        self.assertTrue(run_result["accepted"])
        self.assertEqual(self.engine.calls[-1][0], rule_id)

        self.assertEqual(self.ok(self.call("DELETE", "/api/rules/" + rule_id))["deleted"], True)
        self.err(self.call("GET", "/api/rules/" + rule_id), "not_found", 404)

    def test_create_invalid_rule(self):
        payload = self.err(
            self.call("POST", "/api/rules", {"name": "", "trigger": {"type": "schedule", "at": "99:99"}, "actions": []}),
            "rule_invalid",
            400,
        )
        self.assertTrue(payload["error"]["message"])

    def test_export_import(self):
        self.add_rule("规则一")
        exported = self.ok(self.call("GET", "/api/rules/export"))
        self.assertEqual(exported["version"], 1)
        self.assertEqual(len(exported["rules"]), 1)

        result = self.ok(self.call("POST", "/api/rules/import", {"rules": exported["rules"], "mode": "merge"}))
        self.assertEqual(result["imported"], 1)
        self.assertEqual(self.rules.counts()["total"], 2)

        bad = self.err(
            self.call("POST", "/api/rules/import", {"rules": [{"name": "坏规则", "trigger": {"type": "manual"}, "actions": []}]}),
            "rule_invalid",
            400,
        )
        self.assertTrue(bad["error"]["message"])

    def test_run_missing_rule(self):
        self.err(self.call("POST", "/api/rules/not-there/run", {}), "not_found", 404)
        self.err(self.call("PUT", "/api/rules/not-there", {"name": "x"}), "not_found", 404)
        self.err(self.call("POST", "/api/rules/not-there/toggle", {"enabled": True}), "not_found", 404)
        self.err(self.call("DELETE", "/api/rules/not-there", {}), "not_found", 404)

    def test_events_after_run(self):
        rule = self.add_rule()
        self.ok(self.call("POST", "/api/rules/%s/run" % rule["id"], {}))
        events = self.ok(self.call("GET", "/api/events", query={"limit": ["10"]}))
        self.assertEqual(len(events["items"]), 1)


class ApiHookTests(ApiTestBase):
    def test_hook_enabled(self):
        rule = self.add_rule()
        self.rules.set_enabled(rule["id"], True)
        data = self.ok(self.call("POST", "/api/hooks/" + rule["id"], {"x": 1}, query={"source": ["test"]}))
        self.assertTrue(data["fired"])
        self.assertEqual(self.engine.calls[-1][1].get("trigger"), "api")

    def test_hook_disabled_rule(self):
        rule = self.add_rule()
        self.rules.set_enabled(rule["id"], False)
        self.err(self.call("POST", "/api/hooks/" + rule["id"], {}), "rule_disabled", 409)

    def test_hook_get_supported(self):
        rule = self.add_rule()
        self.rules.set_enabled(rule["id"], True)
        self.assertTrue(self.ok(self.call("GET", "/api/hooks/" + rule["id"]))["fired"])


class ApiSaveTests(ApiTestBase):
    """保存能力：声音库上传 + 配置备份导出。"""

    WAV_BYTES = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 40

    def upload(self, name, raw=None, query=None):
        return self.call("POST", "/api/library/upload", query=query or {"name": [name]}, raw=raw or self.WAV_BYTES)

    def test_upload_sound_and_list(self):
        data = self.ok(self.upload("chime-test.wav"))
        self.assertEqual(data["name"], "chime-test.wav")
        self.assertEqual(data["size"], len(self.WAV_BYTES))
        self.assertEqual(data["source"], "user")
        path = os.path.join(self.data_dir, "sounds", "chime-test.wav")
        self.assertTrue(os.path.isfile(path))
        self.assertEqual(open(path, "rb").read(), self.WAV_BYTES)
        library = self.ok(self.call("GET", "/api/library"))
        self.assertIn("chime-test.wav", [item["name"] for item in library["items"]])

    def test_upload_duplicate_gets_suffix(self):
        self.ok(self.upload("same.wav"))
        second = self.ok(self.upload("same.wav"))
        self.assertEqual(second["name"], "same-1.wav")
        self.assertTrue(self.ok(self.upload("same.wav"))["name"].startswith("same-"))

    def test_upload_sanitizes_path_traversal(self):
        data = self.ok(self.upload("../../evil/../ok-name.wav"))
        self.assertEqual(data["name"], "ok-name.wav")
        sounds_dir = os.path.abspath(os.path.join(self.data_dir, "sounds"))
        self.assertTrue(os.path.abspath(data["path"]).startswith(sounds_dir + os.sep))

    def test_upload_rejects_bad_extension(self):
        self.err(self.upload("hack.txt"), "bad_request", 400)
        self.err(self.upload("noext"), "bad_request", 400)

    def test_upload_requires_name_and_content(self):
        self.err(self.call("POST", "/api/library/upload", raw=self.WAV_BYTES), "bad_request", 400)
        self.err(self.call("POST", "/api/library/upload", query={"name": ["empty.wav"]}, raw=b""), "bad_request", 400)

    def test_upload_size_limit(self):
        self.cfg.set("sounds.max_upload_mb", 1)
        oversized = b"x" * (1024 * 1024 + 1)
        self.err(self.upload("big.wav", raw=oversized), "payload_too_large", 413)

    def test_backup_export(self):
        rule = self.add_rule("备份测试规则")
        data = self.ok(self.call("GET", "/api/backup"))
        self.assertRegex(data["filename"], r"^btspeaker-backup-\d{8}-\d{6}\.json$")
        content = data["content"]
        self.assertEqual(content["app"], "蓝音管家")
        self.assertTrue(content["version"])
        self.assertIn("config", content)
        self.assertEqual(content["config"]["server"]["port"], self.cfg.port)
        self.assertEqual(len(content["rules"]), 1)
        self.assertEqual(content["rules"][0]["id"], rule["id"])
        self.assertIn("exported_at", content)

    def test_backup_contains_uploaded_sound_in_library(self):
        self.ok(self.upload("saved.wav"))
        self.ok(self.call("GET", "/api/backup"))
        library = self.ok(self.call("GET", "/api/library"))
        self.assertIn("saved.wav", [item["name"] for item in library["items"]])


class ApiMiscTests(ApiTestBase):
    def test_library(self):
        sounds_dir = os.path.join(self.data_dir, "sounds")
        os.makedirs(sounds_dir, exist_ok=True)
        with open(os.path.join(sounds_dir, "demo.wav"), "wb") as handle:
            handle.write(b"RIFF")
        write_json(os.path.join(sounds_dir, "notes.txt"), {})
        data = self.ok(self.call("GET", "/api/library"))
        names = [item["name"] for item in data["items"]]
        self.assertIn("demo.wav", names)
        self.assertNotIn("notes.txt", names)

    def test_logs(self):
        for index in range(3):
            self.logs.emit(
                logging.LogRecord(
                    name="btspeaker.test",
                    level=logging.INFO,
                    pathname=__file__,
                    lineno=index,
                    msg="日志 %d",
                    args=(index,),
                    exc_info=None,
                )
            )
        data = self.ok(self.call("GET", "/api/logs", query={"limit": ["2"]}))
        self.assertEqual(len(data["items"]), 2)
        self.assertEqual(data["total"], 3)
        self.assertEqual(data["items"][0]["module"], "test")
        self.assertEqual(self.ok(self.call("DELETE", "/api/logs"))["cleared"], True)
        self.assertEqual(self.logs.total(), 0)

    def test_diag(self):
        data = self.ok(self.call("GET", "/api/diag"))
        self.assertTrue(data["checks"])
        names = [check["name"] for check in data["checks"]]
        self.assertIn("服务运行", names)
        self.assertIn("Web 服务", names)
        self.assertTrue(all("ok" in check and "hint" in check for check in data["checks"]))

    def test_unknown_rule_id_in_hook(self):
        self.err(self.call("GET", "/api/hooks/nope"), "not_found", 404)

    def test_status_fallback_when_state_empty(self):
        self.state.clear()
        data = self.ok(self.call("GET", "/api/status"))
        self.assertEqual(len(data["bluetooth"]["devices"]), 3)


if __name__ == "__main__":
    unittest.main()
