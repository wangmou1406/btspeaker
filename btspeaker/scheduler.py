"""调度器：只处理 ``schedule`` 型定时规则（at + days / every_minutes）。

- ``tick(now=None)`` 同步执行一次匹配，返回本次触发数量，便于单元测试；
- 线程每 1 秒 tick 一次，任何异常都被捕获，线程不会退出；
- ``at`` 型按「规则 ID + 日期时间」在本分钟去重，重启后重新计时；
- ``every_minutes`` 型在内存维护下次触发时间，错过多次只补触发一次（不追补）；
- ``next_runs`` 供 Web 界面展示下一次触发时间。
"""

from __future__ import annotations

import re
import threading
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Set

from .util import get_logger, iso_now

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_MAX_DAYS_AHEAD = 8


class Scheduler(threading.Thread):
    """定时规则调度线程。"""

    def __init__(
        self,
        cfg,
        rules_provider: Callable[[], List[dict]],
        on_due: Callable[[dict, str], None],
        state: Optional[Any] = None,
        logger: Optional[Any] = None,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        super().__init__(name="btspeaker-scheduler", daemon=True)
        self.cfg = cfg
        self._rules_provider = rules_provider if callable(rules_provider) else (lambda: [])
        self._on_due = on_due if callable(on_due) else (lambda rule, source: None)
        self.state = state
        self.logger = logger or get_logger("scheduler")
        self._clock = clock if callable(clock) else datetime.now
        self._stop_event = threading.Event()
        self._lock = threading.RLock()
        self._fired = {}  # type: Dict[str, datetime]  at 型本分钟去重键
        self._every_next = {}  # type: Dict[str, float] every_minutes 下次触发 epoch
        self._every_interval = {}  # type: Dict[str, int] 记录 interval，变更时重置
        self._started_once = False
        self._tick_count = 0
        self.last_error = ""

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        """启动线程（幂等；已停止的实例不能重启，请新建）。"""
        if self._started_once:
            if not self.is_alive():
                self.logger.warning("调度器实例已停止，不能重复启动")
            return
        self._stop_event.clear()
        self._started_once = True
        super().start()

    def stop(self) -> None:
        """请求停止并等待线程退出（最多 2 秒）。"""
        self._stop_event.set()
        if self.is_alive() and threading.current_thread() is not self:
            self.join(timeout=2.0)
        if self.is_alive():  # pragma: no cover - 极端情况下线程卡住
            self.logger.warning("调度器未能及时退出")

    @property
    def running(self) -> bool:
        return bool(self._started_once and self.is_alive() and not self._stop_event.is_set())

    def run(self) -> None:  # pragma: no cover - 线程入口，单测直接调 tick
        self.logger.info("调度器已启动（每 1 秒检查一次定时规则）")
        while not self._stop_event.is_set():
            try:
                self.tick()
            except Exception:  # pragma: no cover - tick 内部已兜底，双保险
                self.logger.exception("调度器 tick 异常")
            self._stop_event.wait(1.0)
        self.logger.info("调度器已停止")

    def reload(self) -> None:
        """规则变更后调用：清理已删除规则的内存状态。"""
        with self._lock:
            self.logger.debug("调度器已感知规则变更")

    # ------------------------------------------------------------------ #
    # 匹配
    # ------------------------------------------------------------------ #
    def tick(self, now: Optional[datetime] = None) -> int:
        """同步执行一次匹配，返回触发数量（单测直接调用）。"""
        current = self._safe_now(now)
        if current is None:
            return 0
        rules = self._safe_rules()
        triggered = 0
        active_ids = set()  # type: Set[str]
        with self._lock:
            self._prune_fired_locked(current)
            for rule in rules:
                if not isinstance(rule, dict) or not rule.get("enabled", True):
                    continue
                trigger = rule.get("trigger")
                if not isinstance(trigger, dict):
                    continue
                if str(trigger.get("type") or "").strip().lower() != "schedule":
                    continue
                rule_id = str(rule.get("id") or "").strip()
                if not rule_id:
                    continue
                active_ids.add(rule_id)
                try:
                    if self._match_rule(rule, trigger, current):
                        triggered += 1
                except Exception:  # pragma: no cover - 单条规则异常不影响其它规则
                    self.logger.exception("规则 %s 调度匹配异常", rule_id)
            # 清理已删除 / 已停用规则的内存状态，避免字典无限增长
            for rule_id in list(self._every_next.keys()):
                if rule_id not in active_ids:
                    self._every_next.pop(rule_id, None)
                    self._every_interval.pop(rule_id, None)
        self._tick_count += 1
        return triggered

    def _match_rule(self, rule: dict, trigger: dict, current: datetime) -> bool:
        rule_id = str(rule.get("id") or "")
        every = trigger.get("every_minutes")
        at = trigger.get("at")
        if every is not None and (at is None or str(at).strip() == ""):
            return self._match_every(rule, trigger, current)
        if at is not None and str(at).strip() != "":
            return self._match_at(rule, trigger, current)
        return False

    def _match_at(self, rule: dict, trigger: dict, current: datetime) -> bool:
        rule_id = str(rule.get("id") or "")
        at = str(trigger.get("at") or "").strip()
        if not _TIME_RE.match(at):
            return False
        if current.strftime("%H:%M") != at:
            return False
        days = self._normalize_days(trigger.get("days"))
        if days and current.weekday() not in days:
            return False
        key = "%s|%s" % (rule_id, current.strftime("%Y-%m-%d %H:%M"))
        if key in self._fired:
            return False
        self._fired[key] = current
        self.logger.info("定时触发规则：%s（%s）", rule.get("name") or rule_id, at)
        self._safe_due(rule, "schedule")
        return True

    def _match_every(self, rule: dict, trigger: dict, current: datetime) -> bool:
        rule_id = str(rule.get("id") or "")
        try:
            interval = int(trigger.get("every_minutes"))
        except (TypeError, ValueError):
            return False
        if interval < 1 or interval > 1440:
            return False
        step = interval * 60.0
        now_epoch = current.timestamp()
        observed = self._every_next.get(rule_id)
        if observed is None or self._every_interval.get(rule_id) != interval:
            # 首次见到该规则（或间隔被修改）：从现在起完整等一个周期，不立即触发
            self._every_next[rule_id] = now_epoch + step
            self._every_interval[rule_id] = interval
            return False
        if now_epoch < observed:
            return False
        # 顺延到严格大于当前时间的下一个周期（错过多次也只触发一次）
        next_epoch = observed
        while next_epoch <= now_epoch:
            next_epoch += step
        self._every_next[rule_id] = next_epoch
        self.logger.info(
            "周期触发规则：%s（每 %d 分钟）", rule.get("name") or rule_id, interval
        )
        self._safe_due(rule, "schedule")
        return True

    def _safe_due(self, rule: dict, source: str) -> None:
        try:
            self._on_due(rule, source)
        except Exception:  # pragma: no cover - 防御性
            self.logger.exception("触发规则 %s 的回调异常", rule.get("id"))

    def _prune_fired_locked(self, current: datetime) -> None:
        """只保留当天的去重键，避免跨天无限增长。"""
        if len(self._fired) <= 256:
            return
        today = current.strftime("%Y-%m-%d")
        stale = [key for key, value in self._fired.items() if value.strftime("%Y-%m-%d") != today]
        for key in stale:
            self._fired.pop(key, None)

    def _safe_now(self, now: Optional[datetime]) -> Optional[datetime]:
        try:
            current = now if now is not None else self._clock()
        except Exception as exc:  # pragma: no cover - 注入时钟异常
            self.last_error = "读取当前时间失败：%s" % exc
            self.logger.error(self.last_error)
            return None
        if not isinstance(current, datetime):
            self.last_error = "时钟返回值不是 datetime：%r" % (current,)
            self.logger.error(self.last_error)
            return None
        return current

    def _safe_rules(self) -> List[dict]:
        try:
            rules = self._rules_provider()
        except Exception as exc:
            self.last_error = "读取规则列表失败：%s" % exc
            self.logger.error(self.last_error)
            return []
        if not isinstance(rules, list):
            return []
        return rules

    @staticmethod
    def _normalize_days(raw: Any) -> Set[int]:
        days = set()  # type: Set[int]
        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, bool):
                    continue
                try:
                    value = int(item)
                except (TypeError, ValueError):
                    continue
                if 0 <= value <= 6:
                    days.add(value)
        return days

    # ------------------------------------------------------------------ #
    # 下次触发时间（UI 展示）
    # ------------------------------------------------------------------ #
    def next_runs(self, limit: int = 5) -> List[dict]:
        """返回下几次触发信息：[{"rule_id","name","at","in"}]，按时间升序。"""
        current = self._safe_now(None)
        if current is None:
            return []
        entries = []  # type: List[tuple]
        with self._lock:
            for rule in self._safe_rules():
                if not isinstance(rule, dict) or not rule.get("enabled", True):
                    continue
                trigger = rule.get("trigger")
                if not isinstance(trigger, dict):
                    continue
                if str(trigger.get("type") or "").strip().lower() != "schedule":
                    continue
                try:
                    candidate = self._next_at(rule, trigger, current)
                except Exception:  # pragma: no cover - 防御性
                    self.logger.exception("计算规则 %s 下次触发时间失败", rule.get("id"))
                    continue
                if candidate is None:
                    continue
                entries.append(
                    (
                        candidate,
                        {
                            "rule_id": str(rule.get("id") or ""),
                            "name": str(rule.get("name") or ""),
                            "at": iso_now(candidate),
                            "in": max(0, int((candidate - current).total_seconds())),
                        },
                    )
                )
        entries.sort(key=lambda item: item[0])
        try:
            count = max(1, int(limit))
        except (TypeError, ValueError):
            count = 5
        return [item for _, item in entries[:count]]

    def _next_at(self, rule: dict, trigger: dict, current: datetime) -> Optional[datetime]:
        rule_id = str(rule.get("id") or "")
        every = trigger.get("every_minutes")
        at = trigger.get("at")
        if every is not None and (at is None or str(at).strip() == ""):
            try:
                interval = int(every)
            except (TypeError, ValueError):
                return None
            if interval < 1 or interval > 1440:
                return None
            next_epoch = self._every_next.get(rule_id)
            if next_epoch is None:
                return current + timedelta(minutes=interval)
            candidate = datetime.fromtimestamp(next_epoch)
            while candidate <= current:
                candidate += timedelta(minutes=interval)
            return candidate
        if at is None or str(at).strip() == "":
            return None
        at_text = str(at).strip()
        if not _TIME_RE.match(at_text):
            return None
        days = self._normalize_days(trigger.get("days"))
        hour, minute = int(at_text[:2]), int(at_text[3:])
        for offset in range(0, _MAX_DAYS_AHEAD):
            day = (current + timedelta(days=offset)).date()
            if days and day.weekday() not in days:
                continue
            candidate = datetime(day.year, day.month, day.day, hour, minute, 0)
            if candidate > current:
                return candidate
        return None


__all__ = ["Scheduler"]
