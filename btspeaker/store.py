"""持久化与运行时状态。

- JsonStore：线程安全的原子 JSON 文件存储（配置文件、规则文件、状态文件共用）。
- RuntimeState：进程内共享的易失状态（当前连接、正在播放、扫描中……）。

约定：RuntimeState 的 key 使用扁平点号命名（如 "bluetooth.connected"），
值为 JSON 可序列化对象；写入方应整体替换 dict 而不是原地修改，
snapshot() 返回浅拷贝快照。
"""

from __future__ import annotations

import copy
import json
import threading
from typing import Any, Callable, Dict, Optional

from .util import atomic_write_text, get_logger, read_json


class JsonStore:
    """单文件 JSON 存储：load / read / write / update，全部线程安全且原子落盘。"""

    def __init__(self, path: str, default_factory: Optional[Callable[[], Dict[str, Any]]] = None, logger=None) -> None:
        self.path = path
        self._default_factory = default_factory or dict
        self._lock = threading.RLock()
        self._logger = logger or get_logger("store")
        self._data = self._load()

    def _load(self) -> Dict[str, Any]:
        raw = read_json(self.path, None)
        if isinstance(raw, dict):
            return raw
        if raw is not None:
            self._logger.warning("存储文件格式异常，已忽略: %s", self.path)
        try:
            default = self._default_factory()
            return default if isinstance(default, dict) else {}
        except Exception:  # pragma: no cover - 防御性
            return {}

    def read(self) -> Dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._data)

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return copy.deepcopy(self._data.get(key, default))

    def write(self, data: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            self._data = copy.deepcopy(data) if isinstance(data, dict) else {}
            self._save_locked()
            return copy.deepcopy(self._data)

    def update(self, mutator: Callable[[Dict[str, Any]], Any]) -> Dict[str, Any]:
        """在锁内读-改-写；mutator 可返回新 dict 替换，或返回 None 表示原地修改。"""
        with self._lock:
            result = mutator(self._data)
            if isinstance(result, dict):
                self._data = result
            self._save_locked()
            return copy.deepcopy(self._data)

    def set(self, key: str, value: Any) -> Dict[str, Any]:
        def _mutate(data: Dict[str, Any]) -> None:
            data[key] = copy.deepcopy(value)

        return self.update(_mutate)

    def reload(self) -> Dict[str, Any]:
        with self._lock:
            self._data = self._load()
            return copy.deepcopy(self._data)

    def path_for_log(self) -> str:
        return self.path

    def _save_locked(self) -> None:
        try:
            atomic_write_text(self.path, json.dumps(self._data, ensure_ascii=False, indent=2))
        except OSError as exc:  # pragma: no cover - 磁盘满/权限问题
            self._logger.error("保存失败 %s: %s", self.path, exc)


class RuntimeState:
    """进程内易失状态容器（线程安全）。"""

    def __init__(self, initial: Optional[Dict[str, Any]] = None) -> None:
        self._lock = threading.RLock()
        self._data: Dict[str, Any] = dict(initial or {})

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[str(key)] = value

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._data.get(str(key), default)

    def update(self, mapping: Optional[Dict[str, Any]]) -> None:
        if not mapping:
            return
        with self._lock:
            self._data.update(mapping)

    def delete(self, key: str) -> None:
        with self._lock:
            self._data.pop(str(key), None)

    def incr(self, key: str, delta: int = 1) -> int:
        with self._lock:
            value = int(self._data.get(str(key), 0)) + int(delta)
            self._data[str(key)] = value
            return value

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._data)
