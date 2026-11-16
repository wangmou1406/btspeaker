"""配置管理：默认值 + JSON 文件 + 环境变量覆盖。

配置落盘到 $BTSPEAKER_DATA_DIR/config.json（默认 /etc/btspeaker/config.json）。
所有模块通过 Config.get("a.b.c", default) 读取，禁止直接改内部字典。
"""

from __future__ import annotations

import copy
import json
import os
import threading
from typing import Any, Dict, List, Optional

from . import DEFAULT_DATA_DIR, DEFAULT_PORT
from .util import atomic_write_text, get_logger

ENV_DATA_DIR = "BTSPEAKER_DATA_DIR"
ENV_PORT = "BTSPEAKER_PORT"
ENV_HOST = "BTSPEAKER_HOST"
ENV_SIM = "BTSPEAKER_SIM"
ENV_TOKEN = "BTSPEAKER_TOKEN"
ENV_LOG_LEVEL = "BTSPEAKER_LOG_LEVEL"

DEFAULT_CONFIG = {
    "version": 1,
    "server": {
        "host": "0.0.0.0",
        "port": DEFAULT_PORT,
        "title": "蓝音管家",
    },
    "security": {
        "api_token": "",
        "allow_commands": False,
        "command_timeout": 20,
        "command_allowlist": [],
    },
    "bluetooth": {
        "backend": "cli",  # cli | sim
        "cli": "bluetoothctl",
        "adapter": "hci0",
        "poll_interval": 5,
        "scan_timeout": 12,
        "auto_reconnect": True,
        "default_device": "",
        "trust_on_connect": True,
    },
    "player": {
        "backend": "auto",  # auto | bluealsa | mock
        "volume": 70,
        "alsa_device": "bluealsa:DEV={mac},PROFILE=a2dp",
        "mpg123": "mpg123",
        "aplay": "aplay",
        "ffmpeg": "ffmpeg",
        "max_duration": 900,
        "loop_gap": 1,
        "volume_command": "",
    },
    "tts": {
        "backend": "auto",  # auto | http | espeak | mock
        "url": "",
        "method": "GET",
        "headers": {},
        "body_template": "",
        "voice": "zh",
        "speed": 150,
        "espeak": "espeak",
        "timeout": 20,
        "format": "mp3",
    },
    "rules": {
        "enabled": True,
        "max_concurrent": 1,
        "default_cooldown": 0,
    },
    "logging": {
        "level": "INFO",
        "max_records": 500,
    },
    "sounds": {
        "dirs": ["/usr/share/btspeaker/sounds"],
        "max_upload_mb": 20,
    },
    "sim": {
        "devices_file": "",
    },
}


def deep_get(data: Any, path: str, default: Any = None) -> Any:
    """按 "a.b.c" 路径读取嵌套字典。"""
    node = data
    for part in str(path).split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def deep_set(data: Dict[str, Any], path: str, value: Any) -> None:
    """按 "a.b.c" 路径写入嵌套字典（自动建中间层）。"""
    parts = str(path).split(".")
    node = data
    for part in parts[:-1]:
        nxt = node.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            node[part] = nxt
        node = nxt
    node[parts[-1]] = value


def deep_merge(base: Any, patch: Any) -> Any:
    """递归合并字典，patch 优先；列表整体替换。"""
    if not isinstance(patch, dict):
        return copy.deepcopy(patch)
    result = copy.deepcopy(base) if isinstance(base, dict) else {}
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def default_data_dir() -> str:
    env = os.environ.get(ENV_DATA_DIR, "").strip()
    if env:
        return os.path.abspath(os.path.expanduser(env))
    if os.name == "nt":
        # 开发机默认放到当前目录下的 .btsdata，避免污染系统盘
        return os.path.abspath(".btsdata")
    return DEFAULT_DATA_DIR


class Config:
    """线程安全的配置容器。"""

    def __init__(self, path: str, data: Optional[Dict[str, Any]] = None, data_dir: Optional[str] = None) -> None:
        self.path = os.path.abspath(path)
        self.data_dir = os.path.abspath(data_dir or os.path.dirname(self.path) or ".")
        self._lock = threading.RLock()
        self._data = deep_merge(DEFAULT_CONFIG, data or {})
        self._apply_env()

    # ------------------------------------------------------------------ #
    @classmethod
    def load(cls, data_dir: Optional[str] = None, path: Optional[str] = None) -> "Config":
        data_dir = os.path.abspath(data_dir or default_data_dir())
        path = os.path.abspath(path or os.path.join(data_dir, "config.json"))
        raw = None
        try:
            with open(path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except FileNotFoundError:
            raw = None
        except (OSError, ValueError):
            try:
                os.replace(path, path + ".bad")
            except OSError:
                pass
            raw = None
        if not isinstance(raw, dict):
            raw = None
        return cls(path, raw, data_dir=data_dir)

    def _apply_env(self) -> None:
        env_port = os.environ.get(ENV_PORT, "").strip()
        if env_port:
            try:
                self.set("server.port", int(env_port))
            except ValueError:
                pass
        env_host = os.environ.get(ENV_HOST, "").strip()
        if env_host:
            self.set("server.host", env_host)
        env_token = os.environ.get(ENV_TOKEN, "").strip()
        if env_token:
            self.set("security.api_token", env_token)
        env_level = os.environ.get(ENV_LOG_LEVEL, "").strip()
        if env_level:
            self.set("logging.level", env_level)
        if os.environ.get(ENV_SIM, "").strip() not in ("", "0", "false", "no"):
            self.set("bluetooth.backend", "sim")
            self.set("tts.backend", "mock")
            if self.get("player.backend") in (None, "", "auto"):
                self.set("player.backend", "mock")

    # ------------------------------------------------------------------ #
    def is_sim(self) -> bool:
        flag = os.environ.get(ENV_SIM, "").strip().lower()
        if flag not in ("", "0", "false", "no"):
            return True
        return self.get("bluetooth.backend") == "sim" or self.get("player.backend") == "mock"

    def get(self, path: str, default: Any = None) -> Any:
        with self._lock:
            return deep_get(self._data, path, default)

    def set(self, path: str, value: Any) -> None:
        with self._lock:
            deep_set(self._data, path, copy.deepcopy(value))

    def update(self, patch: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        with self._lock:
            self._data = deep_merge(self._data, patch or {})
            return copy.deepcopy(self._data)

    def reset(self) -> Dict[str, Any]:
        with self._lock:
            self._data = copy.deepcopy(DEFAULT_CONFIG)
            return copy.deepcopy(self._data)

    def save(self) -> None:
        with self._lock:
            payload = copy.deepcopy(self._data)
        atomic_write_text(self.path, json.dumps(payload, ensure_ascii=False, indent=2))

    def to_dict(self) -> Dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._data)

    def as_public_dict(self) -> Dict[str, Any]:
        """脱敏后的配置（用于 API 返回）。"""
        data = self.to_dict()
        token = deep_get(data, "security.api_token", "")
        if token:
            deep_set(data, "security.api_token", "******")
        return data

    # ------------------------------------------------------------------ #
    @property
    def port(self) -> int:
        try:
            return int(self.get("server.port", DEFAULT_PORT))
        except (TypeError, ValueError):
            return DEFAULT_PORT

    @property
    def host(self) -> str:
        return str(self.get("server.host", "0.0.0.0"))

    @property
    def state_path(self) -> str:
        return os.path.join(self.data_dir, "state.json")

    @property
    def rules_path(self) -> str:
        return os.path.join(self.data_dir, "rules.json")

    @property
    def log_file(self) -> str:
        return os.path.join(self.data_dir, "btspeaker.log")

    @property
    def web_dir(self) -> str:
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")

    @property
    def sim_devices_path(self) -> str:
        configured = str(self.get("sim.devices_file", "") or "").strip()
        if configured:
            return os.path.abspath(os.path.expanduser(configured))
        return os.path.join(self.data_dir, "sim_devices.json")

    def sounds_dirs(self) -> List[str]:
        dirs: List[str] = []
        configured = self.get("sounds.dirs", []) or []
        if isinstance(configured, str):
            configured = [configured]
        for item in list(configured) + [os.path.join(self.data_dir, "sounds")]:
            path = os.path.abspath(os.path.expanduser(str(item)))
            if path not in dirs:
                dirs.append(path)
        return dirs

    def ensure_dirs(self) -> None:
        for directory in (self.data_dir, os.path.join(self.data_dir, "sounds")):
            try:
                os.makedirs(directory, exist_ok=True)
            except OSError:  # pragma: no cover
                get_logger("config").warning("无法创建目录 %s", directory)
