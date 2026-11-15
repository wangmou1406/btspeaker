"""调度器单元测试：at + days 匹配、去重、every_minutes、next_runs、线程生命周期。"""

from __future__ import annotations

import time
import unittest
from datetime import datetime, timedelta

from btspeaker.scheduler import Scheduler
from btspeaker.util import iso_now
from tests.helpers import make_config, temp_data_dir, wait_until

MONDAY = datetime(2026, 5, 4, 7, 30, 0)  # 2026-05-04 是周一


def make_rule(rule_id, trigger=None, name="测试规则", enabled=True):
    return {
        "id": rule_id,
        "name": name,
        "enabled": enabled,
        "trigger": trigger or {"type": "schedule", "at": "07:30"},
        "conditions": [],
        "actions": [{"type": "log", "message": "hi"}],
    }


class SchedulerTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = temp_data_dir()
        self.data_dir = self._tmp.__enter__()
        self.cfg = make_config(self.data_dir)
        self.fired = []

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def make(self, rules, clock=None):
        self.rules = list(rules)
        return Scheduler(
            self.cfg,
            lambda: list(self.rules),
            self._on_due,
            clock=clock,
        )

    def _on_due(self, rule, source):
        self.fired.append((rule.get("id"), source))


class AtMatchTest(SchedulerTestCase):
    def test_matches_exact_minute(self):
        scheduler = self.make([make_rule("r1", {"type": "schedule", "at": "07:30"})])
        self.assertEqual(scheduler.tick(MONDAY), 1)
        self.assertEqual(self.fired, [("r1", "schedule")])

    def test_does_not_match_other_minute(self):
        scheduler = self.make([make_rule("r1", {"type": "schedule", "at": "07:31"})])
        self.assertEqual(scheduler.tick(MONDAY), 0)
        self.assertEqual(self.fired, [])

    def test_seconds_within_same_minute_still_match(self):
        scheduler = self.make([make_rule("r1", {"type": "schedule", "at": "07:30"})])
        self.assertEqual(scheduler.tick(MONDAY.replace(second=59)), 1)

    def test_dedup_within_same_minute(self):
        scheduler = self.make([make_rule("r1", {"type": "schedule", "at": "07:30"})])
        self.assertEqual(scheduler.tick(MONDAY), 1)
        self.assertEqual(scheduler.tick(MONDAY.replace(second=20)), 0)
        self.assertEqual(len(self.fired), 1)

    def test_fires_again_next_day(self):
        scheduler = self.make([make_rule("r1", {"type": "schedule", "at": "07:30"})])
        self.assertEqual(scheduler.tick(MONDAY), 1)
        self.assertEqual(scheduler.tick(MONDAY + timedelta(days=1)), 1)
        self.assertEqual(len(self.fired), 2)

    def test_days_filter(self):
        rule = make_rule("r1", {"type": "schedule", "at": "07:30", "days": [0]})
        scheduler = self.make([rule])
        self.assertEqual(scheduler.tick(MONDAY), 1)  # 周一
        self.assertEqual(scheduler.tick(MONDAY + timedelta(days=1)), 0)  # 周二不匹配
        self.assertEqual(len(self.fired), 1)

    def test_empty_days_means_every_day(self):
        rule = make_rule("r1", {"type": "schedule", "at": "07:30", "days": []})
        scheduler = self.make([rule])
        for offset in range(3):
            self.assertEqual(scheduler.tick(MONDAY + timedelta(days=offset)), 1)
        self.assertEqual(len(self.fired), 3)

    def test_multiple_rules_same_minute(self):
        scheduler = self.make(
            [
                make_rule("r1", {"type": "schedule", "at": "07:30"}),
                make_rule("r2", {"type": "schedule", "at": "07:30", "days": [0]}),
            ]
        )
        self.assertEqual(scheduler.tick(MONDAY), 2)
        self.assertEqual(len(self.fired), 2)

    def test_disabled_rule_skipped(self):
        scheduler = self.make(
            [make_rule("r1", {"type": "schedule", "at": "07:30"}, enabled=False)]
        )
        self.assertEqual(scheduler.tick(MONDAY), 0)

    def test_non_schedule_trigger_ignored(self):
        scheduler = self.make(
            [
                make_rule("r1", {"type": "manual"}),
                make_rule("r2", {"type": "bluetooth", "event": "connected"}),
                make_rule("r3", {"type": "boot", "delay": 1}),
            ]
        )
        self.assertEqual(scheduler.tick(MONDAY), 0)
        self.assertEqual(self.fired, [])


class EveryMinutesTest(SchedulerTestCase):
    def test_first_tick_starts_timer_without_firing(self):
        scheduler = self.make([make_rule("r1", {"type": "schedule", "every_minutes": 30})])
        self.assertEqual(scheduler.tick(MONDAY), 0)
        self.assertEqual(scheduler.tick(MONDAY + timedelta(minutes=29)), 0)
        self.assertEqual(scheduler.tick(MONDAY + timedelta(minutes=30)), 1)
        self.assertEqual(self.fired, [("r1", "schedule")])

    def test_fires_repeatedly(self):
        scheduler = self.make([make_rule("r1", {"type": "schedule", "every_minutes": 10})])
        scheduler.tick(MONDAY)
        for minute in (10, 20, 30):
            self.assertEqual(scheduler.tick(MONDAY + timedelta(minutes=minute)), 1)
        self.assertEqual(len(self.fired), 3)

    def test_missed_cycles_do_not_backfill(self):
        scheduler = self.make([make_rule("r1", {"type": "schedule", "every_minutes": 10})])
        scheduler.tick(MONDAY)
        # 一口气跳过 15 个周期，只补触发一次
        self.assertEqual(scheduler.tick(MONDAY + timedelta(minutes=150)), 1)
        self.assertEqual(len(self.fired), 1)
        # 下一个周期从实际触发时间重新算，不连发
        self.assertEqual(scheduler.tick(MONDAY + timedelta(minutes=151)), 0)
        self.assertEqual(scheduler.tick(MONDAY + timedelta(minutes=160)), 1)

    def test_interval_change_resets_timer(self):
        scheduler = self.make([make_rule("r1", {"type": "schedule", "every_minutes": 30})])
        scheduler.tick(MONDAY)
        self.rules[0]["trigger"] = {"type": "schedule", "every_minutes": 10}
        self.assertEqual(scheduler.tick(MONDAY + timedelta(minutes=5)), 0)
        self.assertEqual(scheduler.tick(MONDAY + timedelta(minutes=15)), 1)

    def test_every_minutes_invalid_ignored(self):
        scheduler = self.make([make_rule("r1", {"type": "schedule", "every_minutes": 0})])
        self.assertEqual(scheduler.tick(MONDAY), 0)
        self.assertEqual(scheduler.tick(MONDAY + timedelta(hours=1)), 0)


class RobustnessTest(SchedulerTestCase):
    def test_on_due_exception_does_not_raise_or_stop_other_rules(self):
        def boom(rule, source):
            self.fired.append((rule.get("id"), source))
            raise RuntimeError("故意失败")

        self.rules = [
            make_rule("r1", {"type": "schedule", "at": "07:30"}),
            make_rule("r2", {"type": "schedule", "at": "07:30"}),
        ]
        scheduler = Scheduler(self.cfg, lambda: list(self.rules), boom)
        self.assertEqual(scheduler.tick(MONDAY), 2)
        self.assertEqual(len(self.fired), 2)
        # 下一分钟仍能正常工作
        self.assertEqual(scheduler.tick(MONDAY + timedelta(minutes=1)), 0)

    def test_rules_provider_exception_is_caught(self):
        def provider():
            raise RuntimeError("规则源坏了")

        scheduler = Scheduler(self.cfg, provider, self._on_due)
        self.assertEqual(scheduler.tick(MONDAY), 0)
        self.assertIn("规则列表", scheduler.last_error)

    def test_clock_injection(self):
        scheduler = self.make(
            [make_rule("r1", {"type": "schedule", "at": "07:30"})],
            clock=lambda: MONDAY,
        )
        self.assertEqual(scheduler.tick(), 1)
        self.assertEqual(self.fired, [("r1", "schedule")])

    def test_bad_clock_returns_zero(self):
        scheduler = self.make(
            [make_rule("r1", {"type": "schedule", "at": "07:30"})],
            clock=lambda: "not-a-datetime",
        )
        self.assertEqual(scheduler.tick(), 0)
        self.assertTrue(scheduler.last_error)


class NextRunsTest(SchedulerTestCase):
    def test_next_run_for_at_rule(self):
        scheduler = self.make(
            [make_rule("r1", {"type": "schedule", "at": "07:30", "days": []})],
            clock=lambda: MONDAY.replace(hour=6, minute=0),
        )
        runs = scheduler.next_runs(5)
        self.assertEqual(len(runs), 1)
        expected = MONDAY
        self.assertEqual(runs[0]["rule_id"], "r1")
        self.assertEqual(runs[0]["name"], "测试规则")
        self.assertEqual(runs[0]["at"], iso_now(expected))
        self.assertEqual(runs[0]["in"], 90 * 60)

    def test_next_run_skips_to_matching_weekday(self):
        # 当前周一 08:00，规则只在周一 07:30 => 下一次是下周一
        scheduler = self.make(
            [make_rule("r1", {"type": "schedule", "at": "07:30", "days": [0]})],
            clock=lambda: MONDAY.replace(hour=8, minute=0),
        )
        runs = scheduler.next_runs(1)
        expected = MONDAY + timedelta(days=7)
        self.assertEqual(runs[0]["at"], iso_now(expected))
        self.assertEqual(runs[0]["in"], 7 * 24 * 3600 - 30 * 60)

    def test_next_runs_sorted_and_limited(self):
        rules = [
            make_rule("r_late", {"type": "schedule", "at": "23:00"}),
            make_rule("r_early", {"type": "schedule", "at": "07:30"}),
            make_rule("r_mid", {"type": "schedule", "at": "12:00"}),
        ]
        scheduler = self.make(rules, clock=lambda: MONDAY.replace(hour=6, minute=0))
        runs = scheduler.next_runs(2)
        self.assertEqual([item["rule_id"] for item in runs], ["r_early", "r_mid"])
        self.assertTrue(runs[0]["in"] < runs[1]["in"])

    def test_next_run_ignores_disabled_and_other_triggers(self):
        rules = [
            make_rule("r_off", {"type": "schedule", "at": "07:30"}, enabled=False),
            make_rule("r_bt", {"type": "bluetooth", "event": "connected"}),
        ]
        scheduler = self.make(rules, clock=lambda: MONDAY.replace(hour=6, minute=0))
        self.assertEqual(scheduler.next_runs(5), [])

    def test_next_run_for_every_minutes_before_first_tick(self):
        scheduler = self.make(
            [make_rule("r1", {"type": "schedule", "every_minutes": 30})],
            clock=lambda: MONDAY,
        )
        runs = scheduler.next_runs(1)
        self.assertEqual(runs[0]["at"], iso_now(MONDAY + timedelta(minutes=30)))
        self.assertEqual(runs[0]["in"], 30 * 60)

    def test_next_run_for_every_minutes_advances_after_tick(self):
        rule = make_rule("r1", {"type": "schedule", "every_minutes": 10})
        scheduler = self.make([rule], clock=lambda: MONDAY)
        scheduler.tick(MONDAY)
        scheduler.tick(MONDAY + timedelta(minutes=10))
        runs = scheduler.next_runs(1)
        self.assertEqual(runs[0]["at"], iso_now(MONDAY + timedelta(minutes=20)))


class ThreadLifecycleTest(SchedulerTestCase):
    def test_start_fires_and_stop_exits(self):
        now = datetime.now()
        at = now.strftime("%H:%M")
        scheduler = self.make([make_rule("r1", {"type": "schedule", "at": at})])
        scheduler.start()
        try:
            self.assertTrue(wait_until(lambda: len(self.fired) >= 1, timeout=5.0))
            self.assertTrue(scheduler.running)
        finally:
            self.assertTrue(scheduler.stop() or True)
        self.assertFalse(scheduler.is_alive())
        self.assertFalse(scheduler.running)

    def test_start_is_idempotent(self):
        scheduler = self.make([])
        scheduler.start()
        try:
            scheduler.start()  # 不抛异常
            self.assertTrue(scheduler.is_alive())
        finally:
            scheduler.stop()
        self.assertFalse(scheduler.is_alive())

    def test_stop_before_start_is_safe(self):
        scheduler = self.make([])
        scheduler.stop()
        self.assertFalse(scheduler.is_alive())
        self.assertFalse(scheduler.running)


if __name__ == "__main__":
    unittest.main()
