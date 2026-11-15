"""规则模型 / 规则存储的单元测试（不依赖硬件与网络）。"""

from __future__ import annotations

import json
import os
import unittest
from datetime import datetime

from btspeaker.rules import (
    ACTION_TYPES,
    CONDITION_TYPES,
    TRIGGER_TYPES,
    RuleError,
    RuleStore,
    evaluate_conditions,
    new_rule,
    normalize_rule,
    validate_rule,
)
from tests.helpers import temp_data_dir


def _rule(name="测试规则", trigger=None, actions=None, **extra):
    rule = new_rule(
        name,
        trigger if trigger is not None else {"type": "manual"},
        actions if actions is not None else [{"type": "log", "message": "hello"}],
    )
    rule.update(extra)
    return rule


def _errors(rule):
    return validate_rule(rule)


class NormalizeTest(unittest.TestCase):
    def test_new_rule_has_all_contract_fields(self):
        rule = new_rule("早安播报", {"type": "schedule", "at": "07:30"}, [{"type": "log", "message": "hi"}])
        for key in ("id", "name", "enabled", "created_at", "updated_at", "cooldown", "trigger", "conditions", "actions"):
            self.assertIn(key, rule)
        self.assertTrue(str(rule["id"]).startswith("r_"))
        self.assertEqual(rule["name"], "早安播报")
        self.assertIs(rule["enabled"], True)
        self.assertEqual(rule["cooldown"], 0)
        self.assertEqual(rule["conditions"], [])
        self.assertEqual(validate_rule(rule), [])

    def test_types_are_contract_values(self):
        self.assertEqual(TRIGGER_TYPES, ("schedule", "bluetooth", "boot", "manual"))
        self.assertEqual(
            ACTION_TYPES,
            ("play", "tts", "volume", "connect", "disconnect", "command", "http", "wait", "log"),
        )
        self.assertEqual(CONDITION_TYPES, ("connected", "time_between", "weekday_in"))

    def test_normalize_empty_rule(self):
        rule = normalize_rule({})
        self.assertEqual(rule["trigger"], {"type": "manual"})
        self.assertEqual(rule["actions"], [])
        self.assertEqual(rule["conditions"], [])
        self.assertTrue(rule["enabled"])
        self.assertTrue(_errors(rule))  # 名称为空 + 无动作 => 有错

    def test_normalize_does_not_mutate_input(self):
        raw = {"name": "x", "trigger": {"type": "manual"}, "actions": [{"type": "log", "message": "m"}]}
        normalize_rule(raw)
        self.assertNotIn("id", raw)
        self.assertNotIn("enabled", raw)

    def test_normalize_schedule_defaults_days(self):
        rule = normalize_rule(_rule(trigger={"type": "schedule", "at": "07:30"}))
        self.assertEqual(rule["trigger"]["days"], [])

    def test_normalize_bluetooth_defaults(self):
        rule = normalize_rule(_rule(trigger={"type": "bluetooth", "event": "Connected", "device": "aa:bb:cc:dd:ee:ff"}))
        self.assertEqual(rule["trigger"]["event"], "connected")
        self.assertEqual(rule["trigger"]["device"], "AA:BB:CC:DD:EE:FF")

    def test_normalize_boot_default_delay(self):
        rule = normalize_rule(_rule(trigger={"type": "boot"}))
        self.assertEqual(rule["trigger"]["delay"], 5)

    def test_normalize_condition_and_action_types(self):
        rule = normalize_rule(
            _rule(
                trigger={"type": "manual"},
                actions=[
                    {"type": "PLAY", "source": "x.wav"},
                    {"type": "Wait", "seconds": 1},
                ],
            )
        )
        self.assertEqual(rule["actions"][0]["type"], "play")
        self.assertEqual(rule["actions"][1]["type"], "wait")
        self.assertIs(rule["actions"][0]["loop"], False)

    def test_normalize_garbage_trigger_falls_back_manual(self):
        rule = normalize_rule(_rule(trigger="not-a-dict"))
        self.assertEqual(rule["trigger"], {"type": "manual"})


class ValidateValidTest(unittest.TestCase):
    def test_valid_schedule_at_days(self):
        rule = _rule(trigger={"type": "schedule", "at": "07:30", "days": [0, 1, 2, 3, 4]}, cooldown=300)
        self.assertEqual(_errors(rule), [])

    def test_valid_schedule_at_without_days_means_every_day(self):
        self.assertEqual(_errors(_rule(trigger={"type": "schedule", "at": "00:00"})), [])

    def test_valid_schedule_every_minutes(self):
        for interval in (1, 30, 1440):
            self.assertEqual(_errors(_rule(trigger={"type": "schedule", "every_minutes": interval})), [])

    def test_valid_bluetooth_trigger(self):
        for event in ("connected", "disconnected", "paired"):
            self.assertEqual(_errors(_rule(trigger={"type": "bluetooth", "event": event})), [])
        self.assertEqual(
            _errors(_rule(trigger={"type": "bluetooth", "event": "connected", "device": "AA:BB:CC:DD:EE:FF"})),
            [],
        )

    def test_valid_boot_and_manual(self):
        self.assertEqual(_errors(_rule(trigger={"type": "boot", "delay": 0})), [])
        self.assertEqual(_errors(_rule(trigger={"type": "boot", "delay": 3600})), [])
        self.assertEqual(_errors(_rule(trigger={"type": "manual"})), [])

    def test_valid_conditions(self):
        rule = _rule(
            actions=[{"type": "log", "message": "m"}],
        )
        rule["conditions"] = [
            {"type": "connected", "value": True},
            {"type": "time_between", "start": "08:00", "end": "22:00"},
            {"type": "weekday_in", "days": [0, 1, 2]},
        ]
        self.assertEqual(_errors(rule), [])

    def test_all_nine_action_types_valid(self):
        actions = [
            {"type": "play", "source": "chime.wav", "label": "提示音", "volume": 70, "loop": False},
            {"type": "tts", "text": "你好", "voice": "zh", "volume": 60},
            {"type": "volume", "level": 30},
            {"type": "connect", "address": "AA:BB:CC:DD:EE:FF"},
            {"type": "disconnect"},
            {"type": "command", "command": "echo hi", "timeout": 20},
            {"type": "http", "url": "http://127.0.0.1:1/hook", "method": "POST", "headers": {"X-A": "1"}, "body": "b", "timeout": 10},
            {"type": "wait", "seconds": 2},
            {"type": "log", "message": "记录"},
        ]
        self.assertEqual(len(actions), len(ACTION_TYPES))
        self.assertEqual(_errors(_rule(actions=actions)), [])


class ValidateBoundaryTest(unittest.TestCase):
    def assert_errors(self, rule, *fragments):
        errors = _errors(rule)
        self.assertTrue(errors, "期望有校验错误，实际通过")
        text = "；".join(errors)
        for fragment in fragments:
            self.assertIn(fragment, text)

    # -- 名称/冷却 ------------------------------------------------------ #
    def test_name_empty(self):
        self.assert_errors(_rule(name=""), "name")

    def test_name_too_long(self):
        self.assert_errors(_rule(name="长" * 65), "64")

    def test_name_64_ok(self):
        self.assertEqual(_errors(_rule(name="长" * 64)), [])

    def test_cooldown_invalid(self):
        self.assert_errors(_rule(cooldown=-1), "cooldown")
        self.assert_errors(_rule(cooldown="abc"), "cooldown")

    # -- 触发器 --------------------------------------------------------- #
    def test_trigger_missing_or_bad_type(self):
        rule = _rule()
        rule["trigger"] = {}
        self.assert_errors(rule, "trigger.type")
        rule["trigger"] = {"type": "unknown"}
        self.assert_errors(rule, "trigger.type")

    def test_schedule_requires_one_of_at_every(self):
        self.assert_errors(_rule(trigger={"type": "schedule"}), "at", "every_minutes")
        self.assert_errors(
            _rule(trigger={"type": "schedule", "at": "07:30", "every_minutes": 30}),
            "不能同时设置",
        )

    def test_schedule_bad_at_format(self):
        for bad in ("7:30", "24:00", "07:60", "0730"):
            self.assert_errors(_rule(trigger={"type": "schedule", "at": bad}), "trigger.at")
        # 空 at 等价于未设置，报「需要 at 或 every_minutes」
        self.assert_errors(_rule(trigger={"type": "schedule", "at": ""}), "at")

    def test_schedule_bad_days(self):
        self.assert_errors(_rule(trigger={"type": "schedule", "at": "07:30", "days": [7]}), "days")
        self.assert_errors(_rule(trigger={"type": "schedule", "at": "07:30", "days": ["周一"]}), "days")
        self.assert_errors(_rule(trigger={"type": "schedule", "at": "07:30", "days": "1,2"}), "days")

    def test_every_minutes_range(self):
        for bad in (0, 1441, -5, "abc"):
            self.assert_errors(_rule(trigger={"type": "schedule", "every_minutes": bad}), "every_minutes")
        for good in (1, 60, 1440):
            self.assertEqual(_errors(_rule(trigger={"type": "schedule", "every_minutes": good})), [])

    def test_bluetooth_event_and_mac(self):
        self.assert_errors(_rule(trigger={"type": "bluetooth", "event": "online"}), "trigger.event")
        self.assert_errors(_rule(trigger={"type": "bluetooth", "event": "connected", "device": "zz:bb"}), "MAC")

    def test_boot_delay_range(self):
        self.assert_errors(_rule(trigger={"type": "boot", "delay": -1}), "delay")
        self.assert_errors(_rule(trigger={"type": "boot", "delay": 3601}), "delay")

    # -- 条件 ----------------------------------------------------------- #
    def test_condition_unknown_type(self):
        rule = _rule()
        rule["conditions"] = [{"type": "weather"}]
        self.assert_errors(rule, "第 1 个条件", "type")

    def test_connected_value_must_be_bool(self):
        rule = _rule()
        rule["conditions"] = [{"type": "connected", "value": "yes"}]
        self.assert_errors(rule, "connected", "value")

    def test_time_between_format(self):
        rule = _rule()
        rule["conditions"] = [{"type": "time_between", "start": "8:00", "end": "22:00"}]
        self.assert_errors(rule, "time_between", "start")
        rule["conditions"] = [{"type": "time_between", "start": "08:00", "end": "25:00"}]
        self.assert_errors(rule, "time_between", "end")

    def test_weekday_in_days(self):
        rule = _rule()
        rule["conditions"] = [{"type": "weekday_in", "days": []}]
        self.assert_errors(rule, "weekday_in")
        rule["conditions"] = [{"type": "weekday_in", "days": [0, 9]}]
        self.assert_errors(rule, "weekday_in")

    # -- 动作 ----------------------------------------------------------- #
    def test_actions_required(self):
        rule = _rule()
        rule["actions"] = []
        self.assert_errors(rule, "actions")
        rule["actions"] = "not-a-list"
        self.assert_errors(rule, "actions")

    def test_action_unknown_type(self):
        self.assert_errors(_rule(actions=[{"type": "reboot"}]), "第 1 个动作", "type")

    def test_play_requires_source(self):
        self.assert_errors(_rule(actions=[{"type": "play"}]), "play", "source")

    def test_play_volume_range(self):
        self.assert_errors(_rule(actions=[{"type": "play", "source": "a.wav", "volume": 101}]), "volume")

    def test_tts_requires_text(self):
        self.assert_errors(_rule(actions=[{"type": "tts", "text": "  "}]), "tts", "text")

    def test_volume_level_required_and_range(self):
        self.assert_errors(_rule(actions=[{"type": "volume"}]), "volume", "level")
        self.assert_errors(_rule(actions=[{"type": "volume", "level": 1000}]), "level")

    def test_connect_address_must_be_mac(self):
        self.assert_errors(_rule(actions=[{"type": "connect", "address": "客厅音箱"}]), "MAC")
        self.assertEqual(_errors(_rule(actions=[{"type": "connect"}])), [])

    def test_command_requires_command(self):
        self.assert_errors(_rule(actions=[{"type": "command"}]), "command")
        self.assert_errors(_rule(actions=[{"type": "command", "command": "ls", "timeout": 0}]), "timeout")

    def test_http_requires_valid_url(self):
        self.assert_errors(_rule(actions=[{"type": "http"}]), "http", "url")
        self.assert_errors(_rule(actions=[{"type": "http", "url": "ftp://x"}]), "http://")
        self.assert_errors(
            _rule(actions=[{"type": "http", "url": "http://x", "method": "TRACE"}]),
            "method",
        )
        self.assert_errors(
            _rule(actions=[{"type": "http", "url": "http://x", "headers": [], "body": ""}]),
            "headers",
        )
        self.assert_errors(
            _rule(actions=[{"type": "http", "url": "http://x", "timeout": 999}]),
            "timeout",
        )

    def test_wait_seconds_range(self):
        self.assert_errors(_rule(actions=[{"type": "wait"}]), "wait", "seconds")
        self.assert_errors(_rule(actions=[{"type": "wait", "seconds": 301}]), "seconds")

    def test_log_requires_message(self):
        self.assert_errors(_rule(actions=[{"type": "log"}]), "log", "message")

    def test_multiple_errors_reported_together(self):
        rule = _rule(name="")
        rule["trigger"] = {"type": "schedule", "at": "99:99"}
        rule["actions"] = [{"type": "play"}]
        errors = _errors(rule)
        self.assertGreaterEqual(len(errors), 3)


class ConditionEvaluationTest(unittest.TestCase):
    """规则条件求值（全部满足才通过），时间可注入便于断言。"""

    MONDAY_10 = datetime(2026, 5, 4, 10, 0, 0)
    MONDAY_23 = datetime(2026, 5, 4, 23, 30, 0)

    def test_empty_conditions_pass(self):
        self.assertEqual(evaluate_conditions([], True), (True, ""))

    def test_connected_condition(self):
        cond_true = [{"type": "connected", "value": True}]
        self.assertTrue(evaluate_conditions(cond_true, True)[0])
        ok, detail = evaluate_conditions(cond_true, False)
        self.assertFalse(ok)
        self.assertIn("没有已连接", detail)
        cond_false = [{"type": "connected", "value": False}]
        self.assertTrue(evaluate_conditions(cond_false, False)[0])
        ok, detail = evaluate_conditions(cond_false, True)
        self.assertFalse(ok)
        self.assertIn("已有", detail)

    def test_time_between_normal_range(self):
        cond = [{"type": "time_between", "start": "08:00", "end": "22:00"}]
        self.assertTrue(evaluate_conditions(cond, False, self.MONDAY_10)[0])
        self.assertFalse(evaluate_conditions(cond, False, self.MONDAY_23)[0])
        self.assertTrue(evaluate_conditions(cond, False, datetime(2026, 5, 4, 8, 0))[0])
        self.assertFalse(evaluate_conditions(cond, False, datetime(2026, 5, 4, 22, 0))[0])

    def test_time_between_cross_night(self):
        cond = [{"type": "time_between", "start": "22:00", "end": "06:00"}]
        self.assertTrue(evaluate_conditions(cond, False, self.MONDAY_23)[0])
        self.assertTrue(evaluate_conditions(cond, False, datetime(2026, 5, 4, 5, 59))[0])
        self.assertFalse(evaluate_conditions(cond, False, datetime(2026, 5, 4, 6, 0))[0])
        self.assertFalse(evaluate_conditions(cond, False, self.MONDAY_10)[0])

    def test_time_between_same_start_end_means_all_day(self):
        cond = [{"type": "time_between", "start": "12:00", "end": "12:00"}]
        self.assertTrue(evaluate_conditions(cond, False, self.MONDAY_10)[0])
        self.assertTrue(evaluate_conditions(cond, False, self.MONDAY_23)[0])

    def test_time_between_invalid_format(self):
        ok, detail = evaluate_conditions(
            [{"type": "time_between", "start": "25:00", "end": "06:00"}], False, self.MONDAY_10
        )
        self.assertFalse(ok)
        self.assertIn("格式", detail)

    def test_weekday_in(self):
        monday = self.MONDAY_10
        self.assertTrue(evaluate_conditions([{"type": "weekday_in", "days": [0, 1]}], False, monday)[0])
        ok, detail = evaluate_conditions([{"type": "weekday_in", "days": [5, 6]}], False, monday)
        self.assertFalse(ok)
        self.assertIn("星期", detail)
        ok, detail = evaluate_conditions([{"type": "weekday_in", "days": []}], False, monday)
        self.assertFalse(ok)
        self.assertIn("days", detail)

    def test_unknown_condition_and_garbage_item(self):
        ok, detail = evaluate_conditions([{"type": "weather"}], True)
        self.assertFalse(ok)
        self.assertIn("未知条件类型", detail)
        ok, detail = evaluate_conditions([None], True)  # type: ignore[list-item]
        self.assertFalse(ok)
        self.assertIn("格式不正确", detail)

    def test_all_conditions_must_pass(self):
        conditions = [
            {"type": "connected", "value": True},
            {"type": "time_between", "start": "08:00", "end": "22:00"},
            {"type": "weekday_in", "days": [0, 1, 2, 3, 4]},
        ]
        self.assertTrue(evaluate_conditions(conditions, True, self.MONDAY_10)[0])
        self.assertFalse(evaluate_conditions(conditions, False, self.MONDAY_10)[0])
        self.assertFalse(evaluate_conditions(conditions, True, self.MONDAY_23)[0])


class RuleStoreTest(unittest.TestCase):
    def _store(self, data_dir):
        return RuleStore(os.path.join(data_dir, "rules.json"))

    def test_add_list_get_counts(self):
        with temp_data_dir() as data_dir:
            store = self._store(data_dir)
            self.assertEqual(store.list(), [])
            self.assertEqual(store.counts(), {"total": 0, "enabled": 0})
            rule = store.add(_rule(name="早安"))
            self.assertEqual(store.counts(), {"total": 1, "enabled": 1})
            self.assertEqual(store.get(rule["id"])["name"], "早安")
            self.assertIsNone(store.get("r_missing"))
            stored = store.list()
            self.assertEqual(len(stored), 1)
            self.assertIn("id", stored[0])

    def test_file_format_version_and_rules(self):
        with temp_data_dir() as data_dir:
            path = os.path.join(data_dir, "rules.json")
            store = RuleStore(path)
            store.add(_rule(name="A"))
            store.add(_rule(name="B"))
            with open(path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
            self.assertEqual(raw["version"], 1)
            self.assertIsInstance(raw["rules"], list)
            self.assertEqual([item["name"] for item in raw["rules"]], ["A", "B"])

    def test_add_invalid_raises_and_does_not_store(self):
        with temp_data_dir() as data_dir:
            store = self._store(data_dir)
            with self.assertRaises(RuleError) as ctx:
                store.add(_rule(name="", actions=[{"type": "log", "message": "m"}]))
            self.assertIn("name", str(ctx.exception))
            self.assertEqual(store.counts()["total"], 0)

    def test_add_assigns_unique_id(self):
        with temp_data_dir() as data_dir:
            store = self._store(data_dir)
            first = store.add(_rule(name="A"))
            second = store.add(_rule(name="B"))
            third = RuleStore(store.path).add(_rule(name="C"))
            self.assertEqual(len({first["id"], second["id"], third["id"]}), 3)
            self.assertTrue(all(str(item["id"]).startswith("r_") for item in store.list()))

    def test_update_partial_keeps_id_and_created_at(self):
        with temp_data_dir() as data_dir:
            store = self._store(data_dir)
            rule = store.add(_rule(name="原名"))
            updated = store.update(rule["id"], {"name": "新名", "enabled": False})
            self.assertEqual(updated["id"], rule["id"])
            self.assertEqual(updated["created_at"], rule["created_at"])
            self.assertEqual(updated["name"], "新名")
            self.assertFalse(updated["enabled"])
            self.assertEqual(store.counts(), {"total": 1, "enabled": 0})

    def test_update_trigger_and_actions(self):
        with temp_data_dir() as data_dir:
            store = self._store(data_dir)
            rule = store.add(_rule())
            updated = store.update(
                rule["id"],
                {
                    "trigger": {"type": "schedule", "at": "23:00"},
                    "actions": [{"type": "volume", "level": 15}],
                },
            )
            self.assertEqual(updated["trigger"]["at"], "23:00")
            self.assertEqual(updated["actions"][0]["type"], "volume")

    def test_update_invalid_raises_and_keeps_original(self):
        with temp_data_dir() as data_dir:
            store = self._store(data_dir)
            rule = store.add(_rule(name="合法"))
            with self.assertRaises(RuleError):
                store.update(rule["id"], {"name": ""})
            self.assertEqual(store.get(rule["id"])["name"], "合法")

    def test_update_missing_raises(self):
        with temp_data_dir() as data_dir:
            store = self._store(data_dir)
            with self.assertRaises(RuleError) as ctx:
                store.update("r_nope", {"name": "x"})
            self.assertIn("不存在", str(ctx.exception))

    def test_delete(self):
        with temp_data_dir() as data_dir:
            store = self._store(data_dir)
            rule = store.add(_rule())
            self.assertTrue(store.delete(rule["id"]))
            self.assertFalse(store.delete(rule["id"]))
            self.assertEqual(store.list(), [])

    def test_set_enabled(self):
        with temp_data_dir() as data_dir:
            store = self._store(data_dir)
            rule = store.add(_rule())
            updated = store.set_enabled(rule["id"], False)
            self.assertFalse(updated["enabled"])
            self.assertEqual(store.counts(), {"total": 1, "enabled": 0})
            self.assertEqual(store.list_enabled(), [])
            store.set_enabled(rule["id"], True)
            self.assertEqual(len(store.list_enabled()), 1)
            with self.assertRaises(RuleError):
                store.set_enabled("r_nope", True)

    def test_replace_all(self):
        with temp_data_dir() as data_dir:
            store = self._store(data_dir)
            store.add(_rule(name="旧"))
            replaced = store.replace_all([_rule(name="甲"), _rule(name="乙"), _rule(name="丙")])
            self.assertEqual([item["name"] for item in replaced], ["甲", "乙", "丙"])
            self.assertEqual(store.counts()["total"], 3)

    def test_replace_all_invalid_is_atomic(self):
        with temp_data_dir() as data_dir:
            store = self._store(data_dir)
            store.add(_rule(name="保留"))
            with self.assertRaises(RuleError):
                store.replace_all([_rule(name="好的"), _rule(name="")])
            self.assertEqual(store.list()[0]["name"], "保留")

    def test_list_is_deep_copy(self):
        with temp_data_dir() as data_dir:
            store = self._store(data_dir)
            rule = store.add(_rule(name="原名"))
            items = store.list()
            items[0]["name"] = "被篡改"
            items[0]["actions"].append({"type": "log", "message": "x"})
            self.assertEqual(store.get(rule["id"])["name"], "原名")
            self.assertEqual(len(store.get(rule["id"])["actions"]), 1)

    def test_persist_and_reload_across_instances(self):
        with temp_data_dir() as data_dir:
            path = os.path.join(data_dir, "rules.json")
            first = RuleStore(path)
            rule = first.add(_rule(name="持久化"))
            second = RuleStore(path)
            self.assertEqual(second.get(rule["id"])["name"], "持久化")
            second.update(rule["id"], {"name": "已更新"})
            first.reload()
            self.assertEqual(first.get(rule["id"])["name"], "已更新")

    def test_corrupt_file_tolerated(self):
        with temp_data_dir() as data_dir:
            path = os.path.join(data_dir, "rules.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{ not json")
            store = RuleStore(path)
            self.assertEqual(store.list(), [])
            store.add(_rule(name="恢复"))
            self.assertEqual(store.counts()["total"], 1)

    def test_legacy_array_format_loaded(self):
        with temp_data_dir() as data_dir:
            path = os.path.join(data_dir, "rules.json")
            rule = _rule(name="旧格式")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump([rule], handle, ensure_ascii=False)
            store = RuleStore(path)
            self.assertEqual(store.list()[0]["name"], "旧格式")

    def test_invalid_stored_rule_skipped(self):
        with temp_data_dir() as data_dir:
            path = os.path.join(data_dir, "rules.json")
            payload = {
                "version": 1,
                "rules": [
                    _rule(name="合法"),
                    {"id": "r_bad", "name": "", "trigger": {"type": "manual"}, "actions": []},
                ],
            }
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False)
            store = RuleStore(path)
            self.assertEqual([item["name"] for item in store.list()], ["合法"])

    def test_saved_rules_survive_validation(self):
        with temp_data_dir() as data_dir:
            store = self._store(data_dir)
            store.add(_rule(name="完整", trigger={"type": "schedule", "at": "06:00"}))
            for rule in store.list():
                self.assertEqual(validate_rule(rule), [])


if __name__ == "__main__":
    unittest.main()
