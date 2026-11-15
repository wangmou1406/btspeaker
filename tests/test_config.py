"""公共层测试：配置、工具函数、事件总线、存储、运行时状态、CLI 参数解析。"""

from __future__ import annotations

import json
import os
import unittest

import btspeaker
from btspeaker.bus import EventBus
from btspeaker.cli import build_parser
from btspeaker.config import Config, DEFAULT_CONFIG, deep_get, deep_merge, deep_set
from btspeaker.store import JsonStore, RuntimeState
from btspeaker.util import (
    LogRingHandler,
    clamp_int,
    get_logger,
    human_duration,
    human_size,
    iso_now,
    local_minute,
    local_weekday,
    mask_secret,
    new_id,
    parse_bool,
    parse_int,
    render_template,
    setup_logging,
    which,
)
from tests.helpers import temp_data_dir


class ConfigTests(unittest.TestCase):
    def test_defaults_and_paths(self):
        with temp_data_dir() as data_dir:
            cfg = Config.load(data_dir=data_dir)
            self.assertEqual(cfg.get("player.volume"), 70)
            self.assertEqual(cfg.get("server.port"), 8722)
            self.assertEqual(cfg.port, 8722)
            self.assertEqual(cfg.host, "0.0.0.0")
            self.assertTrue(cfg.data_dir.endswith(os.path.basename(data_dir)))
            self.assertTrue(cfg.state_path.endswith("state.json"))
            self.assertTrue(cfg.rules_path.endswith("rules.json"))
            self.assertTrue(cfg.log_file.endswith("btspeaker.log"))
            self.assertTrue(cfg.web_dir.endswith(os.path.join("btspeaker", "web")))
            self.assertFalse(cfg.is_sim())

    def test_save_reload_and_mask(self):
        with temp_data_dir() as data_dir:
            cfg = Config.load(data_dir=data_dir)
            cfg.set("player.volume", 33)
            cfg.set("security.api_token", "secret-token-1234")
            cfg.save()
            self.assertTrue(os.path.isfile(cfg.path))
            reloaded = Config.load(data_dir=data_dir)
            self.assertEqual(reloaded.get("player.volume"), 33)
            self.assertEqual(reloaded.as_public_dict()["security"]["api_token"], "******")
            self.assertEqual(reloaded.to_dict()["security"]["api_token"], "secret-token-1234")

    def test_deep_merge_replaces_lists(self):
        merged = deep_merge({"a": {"b": 1, "c": [1, 2]}}, {"a": {"c": [3]}})
        self.assertEqual(merged["a"]["c"], [3])
        self.assertEqual(merged["a"]["b"], 1)
        self.assertEqual(deep_get(merged, "a.b"), 1)
        self.assertIsNone(deep_get(merged, "a.missing"))
        target = {}
        deep_set(target, "x.y.z", 5)
        self.assertEqual(target["x"]["y"]["z"], 5)

    def test_corrupted_config_is_backed_up(self):
        with temp_data_dir() as data_dir:
            path = os.path.join(data_dir, "config.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{ this is not json ")
            cfg = Config.load(data_dir=data_dir)
            self.assertEqual(cfg.get("player.volume"), 70)
            self.assertTrue(os.path.isfile(path + ".bad"))

    def test_update_is_partial(self):
        with temp_data_dir() as data_dir:
            cfg = Config.load(data_dir=data_dir)
            cfg.update({"tts": {"url": "http://127.0.0.1:9/tts?text={text}"}})
            self.assertEqual(cfg.get("tts.url"), "http://127.0.0.1:9/tts?text={text}")
            self.assertEqual(cfg.get("tts.method"), "GET")  # 未覆盖的默认值保留

    def test_port_property_fallback(self):
        with temp_data_dir() as data_dir:
            cfg = Config.load(data_dir=data_dir)
            cfg.set("server.port", "not-a-port")
            self.assertEqual(cfg.port, 8722)

    def test_sounds_dirs_unique_and_absolute(self):
        with temp_data_dir() as data_dir:
            cfg = Config.load(data_dir=data_dir)
            dirs = cfg.sounds_dirs()
            self.assertTrue(all(os.path.isabs(item) for item in dirs))
            self.assertEqual(len(dirs), len(set(dirs)))
            self.assertTrue(any(item.endswith(os.path.join("btspeaker-test", os.path.basename(data_dir), "sounds")) or item.endswith("sounds") for item in dirs))

    def test_sim_config_flag(self):
        with temp_data_dir() as data_dir:
            cfg = Config.load(data_dir=data_dir)
            cfg.set("bluetooth.backend", "sim")
            self.assertTrue(cfg.is_sim())
            cfg.set("bluetooth.backend", "cli")
            cfg.set("player.backend", "mock")
            self.assertTrue(cfg.is_sim())


class UtilTests(unittest.TestCase):
    def test_render_template(self):
        ctx = {"name": "客厅音箱", "volume": 60, "missing": None}
        self.assertEqual(render_template("你好{name}，音量{volume}%", ctx), "你好客厅音箱，音量60%")
        self.assertEqual(render_template("未知{unknown}", ctx), "未知{unknown}")  # 未知识别符原样保留
        self.assertEqual(render_template("", ctx), "")
        self.assertEqual(render_template(None, ctx), "")

    def test_time_helpers(self):
        self.assertRegex(local_minute(), r"^\d{2}:\d{2}$")
        self.assertIn(local_weekday(), range(7))
        self.assertRegex(iso_now(), r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")

    def test_parse_helpers(self):
        self.assertTrue(parse_bool("yes"))
        self.assertFalse(parse_bool("0"))
        self.assertTrue(parse_bool(None, default=True))
        self.assertEqual(parse_int("42"), 42)
        self.assertEqual(parse_int("x", 7), 7)
        self.assertEqual(clamp_int(150, 0, 100, 50), 100)
        self.assertEqual(clamp_int(-5, 0, 100, 50), 0)
        self.assertEqual(clamp_int("x", 0, 100, 50), 50)

    def test_human_units(self):
        self.assertEqual(human_size(512), "512 B")
        self.assertEqual(human_size(2048), "2.0 KB")
        self.assertEqual(human_duration(5), "5 秒")
        self.assertEqual(human_duration(90), "1 分 30 秒")
        self.assertEqual(human_duration(3600), "1 小时 0 分")

    def test_ids_and_mask(self):
        self.assertRegex(new_id("job"), r"^job_[0-9a-f]{8}$")
        self.assertNotEqual(new_id(), new_id())
        self.assertEqual(mask_secret(""), "")
        self.assertEqual(mask_secret("short"), "*****")
        self.assertTrue(mask_secret("abcdefgh1234").startswith("abcd"))

    def test_which_python(self):
        self.assertIsNotNone(which("python") or which("python3") or which("sh"))

    def test_log_ring(self):
        ring = setup_logging("DEBUG", capacity=10)
        logger = get_logger("unittest")
        for index in range(15):
            logger.info("第 %d 条日志", index)
        records = ring.records(5)
        self.assertEqual(len(records), 5)
        self.assertIn("第 14 条日志", records[-1]["message"])
        self.assertEqual(records[-1]["module"], "unittest")
        info_only = ring.records(100, "INFO")
        self.assertTrue(all(item["level"] == "INFO" for item in info_only))
        self.assertEqual(ring.total(), 10)  # 环形缓冲容量
        ring.clear()
        self.assertEqual(ring.total(), 0)

    def test_manual_ring_handler(self):
        ring = LogRingHandler(capacity=50)
        self.assertEqual(ring.total(), 0)
        self.assertEqual(ring.records(), [])


class BusTests(unittest.TestCase):
    def test_publish_subscribe(self):
        bus = EventBus()
        received = []
        token = bus.subscribe("player.finished", lambda event, payload: received.append((event, payload)))
        bus.publish("player.finished", {"source": "chime.wav"})
        self.assertEqual(received, [("player.finished", {"source": "chime.wav"})])
        self.assertTrue(bus.unsubscribe(token))
        bus.publish("player.finished", {"source": "chime.wav"})
        self.assertEqual(len(received), 1)

    def test_wildcard_and_error_isolation(self):
        bus = EventBus()
        seen = []

        def bad_handler(event, payload):
            raise RuntimeError("故意失败")

        bus.subscribe("rule.fired", bad_handler)
        bus.subscribe("*", lambda event, payload: seen.append(event))
        delivered = bus.publish("rule.fired", {"rule_id": "r_1"})
        self.assertEqual(delivered, 1)  # 失败的处理函数不计入
        self.assertEqual(seen, ["rule.fired"])

    def test_unsubscribe_unknown(self):
        bus = EventBus()
        self.assertFalse(bus.unsubscribe("nope"))
        self.assertEqual(bus.subscriber_count(), 0)


class StoreTests(unittest.TestCase):
    def test_json_store_persistence(self):
        with temp_data_dir() as data_dir:
            path = os.path.join(data_dir, "state.json")
            store = JsonStore(path, default_factory=lambda: {"last_runs": {}})
            self.assertEqual(store.read(), {"last_runs": {}})
            store.set("counter", 1)
            self.assertTrue(os.path.isfile(path))
            reloaded = JsonStore(path)
            self.assertEqual(reloaded.get("counter"), 1)
            # 返回的是副本，外部修改不影响内部
            data = reloaded.read()
            data["counter"] = 999
            self.assertEqual(reloaded.get("counter"), 1)

    def test_json_store_update_mutator(self):
        with temp_data_dir() as data_dir:
            store = JsonStore(os.path.join(data_dir, "state.json"))
            store.update(lambda data: data.update({"a": 1}))
            self.assertEqual(store.get("a"), 1)
            store.update(lambda data: {"b": 2})
            self.assertEqual(store.read(), {"b": 2})

    def test_runtime_state(self):
        state = RuntimeState({"player.volume": 70})
        state.set("bluetooth.connected", None)
        state.update({"player.playing": True})
        self.assertEqual(state.get("player.volume"), 70)
        self.assertTrue(state.get("player.playing"))
        self.assertEqual(state.incr("counter", 2), 2)
        snapshot = state.snapshot()
        snapshot["player.volume"] = 1
        self.assertEqual(state.get("player.volume"), 70)
        state.delete("counter")
        self.assertFalse("counter" in state.snapshot())


class CliParserTests(unittest.TestCase):
    def test_parse_status(self):
        args = build_parser().parse_args(["--port", "9000", "--json", "status"])
        self.assertEqual(args.command, "status")
        self.assertEqual(args.port, 9000)
        self.assertTrue(args.json)

    def test_parse_serve(self):
        args = build_parser().parse_args(["serve", "--data-dir", "./x", "--port", "8723", "--sim"])
        self.assertEqual(args.serve_port, 8723)
        self.assertEqual(args.serve_host, None)
        self.assertTrue(args.sim)

    def test_parse_subcommands(self):
        parser = build_parser()
        self.assertEqual(parser.parse_args(["connect", "AA:BB:CC:DD:EE:FF"]).address, "AA:BB:CC:DD:EE:FF")
        self.assertEqual(parser.parse_args(["say", "你好", "世界"]).text, ["你好", "世界"])
        self.assertEqual(parser.parse_args(["rules", "run", "r_1"]).rule_id, "r_1")
        self.assertEqual(parser.parse_args(["logs", "-n", "20"]).lines, 20)
        with self.assertRaises(SystemExit):
            parser.parse_args(["not-a-command"])

    @unittest.skipUnless(
        os.path.isfile(os.path.join(os.path.dirname(btspeaker.__file__), "audio.py")),
        "audio.py 尚未实现，跳过 serve 冒烟测试",
    )
    def test_no_command_defaults_to_serve(self):
        from btspeaker.cli import main

        # 不带子命令时进入 serve 分支；用假的 run_service 避免真的监听端口
        import btspeaker.cli as cli_module
        import btspeaker.service as service_module

        called = {}

        def fake_run_service(data_dir=None, host=None, port=None):
            called.update({"data_dir": data_dir, "host": host, "port": port})
            return 0

        original = service_module.run_service
        service_module.run_service = fake_run_service
        try:
            self.assertEqual(main([]), 0)
        finally:
            service_module.run_service = original
            del cli_module
        self.assertIn("data_dir", called)


if __name__ == "__main__":
    unittest.main()
