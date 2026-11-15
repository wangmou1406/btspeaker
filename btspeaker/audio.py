"""音频播放：mpg123 / aplay / ffmpeg 输出到 BlueALSA，以及模拟播放后端。

- ``play()`` 阻塞播完，内部串行锁保证同一时间只有一路播放；
- ``stop()`` 置停止事件并终止子进程，被打断的播放返回 ``skipped=True``；
- ``mock`` 后端（模拟模式）不发声、可被打断，用于开发机与测试；
- 失败一律转换为可操作的中文错误信息。
"""

from __future__ import annotations

import array
import os
import shlex
import subprocess
import threading
import time
import urllib.error
import urllib.request
import wave
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .bluetooth import normalize_address
from .bus import (
    EV_BT_CONNECTED,
    EV_BT_DISCONNECTED,
    EV_PLAYER_FAILED,
    EV_PLAYER_FINISHED,
    EV_PLAYER_STARTED,
    EV_PLAYER_STOPPED,
)
from .util import get_logger, iso_now, new_id, run_cmd, which

MAX_MOCK_SECONDS = 2.5  # mock 后端单次播放等待上限（秒）
_DOWNLOAD_CHUNK = 64 * 1024
_DOWNLOAD_TIMEOUT = 30.0
_WATCHDOG_POLL = 0.05

_MP3_EXTS = (".mp3", ".mp2", ".mpeg")
_WAV_EXTS = (".wav", ".wave")
_AUDIO_EXTS = (
    ".mp3", ".mp2", ".mpeg", ".wav", ".wave", ".ogg", ".oga",
    ".flac", ".m4a", ".aac", ".opus", ".wma", ".ape",
)
_INSTALL_HINTS = {
    "mpg123": "mpg123 未安装：opkg install mpg123",
    "aplay": "aplay 未安装：opkg install alsa-utils",
    "ffmpeg": "ffmpeg 未安装：opkg install ffmpeg",
}


class AudioError(Exception):
    """音频相关失败（面向用户的信息为中文，并给出可操作建议）。"""


@dataclass
class PlayRequest:
    """一次播放请求。"""

    source: str
    label: str = ""
    volume: Optional[int] = None
    loop: bool = False
    address: Optional[str] = None
    rule_id: Optional[str] = None


@dataclass
class PlayResult:
    """一次播放的结果。"""

    ok: bool
    source: str = ""
    backend: str = ""
    duration: float = 0.0
    error: str = ""
    skipped: bool = False


class Player:
    """音频播放器（串行播放，可在任意线程调用）。"""

    def __init__(self, cfg, bus, logger=None) -> None:
        self.cfg = cfg
        self.bus = bus
        self.logger = logger or get_logger("audio")
        self._play_lock = threading.Lock()  # 串行播放锁
        self._state_lock = threading.RLock()  # 保护当前状态与子进程表
        self._stop_event = threading.Event()
        self._playing = False
        self._current: Optional[Dict[str, Any]] = None
        self._started_ts = 0.0
        self._procs: List[Any] = []
        self._volume = _clamp_volume(cfg.get("player.volume", 70))
        # 记录「当前已连接音箱」，作为 address=None 时的默认目标
        self._bt_address = ""
        self._bus_tokens: List[str] = []
        if bus is not None:
            try:
                self._bus_tokens.append(
                    bus.subscribe(EV_BT_CONNECTED, self._on_bluetooth_event)
                )
                self._bus_tokens.append(
                    bus.subscribe(EV_BT_DISCONNECTED, self._on_bluetooth_event)
                )
            except Exception:  # pragma: no cover - 防御性
                self.logger.warning("订阅蓝牙事件失败，默认设备将只依赖配置")

    def close(self) -> None:
        """释放事件订阅（服务退出时调用）。"""
        if self.bus is not None:
            for token in list(self._bus_tokens):
                try:
                    self.bus.unsubscribe(token)
                except Exception:  # pragma: no cover
                    pass
        self._bus_tokens = []

    def _on_bluetooth_event(self, event: str, payload: Dict[str, Any]) -> None:
        device = payload.get("device") if isinstance(payload, dict) else None
        address = normalize_address(payload.get("address", "")) if isinstance(payload, dict) else ""
        if not address and isinstance(device, dict):
            address = normalize_address(device.get("address", ""))
        if not address:
            return
        with self._state_lock:
            if event == EV_BT_CONNECTED:
                self._bt_address = address
            elif event == EV_BT_DISCONNECTED and self._bt_address == address:
                self._bt_address = ""

    # -- 工具探测 ------------------------------------------------------ #
    def _is_mock(self) -> bool:
        backend = str(self.cfg.get("player.backend", "auto") or "auto").strip().lower()
        if backend == "mock":
            return True
        try:
            return bool(self.cfg.is_sim())
        except Exception:  # pragma: no cover
            return False

    def _tool_path(self, name: str) -> str:
        tool = str(self.cfg.get("player.%s" % name, name) or name).strip() or name
        found = which(tool)
        if found:
            return found
        if os.path.isabs(tool) and os.path.isfile(tool):
            return tool
        return ""

    def _tool_argv(self, name: str) -> str:
        return self._tool_path(name) or str(self.cfg.get("player.%s" % name, name) or name)

    def _detect_bluealsa_pcm(self) -> bool:
        aplay = self._tool_path("aplay")
        if not aplay:
            return False
        try:
            result = run_cmd([aplay, "-L"], timeout=5.0)
        except Exception:  # pragma: no cover
            return False
        return "bluealsa" in ((result.stdout or "") + "\n" + (result.stderr or "")).lower()

    def probe(self) -> Dict[str, Any]:
        """检测播放后端是否就绪，返回工具状态与中文提示。"""
        tools = {
            "mpg123": bool(self._tool_path("mpg123")),
            "aplay": bool(self._tool_path("aplay")),
            "ffmpeg": bool(self._tool_path("ffmpeg")),
        }
        if self._is_mock():
            return {"ready": True, "backend": "mock", "tools": tools,
                    "bluealsa_pcm": False, "hint": ""}
        bluealsa_pcm = self._detect_bluealsa_pcm()
        ready = bool(bluealsa_pcm and tools["aplay"])
        hints: List[str] = []
        if not tools["aplay"]:
            hints.append(_INSTALL_HINTS["aplay"])
        if not bluealsa_pcm:
            hints.append(
                "未检测到 bluealsa 音频设备：opkg install bluealsa，"
                "并在 /etc/asound.conf 配置 bluealsa 插件"
            )
        if not tools["mpg123"]:
            hints.append(_INSTALL_HINTS["mpg123"])
        if not ready and not hints:  # pragma: no cover - 理论上不会走到
            hints.append("音频后端未就绪：请检查 bluealsa 与 ALSA 配置")
        return {"ready": ready, "backend": "bluealsa" if ready else "none",
                "tools": tools, "bluealsa_pcm": bluealsa_pcm,
                "hint": "" if ready else "；".join(hints)}

    def ready(self) -> bool:
        return bool(self.probe().get("ready"))

    def diag(self) -> Dict[str, Any]:
        probe = self.probe()
        hint = str(probe.get("hint") or "")
        detail = "后端 %s，音量 %d" % (probe.get("backend"), self.get_volume())
        if hint:
            detail += "（%s）" % hint
        return {"tool": "audio", "ok": bool(probe.get("ready")), "detail": detail,
                "backend": probe.get("backend"), "tools": probe.get("tools"),
                "bluealsa_pcm": probe.get("bluealsa_pcm"), "hint": hint,
                "volume": self.get_volume(), "playing": self.is_playing()}

    # -- 播放源解析 ---------------------------------------------------- #
    def resolve_source(self, source: str) -> str:
        """解析播放源：本地路径 / 声音库相对名 / http(s) URL。"""
        text = str(source or "").strip()
        if not text:
            raise AudioError("播放源为空：请指定音频文件、声音库名称或 http(s) 链接")
        lowered = text.lower()
        is_url = lowered.startswith("http://") or lowered.startswith("https://")
        if is_url and self._is_mock():
            return "mock://" + text  # 模拟模式不联网
        if is_url:
            return self._download(text)
        if "://" in text:
            raise AudioError(
                "不支持的播放源协议：%s（只支持本地文件、声音库名称或 http(s) 链接）" % text
            )
        expanded = os.path.abspath(os.path.expanduser(text))
        if os.path.isfile(expanded):
            return expanded
        dirs = list(self.cfg.sounds_dirs())
        for directory in dirs:
            base = os.path.abspath(os.path.expanduser(str(directory)))
            candidate = os.path.abspath(os.path.join(base, text))
            try:
                inside = os.path.commonpath([candidate, base]) == base
            except ValueError:  # pragma: no cover - 跨盘符等极端情况
                inside = False
            if inside and os.path.isfile(candidate):
                return candidate
        raise AudioError(
            "找不到音频文件：%s（请确认路径正确，或把文件放入声音库目录：%s）"
            % (text, "、".join(dirs) if dirs else "未配置")
        )

    def _download(self, url: str) -> str:
        """下载 http(s) 音频到 data_dir/tmp/，并限制大小。"""
        tmp_dir = os.path.join(self.cfg.data_dir, "tmp")
        try:
            os.makedirs(tmp_dir, exist_ok=True)
        except OSError as exc:
            raise AudioError("无法创建临时目录 %s：%s" % (tmp_dir, exc))
        max_mb = max(1, _safe_int(self.cfg.get("sounds.max_upload_mb", 20), 20))
        max_bytes = max_mb * 1024 * 1024
        dest = os.path.join(tmp_dir, "download_%s%s" % (new_id("a"), _url_suffix(url)))
        request = urllib.request.Request(
            url, headers={"User-Agent": "BTSpeaker/1.0", "Accept": "audio/*,*/*"}
        )
        written = 0
        try:
            with urllib.request.urlopen(request, timeout=_DOWNLOAD_TIMEOUT) as response:
                with open(dest, "wb") as handle:
                    while True:
                        chunk = response.read(_DOWNLOAD_CHUNK)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > max_bytes:
                            raise AudioError(
                                "音频文件超过大小限制（%d MB）：%s" % (max_mb, url)
                            )
                        handle.write(chunk)
        except AudioError:
            _remove_file(dest)
            raise
        except (urllib.error.URLError, OSError, ValueError) as exc:
            _remove_file(dest)
            raise AudioError(
                "下载音频失败：%s（请检查链接是否可访问、网络是否正常）" % exc
            )
        if written <= 0:
            _remove_file(dest)
            raise AudioError("下载的音频文件为空：%s" % url)
        self.logger.info("已下载音频：%s -> %s（%d 字节）", url, dest, written)
        return dest

    # -- 音量 ---------------------------------------------------------- #
    def get_volume(self) -> int:
        with self._state_lock:
            return self._volume

    def set_volume(self, level: int) -> int:
        """设置音量（0-100）并持久化到配置，返回生效值。"""
        value = _clamp_volume(level)
        with self._state_lock:
            self._volume = value
        try:
            self.cfg.set("player.volume", value)
            self.cfg.save()
        except Exception as exc:  # pragma: no cover - 磁盘/权限问题
            self.logger.warning("保存音量配置失败：%s", exc)
        self.logger.info("音量已调整为 %d", value)
        return value

    # -- 播放控制 ------------------------------------------------------ #
    def is_playing(self) -> bool:
        with self._state_lock:
            return self._playing

    def current(self) -> Optional[Dict[str, Any]]:
        """返回当前播放信息；未在播放时返回 None。"""
        with self._state_lock:
            if not self._playing or not self._current:
                return None
            data = dict(self._current)
            started = self._started_ts
        if started > 0:
            data["elapsed"] = round(max(0.0, time.time() - started), 1)
        return data

    def stop(self) -> bool:
        """请求停止当前播放，返回是否确实在播放。"""
        with self._state_lock:
            was_playing = self._playing
            procs = list(self._procs)
        self._stop_event.set()
        for proc in procs:
            _terminate_proc(proc)
        if was_playing:
            self.logger.info("已请求停止播放")
        return bool(was_playing)

    def play(self, req: PlayRequest) -> PlayResult:
        """播放音频，阻塞直到播完 / 被 stop() 打断 / 失败。"""
        if req is None:
            raise AudioError("播放请求不能为空")
        source = str(req.source or "").strip()
        if not source:
            return self._fail_result(req, "播放源为空：请指定音频文件、声音库名称或 http(s) 链接")
        probe = self.probe()
        backend = str(probe.get("backend") or "none")
        if backend == "none" or not probe.get("ready"):
            return self._fail_result(
                req, str(probe.get("hint") or "未检测到可用的音频后端"), backend
            )
        try:
            resolved = self.resolve_source(source)
        except AudioError as exc:
            return self._fail_result(req, str(exc), backend)
        volume = self._resolve_volume(req.volume)
        address = self._pick_address(req.address)
        if backend != "mock" and not address:
            return self._fail_result(
                req, "未连接蓝牙音箱：请先在设备页连接音箱后再播放", backend
            )
        pcm = self._render_alsa_device(address)
        started = time.time()
        with self._play_lock:
            self._stop_event.clear()
            if backend != "mock":
                self._apply_volume_command(volume, address)
            self._begin(req, resolved, address, volume, backend)
            max_duration = self._max_duration()
            deadline = (time.time() + max_duration) if max_duration > 0 else None
            skipped = False
            error = ""
            try:
                total = 0.0
                while True:
                    if backend == "mock":
                        outcome = self._play_mock(resolved)
                    else:
                        outcome = self._play_audio(resolved, volume, pcm, deadline)
                    if outcome.get("skipped"):
                        skipped = True
                        break
                    if not outcome.get("ok"):
                        error = str(outcome.get("error") or "播放失败")
                        break
                    total += float(outcome.get("duration") or 0.0)
                    if not req.loop:
                        break
                    if deadline is not None and time.time() >= deadline:
                        error = ("播放超过最大时长限制（%s 秒），已自动停止"
                                 % self._max_duration_text())
                        break
                    gap = self._loop_gap()
                    if gap > 0 and self._stop_event.wait(gap):
                        skipped = True
                        break
            finally:
                self._finish_state()
        duration = round(max(0.0, time.time() - started), 2)
        if skipped:
            self._publish(EV_PLAYER_STOPPED, {"source": req.source, "reason": "用户停止"})
            self.logger.info("播放已停止：%s", req.source)
            return PlayResult(ok=False, source=resolved, backend=backend,
                              duration=duration, skipped=True)
        if error:
            self._publish(EV_PLAYER_FAILED, {"source": req.source, "error": error})
            self.logger.warning("播放失败：%s（%s）", req.source, error)
            return PlayResult(ok=False, source=resolved, backend=backend,
                              duration=duration, error=error)
        self._publish(EV_PLAYER_FINISHED,
                      {"source": req.source, "duration": duration, "reason": "completed"})
        self.logger.info("播放完成：%s（%.2f 秒）", req.source, duration)
        return PlayResult(ok=True, source=resolved, backend=backend, duration=duration)

    def _begin(self, req: PlayRequest, resolved: str, address: str,
               volume: int, backend: str) -> None:
        with self._state_lock:
            self._playing = True
            self._started_ts = time.time()
            self._current = {"playing": True, "source": req.source,
                             "label": req.label or "", "address": address,
                             "volume": volume, "backend": backend,
                             "started_at": iso_now(), "elapsed": 0}
        self.logger.info("开始播放：%s（音量 %d，设备 %s，后端 %s）",
                         req.source, volume, address or "-", backend)
        self._publish(EV_PLAYER_STARTED, {
            "source": req.source, "label": req.label or "", "address": address,
            "rule_id": req.rule_id, "backend": backend, "volume": volume,
        })

    def _finish_state(self) -> None:
        with self._state_lock:
            self._playing = False
            self._current = None
            self._started_ts = 0.0
            self._procs = []

    def _publish(self, event: str, payload: Dict[str, Any]) -> None:
        if self.bus is None:
            return
        try:
            self.bus.publish(event, payload)
        except Exception:  # pragma: no cover - 事件总线自身已兜底
            self.logger.exception("发布播放事件失败：%s", event)

    def _fail_result(self, req: PlayRequest, error: str, backend: str = "") -> PlayResult:
        source = str(getattr(req, "source", "") or "")
        self.logger.warning("播放不可用：%s（源 %s）", error, source)
        self._publish(EV_PLAYER_FAILED, {"source": source, "error": error})
        return PlayResult(ok=False, source=source, backend=backend, error=error)

    # -- 音量 / 地址 / 设备串 ------------------------------------------ #
    def _resolve_volume(self, volume: Optional[int]) -> int:
        return _clamp_volume(self._volume) if volume is None else _clamp_volume(volume)

    def _pick_address(self, requested: Optional[str]) -> str:
        value = normalize_address(requested)
        if value:
            return value
        with self._state_lock:
            tracked = self._bt_address
        return tracked or normalize_address(self.cfg.get("bluetooth.default_device", ""))

    def _hci(self) -> str:
        return str(self.cfg.get("bluetooth.adapter", "hci0") or "hci0").strip() or "hci0"

    def _render_alsa_device(self, address: str) -> str:
        """渲染 bluealsa PCM 设备串：{mac} 用大写 MAC，{hci} 用适配器名。"""
        template = str(self.cfg.get("player.alsa_device", "")
                       or "bluealsa:HCI={hci},DEV={mac},PROFILE=a2dp")
        mac = normalize_address(address).upper() if address else ""
        # 用 str.replace 而非 format：避免模板里蓝色 ALSA 参数中的花括号冲突
        return template.replace("{mac}", mac).replace("{hci}", self._hci())

    def _apply_volume_command(self, volume: int, address: str) -> None:
        """执行硬件音量命令模板（{level} / {mac} / {hci}）。"""
        template = str(self.cfg.get("player.volume_command", "") or "").strip()
        if not template:
            return
        text = template.replace("{level}", str(int(volume)))
        text = text.replace("{mac}", normalize_address(address).upper())
        text = text.replace("{hci}", self._hci())
        try:
            argv = shlex.split(text)
        except ValueError as exc:
            self.logger.warning("音量命令模板无法解析：%s（%s）", template, exc)
            return
        if not argv:
            return
        try:
            result = run_cmd(argv, timeout=10.0)
        except Exception as exc:  # pragma: no cover
            self.logger.warning("执行硬件音量命令异常：%s", exc)
            return
        if not result.ok:
            self.logger.warning("设置硬件音量失败（退出码 %s）：%s",
                                result.returncode, _clip(result.text()))
        else:
            self.logger.info("已通过 volume_command 设置音量 %d", volume)

    def _max_duration(self) -> float:
        try:
            value = float(self.cfg.get("player.max_duration", 900) or 0)
        except (TypeError, ValueError):
            value = 900.0
        return max(0.0, value)

    def _max_duration_text(self) -> str:
        return "%d" % int(round(self._max_duration()))

    def _loop_gap(self) -> float:
        try:
            value = float(self.cfg.get("player.loop_gap", 1) or 0)
        except (TypeError, ValueError):
            value = 1.0
        return max(0.0, value)

    # -- mock 播放 ----------------------------------------------------- #
    def _estimate_duration(self, path: str) -> float:
        """估算音频时长（秒），用于 mock 后端决定等待多久。"""
        if not path or str(path).startswith("mock://"):
            return 3.0
        if _detect_format(path) == "wav":
            try:
                with wave.open(path, "rb") as handle:
                    rate = int(handle.getframerate() or 0)
                    frames = int(handle.getnframes() or 0)
                if rate > 0 and frames > 0:
                    return float(frames) / float(rate)
            except (wave.Error, OSError, ValueError):
                pass
        try:
            size = os.path.getsize(path)
        except OSError:
            return 3.0
        return float(size) / 16000.0 if size > 0 else 3.0  # 按 128kbps 粗估

    def _play_mock(self, resolved: str) -> Dict[str, Any]:
        estimate = self._estimate_duration(resolved)
        wait = min(MAX_MOCK_SECONDS, estimate) if estimate > 0 else MAX_MOCK_SECONDS
        wait = max(0.0, wait)
        self.logger.debug("模拟播放 %.2f 秒：%s", wait, resolved)
        if wait > 0 and self._stop_event.wait(wait):
            return {"ok": False, "skipped": True, "error": "", "duration": 0.0}
        if self._stop_event.is_set():
            return {"ok": False, "skipped": True, "error": "", "duration": 0.0}
        return {"ok": True, "skipped": False, "error": "", "duration": wait}

    # -- 真实播放（bluealsa） ------------------------------------------ #
    def _play_audio(self, resolved: str, volume: int, pcm: str,
                    deadline: Optional[float]) -> Dict[str, Any]:
        fmt = _detect_format(resolved)
        try:
            if fmt == "mp3":
                return self._play_mpg123(resolved, volume, pcm, deadline)
            if fmt == "wav":
                return self._play_wav(resolved, volume, pcm, deadline)
            return self._play_ffmpeg(resolved, volume, pcm, deadline)
        except AudioError as exc:
            return {"ok": False, "skipped": False, "error": str(exc), "duration": 0.0}

    @staticmethod
    def _mpg123_scale(volume: int) -> int:
        """音量 0-100 映射到 mpg123 的 -f 缩放因子（32768 = 原始音量）。"""
        return max(0, min(32768, int(round(32768.0 * _clamp_volume(volume) / 100.0))))

    def _mpg123_argv(self, path: str, pcm: str, volume: int) -> List[str]:
        argv = [self._tool_argv("mpg123"), "-q", "-o", "alsa", "-a", pcm]
        if _clamp_volume(volume) < 100:  # volume=100 时省略 -f，保持原始音量
            argv += ["-f", str(self._mpg123_scale(volume))]
        argv.append(path)
        return argv

    def _play_mpg123(self, path: str, volume: int, pcm: str,
                     deadline: Optional[float]) -> Dict[str, Any]:
        return self._play_with_tool(
            self._mpg123_argv(path, pcm, volume), "mpg123", deadline, pcm
        )

    def _play_wav(self, path: str, volume: int, pcm: str,
                  deadline: Optional[float]) -> Dict[str, Any]:
        gain_path = ""
        target = path
        if _clamp_volume(volume) < 100:
            gain_path = self._make_gain_wav(path, volume)
            if gain_path:
                target = gain_path
            else:
                self.logger.warning("wav 软增益不可用（仅支持 16bit），按原音量播放：%s", path)
        try:
            argv = [self._tool_argv("aplay"), "-q", "-D", pcm, target]
            return self._play_with_tool(argv, "aplay", deadline, pcm)
        finally:
            if gain_path:
                _remove_file(gain_path)

    def _make_gain_wav(self, path: str, volume: int) -> str:
        """用 wave + array 给 16bit wav 做软增益，返回临时文件路径（失败返回空串）。"""
        target = ""
        try:
            factor = max(0.0, min(1.0, float(_clamp_volume(volume)) / 100.0))
            with wave.open(path, "rb") as source:
                channels = source.getnchannels()
                width = source.getsampwidth()
                rate = source.getframerate()
                total = source.getnframes()
                if width != 2 or total <= 0:
                    return ""
                tmp_dir = os.path.join(self.cfg.data_dir, "tmp")
                os.makedirs(tmp_dir, exist_ok=True)
                target = os.path.join(tmp_dir, "gain_%s.wav" % new_id("g"))
                with wave.open(target, "wb") as dest:
                    dest.setnchannels(channels)
                    dest.setsampwidth(width)
                    dest.setframerate(rate)
                    remaining = total
                    while remaining > 0:
                        count = min(65536, remaining)
                        frames = source.readframes(count)
                        if not frames:
                            break
                        samples = array.array("h")
                        samples.frombytes(frames)
                        if factor != 1.0:
                            for index in range(len(samples)):
                                samples[index] = int(samples[index] * factor)
                        dest.writeframes(samples.tobytes())
                        remaining -= count
            return target
        except (wave.Error, OSError, ValueError) as exc:
            self.logger.warning("生成 wav 软增益文件失败：%s", exc)
            if target:
                _remove_file(target)
            return ""

    def _play_ffmpeg(self, path: str, volume: int, pcm: str,
                     deadline: Optional[float]) -> Dict[str, Any]:
        ffmpeg = self._tool_path("ffmpeg")
        aplay = self._tool_path("aplay")
        if not ffmpeg:
            raise AudioError(_INSTALL_HINTS["ffmpeg"])
        if not aplay:
            raise AudioError(_INSTALL_HINTS["aplay"])
        ff_argv = [ffmpeg, "-v", "error", "-nostdin", "-i", path,
                   "-f", "wav", "-acodec", "pcm_s16le"]
        level = _clamp_volume(volume)
        if level < 100:
            ff_argv += ["-af", "volume=%.3f" % (float(level) / 100.0)]
        ff_argv.append("-")
        ap_argv = [aplay, "-q", "-D", pcm]
        ff_proc = None
        ap_proc = None
        started = time.time()
        try:
            try:
                ff_proc = subprocess.Popen(
                    ff_argv, stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )
                ap_proc = subprocess.Popen(
                    ap_argv, stdin=ff_proc.stdout,
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                )
            except OSError as exc:
                raise AudioError(
                    "ffmpeg 播放管道启动失败：%s（请确认 ffmpeg 与 alsa-utils 已安装）" % exc
                )
            try:
                if ff_proc.stdout is not None:
                    ff_proc.stdout.close()
            except (OSError, ValueError):  # pragma: no cover
                pass
            self._register_proc(ff_proc)
            self._register_proc(ap_proc)
            reason = self._wait_procs([ff_proc, ap_proc], deadline)
            duration = max(0.0, time.time() - started)
            if reason == "stopped":
                return {"ok": False, "skipped": True, "error": "", "duration": duration}
            if reason == "timeout":
                return {"ok": False, "skipped": False, "duration": duration,
                        "error": "播放超过最大时长限制（%s 秒），已自动停止"
                                 % self._max_duration_text()}
            ff_err = _read_stderr(ff_proc)
            ap_err = _read_stderr(ap_proc)
            self._close_proc(ff_proc)
            self._close_proc(ap_proc)
            if ff_proc.returncode not in (0, None):
                return {"ok": False, "skipped": False, "duration": duration,
                        "error": "ffmpeg 解码失败（退出码 %s）：%s"
                                 % (ff_proc.returncode, _clip(ff_err) or "音频格式无法识别")}
            if ap_proc.returncode not in (0, None):
                return {"ok": False, "skipped": False, "duration": duration,
                        "error": self._play_error("aplay", ap_proc.returncode, ap_err, pcm)}
            return {"ok": True, "skipped": False, "error": "", "duration": duration}
        finally:
            if ff_proc is not None:
                self._unregister_proc(ff_proc)
                _terminate_proc(ff_proc)
            if ap_proc is not None:
                self._unregister_proc(ap_proc)
                _terminate_proc(ap_proc)

    def _play_with_tool(self, argv: List[str], tool_name: str,
                        deadline: Optional[float], pcm: str) -> Dict[str, Any]:
        tool_path = self._tool_path(tool_name)
        if not tool_path:
            raise AudioError(_INSTALL_HINTS.get(
                tool_name, "%s 未安装：请通过 opkg 安装" % tool_name))
        full_argv = list(argv)
        full_argv[0] = tool_path
        try:
            proc = subprocess.Popen(
                full_argv, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            )
        except OSError as exc:
            raise AudioError("%s 启动失败：%s（%s）" % (
                tool_name, exc, _INSTALL_HINTS.get(tool_name, "请通过 opkg 安装")))
        self._register_proc(proc)
        started = time.time()
        try:
            reason = self._wait_procs([proc], deadline)
            duration = max(0.0, time.time() - started)
            if reason == "stopped":
                return {"ok": False, "skipped": True, "error": "", "duration": duration}
            if reason == "timeout":
                return {"ok": False, "skipped": False, "duration": duration,
                        "error": "播放超过最大时长限制（%s 秒），已自动停止"
                                 % self._max_duration_text()}
            stderr_text = _read_stderr(proc)
            code = proc.returncode
            self._close_proc(proc)
            if code not in (0, None):
                return {"ok": False, "skipped": False, "duration": duration,
                        "error": self._play_error(tool_name, code, stderr_text, pcm)}
            return {"ok": True, "skipped": False, "error": "", "duration": duration}
        finally:
            self._unregister_proc(proc)
            _terminate_proc(proc)

    # -- 子进程管理 ---------------------------------------------------- #
    def _register_proc(self, proc: Any) -> None:
        with self._state_lock:
            self._procs.append(proc)

    def _unregister_proc(self, proc: Any) -> None:
        with self._state_lock:
            try:
                self._procs.remove(proc)
            except ValueError:
                pass

    def _wait_procs(self, procs: List[Any], deadline: Optional[float]) -> str:
        """阻塞等待子进程结束，处理 stop() 打断与 max_duration 看门狗。"""
        while True:
            if self._stop_event.is_set():
                for proc in procs:
                    _terminate_proc(proc)
                return "stopped"
            if deadline is not None and time.time() >= deadline:
                for proc in procs:
                    _terminate_proc(proc)
                return "timeout"
            if all(proc.poll() is not None for proc in procs):
                return "done"
            time.sleep(_WATCHDOG_POLL)

    @staticmethod
    def _close_proc(proc: Any) -> None:
        _close_proc(proc)

    @staticmethod
    def _play_error(tool_name: str, code: Any, stderr_text: str,
                    pcm: Optional[str]) -> str:
        detail = _clip(stderr_text)
        low = detail.lower()
        device_markers = (
            "no such device", "audio open error", "cannot open", "can't open",
            "device or resource busy", "unknown pcm", "unable to open",
            "no such file or directory",
        )
        if any(marker in low for marker in device_markers):
            return (
                "音频设备无法打开（%s）：请确认蓝牙音箱已连接，且 /etc/asound.conf "
                "已配置 bluealsa 插件（opkg install bluealsa）。详情：%s"
                % (pcm or "bluealsa", detail)
            )
        return "%s 播放失败（退出码 %s）：%s" % (
            tool_name, code, detail or "请检查音频文件是否损坏、音箱是否已连接")


# --------------------------------------------------------------------------- #
# 模块级小工具
# --------------------------------------------------------------------------- #
def _clamp_volume(value: Any) -> int:
    try:
        level = int(float(value))
    except (TypeError, ValueError):
        level = 70
    return max(0, min(100, level))


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _detect_format(path: str) -> str:
    """按扩展名判断播放方式：mp3 / wav / other。"""
    ext = os.path.splitext(str(path or "").split("?", 1)[0])[1].lower()
    if ext in _MP3_EXTS:
        return "mp3"
    if ext in _WAV_EXTS:
        return "wav"
    return "other"


def _url_suffix(url: str) -> str:
    """从 URL 推断落盘扩展名，非音频扩展名用 .audio（交给 ffmpeg 兜底）。"""
    path = str(url or "").split("?", 1)[0].split("#", 1)[0]
    ext = os.path.splitext(path)[1].lower()
    return ext if ext in _AUDIO_EXTS else ".audio"


def _remove_file(path: str) -> None:
    if not path:
        return
    try:
        os.remove(path)
    except OSError:  # pragma: no cover - 文件已删除/占用
        pass


def _read_stderr(proc: Any) -> str:
    """读取子进程 stderr（进程结束后调用，返回 UTF-8 文本）。"""
    if proc is None or getattr(proc, "stderr", None) is None:
        return ""
    try:
        data = proc.stderr.read()
    except (OSError, ValueError):  # pragma: no cover - 管道已关闭
        return ""
    if isinstance(data, bytes):
        return data.decode("utf-8", "replace").strip()
    return str(data or "").strip()


def _close_proc(proc: Any) -> None:
    if proc is None:
        return
    stderr = getattr(proc, "stderr", None)
    if stderr is not None:
        try:
            stderr.close()
        except (OSError, ValueError):  # pragma: no cover
            pass


def _terminate_proc(proc: Any) -> None:
    """优雅终止子进程：terminate → 等待 → kill。"""
    if proc is None:
        return
    try:
        if proc.poll() is not None:
            return
    except Exception:  # pragma: no cover
        return
    try:
        proc.terminate()
    except (OSError, ValueError):  # pragma: no cover - 进程已退出
        return
    try:
        proc.wait(timeout=2.0)
    except Exception:  # pragma: no cover - 超时后强杀
        try:
            proc.kill()
            proc.wait(timeout=1.0)
        except Exception:
            pass


def _clip(text: Any, limit: int = 160) -> str:
    """把多行错误信息压成一行并截断，便于展示。"""
    value = " ".join(str(text or "").split())
    return value[:limit] + "…" if len(value) > limit else value
