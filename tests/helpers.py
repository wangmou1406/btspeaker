"""测试公共工具：临时数据目录、配置构造、假实现、等待助手。

约定：测试不依赖硬件 / 外网 / 第三方包；端口用 free_port() 临时申请。
"""

from __future__ import annotations

import contextlib
import os
import shutil
import socket
import threading
import time
import uuid
import wave
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional

from btspeaker.bus import EventBus
from btspeaker.config import Config
from btspeaker.util import new_id, write_json

SIM_DEVICES: List[Dict[str, Any]] = [
    {
        "address": "AA:BB:CC:11:22:33",
        "name": "客厅小爱音箱",
        "paired": True,
        "connected": True,
        "trusted": True,
        "icon": "audio-card",
    },
    {
        "address": "AA:BB:CC:44:55:66",
        "name": "书房 SoundCore",
        "paired": True,
        "connected": False,
        "trusted": True,
        "icon": "audio-card",
    },
    {
        "address": "AA:BB:CC:77:88:99",
        "name": "便携蓝牙音箱",
        "paired": False,
        "connected": False,
        "trusted": False,
        "icon": "audio-card",
        "discoverable": True,
    },
]


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_LOCAL_TMP = os.path.join(_REPO_ROOT, ".testtmp")


def local_temp_dir(prefix: str = "btspeaker-") -> str:
    """在仓库内 .testtmp 下创建临时目录。

    注意：
    1. 本机沙箱只允许写工作区，系统临时目录不可写；
    2. 不能用 tempfile.mkdtemp —— 它用 0o700 建目录，Windows 下会生成不继承
       沙箱 ACE 的目录导致后续写入被拒绝；这里用普通 os.makedirs 继承权限。
    """
    base = os.path.join(_REPO_ROOT, ".testtmp")
    os.makedirs(base, exist_ok=True)
    for _ in range(100):
        path = os.path.join(base, "%s%s" % (prefix, uuid.uuid4().hex[:10]))
        try:
            os.makedirs(path)
            return path
        except FileExistsError:
            continue
    raise RuntimeError("无法创建测试临时目录: %s" % base)


@contextlib.contextmanager
def temp_data_dir():
    """临时数据目录上下文（工作区内，自动清理）。"""
    path = local_temp_dir("btspeaker-test-")
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def make_config(data_dir: str, overrides: Optional[Dict[str, Any]] = None, sim: bool = False) -> Config:
    """在 data_dir 下构造配置；sim=True 时切到模拟后端。"""
    cfg = Config.load(data_dir=data_dir)
    if sim:
        cfg.update(
            {
                "bluetooth": {"backend": "sim"},
                "player": {"backend": "mock"},
                "tts": {"backend": "mock"},
            }
        )
    if overrides:
        cfg.update(overrides)
    cfg.ensure_dirs()
    cfg.save()
    return cfg


def write_sim_devices(path: str, devices: Optional[List[Dict[str, Any]]] = None) -> str:
    write_json(path, [dict(d) for d in (devices if devices is not None else SIM_DEVICES)])
    return path


def free_port() -> int:
    """申请一个当前空闲的本地端口。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


def wait_until(predicate: Callable[[], Any], timeout: float = 8.0, interval: float = 0.05) -> bool:
    """轮询直到 predicate 返回真值或超时。"""
    deadline = time.time() + float(timeout)
    while time.time() < deadline:
        try:
            if predicate():
                return True
        except Exception:
            pass
        time.sleep(interval)
    return False


class EventRecorder:
    """把事件总线上的事件记录下来，便于断言。"""

    def __init__(self, bus: EventBus) -> None:
        self.bus = bus
        self.events: List[Any] = []
        self._lock = threading.Lock()
        self._token = bus.subscribe("*", self._on_event)

    def _on_event(self, event: str, payload: Dict[str, Any]) -> None:
        with self._lock:
            self.events.append((event, dict(payload or {})))

    def names(self) -> List[str]:
        with self._lock:
            return [item[0] for item in self.events]

    def payloads(self, event: str) -> List[Dict[str, Any]]:
        with self._lock:
            return [payload for name, payload in self.events if name == event]

    def last(self, event: Optional[str] = None):
        with self._lock:
            for name, payload in reversed(self.events):
                if event is None or name == event:
                    return (name, payload)
        return None

    def clear(self) -> None:
        with self._lock:
            self.events.clear()

    def close(self) -> None:
        self.bus.unsubscribe(self._token)


class FakePlayer:
    """内存假播放器：实现 audio.Player 的鸭子接口，不产生真实声音。"""

    def __init__(self, volume: int = 70) -> None:
        self.volume = int(volume)
        self.played: List[Any] = []
        self.stopped = 0
        self._playing = False
        self._lock = threading.Lock()
        self.fail_next = False

    def probe(self) -> Dict[str, Any]:
        return {"ready": True, "backend": "mock", "tools": {"mpg123": True, "aplay": True}, "bluealsa_pcm": True, "hint": ""}

    def ready(self) -> bool:
        return True

    def play(self, req: Any) -> Any:
        with self._lock:
            self.played.append(req)
        if self.fail_next:
            self.fail_next = False
            return SimpleNamespace(ok=False, source=getattr(req, "source", ""), duration=0.0, error="假播放器故意失败", skipped=False, backend="mock")
        self._playing = True
        time.sleep(float(getattr(req, "fake_sleep", 0.0) or 0.0))
        self._playing = False
        return SimpleNamespace(ok=True, source=getattr(req, "source", ""), duration=0.0, error="", skipped=False, backend="mock")

    def stop(self) -> bool:
        with self._lock:
            self.stopped += 1
        self._playing = False
        return True

    def is_playing(self) -> bool:
        return self._playing

    def current(self) -> Optional[Dict[str, Any]]:
        return None

    def get_volume(self) -> int:
        return self.volume

    def set_volume(self, level: int) -> int:
        self.volume = max(0, min(100, int(level)))
        return self.volume

    def resolve_source(self, source: str) -> str:
        return str(source)

    def diag(self) -> Dict[str, Any]:
        return {"tool": "fake-player", "ok": True, "detail": "内存假播放器"}


class FakeTts:
    """假 TTS：生成一个极短的合法 wav 文件。"""

    def __init__(self, data_dir: Optional[str] = None) -> None:
        self.data_dir = data_dir or local_temp_dir("btspeaker-tts-")
        self.texts: List[str] = []

    def probe(self) -> Dict[str, Any]:
        return {"backend": "mock", "ready": True, "hint": ""}

    def synthesize(self, text: str, voice: Optional[str] = None) -> str:
        self.texts.append(str(text))
        tmp_dir = os.path.join(self.data_dir, "tmp")
        os.makedirs(tmp_dir, exist_ok=True)
        path = os.path.join(tmp_dir, "tts_%s.wav" % new_id("t"))
        with wave.open(path, "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(8000)
            handle.writeframes(b"\x00\x00" * 320)  # 0.04 秒静音
        return path

    def cleanup(self) -> int:
        return 0


class FakeBluetooth:
    """内存假蓝牙后端：实现 bluetooth.BluetoothBackend 的鸭子接口。"""

    name = "fake"

    def __init__(self, devices: Optional[List[Dict[str, Any]]] = None) -> None:
        self._lock = threading.Lock()
        self._devices = [dict(d) for d in (devices if devices is not None else SIM_DEVICES)]
        self.powered = True

    def available(self) -> bool:
        return True

    def adapter_info(self) -> Dict[str, Any]:
        return {
            "address": "00:11:22:33:44:55",
            "name": "iStoreOS",
            "powered": self.powered,
            "discovering": False,
            "hci": "hci0",
        }

    def power_on(self) -> bool:
        self.powered = True
        return True

    def list_devices(self, paired_only: bool = False) -> List[Any]:
        with self._lock:
            items = [self._wrap(d) for d in self._devices if d.get("paired") or not paired_only]
        return items

    def device_info(self, address: str) -> Optional[Any]:
        with self._lock:
            for device in self._devices:
                if device["address"].upper() == str(address).upper():
                    return self._wrap(device)
        return None

    def scan(self, timeout: float = 12.0) -> List[Any]:
        with self._lock:
            return [self._wrap(d) for d in self._devices]

    def pair(self, address: str) -> bool:
        return self._mutate(address, {"paired": True, "trusted": True})

    def connect(self, address: str) -> bool:
        with self._lock:
            for device in self._devices:
                device["connected"] = device["address"].upper() == str(address).upper()
        return True

    def disconnect(self, address: str) -> bool:
        return self._mutate(address, {"connected": False})

    def remove(self, address: str) -> bool:
        with self._lock:
            self._devices = [d for d in self._devices if d["address"].upper() != str(address).upper()]
        return True

    def trust(self, address: str) -> bool:
        return self._mutate(address, {"trusted": True})

    def diag(self) -> Dict[str, Any]:
        return {"tool": "fake", "ok": True, "detail": "内存假蓝牙后端"}

    # -- 辅助 ------------------------------------------------------------ #
    def _wrap(self, data: Dict[str, Any]) -> Any:
        from btspeaker.bluetooth import Device

        return Device(
            address=data["address"],
            name=data.get("name", ""),
            paired=bool(data.get("paired")),
            connected=bool(data.get("connected")),
            trusted=bool(data.get("trusted")),
            icon=data.get("icon", ""),
        )

    def _mutate(self, address: str, patch: Dict[str, Any]) -> bool:
        with self._lock:
            for device in self._devices:
                if device["address"].upper() == str(address).upper():
                    device.update(patch)
                    return True
        return False
