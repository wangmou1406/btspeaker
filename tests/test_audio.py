"""音频模块测试：探针、音量、播放源解析、mock 生命周期、bluealsa 命令拼装。

用假子进程（FakeProc）验证 mpg123/aplay/ffmpeg 的调用方式，不依赖声卡与硬件。
临时目录与临时文件统一落在工作区内（tests.helpers / cfg.data_dir/tmp）。
"""

from __future__ import annotations

import array
import contextlib
import os
import subprocess
import threading
import time
import unittest
import urllib.error
import wave
from unittest import mock

from btspeaker.audio import AudioError, PlayRequest, Player
from btspeaker.bus import (
    EV_BT_CONNECTED,
    EV_BT_DISCONNECTED,
    EV_PLAYER_FAILED,
    EV_PLAYER_FINISHED,
    EV_PLAYER_STARTED,
    EV_PLAYER_STOPPED,
    EventBus,
)
from btspeaker.config import Config
from btspeaker.util import CmdResult
from tests.helpers import EventRecorder, make_config, temp_data_dir, wait_until

MAC = "AA:BB:CC:11:22:33"
PCM = "bluealsa:HCI=hci0,DEV=%s,PROFILE=a2dp" % MAC


def write_wav(path, seconds=0.05, rate=8000, amplitude=1000, channels=1, width=2):
    """生成一个可读的 wav 文件（默认 16bit 单声道）。"""
    frames = max(1, int(rate * seconds))
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with wave.open(path, "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(width)
        handle.setframerate(rate)
        if width == 1:
            handle.writeframes(bytes([128]) * (frames * channels))
        else:
            samples = array.array("h", [amplitude] * (frames * channels))
            handle.writeframes(samples.tobytes())
    return path


class _FakePipe:
    """伪管道：提供 close() / read()，供 Player 读取子进程 stderr。"""

    def __init__(self, data: bytes = b"") -> None:
        self._data = data

    def close(self) -> None:  # pragma: no cover - 只验证不抛异常
        pass

    def read(self, size: int = -1) -> bytes:
        return self._data


class FakeProc:
    """伪 subprocess.Popen：记录 argv，按 finish_after 秒后「退出」。"""

    instances = []
    finish_after = 0.02
    exit_code = 0
    stderr_data = b""

    def __init__(self, argv, **kwargs) -> None:
        self.argv = [str(item) for item in argv]
        self.returncode = None
        self.pid = 2000 + len(FakeProc.instances)
        self.stdin = kwargs.get("stdin")
        self.stdout = _FakePipe() if kwargs.get("stdout") == subprocess.PIPE else None
        self.stderr = (
            _FakePipe(self.stderr_data) if kwargs.get("stderr") == subprocess.PIPE else None
        )
        self._started = time.time()
        FakeProc.instances.append(self)

    def poll(self):
        if self.returncode is None and time.time() - self._started >= self.finish_after:
            self.returncode = self.exit_code
        return self.returncode

    def terminate(self) -> None:
        if self.returncode is None:
            self.returncode = -15

    def kill(self) -> None:
        self.returncode = -9

    def wait(self, timeout=None):
        if self.returncode is None:
            self.returncode = self.exit_code
        return self.returncode


class HangProc(FakeProc):
    """永不结束的子进程，用于 stop() / 看门狗测试。"""

    finish_after = 999.0


class FailProc(FakeProc):
    """以 1 退出并输出典型 ALSA 设备错误的子进程。"""

    exit_code = 1
    stderr_data = b"audio open error: No such device"


class _PlayerCase(unittest.TestCase):
    """公共脚手架：临时目录、mock/真实配置、事件记录、假子进程环境。"""

    def setUp(self) -> None:
        context = temp_data_dir()
        self.data_dir = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        FakeProc.instances = []

    # -- 构造 ---------------------------------------------------------- #
    def make_mock(self):
        cfg = make_config(self.data_dir, sim=True)
        bus = EventBus()
        recorder = EventRecorder(bus)
        self.addCleanup(recorder.close)
        player = Player(cfg, bus)
        self.addCleanup(player.close)
        return cfg, player, bus, recorder

    def make_real(self, player_overrides=None):
        cfg = make_config(self.data_dir)
        if player_overrides:
            cfg.update({"player": player_overrides})
        bus = EventBus()
        recorder = EventRecorder(bus)
        self.addCleanup(recorder.close)
        player = Player(cfg, bus)
        self.addCleanup(player.close)
        return cfg, player, bus, recorder

    def sound_path(self, cfg, name, **kwargs):
        return write_wav(os.path.join(cfg.data_dir, "sounds", name), **kwargs)

    def media_path(self, cfg, name, data=b"\x00" * 1024):
        path = os.path.join(cfg.data_dir, "sounds", name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(data)
        return path

    # -- 假执行环境 ---------------------------------------------------- #
    @contextlib.contextmanager
    def bluealsa_env(self, proc_cls=FakeProc):
        """把工具探测与子进程启动替换成假实现，返回 run_cmd 调用记录。"""
        calls = []

        def fake_which(name):
            return "/usr/bin/%s" % name

        def fake_run_cmd(argv, timeout=20.0, env=None, input_text=None, cwd=None):
            argv_text = [str(item) for item in argv]
            calls.append(argv_text)
            joined = " ".join(argv_text)
            if "-L" in joined:
                return CmdResult(
                    returncode=0,
                    stdout="bluealsa\nbluealsa:HCI=hci0,DEV=%s,PROFILE=a2dp\n" % MAC,
                    argv=argv_text,
                )
            return CmdResult(returncode=0, stdout="", argv=argv_text)

        with mock.patch("btspeaker.audio.which", side_effect=fake_which):
            with mock.patch("btspeaker.audio.run_cmd", side_effect=fake_run_cmd):
                with mock.patch("btspeaker.audio.subprocess.Popen", proc_cls):
                    yield calls


# --------------------------------------------------------------------------- #
# 探针 / 音量 / 设备串
# --------------------------------------------------------------------------- #
class ProbeTest(_PlayerCase):
    def test_probe_mock(self) -> None:
        _cfg, player, _bus, _rec = self.make_mock()
        probe = player.probe()
        self.assertTrue(probe["ready"])
        self.assertEqual("mock", probe["backend"])
        self.assertEqual({"mpg123", "aplay", "ffmpeg"}, set(probe["tools"]))
        for value in probe["tools"].values():
            self.assertIsInstance(value, bool)
        self.assertFalse(probe["bluealsa_pcm"])
        self.assertEqual("", probe["hint"])
        self.assertTrue(player.ready())

    def test_probe_auto_without_tools(self) -> None:
        _cfg, player, _bus, _rec = self.make_real()
        with mock.patch("btspeaker.audio.which", return_value=None):
            probe = player.probe()
        self.assertFalse(probe["ready"])
        self.assertEqual("none", probe["backend"])
        self.assertFalse(probe["tools"]["aplay"])
        self.assertIn("opkg install alsa-utils", probe["hint"])
        self.assertIn("bluealsa", probe["hint"])

    def test_probe_auto_aplay_without_bluealsa(self) -> None:
        _cfg, player, _bus, _rec = self.make_real()
        with mock.patch(
            "btspeaker.audio.which",
            side_effect=lambda name: "/usr/bin/%s" % name if name == "aplay" else None,
        ):
            with mock.patch(
                "btspeaker.audio.run_cmd",
                return_value=CmdResult(returncode=0, stdout="null\nsysdefault\n"),
            ):
                probe = player.probe()
        self.assertFalse(probe["ready"])
        self.assertEqual("none", probe["backend"])
        self.assertFalse(probe["bluealsa_pcm"])
        self.assertIn("bluealsa", probe["hint"])

    def test_probe_auto_bluealsa_ready(self) -> None:
        _cfg, player, _bus, _rec = self.make_real()
        with mock.patch("btspeaker.audio.which", side_effect=lambda name: "/usr/bin/%s" % name):
            with mock.patch(
                "btspeaker.audio.run_cmd",
                return_value=CmdResult(returncode=0, stdout="bluealsa\nbluealsa:DEV=AA\n"),
            ):
                probe = player.probe()
                self.assertTrue(player.ready())
        self.assertTrue(probe["ready"])
        self.assertEqual("bluealsa", probe["backend"])
        self.assertTrue(probe["bluealsa_pcm"])
        self.assertTrue(probe["tools"]["mpg123"])
        self.assertEqual("", probe["hint"])

    def test_volume_bounds_and_persistence(self) -> None:
        cfg, player, _bus, _rec = self.make_mock()
        self.assertEqual(70, player.get_volume())
        self.assertEqual(100, player.set_volume(150))
        self.assertEqual(0, player.set_volume(-10))
        self.assertEqual(55, player.set_volume("55"))  # 字符串也容错
        self.assertEqual(55, player.get_volume())
        self.assertEqual(55, cfg.get("player.volume"))
        reloaded = Config.load(data_dir=self.data_dir)
        self.assertEqual(55, reloaded.get("player.volume"))

    def test_render_alsa_device(self) -> None:
        cfg, player, _bus, _rec = self.make_mock()
        default_pcm = "bluealsa:HCI=hci0,DEV=AA:BB:CC:11:22:33,PROFILE=a2dp"
        self.assertEqual(default_pcm, player._render_alsa_device("aa:bb:cc:11:22:33"))
        self.assertEqual(default_pcm, player._render_alsa_device("AA:BB:CC:11:22:33"))
        cfg.set("bluetooth.adapter", "hci5")
        cfg.set("player.alsa_device", "bluealsa:HCI={hci},DEV={mac}")
        player2 = Player(cfg, EventBus())
        self.addCleanup(player2.close)
        self.assertEqual(
            "bluealsa:HCI=hci5,DEV=AA:BB:CC:11:22:33",
            player2._render_alsa_device("AA:BB:CC:11:22:33"),
        )
        # 未知占位符保持原样；address 为空时 DEV 为空
        cfg.set("player.alsa_device", "dev={mac},foo={foo}")
        self.assertEqual("dev=AA:BB:CC:11:22:33,foo={foo}", player2._render_alsa_device(MAC))
        self.assertEqual("dev=,foo={foo}", player2._render_alsa_device(""))

    def test_mpg123_argv(self) -> None:
        _cfg, player, _bus, _rec = self.make_mock()
        argv100 = player._mpg123_argv("/tmp/a.mp3", "pcm0", 100)
        self.assertNotIn("-f", argv100)
        self.assertEqual("pcm0", argv100[argv100.index("-a") + 1])
        self.assertEqual("/tmp/a.mp3", argv100[-1])
        argv70 = player._mpg123_argv("/tmp/a.mp3", "pcm0", 70)
        self.assertIn("-f", argv70)
        self.assertEqual(str(Player._mpg123_scale(70)), argv70[argv70.index("-f") + 1])
        self.assertEqual(22938, Player._mpg123_scale(70))
        self.assertEqual(0, Player._mpg123_scale(-5))
        self.assertEqual(32768, Player._mpg123_scale(200))

    def test_wav_soft_gain(self) -> None:
        cfg, player, _bus, _rec = self.make_mock()
        path = self.sound_path(cfg, "tone.wav", seconds=0.02, amplitude=1000)
        gain_path = player._make_gain_wav(path, 50)
        self.assertTrue(gain_path)
        self.assertTrue(gain_path.startswith(os.path.join(cfg.data_dir, "tmp")))
        with wave.open(gain_path, "rb") as handle:
            data = handle.readframes(handle.getnframes())
        samples = array.array("h")
        samples.frombytes(data)
        self.assertEqual(500, max(abs(value) for value in samples))
        os.remove(gain_path)
        # 非 16bit 不做软增益
        path8 = write_wav(
            os.path.join(cfg.data_dir, "sounds", "tone8.wav"), seconds=0.02, width=1
        )
        self.assertEqual("", player._make_gain_wav(path8, 50))

    def test_diag(self) -> None:
        _cfg, player, _bus, _rec = self.make_mock()
        diag = player.diag()
        self.assertTrue(diag["ok"])
        self.assertEqual("audio", diag["tool"])
        self.assertEqual("mock", diag["backend"])
        self.assertEqual(70, diag["volume"])
        self.assertFalse(diag["playing"])
        self.assertIn("detail", diag)


# --------------------------------------------------------------------------- #
# 播放源解析
# --------------------------------------------------------------------------- #
class ResolveSourceTest(_PlayerCase):
    def test_library_and_absolute_path(self) -> None:
        cfg, player, _bus, _rec = self.make_mock()
        path = self.sound_path(cfg, "chime.wav")
        self.assertEqual(os.path.abspath(path), player.resolve_source("chime.wav"))
        self.assertEqual(os.path.abspath(path), player.resolve_source(path))
        nested = write_wav(os.path.join(cfg.data_dir, "sounds", "tv", "ding.wav"))
        self.assertEqual(os.path.abspath(nested), player.resolve_source("tv/ding.wav"))

    def test_missing_and_invalid_sources(self) -> None:
        _cfg, player, _bus, _rec = self.make_mock()
        with self.assertRaises(AudioError) as context:
            player.resolve_source("nope.wav")
        self.assertIn("找不到音频文件", str(context.exception))
        with self.assertRaises(AudioError):
            player.resolve_source("")
        with self.assertRaises(AudioError):
            player.resolve_source("../config.json")  # 不允许逃出声音库
        with self.assertRaises(AudioError) as context:
            player.resolve_source("ftp://example.com/a.mp3")
        self.assertIn("不支持的播放源协议", str(context.exception))

    def test_mock_url_does_not_download(self) -> None:
        cfg, player, _bus, _rec = self.make_mock()
        resolved = player.resolve_source("https://example.com/a.mp3")
        self.assertEqual("mock://https://example.com/a.mp3", resolved)
        tmp_dir = os.path.join(cfg.data_dir, "tmp")
        self.assertEqual([], os.listdir(tmp_dir) if os.path.isdir(tmp_dir) else [])

    def test_real_url_download_failure_is_actionable(self) -> None:
        _cfg, player, _bus, _rec = self.make_real()
        with mock.patch(
            "btspeaker.audio.urllib.request.urlopen",
            side_effect=urllib.error.URLError("网络不可达"),
        ):
            with self.assertRaises(AudioError) as context:
                player.resolve_source("https://example.com/a.mp3")
        self.assertIn("下载音频失败", str(context.exception))
        self.assertIn("网络", str(context.exception))


# --------------------------------------------------------------------------- #
# mock 播放生命周期
# --------------------------------------------------------------------------- #
class MockPlayTest(_PlayerCase):
    def test_play_lifecycle_and_events(self) -> None:
        cfg, player, _bus, recorder = self.make_mock()
        self.sound_path(cfg, "chime.wav", seconds=0.05)
        result = player.play(PlayRequest(source="chime.wav", label="提示音"))
        self.assertTrue(result.ok)
        self.assertEqual("mock", result.backend)
        self.assertFalse(result.skipped)
        self.assertGreaterEqual(result.duration, 0.0)
        self.assertEqual(
            [EV_PLAYER_STARTED, EV_PLAYER_FINISHED], recorder.names()
        )
        started = recorder.payloads(EV_PLAYER_STARTED)[0]
        self.assertEqual("chime.wav", started["source"])
        self.assertEqual("提示音", started["label"])
        self.assertEqual("", started["address"])
        self.assertEqual(70, started["volume"])
        self.assertEqual("mock", started["backend"])
        self.assertIsNone(started["rule_id"])
        finished = recorder.payloads(EV_PLAYER_FINISHED)[0]
        self.assertEqual("chime.wav", finished["source"])
        self.assertEqual("completed", finished["reason"])
        self.assertIsNone(player.current())
        self.assertFalse(player.is_playing())

    def test_stop_interrupts_mock_playback(self) -> None:
        _cfg, player, _bus, recorder = self.make_mock()
        results = []
        thread = threading.Thread(
            target=lambda: results.append(
                player.play(PlayRequest(source="https://example.com/long.mp3"))
            )
        )
        thread.start()
        try:
            self.assertTrue(wait_until(player.is_playing, timeout=2.0))
            current = player.current()
            self.assertIsNotNone(current)
            assert current is not None
            self.assertEqual(
                {
                    "playing",
                    "source",
                    "label",
                    "address",
                    "volume",
                    "backend",
                    "started_at",
                    "elapsed",
                },
                set(current),
            )
            self.assertTrue(current["playing"])
            self.assertEqual("mock", current["backend"])
            self.assertTrue(player.stop())
            thread.join(3.0)
        finally:
            if thread.is_alive():  # pragma: no cover - 防御性
                player.stop()
                thread.join(3.0)
        self.assertFalse(thread.is_alive())
        result = results[0]
        self.assertFalse(result.ok)
        self.assertTrue(result.skipped)
        self.assertEqual("", result.error)
        self.assertEqual([EV_PLAYER_STARTED, EV_PLAYER_STOPPED], recorder.names())
        stopped = recorder.payloads(EV_PLAYER_STOPPED)[0]
        self.assertEqual("用户停止", stopped["reason"])
        self.assertIsNone(player.current())
        self.assertFalse(player.is_playing())

    def test_play_is_serialized(self) -> None:
        cfg, player, _bus, _rec = self.make_mock()
        self.sound_path(cfg, "chime.wav", seconds=0.05)
        results = []
        with player._play_lock:
            thread = threading.Thread(
                target=lambda: results.append(player.play(PlayRequest(source="chime.wav")))
            )
            thread.start()
            try:
                time.sleep(0.2)
                self.assertFalse(player.is_playing())
            finally:
                pass
        thread.join(5.0)
        self.assertFalse(thread.is_alive())
        self.assertTrue(results[0].ok)

    def test_failure_events(self) -> None:
        _cfg, player, _bus, recorder = self.make_mock()
        result = player.play(PlayRequest(source="missing.wav"))
        self.assertFalse(result.ok)
        self.assertIn("找不到音频文件", result.error)
        self.assertEqual([EV_PLAYER_FAILED], recorder.names())
        self.assertIn("找不到音频文件", recorder.payloads(EV_PLAYER_FAILED)[0]["error"])

        recorder.clear()
        empty = player.play(PlayRequest(source="   "))
        self.assertFalse(empty.ok)
        self.assertIn("播放源为空", empty.error)
        self.assertEqual([EV_PLAYER_FAILED], recorder.names())


# --------------------------------------------------------------------------- #
# bluealsa 真实播放链路（假子进程）
# --------------------------------------------------------------------------- #
class BluealsaPlayTest(_PlayerCase):
    def test_mp3_play_command(self) -> None:
        cfg, player, _bus, recorder = self.make_real()
        mp3 = self.media_path(cfg, "a.mp3")
        with self.bluealsa_env():
            result = player.play(
                PlayRequest(source=mp3, label="测试", volume=70, address=MAC)
            )
        self.assertTrue(result.ok)
        self.assertEqual("bluealsa", result.backend)
        self.assertEqual(1, len(FakeProc.instances))
        argv = FakeProc.instances[0].argv
        self.assertEqual("/usr/bin/mpg123", argv[0])
        self.assertEqual("alsa", argv[argv.index("-o") + 1])
        self.assertEqual(PCM, argv[argv.index("-a") + 1])
        self.assertEqual(str(Player._mpg123_scale(70)), argv[argv.index("-f") + 1])
        self.assertEqual(mp3, argv[-1])
        self.assertEqual([EV_PLAYER_STARTED, EV_PLAYER_FINISHED], recorder.names())
        started = recorder.payloads(EV_PLAYER_STARTED)[0]
        self.assertEqual(MAC, started["address"])
        self.assertEqual("bluealsa", started["backend"])

    def test_mp3_volume_100_omits_f(self) -> None:
        cfg, player, _bus, _rec = self.make_real()
        mp3 = self.media_path(cfg, "a.mp3")
        with self.bluealsa_env():
            result = player.play(PlayRequest(source=mp3, volume=100, address=MAC))
        self.assertTrue(result.ok)
        argv = FakeProc.instances[0].argv
        self.assertNotIn("-f", argv)
        self.assertEqual(PCM, argv[argv.index("-a") + 1])

    def test_wav_uses_aplay_and_soft_gain(self) -> None:
        cfg, player, _bus, _rec = self.make_real()
        wav = self.sound_path(cfg, "tone.wav", seconds=0.02)
        with self.bluealsa_env():
            result = player.play(PlayRequest(source=wav, volume=50, address=MAC))
        self.assertTrue(result.ok)
        self.assertEqual(1, len(FakeProc.instances))
        argv = FakeProc.instances[0].argv
        self.assertEqual("/usr/bin/aplay", argv[0])
        self.assertIn("-q", argv)
        self.assertEqual(PCM, argv[argv.index("-D") + 1])
        gain_path = argv[-1]
        self.assertNotEqual(wav, gain_path)  # 用了软增益临时文件
        self.assertFalse(os.path.exists(gain_path))  # 播放后被清理

    def test_ffmpeg_pipeline(self) -> None:
        cfg, player, _bus, _rec = self.make_real()
        ogg = self.media_path(cfg, "x.ogg", data=b"OggS" * 100)
        with self.bluealsa_env():
            result = player.play(PlayRequest(source=ogg, volume=60, address=MAC))
        self.assertTrue(result.ok)
        self.assertEqual(2, len(FakeProc.instances))
        ff_argv = FakeProc.instances[0].argv
        ap_argv = FakeProc.instances[1].argv
        self.assertEqual("/usr/bin/ffmpeg", ff_argv[0])
        self.assertEqual(ogg, ff_argv[ff_argv.index("-i") + 1])
        self.assertEqual("-", ff_argv[-1])
        self.assertEqual("volume=0.600", ff_argv[ff_argv.index("-af") + 1])
        self.assertEqual("/usr/bin/aplay", ap_argv[0])
        self.assertEqual(PCM, ap_argv[ap_argv.index("-D") + 1])

    def test_address_from_bus_and_clear_on_disconnect(self) -> None:
        cfg, player, bus, _rec = self.make_real()
        mp3 = self.media_path(cfg, "a.mp3")
        bus.publish(
            EV_BT_CONNECTED,
            {"address": "aa:bb:cc:11:22:33", "name": "客厅", "device": {"address": "aa:bb:cc:11:22:33"}},
        )
        with self.bluealsa_env():
            result = player.play(PlayRequest(source=mp3))
            self.assertTrue(result.ok)
            argv = FakeProc.instances[0].argv
            self.assertEqual(PCM, argv[argv.index("-a") + 1])
            # 断开事件后不再有默认设备，播放前应给出中文提示
            bus.publish(
                EV_BT_DISCONNECTED,
                {"address": MAC, "name": "客厅", "device": {"address": MAC}},
            )
            second = player.play(PlayRequest(source=mp3))
        self.assertFalse(second.ok)
        self.assertIn("未连接蓝牙音箱", second.error)
        self.assertEqual(1, len(FakeProc.instances))

    def test_volume_command_template(self) -> None:
        cfg, player, _bus, _rec = self.make_real(
            {"volume_command": "amixer -D bluealsa sset {level}"}
        )
        mp3 = self.media_path(cfg, "a.mp3")
        with self.bluealsa_env() as calls:
            result = player.play(PlayRequest(source=mp3, volume=40, address=MAC))
        self.assertTrue(result.ok)
        joined = [" ".join(call) for call in calls]
        self.assertIn("amixer -D bluealsa sset 40", joined)

    def test_stop_interrupts_real_process(self) -> None:
        cfg, player, _bus, recorder = self.make_real()
        mp3 = self.media_path(cfg, "a.mp3")
        results = []
        with self.bluealsa_env(proc_cls=HangProc):
            thread = threading.Thread(
                target=lambda: results.append(player.play(PlayRequest(source=mp3, address=MAC)))
            )
            thread.start()
            try:
                self.assertTrue(wait_until(player.is_playing, timeout=2.0))
                self.assertTrue(player.stop())
                thread.join(3.0)
            finally:
                if thread.is_alive():  # pragma: no cover - 防御性
                    player.stop()
                    thread.join(3.0)
        self.assertFalse(thread.is_alive())
        result = results[0]
        self.assertFalse(result.ok)
        self.assertTrue(result.skipped)
        self.assertEqual([EV_PLAYER_STARTED, EV_PLAYER_STOPPED], recorder.names())

    def test_max_duration_watchdog(self) -> None:
        cfg, player, _bus, recorder = self.make_real({"max_duration": 0.15})
        mp3 = self.media_path(cfg, "a.mp3")
        with self.bluealsa_env(proc_cls=HangProc):
            result = player.play(PlayRequest(source=mp3, address=MAC))
        self.assertFalse(result.ok)
        self.assertIn("最大时长限制", result.error)
        self.assertEqual([EV_PLAYER_STARTED, EV_PLAYER_FAILED], recorder.names())

    def test_playback_failure_reports_device_error(self) -> None:
        cfg, player, _bus, recorder = self.make_real()
        mp3 = self.media_path(cfg, "a.mp3")
        with self.bluealsa_env(proc_cls=FailProc):
            result = player.play(PlayRequest(source=mp3, address=MAC))
        self.assertFalse(result.ok)
        self.assertIn("音频设备无法打开", result.error)
        self.assertIn("bluealsa", result.error)
        self.assertEqual([EV_PLAYER_STARTED, EV_PLAYER_FAILED], recorder.names())
        self.assertIn("音频设备无法打开", recorder.payloads(EV_PLAYER_FAILED)[0]["error"])

    def test_probe_none_blocks_play(self) -> None:
        cfg, player, _bus, recorder = self.make_real()
        mp3 = self.media_path(cfg, "a.mp3")
        with mock.patch("btspeaker.audio.which", return_value=None):
            result = player.play(PlayRequest(source=mp3, address=MAC))
        self.assertFalse(result.ok)
        self.assertIn("opkg install", result.error)
        self.assertEqual([EV_PLAYER_FAILED], recorder.names())
        self.assertEqual(0, len(FakeProc.instances))

    def test_missing_device_error(self) -> None:
        cfg, player, _bus, recorder = self.make_real()
        mp3 = self.media_path(cfg, "a.mp3")
        with self.bluealsa_env():
            result = player.play(PlayRequest(source=mp3))
        self.assertFalse(result.ok)
        self.assertIn("未连接蓝牙音箱", result.error)
        self.assertEqual(0, len(FakeProc.instances))
        self.assertEqual([EV_PLAYER_FAILED], recorder.names())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
