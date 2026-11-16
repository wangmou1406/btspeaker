"""蓝牙后端与状态监视。

- ``CliBackend``：调用 ``bluetoothctl`` 非交互命令管理适配器与音箱；解析逻辑集中在
  本文件的 ``parse_*`` 纯函数里，容忍 BlueZ 各版本输出差异。
- ``SimBackend``：开发/测试用模拟后端，读写 ``cfg.sim_devices_path``，文件缺失时
  自动生成 3 个示例设备。
- ``BluetoothMonitor``：后台线程轮询后端，仅在连接状态发生迁移时发布
  ``bluetooth.connected`` / ``bluetooth.disconnected`` 事件，启动时不误报。
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .bus import EV_BT_CONNECTED, EV_BT_DISCONNECTED
from .util import (
    CmdResult,
    get_logger,
    iso_now,
    read_json,
    run_cmd,
    wait_or_stop,
    which,
    write_json,
)

MAC_RE = re.compile(r"^[0-9A-F]{2}(?::[0-9A-F]{2}){5}$")

#: 模拟设备文件默认内容（与 tests/helpers.py 的 SIM_DEVICES 保持一致）
DEFAULT_SIM_DEVICES = [
    {"address": "AA:BB:CC:11:22:33", "name": "客厅小爱音箱", "paired": True,
     "connected": True, "trusted": True, "icon": "audio-card"},
    {"address": "AA:BB:CC:44:55:66", "name": "书房 SoundCore", "paired": True,
     "connected": False, "trusted": True, "icon": "audio-card"},
    {"address": "AA:BB:CC:77:88:99", "name": "便携蓝牙音箱", "paired": False,
     "connected": False, "trusted": False, "icon": "audio-card", "discoverable": True},
]

#: bluetoothctl 统一使用 C locale，避免本地化输出影响解析
_CLI_ENV = {"LANG": "C", "LC_ALL": "C"}

_DEVICE_LINE_RE = re.compile(
    r"Device\s+([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})(?:\s+(.*))?"
)
_NEW_DEVICE_RE = re.compile(
    r"\[NEW\]\s*Device\s+([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})(?:\s+(.*))?"
)
_DEVICE_ADDRESS_RE = re.compile(r"Device\s+([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})")
_FIELD_RE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9 _]*?)\s*:\s*(.*)$")


class BluetoothError(Exception):
    """蓝牙操作失败（面向用户的错误信息为中文）。"""


# --------------------------------------------------------------------------- #
# 基础数据结构与纯函数解析
# --------------------------------------------------------------------------- #
@dataclass
class Device:
    """一台蓝牙设备。"""

    address: str
    name: str = ""
    paired: bool = False
    connected: bool = False
    trusted: bool = False
    icon: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"address": self.address, "name": self.name, "paired": self.paired,
                "connected": self.connected, "trusted": self.trusted, "icon": self.icon}


def normalize_address(value: str) -> str:
    """把 MAC 归一化为大写冒号格式；不合法时原样 strip 返回。"""
    text = str(value or "").strip()
    return text.upper() if MAC_RE.match(text.upper()) else text


def is_valid_address(value: str) -> bool:
    return bool(MAC_RE.match(str(value or "").strip().upper()))


def _parse_yes_no(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in (
        "yes", "true", "1", "on", "是", "已连接", "已配对", "已信任",
    )


def _parse_fields(text: str) -> Dict[str, str]:
    """把 ``Key: Value`` 行解析成小写键字典（同键保留第一份）。"""
    fields: Dict[str, str] = {}
    for line in str(text or "").splitlines():
        match = _FIELD_RE.match(line)
        if match:
            fields.setdefault(match.group(1).strip().lower(), match.group(2).strip())
    return fields


def _clean_name(value: Optional[str]) -> str:
    name = str(value or "").strip()
    return "" if name.startswith("(") and name.endswith(")") else name


def parse_devices_output(text: str) -> List[Device]:
    """解析 ``bluetoothctl devices`` 输出：每行 ``Device <MAC> <名称>``。"""
    devices: List[Device] = []
    seen = set()
    for line in str(text or "").splitlines():
        match = _DEVICE_LINE_RE.search(line)
        if not match:
            continue
        address = normalize_address(match.group(1))
        if not address or address in seen:
            continue
        devices.append(Device(address=address, name=_clean_name(match.group(2))))
        seen.add(address)
    return devices


def parse_device_info(text: str) -> Optional[Device]:
    """解析 ``bluetoothctl info <MAC>`` 输出为 Device；无设备行返回 None。"""
    raw = str(text or "")
    match = _DEVICE_ADDRESS_RE.search(raw)
    if not match:
        return None
    fields = _parse_fields(raw)
    return Device(
        address=normalize_address(match.group(1)),
        name=fields.get("name") or fields.get("alias") or "",
        paired=_parse_yes_no(fields.get("paired")),
        connected=_parse_yes_no(fields.get("connected")),
        trusted=_parse_yes_no(fields.get("trusted")),
        icon=fields.get("icon", ""),
    )


def parse_adapter_info(text: str, hci: str = "hci0") -> Dict[str, Any]:
    """解析 ``bluetoothctl show`` 输出为适配器信息字典。"""
    info: Dict[str, Any] = {"address": "", "name": "", "powered": False,
                            "discovering": False, "hci": str(hci or "hci0")}
    raw = str(text or "")
    match = re.search(
        r"Controller\s+([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})(?:\s+\(([^)]*)\))?", raw
    )
    if match:
        info["address"] = normalize_address(match.group(1))
        if match.group(2):
            info["name"] = match.group(2).strip()
    fields = _parse_fields(raw)
    if not info["name"]:
        info["name"] = fields.get("name") or fields.get("alias") or ""
    if "powered" in fields:
        info["powered"] = _parse_yes_no(fields["powered"])
    if "discovering" in fields:
        info["discovering"] = _parse_yes_no(fields["discovering"])
    return info


def parse_scan_output(text: str) -> List[Device]:
    """解析扫描输出里的 ``[NEW] Device <MAC> <名称>`` 行（去重保序）。"""
    devices: List[Device] = []
    seen = set()
    for line in str(text or "").splitlines():
        match = _NEW_DEVICE_RE.search(line)
        if not match:
            continue
        address = normalize_address(match.group(1))
        if not address or address in seen:
            continue
        devices.append(Device(address=address, name=_clean_name(match.group(2))))
        seen.add(address)
    return devices


def _device_from_mapping(data: Dict[str, Any]) -> Device:
    """从模拟设备文件条目构造 Device。"""
    return Device(
        address=normalize_address(data.get("address", "")),
        name=str(data.get("name") or ""),
        paired=bool(data.get("paired")),
        connected=bool(data.get("connected")),
        trusted=bool(data.get("trusted")),
        icon=str(data.get("icon") or ""),
    )


# --------------------------------------------------------------------------- #
# 后端抽象
# --------------------------------------------------------------------------- #
class BluetoothBackend:
    """蓝牙后端抽象基类：CLI / 模拟后端实现同一套接口。"""

    name = "base"

    def available(self) -> bool:
        raise NotImplementedError

    def adapter_info(self) -> Dict[str, Any]:
        raise NotImplementedError

    def power_on(self) -> bool:
        raise NotImplementedError

    def list_devices(self, paired_only: bool = False) -> List[Device]:
        raise NotImplementedError

    def device_info(self, address: str) -> Optional[Device]:
        raise NotImplementedError

    def scan(self, timeout: float = 12.0) -> List[Device]:
        raise NotImplementedError

    def pair(self, address: str) -> bool:
        raise NotImplementedError

    def connect(self, address: str) -> bool:
        raise NotImplementedError

    def disconnect(self, address: str) -> bool:
        raise NotImplementedError

    def remove(self, address: str) -> bool:
        raise NotImplementedError

    def trust(self, address: str) -> bool:
        raise NotImplementedError

    def diag(self) -> Dict[str, Any]:
        raise NotImplementedError

    @staticmethod
    def _require_address(address: str) -> str:
        """校验并归一化 MAC；明显参数错误抛 BluetoothError。"""
        normalized = normalize_address(address)
        if not is_valid_address(normalized):
            raise BluetoothError(
                "蓝牙地址格式不正确：%r（应为 AA:BB:CC:DD:EE:FF 这种格式）" % (address,)
            )
        return normalized


# --------------------------------------------------------------------------- #
# bluetoothctl 后端
# --------------------------------------------------------------------------- #
class CliBackend(BluetoothBackend):
    """用 ``bluetoothctl`` 非交互命令实现的真实后端（OpenWrt 默认）。"""

    name = "cli"

    def __init__(self, cfg, logger=None) -> None:
        self.cfg = cfg
        self.logger = logger or get_logger("bluetooth")
        self.cli = str(cfg.get("bluetooth.cli", "bluetoothctl") or "bluetoothctl").strip()
        self.adapter = str(cfg.get("bluetooth.adapter", "hci0") or "hci0").strip()
        self.scan_timeout = float(cfg.get("bluetooth.scan_timeout", 12) or 12)
        self._missing_warned = False

    # -- 命令执行 ------------------------------------------------------ #
    def _tool_path(self) -> str:
        tool = self.cli or "bluetoothctl"
        found = which(tool)
        if found:
            return found
        if os.path.isabs(tool) and os.path.isfile(tool):
            return tool
        return ""

    def _run(self, args: List[str], timeout: float = 20.0) -> CmdResult:
        """执行一条 bluetoothctl 非交互命令；缺失工具时返回 127 而不抛异常。"""
        tool = self._tool_path()
        argv = [str(a) for a in args]
        if not tool:
            if not self._missing_warned:
                self._missing_warned = True
                self.logger.warning(
                    "找不到蓝牙工具 %s：请安装 opkg install bluez bluez-utils", self.cli
                )
            return CmdResult(
                returncode=127,
                stderr="找不到命令: %s（请安装：opkg install bluez bluez-utils）" % self.cli,
                argv=[self.cli] + argv,
            )
        return run_cmd([tool] + argv, timeout=timeout, env=dict(_CLI_ENV))

    def _result_ok(self, result: CmdResult, tolerated: tuple = ()) -> bool:
        """把退出码 + 文本转成成功/失败，容忍「已连接/已配对」这类报错。"""
        text = ((result.stdout or "") + "\n" + (result.stderr or "")).strip()
        low = text.lower()
        if any(marker in low for marker in tolerated):
            return True
        if not result.ok:
            self.logger.warning("bluetoothctl 命令失败（%s）：%s",
                                " ".join(result.argv or []),
                                text[:200] or ("退出码 %s" % result.returncode))
            return False
        if "failed" in low or "error:" in low:
            self.logger.warning("bluetoothctl 返回错误（%s）：%s",
                                " ".join(result.argv or []), text[:200])
            return False
        return True

    # -- 适配器 -------------------------------------------------------- #
    def available(self) -> bool:
        return bool(self._tool_path())

    def adapter_info(self) -> Dict[str, Any]:
        result = self._run(["show"], timeout=8.0)
        text = (result.stdout or "") + "\n" + (result.stderr or "")
        info = parse_adapter_info(text, hci=self.adapter)
        if not info["address"] and result.returncode != 0:
            self.logger.debug("读取适配器信息失败：%s", text.strip()[:160])
        return info

    def power_on(self) -> bool:
        ok = self._result_ok(self._run(["power", "on"], timeout=15.0))
        if ok:
            self.logger.info("已请求打开蓝牙适配器电源")
        return ok

    # -- 设备列表 / 详情 ----------------------------------------------- #
    #: list_devices 逐台查询 info 的总预算（秒），保证 poll_once 在 30 秒内完成
    _LIST_ENRICH_BUDGET = 12.0

    def list_devices(self, paired_only: bool = False) -> List[Device]:
        base_devices = parse_devices_output(self._run(["devices"], timeout=8.0).stdout)
        devices: List[Device] = []
        deadline = time.time() + self._LIST_ENRICH_BUDGET
        truncated = False
        for base in base_devices:
            remaining = deadline - time.time()
            if remaining <= 0.5:
                truncated = True
                if paired_only:
                    break  # 无法确认配对状态，宁缺勿错
                devices.append(base)
                continue
            try:
                info = self._device_info(base.address, timeout=min(3.0, remaining))
            except BluetoothError:
                info = None
            if info is None:
                info = base
            elif not info.name:
                info.name = base.name
            if paired_only and not info.paired:
                continue
            devices.append(info)
        if truncated:
            self.logger.warning("蓝牙设备信息查询超出预算（%.0f 秒），已返回部分结果",
                                self._LIST_ENRICH_BUDGET)
        return devices

    def device_info(self, address: str) -> Optional[Device]:
        return self._device_info(address, timeout=12.0)

    def _device_info(self, address: str, timeout: float = 12.0) -> Optional[Device]:
        normalized = self._require_address(address)
        result = self._run(["info", normalized], timeout=timeout)
        if not (result.stdout or "").strip():
            return None
        device = parse_device_info(result.stdout)
        if device is None and not result.ok:
            self.logger.debug("读取设备信息失败 %s：%s",
                              normalized, (result.stderr or "")[:160])
        return device

    # -- 扫描 ---------------------------------------------------------- #
    @staticmethod
    def _scan_unsupported(text: str) -> bool:
        """判断扫描失败是否因为当前 BlueZ 不支持 ``--timeout`` 选项。"""
        low = str(text or "").lower()
        return any(marker in low for marker in (
            "unknown option", "unrecognized option", "invalid option",
            "unknown command", "invalid command", "not supported",
            "unsupported option", "--timeout",
        ))

    def scan(self, timeout: float = 12.0) -> List[Device]:
        if not self.available():
            return []
        try:
            seconds = max(1.0, float(timeout if timeout is not None else self.scan_timeout))
        except (TypeError, ValueError):
            seconds = max(1.0, self.scan_timeout)
        # 首选 BlueZ 5.x 的 --timeout（scan on 会自行退出，输出完整）
        result = self._run(["--timeout", str(int(round(seconds))), "scan", "on"],
                           timeout=seconds + 15.0)
        text = (result.stdout or "") + "\n" + (result.stderr or "")
        if result.timed_out or (not result.ok and self._scan_unsupported(text)):
            self.logger.info("bluetoothctl 自带 --timeout 不可用，改用 Popen 兜底扫描")
            text = self._scan_popen(seconds)
        found = parse_scan_output(text)
        # 扫描结束后 BlueZ 会把新设备加入已知设备列表，合并以返回「全部发现设备」
        merged: Dict[str, Device] = {}
        try:
            for device in self.list_devices():
                merged[device.address] = device
        except BluetoothError:
            pass
        for device in found:
            merged.setdefault(device.address, device)
        self.logger.info("蓝牙扫描完成，共发现 %d 台设备", len(merged))
        return list(merged.values())

    def _scan_popen(self, seconds: float) -> str:
        """``--timeout`` 不受支持时的兜底：Popen + 定时 terminate，收集输出。"""
        tool = self._tool_path()
        if not tool:
            return ""
        env = dict(os.environ)
        env.update(_CLI_ENV)
        try:
            proc = subprocess.Popen(
                [tool, "scan", "on"], stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                env=env, universal_newlines=True,
            )
        except OSError as exc:
            self.logger.warning("启动 bluetoothctl 扫描失败：%s", exc)
            return ""
        try:
            proc.wait(timeout=max(1.0, float(seconds)))
        except subprocess.TimeoutExpired:
            pass
        finally:
            if proc.poll() is None:
                try:
                    proc.terminate()
                except OSError:  # pragma: no cover - 进程已退出
                    pass
        output = ""
        try:
            output, _ = proc.communicate(timeout=5.0)
        except subprocess.TimeoutExpired:  # pragma: no cover - 极端情况
            proc.kill()
            try:
                output, _ = proc.communicate(timeout=2.0)
            except Exception:
                output = ""
        except (OSError, ValueError):  # pragma: no cover - 管道异常
            output = ""
        return str(output or "")

    # -- 配对 / 连接 / 断开 / 删除 / 信任 ------------------------------ #
    def pair(self, address: str) -> bool:
        normalized = self._require_address(address)
        ok = self._result_ok(
            self._run(["pair", normalized], timeout=45.0),
            tolerated=("alreadyexists", "already paired", "already exists"),
        )
        if ok:
            self.logger.info("蓝牙配对成功：%s", normalized)
        return ok

    def connect(self, address: str) -> bool:
        normalized = self._require_address(address)
        if not self.available():
            return False
        if bool(self.cfg.get("bluetooth.trust_on_connect", True)):
            try:
                self.trust(normalized)  # 信任失败不阻断连接
            except BluetoothError:
                pass
        # 未配对设备先配对：小米等音箱必须先完成配对（链路密钥）才能连接，
        # 否则底层会直接 page timeout，报"设备无响应"。
        if not self._is_paired(normalized):
            pair_result = self._run(["pair", normalized], timeout=30.0)
            pair_ok = self._result_ok(
                pair_result,
                tolerated=("alreadyexists", "already paired", "already exists"),
            )
            if not pair_ok:
                self._raise_connection_failure(pair_result, "配对")
        result = self._run(["connect", normalized], timeout=40.0)
        if self._result_ok(result, tolerated=("alreadyconnected", "already connected")):
            self.logger.info("蓝牙已连接：%s", normalized)
            return True
        self._raise_connection_failure(result, "连接")
        return False

    def _is_paired(self, address: str) -> bool:
        """查询设备是否已经配对（Bluetooth 键信息是否存在）。"""
        try:
            result = self._run(["info", address], timeout=15.0)
        except Exception:  # pragma: no cover - 防御性
            return False
        text = ((result.stdout or "") + "\n" + (result.stderr or "")).lower()
        return "paired: yes" in text or "bonded: yes" in text

    def _raise_connection_failure(self, result: Any, action: str) -> None:
        """连接/配对失败时抛出带中文排查提示的 BluetoothError。"""
        text = ((result.stdout or "") + "\n" + (result.stderr or "")).strip()
        low = text.lower()
        no_response_markers = (
            "page-timeout", "page timeout", "status 0x04", "connect failed",
            "connectionattemptfailed", "br-connection-busy",
        )
        if any(marker in low for marker in no_response_markers):
            raise BluetoothError(
                "音箱无响应（page timeout）。请依次确认："
                "①音箱已开机并长按蓝牙键进入配对模式（指示灯快闪/语音提示）；"
                "②音箱没有连接手机或其它设备（先在手机上忽略/关闭蓝牙）；"
                "③音箱靠近路由器后重试。原始信息：%s" % (text[:120] or "无")
            )
        raise BluetoothError("%s失败：%s" % (action, text[:120] or "未知原因"))

    def disconnect(self, address: str) -> bool:
        normalized = self._require_address(address)
        ok = self._result_ok(
            self._run(["disconnect", normalized], timeout=25.0),
            tolerated=("notconnected", "not connected", "not available"),
        )
        if ok:
            self.logger.info("蓝牙已断开：%s", normalized)
        return ok

    def remove(self, address: str) -> bool:
        normalized = self._require_address(address)
        ok = self._result_ok(
            self._run(["remove", normalized], timeout=25.0),
            tolerated=("does not exist", "not available", "not found"),
        )
        if ok:
            self.logger.info("已删除蓝牙设备：%s", normalized)
        return ok

    def trust(self, address: str) -> bool:
        normalized = self._require_address(address)
        ok = self._result_ok(self._run(["trust", normalized], timeout=20.0),
                             tolerated=("already trusted", "alreadytrusted"))
        if ok:
            self.logger.info("已信任蓝牙设备：%s", normalized)
        return ok

    # -- 诊断 ---------------------------------------------------------- #
    def diag(self) -> Dict[str, Any]:
        tool = self._tool_path()
        if not tool:
            return {"tool": self.cli, "ok": False,
                    "detail": "未安装 %s：请执行 opkg install bluez bluez-utils" % self.cli}
        version = ""
        try:
            result = run_cmd([tool, "--version"], timeout=8.0, env=dict(_CLI_ENV))
            lines = (result.stdout or result.stderr or "").strip().splitlines()
            version = lines[0].strip() if lines else ""
        except Exception:  # pragma: no cover - 防御性
            version = ""
        adapter = self.adapter_info()
        detail_parts = [version or tool]
        if adapter.get("address"):
            powered = "已上电" if adapter.get("powered") else "未上电"
            detail_parts.append("适配器 %s（%s）%s"
                                % (adapter["address"], self.adapter, powered))
        else:
            detail_parts.append("未检测到蓝牙适配器 %s，请确认硬件已接好" % self.adapter)
        return {"tool": tool, "ok": bool(adapter.get("address")),
                "detail": "；".join(detail_parts)}


# --------------------------------------------------------------------------- #
# 模拟后端
# --------------------------------------------------------------------------- #
class SimBackend(BluetoothBackend):
    """读写 ``cfg.sim_devices_path`` 的模拟后端（开发机 / 无硬件测试）。"""

    name = "sim"

    def __init__(self, cfg, logger=None) -> None:
        self.cfg = cfg
        self.logger = logger or get_logger("bluetooth")
        self.path = cfg.sim_devices_path
        self._lock = threading.RLock()
        with self._lock:
            self._read_locked()  # 文件缺失时立即生成示例设备

    # -- 文件读写（调用方需持锁）--------------------------------------- #
    def _seed_locked(self) -> None:
        try:
            write_json(self.path, [dict(item) for item in DEFAULT_SIM_DEVICES])
            self.logger.info("已创建模拟设备文件：%s（共 %d 台示例设备）",
                             self.path, len(DEFAULT_SIM_DEVICES))
        except OSError as exc:  # pragma: no cover - 磁盘/权限问题
            self.logger.error("创建模拟设备文件失败 %s：%s", self.path, exc)

    def _read_locked(self) -> List[Dict[str, Any]]:
        if not os.path.isfile(self.path):
            self._seed_locked()
        raw = read_json(self.path, None)
        if not isinstance(raw, list):
            self.logger.warning("模拟设备文件格式异常，已重建示例设备：%s", self.path)
            self._seed_locked()
            raw = read_json(self.path, [])
        devices: List[Dict[str, Any]] = []
        for item in raw if isinstance(raw, list) else []:
            if isinstance(item, dict) and str(item.get("address") or "").strip():
                devices.append(dict(item))
        return devices

    def _write_locked(self, devices: List[Dict[str, Any]]) -> None:
        payload: List[Dict[str, Any]] = []
        for item in devices:
            if not isinstance(item, dict):
                continue
            clean = dict(item)
            clean["address"] = normalize_address(clean.get("address", ""))
            clean["name"] = str(clean.get("name") or "")
            for key in ("paired", "connected", "trusted", "discoverable"):
                if key in clean:
                    clean[key] = bool(clean[key])
            clean["icon"] = str(clean.get("icon") or "")
            payload.append(clean)
        try:
            write_json(self.path, payload)
        except OSError as exc:  # pragma: no cover - 磁盘/权限问题
            self.logger.error("保存模拟设备文件失败 %s：%s", self.path, exc)

    @staticmethod
    def _find_index(devices: List[Dict[str, Any]], address: str) -> int:
        target = normalize_address(address)
        for index, item in enumerate(devices):
            if normalize_address(item.get("address", "")) == target:
                return index
        return -1

    @staticmethod
    def _wrap(data: Dict[str, Any]) -> Device:
        return _device_from_mapping(data)

    # -- 后端接口 ------------------------------------------------------ #
    def available(self) -> bool:
        return True

    def adapter_info(self) -> Dict[str, Any]:
        return {"address": "00:11:22:33:44:55", "name": "iStoreOS 模拟适配器",
                "powered": True, "discovering": False,
                "hci": str(self.cfg.get("bluetooth.adapter", "hci0") or "hci0")}

    def power_on(self) -> bool:
        return True

    def list_devices(self, paired_only: bool = False) -> List[Device]:
        with self._lock:
            devices = self._read_locked()
        return [self._wrap(item) for item in devices
                if item.get("paired") or not paired_only]

    def device_info(self, address: str) -> Optional[Device]:
        normalized = self._require_address(address)
        with self._lock:
            devices = self._read_locked()
            index = self._find_index(devices, normalized)
            return self._wrap(devices[index]) if index >= 0 else None

    def scan(self, timeout: float = 12.0) -> List[Device]:
        """返回「可发现」设备 + 已配对设备（模拟扫描结果）。"""
        with self._lock:
            devices = self._read_locked()
        result: List[Device] = []
        seen = set()
        for item in devices:
            address = normalize_address(item.get("address", ""))
            if not address or address in seen:
                continue
            if item.get("discoverable") or item.get("paired"):
                result.append(self._wrap(item))
                seen.add(address)
        return result

    def pair(self, address: str) -> bool:
        normalized = self._require_address(address)
        with self._lock:
            devices = self._read_locked()
            index = self._find_index(devices, normalized)
            if index < 0:
                self.logger.warning("模拟配对失败：设备不存在 %s", normalized)
                return False
            devices[index]["paired"] = True
            self._write_locked(devices)
        self.logger.info("模拟蓝牙配对成功：%s", normalized)
        return True

    def connect(self, address: str) -> bool:
        normalized = self._require_address(address)
        trust_on_connect = bool(self.cfg.get("bluetooth.trust_on_connect", True))
        with self._lock:
            devices = self._read_locked()
            index = self._find_index(devices, normalized)
            if index < 0:
                self.logger.warning("模拟连接失败：设备不存在 %s", normalized)
                return False
            # 同一时间只允许一台音箱在线：互斥置位
            for item_index, item in enumerate(devices):
                item["connected"] = item_index == index
            if trust_on_connect:
                devices[index]["trusted"] = True
            self._write_locked(devices)
        self.logger.info("模拟蓝牙已连接：%s", normalized)
        return True

    def disconnect(self, address: str) -> bool:
        normalized = self._require_address(address)
        with self._lock:
            devices = self._read_locked()
            index = self._find_index(devices, normalized)
            if index < 0:
                return False
            devices[index]["connected"] = False
            self._write_locked(devices)
        self.logger.info("模拟蓝牙已断开：%s", normalized)
        return True

    def remove(self, address: str) -> bool:
        normalized = self._require_address(address)
        with self._lock:
            devices = self._read_locked()
            index = self._find_index(devices, normalized)
            if index < 0:
                return False
            devices.pop(index)
            self._write_locked(devices)
        self.logger.info("已删除模拟蓝牙设备：%s", normalized)
        return True

    def trust(self, address: str) -> bool:
        normalized = self._require_address(address)
        with self._lock:
            devices = self._read_locked()
            index = self._find_index(devices, normalized)
            if index < 0:
                return False
            devices[index]["trusted"] = True
            self._write_locked(devices)
        self.logger.info("已信任模拟蓝牙设备：%s", normalized)
        return True

    def diag(self) -> Dict[str, Any]:
        return {"tool": "sim", "ok": True,
                "detail": "模拟后端：设备文件 %s" % self.path}


def create_backend(cfg, logger=None) -> BluetoothBackend:
    """按 ``cfg.bluetooth.backend`` 创建后端；未知值回退到 CLI。"""
    log = logger or get_logger("bluetooth")
    backend = str(cfg.get("bluetooth.backend", "cli") or "cli").strip().lower()
    if backend in ("sim", "mock", "simulate", "模拟"):
        return SimBackend(cfg, log)
    if backend not in ("cli", "bluetoothctl", "real", "bluez"):
        log.warning("未知的蓝牙后端 %r，已回退到 cli（bluetoothctl）", backend)
    return CliBackend(cfg, log)


# --------------------------------------------------------------------------- #
# 状态监视线程
# --------------------------------------------------------------------------- #
class BluetoothMonitor(threading.Thread):
    """轮询蓝牙后端，状态迁移时发布事件并同步 RuntimeState。"""

    def __init__(self, cfg, backend, bus, state,
                 interval: Optional[float] = None, logger=None) -> None:
        super().__init__(name="bt-monitor", daemon=True)
        self.cfg = cfg
        self.backend = backend
        self.bus = bus
        self.state = state
        self.logger = logger or get_logger("bluetooth")
        if interval is None:
            try:
                interval = float(cfg.get("bluetooth.poll_interval", 5) or 5)
            except (TypeError, ValueError):
                interval = 5.0
        self.interval = max(0.2, float(interval))  # 下限防止忙等
        self._stop_event = threading.Event()
        self._poll_lock = threading.Lock()
        self._baseline_ready = False
        self._last_connected: Optional[Dict[str, Any]] = None

    # -- 生命周期 ------------------------------------------------------ #
    def stop(self) -> None:
        self._stop_event.set()
        if self.is_alive() and threading.current_thread() is not self:
            self.join(timeout=5.0)

    def run(self) -> None:
        self.logger.info("蓝牙监视器启动：间隔 %.1f 秒，后端 %s",
                         self.interval, getattr(self.backend, "name", "?"))
        while not self._stop_event.is_set():
            try:
                self.poll_once()
            except Exception:  # pragma: no cover - 线程不能死
                self.logger.exception("蓝牙轮询出现未预期异常")
            if wait_or_stop(self._stop_event, self.interval):
                break
        self.logger.info("蓝牙监视器已停止")

    # -- 单次轮询 ------------------------------------------------------ #
    def poll_once(self, publish: bool = True) -> Dict[str, Any]:
        """轮询一次并更新状态；返回本次快照（测试可直接调用）。"""
        with self._poll_lock:
            snapshot = self._collect()
            self._update_state(snapshot)
            if not snapshot.get("available"):
                # 工具/适配器不可用：跳过迁移判断，避免误报「已断开」
                return snapshot
            connected = snapshot.get("connected")
            if not self._baseline_ready:
                # 首次轮询只建立基线，绝不发布 connected 事件
                self._baseline_ready = True
                self._last_connected = connected
                self.logger.debug("蓝牙连接基线：%s",
                                  self._address_of(connected) or "无")
                return snapshot
            if publish:
                self._emit_transitions(self._last_connected, connected)
            self._last_connected = connected
            return snapshot

    def _collect(self) -> Dict[str, Any]:
        snapshot: Dict[str, Any] = {"available": False, "powered": False,
                                    "adapter": {}, "devices": [], "connected": None,
                                    "scanning": False, "at": iso_now()}
        try:
            snapshot["available"] = bool(self.backend.available())
        except Exception:
            self.logger.exception("检测蓝牙后端可用性失败")
            return snapshot
        if not snapshot["available"]:
            return snapshot
        adapter: Dict[str, Any] = {}
        try:
            adapter = dict(self.backend.adapter_info() or {})
        except Exception:
            self.logger.exception("读取蓝牙适配器信息失败")
        snapshot["adapter"] = adapter
        snapshot["powered"] = bool(adapter.get("powered"))
        snapshot["scanning"] = bool(adapter.get("discovering"))
        try:
            raw_devices = self.backend.list_devices() or []
        except Exception:
            self.logger.exception("读取蓝牙设备列表失败")
            raw_devices = []
        devices: List[Dict[str, Any]] = []
        connected: Optional[Dict[str, Any]] = None
        seen = set()
        for device in raw_devices:
            try:
                data = device.to_dict() if hasattr(device, "to_dict") else dict(device)
            except Exception:  # pragma: no cover - 防御性
                continue
            address = normalize_address(data.get("address", ""))
            if not address or address in seen:
                continue
            data["address"] = address
            seen.add(address)
            devices.append(data)
            if data.get("connected") and connected is None:
                connected = data
        snapshot["devices"] = devices
        snapshot["connected"] = connected
        return snapshot

    def _update_state(self, snapshot: Dict[str, Any]) -> None:
        if self.state is None:
            return
        try:
            self.state.update({
                "bluetooth.available": bool(snapshot.get("available")),
                "bluetooth.powered": bool(snapshot.get("powered")),
                "bluetooth.adapter": snapshot.get("adapter") or {},
                "bluetooth.connected": snapshot.get("connected"),
                "bluetooth.devices": snapshot.get("devices") or [],
                "bluetooth.scanning": bool(snapshot.get("scanning")),
            })
        except Exception:  # pragma: no cover - 防御性
            self.logger.exception("更新蓝牙运行状态失败")

    def _emit_transitions(self, previous: Optional[Dict[str, Any]],
                          current: Optional[Dict[str, Any]]) -> None:
        previous_address = self._address_of(previous)
        current_address = self._address_of(current)
        if previous_address == current_address:
            return
        if previous_address:
            self.logger.info("蓝牙已断开：%s（%s）",
                             previous.get("name") or "未知设备", previous_address)
            self._publish(EV_BT_DISCONNECTED, self._event_payload(previous))
        if current_address:
            self.logger.info("蓝牙已连接：%s（%s）",
                             current.get("name") or "未知设备", current_address)
            self._publish(EV_BT_CONNECTED, self._event_payload(current))

    @staticmethod
    def _address_of(device: Optional[Dict[str, Any]]) -> str:
        if not isinstance(device, dict):
            return ""
        return normalize_address(device.get("address", ""))

    def _event_payload(self, device: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        data = dict(device or {})
        return {"address": data.get("address", ""), "name": data.get("name", ""),
                "device": data, "at": iso_now()}

    def _publish(self, event: str, payload: Dict[str, Any]) -> None:
        if self.bus is None:
            return
        try:
            self.bus.publish(event, payload)
        except Exception:  # pragma: no cover - 事件总线自身已兜底
            self.logger.exception("发布蓝牙事件失败：%s", event)
