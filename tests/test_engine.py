"""自动化引擎与 TTS 引擎的单元测试。

覆盖：动作顺序执行 / 单条失败不中断、模板渲染、冷却、条件、命令安全、
蓝牙事件订阅、boot 延迟、队列上限、事件发布、recent / status / 持久化，
以及 TTS 的 http / mock / espeak 缺失与临时文件清理。

全部使用 tests/helpers.py 的假实现，不依赖硬件与外部网络。
"""

from __future__ import annotations

import json
import os
import time
import unittest
import urllib.error
import uuid
import wave
from unittest import mock

from btspeaker.bus import (
    EV_BT_CONNECTED,
    EV_BT_DISCONNECTED,
    EV_BT_PAIRED,
    EV_RULE_ERROR,
    EV_RULE_FINISHED,
    EV_RULE_FIRED,
    EV_SYSTEM_BOOT,
    EventBus,
)
from btspeaker.engine import AutomationEngine
from btspeaker.rules import RuleStore, new_rule, validate_rule
from btspeaker.store import JsonStore, RuntimeState
from btspeaker.tts import TtsEngine, TtsError
from tests.helpers import (
    EventRecorder,
    FakeBluetooth,
    FakePlayer,
    FakeTts,
    make_config,
    temp_data_dir,
    wait_until,
)

DEVICE_A = {
    "address": "AA:BB:CC:11:22:33",
    "name": "客厅小爱音箱",
    "paired": True,
    "connected": True,
    "trusted": True,
    "icon": "audio-card",
}
DEVICE_B = {"address": "AA:BB:CC:44:55:66", "name": "书房 SoundCore", "paired": True, "connected": False}


def make_rule(name="测试规则", trigger=None, actions=None, **extra):
    rule = new_rule(
        name,
        trigger if trigger is not None else {"type": "manual"},
        actions if actions is not None else [{"type": "log", "message": "hello"}],
    )
    rule.update(extra)
    return rule


class EngineTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = temp_data_dir()
        self.data_dir = self._tmp.__enter__()
        self.cfg = make_config(self.data_dir, sim=True)  # bluetooth=sim, player=mock, tts=mock
        self.bus = EventBus()
        self.state = RuntimeState()
        self.state.set("bluetooth.connected", None)  # 默认无连接，行为确定
        self.player = FakePlayer(volume=70)
        self.tts = FakeTts(self.data_dir)
        self.bt = FakeBluetooth()
        self.store = RuleStore(os.path.join(self.data_dir, "rules.json"))
        self.persist = JsonStore(
            os.path.join(self.data_dir, "state.json"),
            default_factory=lambda: {"last_runs": {}, "counters": {}},
        )
        self.recorder = EventRecorder(self.bus)
        self.engine = AutomationEngine(
            self.cfg,
            self.bus,
            self.player,
            self.tts,
            self.bt,
            self.store,
            self.state,
            persist=self.persist,
        )

    def tearDown(self):
        try:
            self.engine.stop()
        except Exception:
            pass
        self.recorder.close()
        self._tmp.__exit__(None, None, None)

    # -- 辅助 ------------------------------------------------------------ #
    def add_rule(self, name="测试规则", trigger=None, actions=None, **extra):
        return self.store.add(make_rule(name, trigger, actions, **extra))

    def start_engine(self):
        self.engine.start()
        return self.engine

    def wait_recent(self, timeout=5.0):
        self.assertTrue(wait_until(lambda: len(self.engine.recent(10)) > 0, timeout=timeout), "等待规则执行超时")
        return self.engine.recent(1)[0]


class ExecuteRuleTest(EngineTestCase):
    def test_execute_contract_structure(self):
        rule = self.add_rule(
            actions=[
                {"type": "log", "message": "第一步"},
                {"type": "tts", "text": "你好，世界", "voice": "zh", "volume": 60},
                {"type": "volume", "level": 55},
                {"type": "log", "message": "最后一步"},
            ]
        )
        result = self.engine.execute_rule(rule, {"trigger": "manual", "job_id": "job_test"})
        for key in ("ok", "rule_id", "rule_name", "trigger", "started_at", "duration", "actions", "error"):
            self.assertIn(key, result)
        self.assertTrue(result["ok"])
        self.assertEqual(result["rule_id"], rule["id"])
        self.assertEqual(result["rule_name"], "测试规则")
        self.assertEqual(result["trigger"], "manual")
        self.assertEqual(result["error"], "")
        self.assertGreaterEqual(result["duration"], 0.0)
        self.assertEqual(
            [item["type"] for item in result["actions"]], ["log", "tts", "volume", "log"]
        )
        self.assertTrue(all(item["ok"] for item in result["actions"]))
        self.assertEqual(self.tts.texts, ["你好，世界"])
        self.assertEqual(self.player.volume, 55)
        self.assertEqual(self.state.get("player.volume"), 55)
        self.assertEqual(len(self.player.played), 1)  # TTS 合成后播放

    def test_action_order_follows_definition(self):
        order = []

        class RecordingPlayer(FakePlayer):
            def play(self, req):
                order.append("play")
                return super().play(req)

        self.player = RecordingPlayer()
        self.engine.player = self.player
        self.add_rule(
            actions=[
                {"type": "log", "message": "a"},
                {"type": "play", "source": "chime.wav"},
                {"type": "wait", "seconds": 0},
            ]
        )
        rule = self.store.list()[0]
        self.engine.execute_rule(rule)
        self.assertEqual(order, ["play"])

    def test_single_action_failure_does_not_stop_following(self):
        self.player.fail_next = True
        rule = self.add_rule(
            actions=[
                {"type": "log", "message": "开始"},
                {"type": "play", "source": "chime.wav"},
                {"type": "log", "message": "结束"},
            ]
        )
        result = self.engine.execute_rule(rule)
        self.assertFalse(result["ok"])
        self.assertEqual(len(result["actions"]), 3)
        self.assertEqual([item["ok"] for item in result["actions"]], [True, False, True])
        self.assertIn("播放失败", result["error"])
        self.assertIn("结束", result["actions"][2]["message"])

    def test_unknown_action_type_does_not_interrupt(self):
        rule = {
            "id": "r_raw",
            "name": "原始规则",
            "enabled": True,
            "trigger": {"type": "manual"},
            "conditions": [],
            "actions": [{"type": "reboot"}, {"type": "log", "message": "继续"}],
        }
        result = self.engine.execute_rule(rule)
        self.assertFalse(result["ok"])
        self.assertEqual(result["actions"][0]["type"], "reboot")
        self.assertFalse(result["actions"][0]["ok"])
        self.assertTrue(result["actions"][1]["ok"])

    def test_missing_rule_returns_error(self):
        result = self.engine.execute_rule("r_not_exist")
        self.assertFalse(result["ok"])
        self.assertIn("不存在", result["error"])

    def test_play_and_tts_play_requests(self):
        rule = self.add_rule(
            actions=[
                {"type": "play", "source": "welcome.wav", "label": "欢迎光临", "volume": 40},
                {"type": "tts", "text": "回家了"},
            ]
        )
        result = self.engine.execute_rule(rule)
        self.assertTrue(result["ok"])
        self.assertEqual(len(self.player.played), 2)
        first = self.player.played[0]
        self.assertEqual(first.source, "welcome.wav")
        self.assertEqual(first.label, "欢迎光临")
        self.assertEqual(first.volume, 40)
        self.assertIs(first.loop, False)
        self.assertEqual(first.rule_id, rule["id"])
        self.assertIn("tts_", os.path.basename(self.player.played[1].source))


class TemplateRenderTest(EngineTestCase):
    def test_tts_text_template_variables(self):
        self.state.set("bluetooth.connected", DEVICE_A)
        rule = self.add_rule(
            actions=[
                {
                    "type": "tts",
                    "text": "{date}|{time}|{weekday}|{hostname}|{rule_name}|{event}|{volume}|{bt_name}|{bt_addr}",
                }
            ],
            cooldown=0,
        )
        self.engine.execute_rule(rule, {"trigger": "bluetooth", "event": "connected"})
        rendered = self.tts.texts[0].split("|")
        self.assertEqual(len(rendered), 9)
        self.assertRegex(rendered[0], r"^\d{4}-\d{2}-\d{2}$")
        self.assertRegex(rendered[1], r"^\d{2}:\d{2}$")
        self.assertIn(rendered[2], list("一二三四五六日"))
        self.assertEqual(rendered[3], self.engine.render_context()["hostname"])
        self.assertEqual(rendered[4], "测试规则")
        self.assertEqual(rendered[5], "connected")
        self.assertEqual(rendered[6], "70")
        self.assertEqual(rendered[7], DEVICE_A["name"])
        self.assertEqual(rendered[8], DEVICE_A["address"])

    def test_datetime_and_play_source_template(self):
        rule = self.add_rule(
            actions=[
                {"type": "log", "message": "{datetime}"},
                {"type": "play", "source": "sounds/{rule_name}.wav"},
            ]
        )
        self.engine.execute_rule(rule)
        self.assertEqual(self.player.played[0].source, "sounds/测试规则.wav")

    def test_render_context_required_keys(self):
        self.state.set("bluetooth.connected", DEVICE_A)
        rule = make_rule(name="上下文")
        ctx = self.engine.render_context(rule, {"trigger": "boot", "event": "system.boot"})
        for key in (
            "time",
            "date",
            "datetime",
            "weekday",
            "hostname",
            "bt_name",
            "bt_addr",
            "volume",
            "rule_name",
            "event",
        ):
            self.assertIn(key, ctx)
        self.assertEqual(ctx["rule_name"], "上下文")
        self.assertEqual(ctx["event"], "system.boot")
        self.assertEqual(ctx["bt_addr"], DEVICE_A["address"])
        self.assertIsInstance(ctx["volume"], int)

    def test_unknown_variable_preserved(self):
        rule = self.add_rule(actions=[{"type": "tts", "text": "保留{unknown_var}原样"}])
        self.engine.execute_rule(rule)
        self.assertEqual(self.tts.texts[0], "保留{unknown_var}原样")


class CooldownAndConditionTest(EngineTestCase):
    def test_cooldown_rejects_second_execution(self):
        rule = self.add_rule(actions=[{"type": "log", "message": "冷却测试"}], cooldown=300)
        first = self.engine.execute_rule(rule)
        self.assertTrue(first["ok"])
        second = self.engine.execute_rule(self.store.get(rule["id"]))
        self.assertFalse(second["ok"])
        self.assertIn("冷却", second["error"])
        self.assertEqual(len(self.engine.recent(10)), 2)

    def test_cooldown_expired_allows_execution(self):
        rule = self.add_rule(actions=[{"type": "log", "message": "冷却测试"}], cooldown=300)
        self.assertTrue(self.engine.execute_rule(rule)["ok"])
        self.engine._last_runs[rule["id"]]["ts"] = time.time() - 301  # 模拟冷却已过
        self.assertTrue(self.engine.execute_rule(self.store.get(rule["id"]))["ok"])

    def test_condition_connected_rejects_when_disconnected(self):
        rule = self.add_rule(
            actions=[{"type": "log", "message": "不该执行"}],
        )
        rule["conditions"] = [{"type": "connected", "value": True}]
        result = self.engine.execute_rule(rule)
        self.assertFalse(result["ok"])
        self.assertIn("条件不满足", result["error"])
        self.assertIn("没有已连接", result["error"])
        self.assertEqual(result["actions"], [])

    def test_condition_connected_passes_when_connected(self):
        self.state.set("bluetooth.connected", DEVICE_A)
        rule = self.add_rule(actions=[{"type": "log", "message": "可以执行"}])
        rule["conditions"] = [{"type": "connected", "value": True}]
        result = self.engine.execute_rule(rule)
        self.assertTrue(result["ok"], result["error"])

    def test_condition_connected_false_requires_no_device(self):
        rule = self.add_rule(actions=[{"type": "log", "message": "未连接才执行"}])
        rule["conditions"] = [{"type": "connected", "value": False}]
        self.assertTrue(self.engine.execute_rule(rule)["ok"])

    def test_condition_time_between_outside(self):
        now = time.localtime()
        start_min = (now.tm_hour * 60 + now.tm_min + 1) % 1440
        end_min = (start_min + 30) % 1440
        start = "%02d:%02d" % (start_min // 60, start_min % 60)
        end = "%02d:%02d" % (end_min // 60, end_min % 60)
        rule = self.add_rule(actions=[{"type": "log", "message": "夜间"}])
        rule["conditions"] = [{"type": "time_between", "start": start, "end": end}]
        result = self.engine.execute_rule(rule)
        self.assertFalse(result["ok"])
        self.assertIn("时间", result["error"])

    def test_condition_time_between_cross_night_inside(self):
        # 构造一个必然包含当前时间的跨夜区间
        current = time.localtime()
        current_min = current.tm_hour * 60 + current.tm_min
        start_min = (current_min - 60) % 1440
        end_min = (current_min + 60) % 1440
        start = "%02d:%02d" % (start_min // 60, start_min % 60)
        end = "%02d:%02d" % (end_min // 60, end_min % 60)
        rule = self.add_rule(actions=[{"type": "log", "message": "跨夜"}])
        rule["conditions"] = [{"type": "time_between", "start": start, "end": end}]
        self.assertTrue(self.engine.execute_rule(rule)["ok"])

    def test_condition_weekday(self):
        today = time.localtime().tm_wday
        rule = self.add_rule(actions=[{"type": "log", "message": "工作日"}])
        rule["conditions"] = [{"type": "weekday_in", "days": [(today + 1) % 7]}]
        result = self.engine.execute_rule(rule)
        self.assertFalse(result["ok"])
        self.assertIn("星期", result["error"])

        rule["conditions"] = [{"type": "weekday_in", "days": [today]}]
        self.assertTrue(self.engine.execute_rule(rule)["ok"])

    def test_disabled_rule_rejected(self):
        rule = self.add_rule(actions=[{"type": "log", "message": "停用"}])
        rule["enabled"] = False
        result = self.engine.execute_rule(rule)
        self.assertFalse(result["ok"])
        self.assertIn("停用", result["error"])

    def test_global_rules_disabled_rejected(self):
        self.cfg.set("rules.enabled", False)
        rule = self.add_rule(actions=[{"type": "log", "message": "全局关闭"}])
        result = self.engine.execute_rule(rule)
        self.assertFalse(result["ok"])
        self.assertIn("关闭", result["error"])

    def test_default_cooldown_from_config(self):
        self.cfg.set("rules.default_cooldown", 120)
        rule = self.add_rule(actions=[{"type": "log", "message": "默认冷却"}])
        rule["cooldown"] = 0
        self.assertTrue(self.engine.execute_rule(rule)["ok"])
        second = self.engine.execute_rule(self.store.get(rule["id"]))
        self.assertFalse(second["ok"])
        self.assertIn("冷却", second["error"])


class CommandSecurityTest(EngineTestCase):
    def test_command_blocked_when_permission_disabled(self):
        rule = self.add_rule(
            actions=[
                {"type": "command", "command": "echo hi"},
                {"type": "log", "message": "后续动作"},
            ]
        )
        result = self.engine.execute_rule(rule)
        self.assertFalse(result["ok"])
        self.assertIn("未开启命令执行权限", result["actions"][0]["message"])
        self.assertTrue(result["actions"][1]["ok"])

    def test_command_blocked_by_allowlist(self):
        self.cfg.set("security.allow_commands", True)
        self.cfg.set("security.command_allowlist", ["echo"])
        rule = self.add_rule(actions=[{"type": "command", "command": "python -c \"print(1)\""}])
        result = self.engine.execute_rule(rule)
        self.assertFalse(result["ok"])
        self.assertIn("允许列表", result["actions"][0]["message"])

    def test_command_allowed_runs(self):
        import sys

        self.cfg.set("security.allow_commands", True)
        self.cfg.set("security.command_allowlist", ["python"])
        interpreter = os.path.basename(sys.executable) or "python"
        command = '%s -c "print(2468)"' % interpreter
        rule = self.add_rule(actions=[{"type": "command", "command": command, "timeout": 20}])
        result = self.engine.execute_rule(rule)
        self.assertTrue(result["ok"], result["error"])
        self.assertIn("2468", result["actions"][0]["message"])

    def test_command_failure_reported(self):
        import sys

        self.cfg.set("security.allow_commands", True)
        self.cfg.set("security.command_allowlist", [])
        interpreter = os.path.basename(sys.executable) or "python"
        rule = self.add_rule(
            actions=[{"type": "command", "command": '%s -c "import sys; sys.exit(3)"' % interpreter}]
        )
        result = self.engine.execute_rule(rule)
        self.assertFalse(result["ok"])
        self.assertIn("退出码 3", result["actions"][0]["message"])


class HttpAndBluetoothActionTest(EngineTestCase):
    def test_http_action_success(self):
        response = mock.MagicMock()
        response.status = 200
        response.read.return_value = b"ok"
        rule = self.add_rule(
            actions=[
                {
                    "type": "http",
                    "url": "http://127.0.0.1:9/hook?rule={rule_name}&date={date}",
                    "method": "POST",
                    "body": "text={event}",
                    "timeout": 5,
                }
            ]
        )
        with mock.patch("btspeaker.engine.urlrequest.urlopen", return_value=response) as urlopen:
            result = self.engine.execute_rule(rule, {"trigger": "manual"})
        self.assertTrue(result["ok"], result["error"])
        self.assertIn("200", result["actions"][0]["message"])
        request = urlopen.call_args[0][0]
        self.assertIn("/hook?rule=", request.full_url)
        self.assertEqual(request.data, b"text=manual")
        self.assertTrue(response.close.called)

    def test_http_action_failure(self):
        rule = self.add_rule(actions=[{"type": "http", "url": "http://127.0.0.1:9/hook"}])
        with mock.patch(
            "btspeaker.engine.urlrequest.urlopen",
            side_effect=urllib.error.URLError("拒绝连接"),
        ):
            result = self.engine.execute_rule(rule)
        self.assertFalse(result["ok"])
        self.assertIn("HTTP 请求失败", result["actions"][0]["message"])

    def test_connect_action(self):
        rule = self.add_rule(actions=[{"type": "connect", "address": DEVICE_B["address"]}])
        result = self.engine.execute_rule(rule)
        self.assertTrue(result["ok"], result["error"])
        info = self.bt.device_info(DEVICE_B["address"])
        self.assertTrue(info.connected)

    def test_connect_action_uses_default_device(self):
        self.cfg.set("bluetooth.default_device", DEVICE_B["address"])
        rule = self.add_rule(actions=[{"type": "connect"}])
        result = self.engine.execute_rule(rule)
        self.assertTrue(result["ok"], result["error"])

    def test_disconnect_action_uses_connected_device(self):
        self.state.set("bluetooth.connected", DEVICE_A)
        rule = self.add_rule(actions=[{"type": "disconnect"}])
        result = self.engine.execute_rule(rule)
        self.assertTrue(result["ok"], result["error"])
        self.assertFalse(self.bt.device_info(DEVICE_A["address"]).connected)

    def test_connect_without_address_or_default_fails(self):
        self.cfg.set("bluetooth.default_device", "")
        rule = self.add_rule(actions=[{"type": "connect"}])
        result = self.engine.execute_rule(rule)
        self.assertFalse(result["ok"])
        self.assertIn("默认设备", result["actions"][0]["message"])


class EventsAndBusTest(EngineTestCase):
    def test_fired_and_finished_events(self):
        rule = self.add_rule(actions=[{"type": "log", "message": "事件"}])
        self.engine.execute_rule(rule, {"trigger": "api", "job_id": "job_1"})
        fired = self.recorder.payloads(EV_RULE_FIRED)
        finished = self.recorder.payloads(EV_RULE_FINISHED)
        self.assertEqual(len(fired), 1)
        self.assertEqual(fired[0]["rule_id"], rule["id"])
        self.assertEqual(fired[0]["rule_name"], "测试规则")
        self.assertEqual(fired[0]["trigger"], "api")
        self.assertEqual(fired[0]["job_id"], "job_1")
        self.assertEqual(len(finished), 1)
        self.assertTrue(finished[0]["ok"])

    def test_error_event_on_action_failure(self):
        self.player.fail_next = True
        rule = self.add_rule(actions=[{"type": "play", "source": "a.wav"}])
        self.engine.execute_rule(rule)
        errors = self.recorder.payloads(EV_RULE_ERROR)
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["rule_id"], rule["id"])
        self.assertIn("error", errors[0])
        self.assertEqual(self.recorder.payloads(EV_RULE_FINISHED), [])

    def test_bluetooth_event_triggers_matching_rule(self):
        self.add_rule(
            name="回家迎宾",
            trigger={"type": "bluetooth", "event": "connected"},
            actions=[{"type": "tts", "text": "欢迎回家，{bt_name}"}],
            cooldown=300,
        )
        self.start_engine()
        self.bus.publish(EV_BT_CONNECTED, {"address": DEVICE_B["address"], "name": DEVICE_B["name"]})
        entry = self.wait_recent()
        self.assertEqual(entry["rule_name"], "回家迎宾")
        self.assertEqual(entry["trigger"], "bluetooth")
        self.assertTrue(wait_until(lambda: len(self.tts.texts) > 0, timeout=3.0))
        self.assertEqual(self.tts.texts[0], "欢迎回家，%s" % DEVICE_B["name"])

    def test_bluetooth_event_device_filter(self):
        self.add_rule(
            name="指定设备",
            trigger={"type": "bluetooth", "event": "connected", "device": DEVICE_A["address"]},
            actions=[{"type": "log", "message": "指定设备触发"}],
        )
        self.start_engine()
        self.bus.publish(EV_BT_CONNECTED, {"address": DEVICE_B["address"], "name": DEVICE_B["name"]})
        time.sleep(0.3)
        self.assertEqual(self.engine.recent(5), [])
        self.bus.publish(EV_BT_CONNECTED, {"address": DEVICE_A["address"], "name": DEVICE_A["name"]})
        entry = self.wait_recent()
        self.assertEqual(entry["rule_name"], "指定设备")

    def test_bluetooth_paired_and_disconnected_events(self):
        self.add_rule(
            name="配对提醒",
            trigger={"type": "bluetooth", "event": "paired"},
            actions=[{"type": "log", "message": "已配对"}],
        )
        self.add_rule(
            name="断开提醒",
            trigger={"type": "bluetooth", "event": "disconnected"},
            actions=[{"type": "log", "message": "已断开"}],
        )
        self.start_engine()
        self.bus.publish(EV_BT_PAIRED, {"address": DEVICE_B["address"], "name": DEVICE_B["name"]})
        self.assertTrue(wait_until(lambda: len(self.engine.recent(5)) >= 1, timeout=5.0))
        self.bus.publish(EV_BT_DISCONNECTED, {"address": DEVICE_B["address"], "name": DEVICE_B["name"]})
        self.assertTrue(wait_until(lambda: len(self.engine.recent(5)) >= 2, timeout=5.0))
        names = [item["rule_name"] for item in self.engine.recent(5)]
        self.assertIn("配对提醒", names)
        self.assertIn("断开提醒", names)

    def test_start_does_not_publish_system_boot(self):
        self.add_rule(
            name="开机播报",
            trigger={"type": "boot", "delay": 0},
            actions=[{"type": "log", "message": "启动"}],
        )
        self.start_engine()
        time.sleep(0.4)
        self.assertEqual(self.engine.recent(5), [], "engine.start() 不应自己触发 boot 规则")

    def test_boot_event_fires_immediately_when_delay_zero(self):
        self.add_rule(
            name="开机播报",
            trigger={"type": "boot", "delay": 0},
            actions=[{"type": "log", "message": "启动"}],
        )
        self.start_engine()
        self.bus.publish(EV_SYSTEM_BOOT, {})
        entry = self.wait_recent()
        self.assertEqual(entry["rule_name"], "开机播报")
        self.assertEqual(entry["trigger"], "boot")

    def test_boot_delay_uses_timer_and_can_be_fired(self):
        rule = self.add_rule(
            name="延迟开机",
            trigger={"type": "boot", "delay": 1},
            actions=[{"type": "log", "message": "开机延迟触发"}],
        )
        self.assertIsNotNone(rule)

        class FakeTimer:
            created = []

            def __init__(self, delay, function, args=(), kwargs=None):
                self.delay = delay
                self.function = function
                self.args = args
                self.kwargs = kwargs or {}
                self.daemon = False
                self.started = False
                self.cancelled = False
                FakeTimer.created.append(self)

            def start(self):
                self.started = True

            def is_alive(self):
                return self.started and not self.cancelled

            def cancel(self):
                self.cancelled = True

            def fire(self):
                self.function(*self.args, **self.kwargs)

        FakeTimer.created = []
        self.start_engine()
        with mock.patch("btspeaker.engine.threading.Timer", FakeTimer):
            self.bus.publish(EV_SYSTEM_BOOT, {})
        self.assertEqual(len(FakeTimer.created), 1)
        timer = FakeTimer.created[0]
        self.assertEqual(timer.delay, 1)
        self.assertTrue(timer.started)
        self.assertEqual(self.engine.recent(5), [])  # 延迟期间不应执行
        timer.fire()
        entry = self.wait_recent()
        self.assertEqual(entry["rule_name"], "延迟开机")

    def test_real_timer_delays_execution(self):
        rules = [
            {
                "id": "r_boot_real",
                "name": "真实延迟",
                "enabled": True,
                "trigger": {"type": "boot", "delay": 1},
                "conditions": [],
                "actions": [{"type": "log", "message": "真实定时器"}],
            }
        ]
        self.engine.rules = rules
        self.start_engine()
        started = time.time()
        self.bus.publish(EV_SYSTEM_BOOT, {})
        time.sleep(0.2)
        self.assertEqual(self.engine.recent(5), [])
        self.assertTrue(wait_until(lambda: len(self.engine.recent(5)) > 0, timeout=4.0))
        self.assertGreaterEqual(time.time() - started, 0.8)


class AsyncAndQueueTest(EngineTestCase):
    def test_run_rule_async_executes(self):
        rule = self.add_rule(actions=[{"type": "log", "message": "异步"}])
        self.start_engine()
        result = self.engine.run_rule(rule["id"], {"trigger": "manual"})
        self.assertTrue(result["accepted"])
        self.assertTrue(str(result["job_id"]).startswith("job_"))
        self.assertEqual(result["reason"], "")
        entry = self.wait_recent()
        self.assertEqual(entry["rule_id"], rule["id"])
        self.assertEqual(entry["trigger"], "manual")
        self.assertEqual(entry["job_id"], result["job_id"])
        self.assertEqual(self.recorder.payloads(EV_RULE_FIRED)[0]["job_id"], result["job_id"])

    def test_run_rule_rejects_missing_and_disabled(self):
        missing = self.engine.run_rule("r_not_exist", {})
        self.assertFalse(missing["accepted"])
        self.assertIn("不存在", missing["reason"])

        rule = self.add_rule(actions=[{"type": "log", "message": "x"}])
        self.store.set_enabled(rule["id"], False)
        disabled = self.engine.run_rule(rule["id"], {})
        self.assertFalse(disabled["accepted"])
        self.assertIn("停用", disabled["reason"])

    def test_run_rule_rejects_when_global_disabled(self):
        rule = self.add_rule(actions=[{"type": "log", "message": "x"}])
        self.cfg.set("rules.enabled", False)
        result = self.engine.run_rule(rule["id"], {})
        self.assertFalse(result["accepted"])
        self.assertIn("关闭", result["reason"])

    def test_queue_full_rejected(self):
        rule = self.add_rule(actions=[{"type": "log", "message": "x"}])
        accepted = 0
        for _ in range(100):
            result = self.engine.run_rule(rule["id"], {"trigger": "manual"})
            if result["accepted"]:
                accepted += 1
        self.assertEqual(accepted, 100)
        rejected = self.engine.run_rule(rule["id"], {"trigger": "manual"})
        self.assertFalse(rejected["accepted"])
        self.assertIn("队列已满", rejected["reason"])

    def test_max_concurrent_workers(self):
        self.cfg.set("rules.max_concurrent", 3)
        engine = AutomationEngine(
            self.cfg, self.bus, self.player, self.tts, self.bt, self.store, self.state
        )
        engine.start()
        try:
            self.assertTrue(wait_until(lambda: engine.status()["workers"] == 3, timeout=2.0))
            self.assertEqual(engine.status()["workers"], 3)
        finally:
            engine.stop()

    def test_stop_is_quick_and_scheduler_stops(self):
        self.start_engine()
        self.assertTrue(self.engine.status()["scheduler_running"])
        started = time.time()
        self.engine.stop()
        self.assertLess(time.time() - started, 4.0)
        status = self.engine.status()
        self.assertFalse(status["scheduler_running"])
        self.assertEqual(status["workers"], 0)


class RecentStatusPersistTest(EngineTestCase):
    def test_recent_newest_first_and_limited(self):
        for index in range(3):
            rule = self.add_rule(name="规则%d" % index, actions=[{"type": "log", "message": "m"}])
            self.engine.execute_rule(rule)
        items = self.engine.recent(10)
        self.assertEqual([item["rule_name"] for item in items], ["规则2", "规则1", "规则0"])
        self.assertEqual(len(self.engine.recent(2)), 2)
        self.assertEqual(len(self.engine.recent(1)), 1)

    def test_recent_keeps_at_most_50(self):
        rule = self.add_rule(actions=[{"type": "log", "message": "many"}])
        for _ in range(55):
            self.engine.execute_rule(self.store.get(rule["id"]))
        self.assertEqual(len(self.engine.recent(100)), 50)

    def test_status_structure(self):
        self.add_rule(
            name="定时",
            trigger={"type": "schedule", "at": "07:30", "days": []},
            actions=[{"type": "log", "message": "定时"}],
        )
        self.start_engine()
        status = self.engine.status()
        for key in ("running", "queued", "scheduler_running", "next_runs", "recent"):
            self.assertIn(key, status)
        self.assertIsInstance(status["running"], int)
        self.assertIsInstance(status["queued"], int)
        self.assertTrue(status["scheduler_running"])
        self.assertEqual(len(status["next_runs"]), 1)
        self.assertEqual(status["next_runs"][0]["name"], "定时")
        self.assertIn("in", status["next_runs"][0])

    def test_next_runs_updates_after_rule_added(self):
        self.add_rule(
            name="后加规则",
            trigger={"type": "schedule", "every_minutes": 30},
            actions=[{"type": "log", "message": "x"}],
        )
        self.engine.reload_rules()
        names = [item["name"] for item in self.engine.status()["next_runs"]]
        self.assertEqual(names, ["后加规则"])

    def test_persist_last_runs_and_cooldown_across_restart(self):
        rule = self.add_rule(actions=[{"type": "log", "message": "持久化"}], cooldown=300)
        self.assertTrue(self.engine.execute_rule(rule)["ok"])
        stored = self.persist.read()
        self.assertIn("last_runs", stored)
        self.assertIn(rule["id"], stored["last_runs"])
        self.assertIn("at", stored["last_runs"][rule["id"]])
        self.assertIn("ts", stored["last_runs"][rule["id"]])

        engine2 = AutomationEngine(
            self.cfg,
            self.bus,
            self.player,
            self.tts,
            self.bt,
            self.store,
            self.state,
            persist=self.persist,
        )
        try:
            result = engine2.execute_rule(self.store.get(rule["id"]))
            self.assertFalse(result["ok"])
            self.assertIn("冷却", result["error"])
        finally:
            engine2.stop()

    def test_rejected_execution_written_to_recent(self):
        rule = self.add_rule(actions=[{"type": "log", "message": "x"}])
        rule["conditions"] = [{"type": "connected", "value": True}]
        self.engine.execute_rule(rule)
        entry = self.engine.recent(1)[0]
        self.assertFalse(entry["ok"])
        self.assertIn("条件不满足", entry["error"])


class RuleValidationIntegrationTest(EngineTestCase):
    def test_stored_rules_are_valid(self):
        self.add_rule(
            name="完整规则",
            trigger={"type": "schedule", "at": "06:30", "days": [0, 1, 2, 3, 4]},
            actions=[
                {"type": "tts", "text": "早安", "volume": 60},
                {"type": "wait", "seconds": 1},
                {"type": "log", "message": "完成"},
            ],
            cooldown=60,
        )
        for rule in self.store.list():
            self.assertEqual(validate_rule(rule), [])


# --------------------------------------------------------------------------- #
# TTS 引擎
# --------------------------------------------------------------------------- #
class FakeHttpResponse:
    def __init__(self, payload=b"", content_type="audio/wav", status=200):
        self.payload = payload
        self.headers = {} if content_type is None else {"Content-Type": content_type}
        self.status = status
        self.closed = False

    def read(self, size=-1):
        if size is None or size < 0:
            return self.payload
        return self.payload[:size]

    def close(self):
        self.closed = True


class TtsEngineTest(unittest.TestCase):
    def setUp(self):
        self._tmp = temp_data_dir()
        self.data_dir = self._tmp.__enter__()
        self.cfg = make_config(self.data_dir, sim=True)
        self.tts = TtsEngine(self.cfg)

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_mock_backend_generates_valid_wav(self):
        path = self.tts.synthesize("测试语音")
        self.assertTrue(os.path.isfile(path))
        self.assertGreater(os.path.getsize(path), 44)
        with wave.open(path, "rb") as handle:
            self.assertEqual(handle.getnchannels(), 1)
            self.assertEqual(handle.getsampwidth(), 2)
            self.assertEqual(handle.getframerate(), 22050)
            self.assertEqual(handle.getnframes(), int(22050 * 0.6))
        self.assertTrue(path.startswith(os.path.join(self.data_dir, "tmp")))

    def test_probe_mock(self):
        probe = self.tts.probe()
        self.assertEqual(probe["backend"], "mock")
        self.assertTrue(probe["ready"])
        self.assertIn("hint", probe)

    def test_auto_prefers_http_when_url_configured(self):
        self.cfg.set("tts.backend", "auto")
        self.cfg.set("tts.url", "http://tts.local/say?text={text}&lang={lang}")
        probe = self.tts.probe()
        self.assertEqual(probe["backend"], "http")
        self.assertTrue(probe["ready"])

    def test_auto_without_any_backend_reports_chinese_hint(self):
        self.cfg.set("tts.backend", "auto")
        self.cfg.set("tts.url", "")
        self.cfg.set("tts.espeak", "btspeaker-no-such-espeak-%s" % uuid.uuid4().hex[:6])
        probe = self.tts.probe()
        self.assertFalse(probe["ready"])
        self.assertIn("未配置 TTS", probe["hint"])
        with self.assertRaises(TtsError) as ctx:
            self.tts.synthesize("你好")
        self.assertIn("未配置 TTS", str(ctx.exception))

    def test_explicit_http_without_url_errors(self):
        self.cfg.set("tts.backend", "http")
        self.cfg.set("tts.url", "")
        self.assertFalse(self.tts.probe()["ready"])
        with self.assertRaises(TtsError):
            self.tts.synthesize("你好")

    def test_espeak_missing_reports_chinese_error(self):
        self.cfg.set("tts.backend", "espeak")
        self.cfg.set("tts.espeak", "btspeaker-no-such-espeak-%s" % uuid.uuid4().hex[:6])
        probe = self.tts.probe()
        self.assertFalse(probe["ready"])
        self.assertIn("espeak", probe["hint"])
        with self.assertRaises(TtsError) as ctx:
            self.tts.synthesize("你好")
        self.assertIn("espeak", str(ctx.exception))

    def test_empty_text_rejected(self):
        with self.assertRaises(TtsError) as ctx:
            self.tts.synthesize("   ")
        self.assertIn("不能为空", str(ctx.exception))

    def test_http_get_template_and_url_encoding(self):
        self.cfg.set("tts.backend", "http")
        self.cfg.set("tts.url", "http://tts.local/say?text={text}&lang={lang}")
        self.cfg.set("tts.method", "GET")
        response = FakeHttpResponse(b"ID3FAKEAUDIO", "audio/mpeg")
        with mock.patch("btspeaker.tts.urlrequest.urlopen", return_value=response) as urlopen:
            path = self.tts.synthesize("你好 世界", "zh")
        request = urlopen.call_args[0][0]
        self.assertIn("text=%E4%BD%A0%E5%A5%BD%20%E4%B8%96%E7%95%8C", request.full_url)
        self.assertIn("lang=zh", request.full_url)
        self.assertIsNone(request.data)
        self.assertEqual(os.path.splitext(path)[1], ".mp3")
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(), b"ID3FAKEAUDIO")
        self.assertTrue(response.closed)

    def test_http_post_body_template_and_headers(self):
        self.cfg.set("tts.backend", "http")
        self.cfg.set("tts.url", "http://tts.local/say")
        self.cfg.set("tts.method", "POST")
        self.cfg.set("tts.body_template", '{"text": "{text}", "lang": "{lang}"}')
        self.cfg.set("tts.headers", {"X-Token": "abc123"})
        response = FakeHttpResponse(b"RIFFfake", "audio/wav")
        with mock.patch("btspeaker.tts.urlrequest.urlopen", return_value=response) as urlopen:
            path = self.tts.synthesize("早上好")
        request = urlopen.call_args[0][0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(payload["text"], "早上好")
        self.assertEqual(payload["lang"], "zh")
        headers = {key.lower(): value for key, value in request.headers.items()}
        self.assertEqual(headers.get("x-token"), "abc123")
        self.assertEqual(os.path.splitext(path)[1], ".wav")

    def test_http_post_sends_raw_text_when_body_template_empty(self):
        self.cfg.set("tts.backend", "http")
        self.cfg.set("tts.url", "http://tts.local/say")
        self.cfg.set("tts.method", "POST")
        self.cfg.set("tts.body_template", "")
        response = FakeHttpResponse(b"OggS", "audio/ogg")
        with mock.patch("btspeaker.tts.urlrequest.urlopen", return_value=response) as urlopen:
            path = self.tts.synthesize("原文文本")
        self.assertEqual(urlopen.call_args[0][0].data, "原文文本".encode("utf-8"))
        self.assertEqual(os.path.splitext(path)[1], ".ogg")

    def test_http_non_audio_response_raises(self):
        self.cfg.set("tts.backend", "http")
        self.cfg.set("tts.url", "http://tts.local/say?text={text}")
        response = FakeHttpResponse(b"<html>404</html>", "text/html")
        with mock.patch("btspeaker.tts.urlrequest.urlopen", return_value=response):
            with self.assertRaises(TtsError) as ctx:
                self.tts.synthesize("你好")
        message = str(ctx.exception)
        self.assertIn("非音频内容", message)
        self.assertIn("html", message)

    def test_http_audio_fallback_uses_configured_format(self):
        self.cfg.set("tts.backend", "http")
        self.cfg.set("tts.url", "http://tts.local/say?text={text}")
        self.cfg.set("tts.format", "mp3")
        response = FakeHttpResponse(b"FAKEAAC", "audio/aac")
        with mock.patch("btspeaker.tts.urlrequest.urlopen", return_value=response):
            path = self.tts.synthesize("你好")
        self.assertEqual(os.path.splitext(path)[1], ".mp3")

    def test_http_error_status_raises(self):
        self.cfg.set("tts.backend", "http")
        self.cfg.set("tts.url", "http://tts.local/say?text={text}")
        error = urllib.error.HTTPError("http://tts.local/say", 500, "服务器错误", {}, None)
        with mock.patch("btspeaker.tts.urlrequest.urlopen", side_effect=error):
            with self.assertRaises(TtsError) as ctx:
                self.tts.synthesize("你好")
        self.assertIn("HTTP 500", str(ctx.exception))

    def test_http_connection_error_raises(self):
        self.cfg.set("tts.backend", "http")
        self.cfg.set("tts.url", "http://tts.local/say?text={text}")
        with mock.patch(
            "btspeaker.tts.urlrequest.urlopen", side_effect=urllib.error.URLError("无法解析主机")
        ):
            with self.assertRaises(TtsError) as ctx:
                self.tts.synthesize("你好")
        self.assertIn("无法连接", str(ctx.exception))

    def test_cleanup_removes_only_old_tts_files(self):
        tmp_dir = os.path.join(self.data_dir, "tmp")
        os.makedirs(tmp_dir, exist_ok=True)
        old_tts = os.path.join(tmp_dir, "tts_old.wav")
        new_tts = os.path.join(tmp_dir, "tts_new.wav")
        other_old = os.path.join(tmp_dir, "download_old.mp3")
        for path in (old_tts, new_tts, other_old):
            with open(path, "wb") as handle:
                handle.write(b"x")
        old_time = time.time() - 7200
        os.utime(old_tts, (old_time, old_time))
        os.utime(other_old, (old_time, old_time))
        removed = self.tts.cleanup()
        self.assertEqual(removed, 1)
        self.assertFalse(os.path.exists(old_tts))
        self.assertTrue(os.path.exists(new_tts))
        self.assertTrue(os.path.exists(other_old))

    def test_cleanup_without_tmp_dir(self):
        self.assertEqual(TtsEngine(self.cfg).cleanup(), 0)


if __name__ == "__main__":
    unittest.main()
