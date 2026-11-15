"""蓝牙模块测试：解析函数、模拟后端、CLI 后端容错、状态监视器。

全部测试不依赖蓝牙硬件与外网，临时目录统一由 tests.helpers 创建在工作区内。
"""

from __future__ import annotations

import os
import unittest
from unittest import mock

from btspeaker.bluetooth import (
    DEFAULT_SIM_DEVICES,
    BluetoothError,
    BluetoothMonitor,
    CliBackend,
    Device,
    SimBackend,
    create_backend,
    normalize_address,
    parse_adapter_info,
    parse_device_info,
    parse_devices_output,
    parse_scan_output,
)
from btspeaker.bus import EV_BT_CONNECTED, EV_BT_DISCONNECTED, EventBus
from btspeaker.store import RuntimeState
from btspeaker.util import CmdResult, read_json
from tests.helpers import (
    EventRecorder,
    FakeBluetooth,
    make_config,
    temp_data_dir,
    wait_until,
    write_sim_devices,
)

ADC = "AA:BB:CC:11:22:33"
ADC2 = "AA:BB:CC:44:55:66"
ADC3 = "AA:BB:CC:77:88:99"


class _TempDirCase(unittest.TestCase):
    """为每个测试准备独立数据目录。"""

    def setUp(self) -> None:
        context = temp_data_dir()
        self.data_dir = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)

    def make_sim(self, devices=None, **overrides):
        """构造模拟后端 + 事件总线 + 运行时状态。"""
        cfg = make_config(self.data_dir, overrides=overrides or None, sim=True)
        if devices is not None:
            write_sim_devices(cfg.sim_devices_path, devices)
        bus = EventBus()
        state = RuntimeState()
        recorder = EventRecorder(bus)
        self.addCleanup(recorder.close)
        return cfg, SimBackend(cfg), bus, state, recorder


# --------------------------------------------------------------------------- #
# 纯函数解析
# --------------------------------------------------------------------------- #
class ParsingTest(unittest.TestCase):
    def test_normalize_address(self) -> None:
        self.assertEqual(ADC, normalize_address("aa:bb:cc:11:22:33"))
        self.assertEqual(ADC, normalize_address("  AA:BB:CC:11:22:33  "))
        # 非法地址原样 strip 返回
        self.assertEqual("not-a-mac", normalize_address("  not-a-mac "))
        self.assertEqual("", normalize_address(None))
        self.assertEqual("AA:BB:CC:11:22:33:44", normalize_address("AA:BB:CC:11:22:33:44"))

    def test_device_to_dict(self) -> None:
        device = Device(address=ADC, name="客厅", paired=True, connected=True, trusted=True, icon="audio-card")
        self.assertEqual(
            {
                "address": ADC,
                "name": "客厅",
                "paired": True,
                "connected": True,
                "trusted": True,
                "icon": "audio-card",
            },
            device.to_dict(),
        )

    def test_parse_devices_output(self) -> None:
        text = (
            "Device AA:BB:CC:11:22:33 客厅小爱音箱\n"
            "Device aa:bb:cc:44:55:66 书房 SoundCore\n"
            "Device AA:BB:CC:77:88:99\n"
            "Total 3 devices\n"
        )
        devices = parse_devices_output(text)
        self.assertEqual([ADC, ADC2, ADC3], [d.address for d in devices])
        self.assertEqual("客厅小爱音箱", devices[0].name)
        self.assertEqual("书房 SoundCore", devices[1].name)
        self.assertEqual("", devices[2].name)
        # 重复地址去重
        self.assertEqual(1, len(parse_devices_output("Device AA:BB:CC:11:22:33 A\nDevice AA:BB:CC:11:22:33 B")))

    def test_parse_device_info_bluez5(self) -> None:
        text = (
            "Device AA:BB:CC:11:22:33 (public)\n"
            "\tName: 客厅小爱音箱\n"
            "\tAlias: 客厅小爱音箱\n"
            "\tClass: 0x00240404\n"
            "\tIcon: audio-card\n"
            "\tPaired: yes\n"
            "\tTrusted: yes\n"
            "\tBlocked: no\n"
            "\tConnected: yes\n"
            "\tLegacyPairing: no\n"
        )
        device = parse_device_info(text)
        self.assertIsNotNone(device)
        assert device is not None
        self.assertEqual(ADC, device.address)
        self.assertEqual("客厅小爱音箱", device.name)
        self.assertTrue(device.paired)
        self.assertTrue(device.trusted)
        self.assertTrue(device.connected)
        self.assertEqual("audio-card", device.icon)

    def test_parse_device_info_older_bluez(self) -> None:
        # 老版本没有 "(public)"、名称用 Alias、字段缺失
        text = "Device aa:bb:cc:44:55:66\n\tAlias: 书房 SoundCore\n\tPaired: no\n\tConnected: no\n"
        device = parse_device_info(text)
        assert device is not None
        self.assertEqual(ADC2, device.address)
        self.assertEqual("书房 SoundCore", device.name)
        self.assertFalse(device.paired)
        self.assertFalse(device.connected)
        self.assertFalse(device.trusted)
        self.assertEqual("", device.icon)
        self.assertIsNone(parse_device_info(""))
        self.assertIsNone(parse_device_info("No such device"))

    def test_parse_adapter_info(self) -> None:
        text = (
            "Controller 00:11:22:33:44:55 (iStoreOS)\n"
            "\tName: iStoreOS\n"
            "\tAlias: iStoreOS\n"
            "\tPowered: yes\n"
            "\tDiscovering: no\n"
        )
        info = parse_adapter_info(text, hci="hci0")
        self.assertEqual("00:11:22:33:44:55", info["address"])
        self.assertEqual("iStoreOS", info["name"])
        self.assertTrue(info["powered"])
        self.assertFalse(info["discovering"])
        self.assertEqual("hci0", info["hci"])
        # 输出为空时给安全的默认值（powered=False）
        empty = parse_adapter_info("", hci="hci7")
        self.assertEqual("", empty["address"])
        self.assertFalse(empty["powered"])
        self.assertFalse(empty["discovering"])
        self.assertEqual("hci7", empty["hci"])

    def test_parse_scan_output(self) -> None:
        text = (
            "Discovery started\n"
            "[CHG] Controller 00:11:22:33:44:55 Discovering: yes\n"
            "[NEW] Device AA:BB:CC:77:88:99 便携蓝牙音箱\n"
            "[NEW] Device AA:BB:CC:77:88:99 便携蓝牙音箱\n"
            "[NEW] Device AA:BB:CC:AA:BB:CC\n"
            "[CHG] Device AA:BB:CC:77:88:99 RSSI: -60\n"
            "Discovery stopped\n"
        )
        devices = parse_scan_output(text)
        self.assertEqual([ADC3, "AA:BB:CC:AA:BB:CC"], [d.address for d in devices])
        self.assertEqual("便携蓝牙音箱", devices[0].name)


# --------------------------------------------------------------------------- #
# 模拟后端
# --------------------------------------------------------------------------- #
class SimBackendTest(_TempDirCase):
    def test_missing_file_creates_example_devices(self) -> None:
        cfg = make_config(self.data_dir, sim=True)
        self.assertFalse(os.path.isfile(cfg.sim_devices_path))
        backend = SimBackend(cfg)
        self.assertEqual(3, len(backend.list_devices()))
        self.assertTrue(os.path.isfile(cfg.sim_devices_path))
        raw = read_json(cfg.sim_devices_path, [])
        self.assertEqual(3, len(raw))
        self.assertTrue(any(item.get("discoverable") for item in raw))
        self.assertEqual(
            [item["address"] for item in DEFAULT_SIM_DEVICES],
            [item["address"] for item in raw],
        )

    def test_list_devices_and_device_info(self) -> None:
        cfg, backend, _bus, _state, _rec = self.make_sim()
        devices = backend.list_devices()
        self.assertEqual(3, len(devices))
        self.assertIsInstance(devices[0], Device)
        paired = backend.list_devices(paired_only=True)
        self.assertEqual([ADC, ADC2], [d.address for d in paired])

        info = backend.device_info("aa:bb:cc:11:22:33")
        assert info is not None
        self.assertTrue(info.connected)
        self.assertTrue(info.paired)
        self.assertEqual("客厅小爱音箱", info.name)

        self.assertIsNone(backend.device_info("11:22:33:44:55:66"))
        with self.assertRaises(BluetoothError):
            backend.device_info("bad-address")

    def test_connect_is_mutually_exclusive_and_persisted(self) -> None:
        cfg, backend, _bus, _state, _rec = self.make_sim()
        self.assertTrue(backend.connect("aa:bb:cc:44:55:66"))  # 小写输入也可用
        raw = read_json(cfg.sim_devices_path, [])
        by_address = {item["address"]: item for item in raw}
        self.assertTrue(by_address[ADC2]["connected"])
        self.assertFalse(by_address[ADC]["connected"])
        # 新实例读取同一文件，状态已持久化
        again = SimBackend(cfg)
        info = again.device_info(ADC2)
        assert info is not None
        self.assertTrue(info.connected)
        self.assertTrue(info.trusted)  # trust_on_connect 默认开启

    def test_pair_disconnect_remove_trust_write_back(self) -> None:
        cfg, backend, _bus, _state, _rec = self.make_sim()
        self.assertTrue(backend.pair(ADC3))
        raw = read_json(cfg.sim_devices_path, [])
        by_address = {item["address"]: item for item in raw}
        self.assertTrue(by_address[ADC3]["paired"])

        self.assertTrue(backend.disconnect(ADC))
        raw = read_json(cfg.sim_devices_path, [])
        by_address = {item["address"]: item for item in raw}
        self.assertFalse(by_address[ADC]["connected"])

        self.assertTrue(backend.trust(ADC3))
        raw = read_json(cfg.sim_devices_path, [])
        by_address = {item["address"]: item for item in raw}
        self.assertTrue(by_address[ADC3]["trusted"])

        self.assertTrue(backend.remove(ADC2))
        raw = read_json(cfg.sim_devices_path, [])
        self.assertEqual([ADC, ADC3], [item["address"] for item in raw])

        # 不存在的设备返回 False 而不是抛异常
        self.assertFalse(backend.remove("11:22:33:44:55:66"))
        self.assertFalse(backend.connect("11:22:33:44:55:66"))
        self.assertFalse(backend.pair("11:22:33:44:55:66"))
        with self.assertRaises(BluetoothError):
            backend.pair("invalid")

    def test_scan_returns_discoverable_plus_paired(self) -> None:
        devices = [
            {"address": "AA:BB:CC:00:00:01", "name": "已配对", "paired": True},
            {"address": "AA:BB:CC:00:00:02", "name": "隐藏未配对", "paired": False},
            {"address": "AA:BB:CC:00:00:03", "name": "可发现", "paired": False, "discoverable": True},
        ]
        _cfg, backend, _bus, _state, _rec = self.make_sim(devices=devices)
        found = backend.scan(timeout=1)
        self.assertEqual(
            ["AA:BB:CC:00:00:01", "AA:BB:CC:00:00:03"], [d.address for d in found]
        )

    def test_adapter_info_and_diag(self) -> None:
        cfg, backend, _bus, _state, _rec = self.make_sim()
        info = backend.adapter_info()
        self.assertTrue(info["powered"])
        self.assertFalse(info["discovering"])
        self.assertEqual("hci0", info["hci"])
        self.assertTrue(info["address"])
        self.assertTrue(backend.available())
        self.assertTrue(backend.power_on())
        diag = backend.diag()
        self.assertTrue(diag["ok"])
        self.assertEqual("sim", diag["tool"])
        self.assertIn(cfg.sim_devices_path, diag["detail"])

    def test_create_backend(self) -> None:
        cfg = make_config(self.data_dir)
        self.assertIsInstance(create_backend(cfg), CliBackend)
        cfg.set("bluetooth.backend", "sim")
        self.assertIsInstance(create_backend(cfg), SimBackend)
        cfg.set("bluetooth.backend", "mock")
        self.assertIsInstance(create_backend(cfg), SimBackend)
        cfg.set("bluetooth.backend", "unknown-backend")
        self.assertIsInstance(create_backend(cfg), CliBackend)


# --------------------------------------------------------------------------- #
# CLI 后端（用假 run_cmd，不执行真实 bluetoothctl）
# --------------------------------------------------------------------------- #
class CliBackendTest(_TempDirCase):
    def _make(self, **overrides):
        return CliBackend(make_config(self.data_dir, overrides=overrides or None))

    def test_missing_tool_is_tolerated(self) -> None:
        backend = self._make(bluetooth={"cli": "btspeaker-no-such-tool-xyz"})
        self.assertFalse(backend.available())
        self.assertEqual([], backend.list_devices())
        self.assertEqual([], backend.scan())
        self.assertFalse(backend.connect(ADC))
        info = backend.adapter_info()
        self.assertEqual("", info["address"])
        self.assertFalse(info["powered"])
        diag = backend.diag()
        self.assertFalse(diag["ok"])
        self.assertIn("opkg install", diag["detail"])
        # 明显参数错误仍然抛 BluetoothError
        with self.assertRaises(BluetoothError):
            backend.pair("not-a-mac")

    def test_devices_and_info_parsing(self) -> None:
        backend = self._make()
        devices_text = "Device AA:BB:CC:11:22:33 客厅小爱音箱\nDevice AA:BB:CC:44:55:66 书房 SoundCore\n"
        info_texts = {
            ADC: (
                "Device AA:BB:CC:11:22:33 (public)\n"
                "\tName: 客厅小爱音箱\n\tIcon: audio-card\n"
                "\tPaired: yes\n\tTrusted: yes\n\tConnected: yes\n"
            ),
            ADC2: (
                "Device AA:BB:CC:44:55:66 (public)\n"
                "\tName: 书房 SoundCore\n"
                "\tPaired: no\n\tTrusted: no\n\tConnected: no\n"
            ),
        }
        calls = []

        def fake_run(argv, timeout=20.0, env=None, input_text=None, cwd=None):
            calls.append([str(item) for item in argv])
            args = [str(item) for item in argv[1:]]
            if args == ["devices"]:
                return CmdResult(returncode=0, stdout=devices_text, argv=args)
            if args[:1] == ["info"] and args[1] in info_texts:
                return CmdResult(returncode=0, stdout=info_texts[args[1]], argv=args)
            return CmdResult(returncode=0, stdout="", argv=args)

        with mock.patch.object(backend, "_tool_path", return_value="/usr/bin/bluetoothctl"):
            with mock.patch("btspeaker.bluetooth.run_cmd", side_effect=fake_run):
                devices = backend.list_devices()
                paired = backend.list_devices(paired_only=True)
                info = backend.device_info("aa:bb:cc:11:22:33")
                missing = backend.device_info("11:22:33:44:55:66")

        self.assertEqual([ADC, ADC2], [d.address for d in devices])
        self.assertTrue(devices[0].connected)
        self.assertEqual("audio-card", devices[0].icon)
        self.assertEqual([ADC], [d.address for d in paired])
        assert info is not None
        self.assertEqual(ADC, info.address)
        self.assertTrue(info.paired)
        self.assertIsNone(missing)
        self.assertTrue(any(call[:2] == ["/usr/bin/bluetoothctl", "devices"] for call in calls))

    def test_show_and_power(self) -> None:
        backend = self._make()
        show_text = (
            "Controller 00:11:22:33:44:55 (iStoreOS)\n"
            "\tName: iStoreOS\n\tPowered: yes\n\tDiscovering: yes\n"
        )

        def fake_run(argv, timeout=20.0, env=None, input_text=None, cwd=None):
            args = [str(item) for item in argv[1:]]
            if args == ["show"]:
                return CmdResult(returncode=0, stdout=show_text, argv=args)
            return CmdResult(returncode=0, stdout="", argv=args)

        with mock.patch.object(backend, "_tool_path", return_value="/usr/bin/bluetoothctl"):
            with mock.patch("btspeaker.bluetooth.run_cmd", side_effect=fake_run):
                info = backend.adapter_info()
                self.assertTrue(backend.power_on())
        self.assertEqual("00:11:22:33:44:55", info["address"])
        self.assertTrue(info["powered"])
        self.assertTrue(info["discovering"])

    def test_scan_unsupported_detection_and_fallback(self) -> None:
        self.assertTrue(CliBackend._scan_unsupported("Unknown option --timeout"))
        self.assertTrue(CliBackend._scan_unsupported("unrecognized option '--timeout'"))
        self.assertTrue(CliBackend._scan_unsupported("Invalid command: scan"))
        self.assertFalse(CliBackend._scan_unsupported("No default controller available"))

        backend = self._make()
        fallback_text = "[NEW] Device AA:BB:CC:77:88:99 便携蓝牙音箱\n"
        unsupported = CmdResult(returncode=1, stderr="Unknown option --timeout", argv=["bluetoothctl"])
        with mock.patch.object(backend, "_tool_path", return_value="/usr/bin/bluetoothctl"):
            with mock.patch("btspeaker.bluetooth.run_cmd", return_value=unsupported):
                with mock.patch.object(backend, "_scan_popen", return_value=fallback_text) as popen:
                    devices = backend.scan(timeout=1)
        self.assertEqual([ADC3], [d.address for d in devices])
        self.assertTrue(popen.called)


# --------------------------------------------------------------------------- #
# 状态监视器
# --------------------------------------------------------------------------- #
class MonitorTest(_TempDirCase):
    def _monitor(self, devices=None, interval=0.05):
        cfg = make_config(self.data_dir, sim=True)
        if devices is not None:
            write_sim_devices(cfg.sim_devices_path, devices)
        backend = SimBackend(cfg)
        bus = EventBus()
        state = RuntimeState()
        recorder = EventRecorder(bus)
        self.addCleanup(recorder.close)
        monitor = BluetoothMonitor(cfg, backend, bus, state, interval=interval)
        return cfg, backend, bus, state, recorder, monitor

    @staticmethod
    def _bt_events(recorder):
        return [
            name
            for name in recorder.names()
            if name in (EV_BT_CONNECTED, EV_BT_DISCONNECTED)
        ]

    def test_first_poll_is_baseline_without_events(self) -> None:
        _cfg, backend, _bus, state, recorder, monitor = self._monitor()
        snapshot = monitor.poll_once()
        self.assertEqual([], self._bt_events(recorder))
        self.assertTrue(snapshot["available"])
        self.assertTrue(state.get("bluetooth.available"))
        self.assertTrue(state.get("bluetooth.powered"))
        self.assertEqual(ADC, state.get("bluetooth.connected")["address"])
        self.assertEqual(3, len(state.get("bluetooth.devices")))
        self.assertFalse(state.get("bluetooth.scanning"))
        self.assertEqual("hci0", state.get("bluetooth.adapter")["hci"])
        self.assertIn("at", snapshot)
        # 再次轮询没有变化也不发事件
        monitor.poll_once()
        self.assertEqual([], self._bt_events(recorder))
        # 后端状态与快照一致
        self.assertTrue(backend.available())

    def test_connect_and_disconnect_events_once(self) -> None:
        devices = [
            {"address": ADC, "name": "客厅小爱音箱", "paired": True},
            {"address": ADC2, "name": "书房 SoundCore", "paired": True},
        ]
        _cfg, backend, _bus, state, recorder, monitor = self._monitor(devices=devices)
        monitor.poll_once()  # 基线：全部未连接
        self.assertIsNone(state.get("bluetooth.connected"))

        self.assertTrue(backend.connect(ADC2))
        monitor.poll_once()
        self.assertEqual([EV_BT_CONNECTED], self._bt_events(recorder))
        payload = recorder.payloads(EV_BT_CONNECTED)[0]
        self.assertEqual(ADC2, payload["address"])
        self.assertEqual("书房 SoundCore", payload["name"])
        self.assertEqual(ADC2, payload["device"]["address"])
        self.assertTrue(payload["at"])
        self.assertEqual(ADC2, state.get("bluetooth.connected")["address"])

        # 状态未变化时事件不重复
        monitor.poll_once()
        self.assertEqual(1, len(recorder.payloads(EV_BT_CONNECTED)))

        self.assertTrue(backend.disconnect(ADC2))
        monitor.poll_once()
        self.assertEqual([EV_BT_CONNECTED, EV_BT_DISCONNECTED], self._bt_events(recorder))
        payload = recorder.payloads(EV_BT_DISCONNECTED)[0]
        self.assertEqual(ADC2, payload["address"])
        self.assertIsNone(state.get("bluetooth.connected"))
        monitor.poll_once()
        self.assertEqual(1, len(recorder.payloads(EV_BT_DISCONNECTED)))

    def test_switch_device_emits_disconnect_then_connect(self) -> None:
        devices = [
            {"address": ADC, "name": "客厅小爱音箱", "paired": True, "connected": True},
            {"address": ADC2, "name": "书房 SoundCore", "paired": True},
        ]
        _cfg, backend, _bus, _state, recorder, monitor = self._monitor(devices=devices)
        monitor.poll_once()  # 基线：ADC 已连接
        self.assertTrue(backend.connect(ADC2))
        monitor.poll_once()
        self.assertEqual([EV_BT_DISCONNECTED, EV_BT_CONNECTED], self._bt_events(recorder))
        self.assertEqual(ADC, recorder.payloads(EV_BT_DISCONNECTED)[0]["address"])
        self.assertEqual(ADC2, recorder.payloads(EV_BT_CONNECTED)[0]["address"])

    def test_publish_false_updates_state_without_events(self) -> None:
        devices = [
            {"address": ADC, "name": "客厅小爱音箱", "paired": True},
            {"address": ADC2, "name": "书房 SoundCore", "paired": True},
        ]
        _cfg, backend, _bus, state, recorder, monitor = self._monitor(devices=devices)
        monitor.poll_once()
        self.assertTrue(backend.connect(ADC2))
        snapshot = monitor.poll_once(publish=False)
        self.assertEqual(ADC2, snapshot["connected"]["address"])
        self.assertEqual(ADC2, state.get("bluetooth.connected")["address"])
        self.assertEqual([], self._bt_events(recorder))
        # 基线已同步：后续断开只发一次断开事件
        backend.disconnect(ADC2)
        monitor.poll_once()
        self.assertEqual([EV_BT_DISCONNECTED], self._bt_events(recorder))

    def test_stop_exits_promptly(self) -> None:
        _cfg, _backend, _bus, state, _recorder, monitor = self._monitor(interval=0.2)
        monitor.start()
        self.assertTrue(wait_until(lambda: state.get("bluetooth.available") is True, timeout=3.0))
        monitor.stop()
        self.assertFalse(monitor.is_alive())

    def test_backend_errors_do_not_crash(self) -> None:
        cfg = make_config(self.data_dir, sim=True)
        bus = EventBus()
        state = RuntimeState()
        recorder = EventRecorder(bus)
        self.addCleanup(recorder.close)

        backend = FakeBluetooth()

        def boom():
            raise RuntimeError("模拟后端炸了")

        backend.available = boom  # type: ignore[assignment]
        monitor = BluetoothMonitor(cfg, backend, bus, state, interval=0.05)
        snapshot = monitor.poll_once()
        self.assertFalse(snapshot["available"])
        self.assertEqual([], recorder.names())
        self.assertFalse(state.get("bluetooth.available"))

        # available 正常但 adapter_info 抛异常：仍返回设备列表
        backend2 = FakeBluetooth()

        def broken_adapter():
            raise RuntimeError("读取适配器失败")

        backend2.adapter_info = broken_adapter  # type: ignore[assignment]
        monitor2 = BluetoothMonitor(cfg, backend2, bus, state, interval=0.05)
        snapshot2 = monitor2.poll_once()
        self.assertTrue(snapshot2["available"])
        self.assertEqual({}, snapshot2["adapter"])
        self.assertEqual(3, len(snapshot2["devices"]))

    def test_confirmed_monitor_does_not_emit_initial_connected(self) -> None:
        # 首次轮询即便已有连接也不发事件（这是最重要的去重约束）
        cfg = make_config(self.data_dir, sim=True)
        write_sim_devices(cfg.sim_devices_path)
        bus = EventBus()
        recorder = EventRecorder(bus)
        self.addCleanup(recorder.close)
        monitor = BluetoothMonitor(cfg, SimBackend(cfg), bus, RuntimeState(), interval=0.05)
        monitor.poll_once()
        monitor.poll_once()
        self.assertEqual([], recorder.payloads(EV_BT_CONNECTED))
        self.assertEqual([], recorder.payloads(EV_BT_DISCONNECTED))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
