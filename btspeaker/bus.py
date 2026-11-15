"""进程内事件总线：蓝牙事件、播放事件、规则事件。

publish 为同步分发：处理函数必须快速返回（重活交给 engine 的 worker 队列），
处理函数抛出的异常会被捕获并记录日志，不会影响发布方。
"""

from __future__ import annotations

import threading
import uuid
from typing import Any, Callable, Dict, List, Optional

from .util import get_logger

Handler = Callable[[str, Dict[str, Any]], None]

# ---- 事件名常量（新增事件必须同步更新 docs/CONTRACTS.md）----------------- #
EV_BT_CONNECTED = "bluetooth.connected"
EV_BT_DISCONNECTED = "bluetooth.disconnected"
EV_BT_PAIRED = "bluetooth.paired"
EV_BT_SCAN_DONE = "bluetooth.scan_done"

EV_PLAYER_STARTED = "player.started"
EV_PLAYER_STOPPED = "player.stopped"
EV_PLAYER_FINISHED = "player.finished"
EV_PLAYER_FAILED = "player.failed"

EV_RULE_FIRED = "rule.fired"
EV_RULE_FINISHED = "rule.finished"
EV_RULE_ERROR = "rule.error"

EV_SYSTEM_BOOT = "system.boot"
EV_SYSTEM_SHUTDOWN = "system.shutdown"

ALL_EVENTS = [
    EV_BT_CONNECTED,
    EV_BT_DISCONNECTED,
    EV_BT_PAIRED,
    EV_BT_SCAN_DONE,
    EV_PLAYER_STARTED,
    EV_PLAYER_STOPPED,
    EV_PLAYER_FINISHED,
    EV_PLAYER_FAILED,
    EV_RULE_FIRED,
    EV_RULE_FINISHED,
    EV_RULE_ERROR,
    EV_SYSTEM_BOOT,
    EV_SYSTEM_SHUTDOWN,
]

WILDCARD = "*"


class EventBus:
    """线程安全的事件总线，支持 "*" 通配订阅。"""

    def __init__(self, logger=None) -> None:
        self._lock = threading.RLock()
        self._subs: Dict[str, Dict[str, Handler]] = {}
        self._logger = logger or get_logger("bus")

    def subscribe(self, event: str, handler: Handler) -> str:
        if not callable(handler):
            raise TypeError("handler 必须可调用")
        token = uuid.uuid4().hex
        with self._lock:
            self._subs.setdefault(str(event), {})[token] = handler
        return token

    def unsubscribe(self, token: Optional[str]) -> bool:
        if not token:
            return False
        with self._lock:
            for handlers in self._subs.values():
                if token in handlers:
                    handlers.pop(token, None)
                    return True
        return False

    def publish(self, event: str, payload: Optional[Dict[str, Any]] = None) -> int:
        """同步分发事件，返回成功调用的处理函数数量。"""
        event_name = str(event)
        data = dict(payload or {})
        with self._lock:
            handlers = list(self._subs.get(event_name, {}).values())
            handlers += list(self._subs.get(WILDCARD, {}).values())
        delivered = 0
        for handler in handlers:
            try:
                handler(event_name, data)
                delivered += 1
            except Exception:  # pragma: no cover - 防御性
                self._logger.exception("事件处理失败: %s", event_name)
        return delivered

    def subscriber_count(self, event: Optional[str] = None) -> int:
        with self._lock:
            if event is None:
                return sum(len(v) for v in self._subs.values())
            return len(self._subs.get(str(event), {}))

    def clear(self) -> None:
        with self._lock:
            self._subs.clear()

    def event_names(self) -> List[str]:
        with self._lock:
            return sorted(self._subs.keys())
