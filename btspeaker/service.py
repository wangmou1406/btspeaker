"""服务装配与生命周期：把配置、事件总线、蓝牙、播放器、TTS、规则引擎、HTTP 组装起来。

`App` 可在测试中单独构造（端口传 0 即随机端口），`run_service()` 是命令行 `serve` 的入口。
"""

from __future__ import annotations

import os
import signal
import socket
import threading
import time
from typing import Any, Dict, Optional

from . import APP_TITLE, __version__
from .api import ApiServer
from .audio import Player
from .ai import AIClient
from .bluetooth import BluetoothMonitor, create_backend
from .bus import EV_SYSTEM_BOOT, EV_SYSTEM_SHUTDOWN, EventBus
from .config import Config
from .engine import AutomationEngine
from .rules import RuleStore
from .store import JsonStore, RuntimeState
from .tts import TtsEngine
from .util import get_log_ring, get_logger, hostname, iso_now, read_json, setup_logging

SEED_RULES_FILE = "seed_rules.json"


def load_seed_rules() -> list:
    """读取随包分发的示例规则（全部默认停用，需用户手动启用）。"""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), SEED_RULES_FILE)
    data = read_json(path, None)
    if isinstance(data, dict):
        rules = data.get("rules")
    elif isinstance(data, list):
        rules = data
    else:
        rules = None
    return [rule for rule in (rules or []) if isinstance(rule, dict)]


def seed_data_dir(cfg: Config) -> Dict[str, Any]:
    """首次运行时初始化数据目录：config.json 与示例 rules.json（已存在则跳过）。"""
    cfg.ensure_dirs()
    created_config = False
    if not os.path.isfile(cfg.path):
        cfg.save()
        created_config = True
    created_rules = 0
    store = RuleStore(cfg.rules_path)
    if store.counts().get("total", 0) == 0:
        seeds = load_seed_rules()
        if seeds:
            store.replace_all(seeds)
            created_rules = len(seeds)
    return {
        "config_created": created_config,
        "rules_created": created_rules,
        "rules_total": int(store.counts().get("total", 0)),
        "config_path": cfg.path,
        "rules_path": cfg.rules_path,
        "data_dir": cfg.data_dir,
    }


def lan_ip() -> str:
    """尽力猜测本机局域网 IP（不产生实际流量）。"""
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        return str(sock.getsockname()[0])
    except OSError:
        return "127.0.0.1"
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


class App:
    """应用上下文：持有全部组件，供 API 与测试使用。"""

    def __init__(self, data_dir: Optional[str] = None, host: Optional[str] = None, port: Optional[int] = None) -> None:
        self.cfg = Config.load(data_dir=data_dir)
        if host:
            self.cfg.set("server.host", str(host))
        if port is not None:
            self.cfg.set("server.port", int(port))
        self.cfg.ensure_dirs()
        self.cfg.save()

        setup_logging(
            level=str(self.cfg.get("logging.level", "INFO") or "INFO"),
            log_file=self.cfg.log_file,
            capacity=int(self.cfg.get("logging.max_records", 500) or 500),
        )
        self.logger = get_logger("service")
        self.logs = get_log_ring()
        self.version = __version__
        self.started_at = iso_now()
        self.started_at_ts = time.time()
        self._stop_event = threading.Event()
        self._stopped = False

        self.seed_info = seed_data_dir(self.cfg)

        self.bus = EventBus()
        self.state = RuntimeState(
            {
                "hostname": hostname(),
                "started_at": self.started_at,
                "pid": os.getpid(),
                "bluetooth.available": False,
                "bluetooth.powered": False,
                "bluetooth.adapter": {},
                "bluetooth.connected": None,
                "bluetooth.devices": [],
                "bluetooth.scanning": False,
                "bluetooth.discovered": [],
                "bluetooth.scan_error": "",
                "player.playing": False,
                "player.current": None,
                "player.volume": int(self.cfg.get("player.volume", 70) or 70),
                "last_event": None,
            }
        )
        self.store = JsonStore(
            self.cfg.state_path,
            default_factory=lambda: {"last_runs": {}, "counters": {}},
        )
        self.bt = create_backend(self.cfg)
        self.player = Player(self.cfg, self.bus)
        self.player.set_address_provider(self._current_bt_address)
        self.tts = TtsEngine(self.cfg)
        self.ai = AIClient(self.cfg)
        self.rules = RuleStore(self.cfg.rules_path)
        self.engine = AutomationEngine(
            self.cfg,
            self.bus,
            self.player,
            self.tts,
            self.bt,
            self.rules,
            self.state,
            persist=self.store,
            ai=self.ai,
        )
        self.monitor = BluetoothMonitor(self.cfg, self.bt, self.bus, self.state)
        self.api = ApiServer(self)
        self.bus.subscribe("*", self._remember_event)
        self.logger.info(
            "已装配：后端=%s 播放器=%s 规则=%d 条",
            getattr(self.bt, "name", "?"),
            self.cfg.get("player.backend", "auto"),
            int(self.rules.counts().get("total", 0)),
        )

    # ------------------------------------------------------------------ #
    def _current_bt_address(self) -> str:
        """当前已连接的蓝牙设备地址（供播放器在 address=None 时兜底）。"""
        try:
            devices = self.bt.list_devices()
        except Exception:  # pragma: no cover - 防御性
            return ""
        for dev in devices:
            if getattr(dev, "connected", False):
                return str(getattr(dev, "address", "") or "")
        return ""

    # ------------------------------------------------------------------ #
    def _remember_event(self, event: str, payload: Dict[str, Any]) -> None:
        try:
            self.state.set("last_event", {"event": event, "at": iso_now(), "payload": payload})
        except Exception:  # pragma: no cover - 防御性
            pass

    # ------------------------------------------------------------------ #
    def start(self) -> None:
        self.logger.info("=" * 64)
        self.logger.info(
            "%s v%s 启动中（数据目录 %s，模拟模式 %s）",
            APP_TITLE,
            __version__,
            self.cfg.data_dir,
            "是" if self.cfg.is_sim() else "否",
        )
        self.state.update({"started_at": self.started_at, "pid": os.getpid()})
        try:
            self.player.set_volume(int(self.cfg.get("player.volume", 70) or 70))
        except Exception:
            self.logger.debug("设置初始音量失败", exc_info=True)
        self.engine.start()
        # 先同步建立一次蓝牙基线，保证首屏 /api/status 就有适配器/设备数据
        try:
            self.monitor.poll_once(publish=False)
            connected = self._current_bt_address()
            if connected:
                self.player.note_connected_address(connected)
                self.logger.info("启动时已有蓝牙设备连接：%s，已同步给播放器", connected)
        except Exception:
            self.logger.debug("首次蓝牙轮询失败", exc_info=True)
        self.monitor.start()
        self.api.start()
        self.bus.publish(EV_SYSTEM_BOOT, {"at": self.started_at, "sim": self.cfg.is_sim()})
        self.logger.info("Web 界面: http://%s:%s/", lan_ip(), self.api.port)
        self.logger.info("本机访问: http://127.0.0.1:%s/", self.api.port)
        self.logger.info("=" * 64)

    def request_stop(self) -> None:
        self._stop_event.set()

    def wait(self) -> None:
        while not self._stop_event.is_set():
            self._stop_event.wait(0.5)

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        self.logger.info("正在停止服务…")
        try:
            self.bus.publish(EV_SYSTEM_SHUTDOWN, {"at": iso_now()})
        except Exception:  # pragma: no cover
            pass
        for name, func in (
            ("HTTP 服务", self.api.stop),
            ("蓝牙监视", self.monitor.stop),
            ("自动化引擎", self.engine.stop),
        ):
            try:
                func()
            except Exception:  # pragma: no cover
                self.logger.exception("%s 停止失败", name)
        try:
            self.player.stop()
        except Exception:  # pragma: no cover
            pass
        close_player = getattr(self.player, "close", None)
        if callable(close_player):
            try:
                close_player()
            except Exception:  # pragma: no cover
                pass
        self.logger.info("服务已停止")


def run_service(
    data_dir: Optional[str] = None,
    host: Optional[str] = None,
    port: Optional[int] = None,
) -> int:
    """前台运行服务（procd / systemd 守护的入口）。"""
    app = App(data_dir=data_dir, host=host, port=port)
    previous_handlers = []

    def _handle_signal(signum, frame):  # noqa: ANN001, ARG001
        get_logger("service").info("收到信号 %s，准备退出", signum)
        app.request_stop()

    for name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            previous_handlers.append((sig, signal.signal(sig, _handle_signal)))
        except (ValueError, OSError):  # pragma: no cover - 非主线程/平台限制
            pass

    try:
        app.start()
        app.wait()
    except OSError as exc:
        get_logger("service").error("启动失败: %s", exc)
        return 2
    except KeyboardInterrupt:  # pragma: no cover
        app.request_stop()
    finally:
        app.stop()
        for sig, previous in previous_handlers:
            try:
                signal.signal(sig, previous)
            except (ValueError, OSError):  # pragma: no cover
                pass
    return 0
