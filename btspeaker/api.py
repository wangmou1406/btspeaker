"""HTTP REST API 服务：把蓝牙 / 播放 / TTS / 规则 / 日志等能力暴露为 JSON 接口，
同时托管 `btspeaker/web/` 下的静态界面（独立 Web 服务，不依赖 LuCI）。

统一信封：
    成功 {"ok": true, "data": ...}
    失败 HTTP 4xx/5xx + {"ok": false, "error": {"code": "...", "message": "中文说明"}}
"""

from __future__ import annotations

import hmac
import json
import mimetypes
import os
import re
import threading
import time
import urllib.parse
import socketserver
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import APP_TITLE, PROJECT_URL, __version__
from .audio import PlayRequest
from .bus import EV_BT_PAIRED, EV_BT_SCAN_DONE, EventBus
from .config import Config
from .rules import RuleError
from .util import (
    clamp_int,
    get_log_ring,
    get_logger,
    human_duration,
    human_size,
    iso_now,
    parse_bool,
    parse_int,
    which,
)

MAX_BODY_BYTES = 2 * 1024 * 1024
# 上传接口的防御性封顶（实际限制取 settings.sounds.max_upload_mb，默认 20MB）
MAX_UPLOAD_ABS_BYTES = 64 * 1024 * 1024
_AUDIO_EXTS = (".mp3", ".wav", ".ogg", ".m4a", ".flac", ".aac", ".opus")
_DEFAULT_DEVICE = {"address": "", "name": "", "paired": False, "connected": False, "trusted": False, "icon": ""}


class ApiError(Exception):
    """带 HTTP 状态码的接口错误。"""

    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = str(code)
        self.message = str(message)
        self.status = int(status)


def device_dict(device: Any) -> Dict[str, Any]:
    """把 Device 对象/字典统一成可 JSON 序列化的设备字典。"""
    if isinstance(device, dict):
        return dict(device)
    to_dict = getattr(device, "to_dict", None)
    if callable(to_dict):
        try:
            return dict(to_dict())
        except Exception:  # pragma: no cover - 防御性
            pass
    data = dict(_DEFAULT_DEVICE)
    data["address"] = str(getattr(device, "address", "") or "")
    data["name"] = str(getattr(device, "name", "") or "")
    data["paired"] = bool(getattr(device, "paired", False))
    data["connected"] = bool(getattr(device, "connected", False))
    data["trusted"] = bool(getattr(device, "trusted", False))
    data["icon"] = str(getattr(device, "icon", "") or "")
    return data


def _first(query: Dict[str, List[str]], name: str, default: Optional[str] = None) -> Optional[str]:
    values = query.get(name)
    if not values:
        return default
    return values[0]


class Api:
    """路由与业务处理（与传输层解耦，便于单元测试）。"""

    def __init__(self, app: Any, logger=None) -> None:
        self.app = app
        self.cfg = app.cfg  # type: Config
        self.logger = logger or get_logger("api")
        self._lock = threading.RLock()
        self._cache: Dict[str, Tuple[float, Any]] = {}
        self._scan_lock = threading.Lock()
        self.started_ts = time.time()

    # ------------------------------------------------------------------ #
    # 分发
    # ------------------------------------------------------------------ #
    def dispatch(
        self,
        method: str,
        path: str,
        query: Dict[str, List[str]],
        body: Any,
        headers: Optional[Dict[str, str]] = None,
        raw_body: Optional[bytes] = None,
    ) -> Tuple[int, Dict[str, Any]]:
        method = str(method).upper()
        route = None
        params: Dict[str, str] = {}
        for route_method, pattern, handler_name in _ROUTES:
            if route_method != method:
                continue
            match = pattern.fullmatch(path)
            if match:
                route = handler_name
                params = {key: urllib.parse.unquote(value) for key, value in match.groupdict().items()}
                break
        if route is None:
            raise _jsonable(ApiError("not_found", "接口不存在: %s %s" % (method, path), 404))

        if route != "health" and not self._authorized(headers or {}, query):
            raise _jsonable(ApiError("unauthorized", "需要访问令牌：请在设置页填写 API Token，或在请求头 X-Api-Token 中提供", 401))

        # 文件上传走原始字节（非 JSON）：把字节塞进 body 供 handler 使用
        if route == "library_upload":
            body = {"_raw_bytes": raw_body if raw_body is not None else b""}

        handler = getattr(self, "h_" + route)
        try:
            data = handler(params, body if isinstance(body, dict) else {}, query)
            return 200, {"ok": True, "data": data}
        except ApiError:
            raise
        except RuleError as exc:
            raise _jsonable(ApiError("rule_invalid", str(exc), 400))
        except Exception as exc:  # pragma: no cover - 兜底
            self.logger.exception("接口处理异常: %s %s", method, path)
            raise _jsonable(ApiError("internal", "服务内部错误: %s" % (str(exc) or exc.__class__.__name__), 500))

    def _authorized(self, headers: Dict[str, str], query: Dict[str, List[str]]) -> bool:
        token = str(self.cfg.get("security.api_token", "") or "").strip()
        if not token:
            return True
        given = ""
        for key, value in (headers or {}).items():
            if str(key).lower() == "x-api-token":
                given = str(value or "")
                break
        if not given:
            given = str(_first(query, "token", "") or "")
        try:
            return hmac.compare_digest(token, given)
        except (TypeError, ValueError):  # pragma: no cover
            return False

    # ------------------------------------------------------------------ #
    # 缓存与后台任务
    # ------------------------------------------------------------------ #
    def _cached(self, key: str, ttl: float, factory: Callable[[], Any]) -> Any:
        now = time.time()
        with self._lock:
            hit = self._cache.get(key)
            if hit is not None and now - hit[0] < float(ttl):
                return hit[1]
        value = factory()
        with self._lock:
            self._cache[key] = (now, value)
        return value

    def _invalidate(self, *keys: str) -> None:
        with self._lock:
            for key in keys:
                self._cache.pop(key, None)

    def _start_job(self, name: str, func: Callable[..., Any], *args: Any, **kwargs: Any) -> str:
        from .util import new_id

        job_id = new_id("job")

        def _runner() -> None:
            try:
                func(*args, **kwargs)
            except Exception:  # pragma: no cover - 后台任务兜底
                self.logger.exception("%s 后台任务失败", name)

        thread = threading.Thread(target=_runner, name="job-%s" % name, daemon=True)
        thread.start()
        return job_id

    # ------------------------------------------------------------------ #
    # 基础接口
    # ------------------------------------------------------------------ #
    def h_health(self, params, body, query):
        return {"status": "ok", "version": __version__, "app": APP_TITLE}

    def h_system_info(self, params, body, query):
        import platform
        import sys

        return {
            "version": __version__,
            "app": APP_TITLE,
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "data_dir": self.cfg.data_dir,
            "hostname": self.app.state.get("hostname", ""),
            "started_at": getattr(self.app, "started_at", ""),
            "uptime": max(0, int(time.time() - self.started_ts)),
            "sim": bool(self.cfg.is_sim()),
            "host": self.cfg.host,
            "port": self.cfg.port,
            "project": PROJECT_URL,
        }

    def h_status(self, params, body, query):
        state = self.app.state.snapshot()
        device_items: List[Dict[str, Any]] = []
        if not state.get("bluetooth.devices"):
            try:
                device_items = [device_dict(d) for d in self.app.bt.list_devices()]
            except Exception:
                device_items = []
        else:
            device_items = [device_dict(d) for d in state.get("bluetooth.devices") or []]
        connected = state.get("bluetooth.connected")
        if connected is not None:
            connected = device_dict(connected)
        else:
            for item in device_items:
                if item.get("connected"):
                    connected = item
                    break
        adapter = state.get("bluetooth.adapter") or {}
        if not isinstance(adapter, dict):
            adapter = {}
        adapter.setdefault("hci", self.cfg.get("bluetooth.adapter", "hci0"))

        probe = self._probe()
        engine_status = self._engine_status()
        rules_counts = self._rules_counts()
        tts_probe = self._tts_probe()
        player_current = None
        try:
            player_current = self.app.player.current()
        except Exception:
            player_current = None
        playing = bool(state.get("player.playing")) or bool(player_current and player_current.get("playing"))
        volume = state.get("player.volume", self.cfg.get("player.volume", 70))
        return {
            "version": __version__,
            "app": APP_TITLE,
            "uptime": max(0, int(time.time() - self.started_ts)),
            "started_at": getattr(self.app, "started_at", ""),
            "hostname": state.get("hostname", ""),
            "sim": bool(self.cfg.is_sim()),
            "bluetooth": {
                "available": bool(state.get("bluetooth.available")),
                "powered": bool(state.get("bluetooth.powered")),
                "adapter": adapter,
                "scanning": bool(state.get("bluetooth.scanning")),
                "connected": connected,
                "devices": device_items,
            },
            "player": {
                "playing": playing,
                "current": player_current,
                "volume": clamp_int(volume, 0, 100, 70),
                "backend": probe.get("backend", "none"),
                "probe": probe,
            },
            "rules": rules_counts,
            "engine": {
                "running": engine_status.get("running", 0),
                "queued": engine_status.get("queued", 0),
                "next_runs": engine_status.get("next_runs", []),
            },
            "tts": tts_probe,
            "ai": self._ai_probe(),
            "last_event": state.get("last_event"),
        }

    def h_settings_get(self, params, body, query):
        return {"config": self.cfg.as_public_dict()}

    def h_settings_put(self, params, body, query):
        patch = body.get("config") if isinstance(body, dict) else None
        if not isinstance(patch, dict):
            raise ApiError("bad_request", '请求体需要 {"config": {...}} 结构', 400)
        patch = json.loads(json.dumps(patch))  # 深拷贝，避免修改调用方数据
        patch.pop("version", None)
        token = patch.get("security", {}).get("api_token") if isinstance(patch.get("security"), dict) else None
        if isinstance(token, str) and token.strip() in ("******", "******"[:6] + "**"):
            patch["security"].pop("api_token", None)
        old_port, old_host = self.cfg.port, self.cfg.host
        old_poll = self.cfg.get("bluetooth.poll_interval")
        old_level = self.cfg.get("logging.level")
        self.cfg.update(patch)
        self.cfg.save()
        volume = clamp_int(self.cfg.get("player.volume", 70), 0, 100, 70)
        try:
            self.app.player.set_volume(volume)
        except Exception:
            pass
        self.app.state.set("player.volume", volume)
        self._invalidate("probe", "tts_probe")
        restart_required = bool(self.cfg.port != old_port or self.cfg.host != old_host)
        if self.cfg.get("bluetooth.poll_interval") != old_poll:
            restart_required = True
        if self.cfg.get("logging.level") != old_level:
            restart_required = True
        self.logger.info("设置已更新（重启生效: %s）", "是" if restart_required else "否")
        return {"config": self.cfg.as_public_dict(), "restart_required": restart_required}

    # ------------------------------------------------------------------ #
    # 蓝牙
    # ------------------------------------------------------------------ #
    def _bt_devices(self) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]], Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        try:
            items = [device_dict(d) for d in self.app.bt.list_devices()]
        except Exception as exc:
            self.logger.warning("读取蓝牙设备失败: %s", exc)
        adapter: Dict[str, Any] = {}
        try:
            info = self.app.bt.adapter_info() or {}
            if isinstance(info, dict):
                adapter = info
        except Exception as exc:
            self.logger.warning("读取适配器信息失败: %s", exc)
        connected = None
        for item in items:
            if item.get("connected"):
                connected = item
                break
        return items, connected, adapter

    def h_bt_devices(self, params, body, query):
        items, connected, adapter = self._bt_devices()
        return {"items": items, "connected": connected, "adapter": adapter}

    def h_bt_scan_start(self, params, body, query):
        timeout = clamp_int(body.get("timeout") or self.cfg.get("bluetooth.scan_timeout", 12), 3, 60, 12)
        with self._scan_lock:
            if self.app.state.get("bluetooth.scanning"):
                return {"job_id": self.app.state.get("bluetooth.scan_job", ""), "scanning": True}
            self.app.state.update(
                {
                    "bluetooth.scanning": True,
                    "bluetooth.discovered": [],
                    "bluetooth.scan_started_at": iso_now(),
                    "bluetooth.scan_error": "",
                }
            )
            job_id = self._start_job("scan", self._do_scan, timeout)
            self.app.state.set("bluetooth.scan_job", job_id)
        return {"job_id": job_id, "scanning": True}

    def _do_scan(self, timeout: int) -> None:
        try:
            devices = self.app.bt.scan(float(timeout))
            items = [device_dict(d) for d in devices]
            self.app.state.update(
                {
                    "bluetooth.scanning": False,
                    "bluetooth.discovered": items,
                    "bluetooth.scan_error": "",
                }
            )
            try:
                self.app.bus.publish(EV_BT_SCAN_DONE, {"count": len(items), "at": iso_now()})
            except Exception:  # pragma: no cover
                pass
            self.logger.info("蓝牙扫描完成，发现 %d 个设备", len(items))
        except Exception as exc:
            self.logger.warning("蓝牙扫描失败: %s", exc)
            self.app.state.update({"bluetooth.scanning": False, "bluetooth.scan_error": str(exc)})

    def h_bt_scan_status(self, params, body, query):
        return {
            "scanning": bool(self.app.state.get("bluetooth.scanning")),
            "items": self.app.state.get("bluetooth.discovered") or [],
            "started_at": self.app.state.get("bluetooth.scan_started_at", ""),
            "error": self.app.state.get("bluetooth.scan_error", ""),
        }

    def _address(self, body: Dict[str, Any]) -> str:
        address = str((body or {}).get("address") or "").strip().upper().replace("-", ":")
        if not address:
            raise ApiError("bad_request", "缺少设备地址 address", 400)
        if not re.fullmatch(r"([0-9A-F]{2}:){5}[0-9A-F]{2}", address):
            raise ApiError("bad_request", "设备地址格式不正确: %s" % address, 400)
        return address

    def _bt_action(self, action: str, address: str, func: Callable[[str], bool]) -> bool:
        try:
            ok = bool(func(address))
        except Exception as exc:
            self.logger.warning("%s 失败 (%s): %s", action, address, exc)
            raise ApiError("bluetooth_error", "%s失败：%s" % (action, str(exc) or "蓝牙服务不可用"), 503)
        if not ok:
            raise ApiError("bluetooth_error", "%s失败，请确认音箱已上电、处于可连接状态，并查看日志" % action, 409)
        self.logger.info("%s 成功: %s", action, address)
        return True

    def h_bt_connect(self, params, body, query):
        address = self._address(body)
        self._bt_action("连接音箱", address, self.app.bt.connect)
        self.app.state.update({"bluetooth.connected": {"address": address, "name": body.get("name", "")}})
        return {"address": address, "connected": True}

    def h_bt_disconnect(self, params, body, query):
        address = self._address(body)
        self._bt_action("断开音箱", address, self.app.bt.disconnect)
        current = self.app.state.get("bluetooth.connected")
        if isinstance(current, dict) and str(current.get("address", "")).upper() == address:
            self.app.state.set("bluetooth.connected", None)
        return {"address": address, "connected": False}

    def h_bt_pair(self, params, body, query):
        address = self._address(body)
        self._bt_action("配对设备", address, self.app.bt.pair)
        device: Dict[str, Any] = {"address": address, "name": str((body or {}).get("name") or "")}
        try:
            info = self.app.bt.device_info(address)
            if info is not None:
                device = device_dict(info)
        except Exception:  # pragma: no cover - 探测失败不影响配对结果
            pass
        try:
            self.app.bus.publish(
                EV_BT_PAIRED,
                {"address": address, "name": device.get("name", ""), "device": device, "at": iso_now()},
            )
        except Exception:  # pragma: no cover
            pass
        return {"address": address, "paired": True}

    def h_bt_set_default(self, params, body, query):
        address = self._address(body)
        self.cfg.set("bluetooth.default_device", address)
        self.cfg.save()
        self.logger.info("默认音箱设置为 %s", address)
        return {"default_device": address}

    def h_bt_remove(self, params, body, query):
        address = str(params.get("address") or "").strip().upper()
        self._bt_action("删除设备", address, self.app.bt.remove)
        return {"removed": True}

    # ------------------------------------------------------------------ #
    # 播放 / TTS
    # ------------------------------------------------------------------ #
    def _probe(self) -> Dict[str, Any]:
        def _factory() -> Dict[str, Any]:
            try:
                result = self.app.player.probe()
                return result if isinstance(result, dict) else {}
            except Exception as exc:
                return {"ready": False, "backend": "none", "tools": {}, "hint": "探测播放器失败: %s" % exc}

        return self._cached("probe", 30.0, _factory)

    def h_player_get(self, params, body, query):
        current = None
        try:
            current = self.app.player.current()
        except Exception:
            current = None
        playing = bool(current and current.get("playing"))
        try:
            playing = playing or bool(self.app.player.is_playing())
        except Exception:
            pass
        return {
            "playing": playing,
            "current": current,
            "volume": clamp_int(self.cfg.get("player.volume", 70), 0, 100, 70),
            "probe": self._probe(),
        }

    def h_player_play(self, params, body, query):
        source = str((body or {}).get("source") or "").strip()
        if not source:
            raise ApiError("bad_request", "缺少播放源 source（本地路径 / 声音库文件名 / http 地址）", 400)
        volume = body.get("volume")
        req = PlayRequest(
            source=source,
            label=str(body.get("label") or ""),
            volume=clamp_int(volume, 0, 100, 70) if volume is not None else None,
            loop=parse_bool(body.get("loop"), False),
            address=(str(body.get("address") or "").strip().upper() or None),
        )
        self.logger.info("手动播放: %s", source)
        job_id = self._start_job("play", self._do_play, req)
        return {"accepted": True, "job_id": job_id}

    def _do_play(self, req: PlayRequest) -> None:
        player = self.app.player
        try:
            if player.is_playing():
                player.stop()
                deadline = time.time() + 3.0
                while player.is_playing() and time.time() < deadline:
                    time.sleep(0.05)
            result = player.play(req)
        except Exception:
            self.logger.exception("播放失败: %s", getattr(req, "source", ""))
            return
        error = str(getattr(result, "error", "") or "")
        if error:
            self.logger.warning("播放失败: %s", error)

    def h_player_stop(self, params, body, query):
        try:
            self.app.player.stop()
        except Exception as exc:
            raise ApiError("player_error", "停止播放失败：%s" % exc, 503)
        self.app.state.update({"player.playing": False, "player.current": None})
        return {"stopped": True}

    def h_player_volume(self, params, body, query):
        level = clamp_int((body or {}).get("level"), 0, 100, -1)
        if level < 0:
            raise ApiError("bad_request", "缺少音量 level（0-100）", 400)
        self.app.player.set_volume(level)
        self.cfg.set("player.volume", level)
        self.cfg.save()
        self.app.state.set("player.volume", level)
        return {"volume": level}

    def h_tts_speak(self, params, body, query):
        text = str((body or {}).get("text") or "").strip()
        if not text:
            raise ApiError("bad_request", "缺少要播报的文本 text", 400)
        if len(text) > 1000:
            raise ApiError("bad_request", "播报文本过长（最多 1000 字）", 400)
        voice = str(body.get("voice") or "").strip() or None
        volume = body.get("volume")
        job_id = self._start_job(
            "tts",
            self._do_speak,
            text,
            voice,
            clamp_int(volume, 0, 100, 70) if volume is not None else None,
        )
        return {"accepted": True, "job_id": job_id}

    def _do_speak(self, text: str, voice: Optional[str], volume: Optional[int]) -> None:
        try:
            path = self.app.tts.synthesize(text, voice)
        except Exception as exc:
            self.logger.warning("TTS 合成失败: %s", exc)
            return
        try:
            req = PlayRequest(source=path, label="语音播报", volume=volume)
            self._do_play(req)
        finally:
            try:
                self.app.tts.cleanup()
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    # 规则
    # ------------------------------------------------------------------ #
    def _engine_status(self) -> Dict[str, Any]:
        engine = getattr(self.app, "engine", None)
        if engine is None:
            return {"running": 0, "queued": 0, "next_runs": [], "recent": []}
        try:
            return dict(engine.status() or {})
        except Exception as exc:
            self.logger.debug("读取引擎状态失败: %s", exc)
            return {"running": 0, "queued": 0, "next_runs": [], "recent": []}

    def _rules_counts(self) -> Dict[str, Any]:
        try:
            counts = self.app.rules.counts()
            return {"total": int(counts.get("total", 0)), "enabled": int(counts.get("enabled", 0))}
        except Exception:
            return {"total": 0, "enabled": 0}

    def _reload_rules(self) -> None:
        engine = getattr(self.app, "engine", None)
        reload_rules = getattr(engine, "reload_rules", None)
        if callable(reload_rules):
            try:
                reload_rules()
            except Exception:  # pragma: no cover
                self.logger.debug("重载规则失败", exc_info=True)

    def h_rules_list(self, params, body, query):
        return {"items": self.app.rules.list(), "counts": self._rules_counts()}

    def h_rules_create(self, params, body, query):
        rule = self.app.rules.add(dict(body or {}))
        self._reload_rules()
        self.logger.info("新建规则: %s (%s)", rule.get("name"), rule.get("id"))
        return rule

    def h_rules_get(self, params, body, query):
        rule = self.app.rules.get(params.get("rule_id", ""))
        if rule is None:
            raise ApiError("not_found", "规则不存在", 404)
        return rule

    def h_rules_update(self, params, body, query):
        rule_id = params.get("rule_id", "")
        if self.app.rules.get(rule_id) is None:
            raise ApiError("not_found", "规则不存在", 404)
        rule = self.app.rules.update(rule_id, dict(body or {}))
        self._reload_rules()
        return rule

    def h_rules_delete(self, params, body, query):
        if not self.app.rules.delete(params.get("rule_id", "")):
            raise ApiError("not_found", "规则不存在", 404)
        self._reload_rules()
        return {"deleted": True}

    def h_rules_run(self, params, body, query):
        rule_id = params.get("rule_id", "")
        rule = self.app.rules.get(rule_id)
        if rule is None:
            raise ApiError("not_found", "规则不存在", 404)
        engine = getattr(self.app, "engine", None)
        if engine is None:
            raise ApiError("engine_unavailable", "自动化引擎未启动", 503)
        result = engine.run_rule(rule_id, {"trigger": "manual"}) or {}
        if not result.get("accepted"):
            raise ApiError("rule_rejected", "规则未执行：%s" % (result.get("reason") or "被拒绝"), 409)
        return {"accepted": True, "job_id": result.get("job_id", ""), "rule_id": rule_id}

    def h_rules_toggle(self, params, body, query):
        rule_id = params.get("rule_id", "")
        rule = self.app.rules.get(rule_id)
        if rule is None:
            raise ApiError("not_found", "规则不存在", 404)
        enabled = parse_bool((body or {}).get("enabled"), not bool(rule.get("enabled")))
        updated = self.app.rules.set_enabled(rule_id, enabled)
        self._reload_rules()
        return updated

    def h_rules_export(self, params, body, query):
        return {"version": 1, "rules": self.app.rules.list()}

    def h_rules_import(self, params, body, query):
        rules = (body or {}).get("rules")
        mode = str((body or {}).get("mode") or "merge").lower()
        if not isinstance(rules, list) or not rules:
            raise ApiError("bad_request", '请求体需要 {"rules": [...]} 结构', 400)
        if mode not in ("merge", "replace"):
            raise ApiError("bad_request", "mode 只能是 merge 或 replace", 400)
        from .rules import normalize_rule, validate_rule

        errors: List[str] = []
        for index, raw in enumerate(rules):
            if not isinstance(raw, dict):
                errors.append("第 %d 条不是对象" % (index + 1))
                continue
            candidate = normalize_rule(dict(raw))
            for message in validate_rule(candidate):
                errors.append("第 %d 条（%s）: %s" % (index + 1, candidate.get("name", ""), message))
        if errors:
            raise ApiError("rule_invalid", "；".join(errors[:5]), 400)
        try:
            imported = self.app.rules.replace_all(rules) if mode == "replace" else [self.app.rules.add(dict(r)) for r in rules]
        except RuleError as exc:
            raise ApiError("rule_invalid", str(exc), 400)
        self._reload_rules()
        self.logger.info("导入规则 %d 条（mode=%s）", len(imported), mode)
        return {"imported": len(imported), "mode": mode}

    # ------------------------------------------------------------------ #
    # 声音库 / 日志 / 事件 / 诊断
    # ------------------------------------------------------------------ #
    def _library(self) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        seen = set()
        for directory in self.cfg.sounds_dirs():
            if not os.path.isdir(directory):
                continue
            for name in sorted(os.listdir(directory)):
                extension = os.path.splitext(name)[1].lower()
                if extension not in _AUDIO_EXTS:
                    continue
                path = os.path.join(directory, name)
                if path in seen or not os.path.isfile(path):
                    continue
                seen.add(path)
                try:
                    size = os.path.getsize(path)
                except OSError:
                    size = 0
                source = "builtin" if directory.startswith("/usr/share") else "user"
                items.append(
                    {
                        "name": name,
                        "path": path,
                        "size": size,
                        "size_text": human_size(size),
                        "source": source,
                    }
                )
        return items

    def h_library(self, params, body, query):
        return {"items": self._library(), "dirs": self.cfg.sounds_dirs()}

    # -- 保存：上传声音 / 配置备份 -------------------------------------- #
    @staticmethod
    def _safe_sound_name(name: str) -> str:
        """清洗上传文件名：去路径、去危险字符、限制长度，保留中文与扩展名。"""
        base = str(name or "").replace("\\", "/").split("/")[-1]
        stem, ext = os.path.splitext(base)
        stem = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", stem).strip(" .")
        stem = stem[:60] or "sound"
        ext = ext.lower()
        if not ext.startswith("."):
            ext = "." + ext if ext else ""
        return stem + ext

    @staticmethod
    def _unique_upload_path(directory: str, name: str) -> str:
        target = os.path.join(directory, name)
        if not os.path.exists(target):
            return target
        stem, ext = os.path.splitext(name)
        for index in range(1, 1000):
            candidate = os.path.join(directory, "%s-%d%s" % (stem, index, ext))
            if not os.path.exists(candidate):
                return candidate
        raise ApiError("conflict", "同名文件过多，请先清理声音库", 409)

    def h_library_upload(self, params, body, query):
        """把上传的音频字节保存到数据目录 sounds/（保存后立即出现在声音库）。"""
        raw_name = str(_first(query, "name", "") or "").strip()
        if not raw_name:
            raise ApiError("bad_request", "缺少文件名参数 name", 400)
        name = self._safe_sound_name(raw_name)
        extension = os.path.splitext(name)[1].lower()
        if extension not in _AUDIO_EXTS:
            raise ApiError(
                "bad_request",
                "不支持的音频格式：%s（支持 %s）" % (extension or "无扩展名", " / ".join(_AUDIO_EXTS)),
                400,
            )
        raw = body.get("_raw_bytes") if isinstance(body, dict) else b""
        if not raw:
            raise ApiError("bad_request", "上传内容为空", 400)
        max_mb = clamp_int(self.cfg.get("sounds.max_upload_mb", 20), 1, 200, 20)
        if len(raw) > max_mb * 1024 * 1024:
            raise ApiError("payload_too_large", "文件超过 %dMB 上限" % max_mb, 413)
        target_dir = os.path.join(self.cfg.data_dir, "sounds")
        try:
            os.makedirs(target_dir, exist_ok=True)
            target = self._unique_upload_path(target_dir, name)
            with open(target, "wb") as handle:
                handle.write(raw)
        except OSError as exc:
            self.logger.error("保存上传声音失败：%s", exc)
            raise ApiError("io_error", "保存失败：%s" % exc, 500)
        self.logger.info("声音已保存到库：%s（%s）", os.path.basename(target), human_size(len(raw)))
        return {
            "name": os.path.basename(target),
            "path": target,
            "size": len(raw),
            "size_text": human_size(len(raw)),
            "source": "user",
        }

    def h_backup(self, params, body, query):
        """导出配置备份（config.json + 全部规则），供 Web 下载保存。"""
        state = getattr(self.app, "state", None)
        hostname = ""
        try:
            hostname = state.get("hostname", "") if state is not None else ""
        except Exception:  # pragma: no cover
            hostname = ""
        content = {
            "app": APP_TITLE,
            "version": __version__,
            "exported_at": iso_now(),
            "hostname": hostname,
            "config": self.cfg.to_dict(),
            "rules": self.app.rules.list(),
        }
        filename = "btspeaker-backup-%s.json" % time.strftime("%Y%m%d-%H%M%S", time.localtime())
        self.logger.info("导出配置备份：%s（%d 条规则）", filename, len(content["rules"]))
        return {"filename": filename, "content": content}

    def h_logs_get(self, params, body, query):
        limit = clamp_int(_first(query, "limit", "200"), 1, 1000, 200)
        level = str(_first(query, "level", "") or "").strip().upper() or None
        ring = getattr(self.app, "logs", None) or get_log_ring()
        items = ring.records(limit, level) if ring else []
        total = ring.total() if ring else 0
        return {
            "items": items,
            "total": total,
            "capacity": int(self.cfg.get("logging.max_records", 500) or 500),
        }

    def h_logs_clear(self, params, body, query):
        ring = getattr(self.app, "logs", None) or get_log_ring()
        if ring:
            ring.clear()
        return {"cleared": True}

    def h_events(self, params, body, query):
        limit = clamp_int(_first(query, "limit", "50"), 1, 200, 50)
        engine = getattr(self.app, "engine", None)
        items: List[Dict[str, Any]] = []
        if engine is not None:
            try:
                items = list(engine.recent(limit) or [])
            except Exception:
                items = []
        return {"items": items}

    def h_diag(self, params, body, query):
        checks: List[Dict[str, Any]] = []

        def add(name: str, ok: bool, detail: str = "", hint: str = "") -> None:
            checks.append({"name": name, "ok": bool(ok), "detail": str(detail), "hint": str(hint)})

        add("服务运行", True, "%s v%s 已运行 %s" % (APP_TITLE, __version__, human_duration(time.time() - self.started_ts)))

        data_dir = self.cfg.data_dir
        try:
            writable = os.access(data_dir, os.W_OK)
        except Exception:
            writable = False
        add("数据目录可写", writable, data_dir, "" if writable else "检查目录权限：chmod 755 %s" % data_dir)

        cli = str(self.cfg.get("bluetooth.cli", "bluetoothctl") or "bluetoothctl")
        cli_path = which(cli)
        add("蓝牙工具", bool(cli_path), cli_path or ("未找到 %s" % cli), "" if cli_path else "opkg install bluez-utils")

        try:
            adapter = self.app.bt.adapter_info() or {}
            powered = bool(adapter.get("powered"))
            add(
                "蓝牙适配器",
                powered,
                "%s %s" % (adapter.get("address", "?"), adapter.get("name", "")),
                "" if powered else "检查 USB 蓝牙适配器（kmod-btusb）与供电，或执行 bluetoothctl show / power on",
            )
        except Exception as exc:
            add("蓝牙适配器", False, str(exc), "检查 bluez-daemon 是否运行：/etc/init.d/bluetooth status")

        try:
            connected = self.app.state.get("bluetooth.connected")
            if not connected:
                for device in self.app.bt.list_devices():
                    info = device_dict(device)
                    if info.get("connected"):
                        connected = info
                        break
            add("音箱连接", bool(connected), (connected or {}).get("name", "未连接"), "在「音箱」页扫描并连接蓝牙音箱" if not connected else "")
        except Exception as exc:
            add("音箱连接", False, str(exc), "")

        probe = self._probe()
        tools = probe.get("tools") or {}
        add("mpg123 解码器", bool(tools.get("mpg123")), "已安装" if tools.get("mpg123") else "未安装", "" if tools.get("mpg123") else "opkg install mpg123")
        add("aplay 播放器", bool(tools.get("aplay")), "已安装" if tools.get("aplay") else "未安装", "" if tools.get("aplay") else "opkg install alsa-utils")
        add(
            "BlueALSA 音频输出",
            bool(probe.get("bluealsa_pcm")),
            "可用" if probe.get("bluealsa_pcm") else "未检测到 bluealsa PCM",
            "" if probe.get("bluealsa_pcm") else "opkg install bluealsa，并确认 /etc/init.d/bluealsa 已启动",
        )
        tts_probe = self._tts_probe()
        add("TTS 语音播报", bool(tts_probe.get("ready")), "%s 后端" % tts_probe.get("backend", "none"), tts_probe.get("hint", ""))
        ai_probe = self._ai_probe()
        add(
            "AI 播报",
            bool(ai_probe.get("ready")),
            "模型 %s" % ai_probe.get("model", "-"),
            ai_probe.get("hint", ""),
        )

        engine_status = self._engine_status()
        add(
            "自动化引擎",
            bool(engine_status.get("scheduler_running", True)),
            "调度器%s，待执行 %d 条" % ("运行中" if engine_status.get("scheduler_running") else "未运行", int(engine_status.get("queued", 0))),
            "" if engine_status.get("scheduler_running") else "重启服务：/etc/init.d/btspeaker restart",
        )
        add("Web 服务", True, "%s:%s" % (self.cfg.host, self.cfg.port))
        return {"checks": checks, "sim": bool(self.cfg.is_sim()), "data_dir": data_dir}

    def _tts_probe(self) -> Dict[str, Any]:
        def _factory() -> Dict[str, Any]:
            try:
                result = self.app.tts.probe()
                return result if isinstance(result, dict) else {}
            except Exception as exc:
                return {"backend": "none", "ready": False, "hint": "探测 TTS 失败: %s" % exc}

        return self._cached("tts_probe", 30.0, _factory)

    # ------------------------------------------------------------------ #
    # AI 播报
    # ------------------------------------------------------------------ #
    def _ai_probe(self) -> Dict[str, Any]:
        def _factory() -> Dict[str, Any]:
            ai = getattr(self.app, "ai", None)
            if ai is None:
                return {"ready": False, "backend": "none", "hint": "AI 未初始化"}
            try:
                result = ai.probe()
                return result if isinstance(result, dict) else {}
            except Exception as exc:
                return {"ready": False, "backend": "none", "hint": "探测 AI 失败: %s" % exc}

        return self._cached("ai_probe", 10.0, _factory)

    def h_ai_get(self, params, body, query):
        """返回 AI 配置探测结果（不暴露 API Key）。"""
        return self._ai_probe()

    def h_ai_chat(self, params, body, query):
        """生成文本；可选 speak=true 直接通过 TTS + 音箱播报。"""
        prompt = str((body or {}).get("prompt") or "").strip()
        if not prompt:
            raise ApiError("bad_request", "缺少 prompt（要问 AI 的内容）", 400)
        system = str((body or {}).get("system") or "").strip() or None
        model = str((body or {}).get("model") or "").strip() or None
        ai = getattr(self.app, "ai", None)
        if ai is None:
            raise ApiError("ai_unavailable", "AI 未初始化", 503)
        try:
            text = ai.complete(prompt, system=system, model=model)
        except Exception as exc:
            raise ApiError("ai_error", "AI 调用失败：%s" % exc, 502)
        result: Dict[str, Any] = {"text": text, "spoken": False, "tts_error": ""}
        if parse_bool((body or {}).get("speak"), False):
            try:
                path = self.app.tts.synthesize(text)
                play_req = PlayRequest(source=str(path), label=text[:60])
                played = self.app.player.play(play_req)
                if bool(getattr(played, "ok", False)):
                    result["spoken"] = True
                else:
                    result["tts_error"] = str(getattr(played, "error", "") or "播放失败")
            except Exception as exc:
                result["tts_error"] = str(exc)
        return result

    # ------------------------------------------------------------------ #
    # 外部触发（webhook / 浏览器一键）
    # ------------------------------------------------------------------ #
    def h_hook(self, params, body, query):
        rule_id = params.get("rule_id", "")
        rule = self.app.rules.get(rule_id)
        if rule is None:
            raise ApiError("not_found", "规则不存在", 404)
        if not rule.get("enabled"):
            raise ApiError("rule_disabled", "规则已停用，请先在自动化页启用", 409)
        engine = getattr(self.app, "engine", None)
        if engine is None:
            raise ApiError("engine_unavailable", "自动化引擎未启动", 503)
        context = {key: value[0] if value else "" for key, value in (query or {}).items()}
        context["trigger"] = "api"
        result = engine.run_rule(rule_id, context) or {}
        if not result.get("accepted"):
            raise ApiError("rule_rejected", "规则未执行：%s" % (result.get("reason") or "被拒绝"), 409)
        self.logger.info("外部触发规则 %s", rule_id)
        return {"fired": True, "rule_id": rule_id, "job_id": result.get("job_id", "")}


def _jsonable(error: ApiError) -> ApiError:
    """让 ApiError 在 dispatch 外层被统一处理（保持类型）。"""
    return error


def _compile_routes(routes: List[Tuple[str, str, str]]):
    return [(method, re.compile(pattern), name) for method, pattern, name in routes]


_ROUTES = _compile_routes(
    [
        ("GET", r"/api/health", "health"),
        ("GET", r"/api/status", "status"),
        ("GET", r"/api/system/info", "system_info"),
        ("GET", r"/api/settings", "settings_get"),
        ("PUT", r"/api/settings", "settings_put"),
        ("GET", r"/api/bluetooth/devices", "bt_devices"),
        ("POST", r"/api/bluetooth/scan", "bt_scan_start"),
        ("GET", r"/api/bluetooth/scan", "bt_scan_status"),
        ("POST", r"/api/bluetooth/connect", "bt_connect"),
        ("POST", r"/api/bluetooth/disconnect", "bt_disconnect"),
        ("POST", r"/api/bluetooth/pair", "bt_pair"),
        ("POST", r"/api/bluetooth/default", "bt_set_default"),
        ("DELETE", r"/api/bluetooth/devices/(?P<address>[^/]+)", "bt_remove"),
        ("GET", r"/api/player", "player_get"),
        ("POST", r"/api/player/play", "player_play"),
        ("POST", r"/api/player/stop", "player_stop"),
        ("POST", r"/api/player/volume", "player_volume"),
        ("POST", r"/api/tts/speak", "tts_speak"),
        ("GET", r"/api/ai", "ai_get"),
        ("POST", r"/api/ai/chat", "ai_chat"),
        ("GET", r"/api/rules", "rules_list"),
        ("POST", r"/api/rules", "rules_create"),
        ("GET", r"/api/rules/export", "rules_export"),
        ("POST", r"/api/rules/import", "rules_import"),
        ("GET", r"/api/rules/(?P<rule_id>[^/]+)", "rules_get"),
        ("PUT", r"/api/rules/(?P<rule_id>[^/]+)", "rules_update"),
        ("DELETE", r"/api/rules/(?P<rule_id>[^/]+)", "rules_delete"),
        ("POST", r"/api/rules/(?P<rule_id>[^/]+)/run", "rules_run"),
        ("POST", r"/api/rules/(?P<rule_id>[^/]+)/toggle", "rules_toggle"),
        ("GET", r"/api/library", "library"),
        ("POST", r"/api/library/upload", "library_upload"),
        ("GET", r"/api/backup", "backup"),
        ("GET", r"/api/logs", "logs_get"),
        ("DELETE", r"/api/logs", "logs_clear"),
        ("GET", r"/api/events", "events"),
        ("GET", r"/api/diag", "diag"),
        ("POST", r"/api/hooks/(?P<rule_id>[^/]+)", "hook"),
        ("GET", r"/api/hooks/(?P<rule_id>[^/]+)", "hook"),
    ]
)


class _RequestHandler(BaseHTTPRequestHandler):
    """把 HTTP 请求转给 Api.dispatch，并托管静态文件。"""

    protocol_version = "HTTP/1.1"
    server_version = "BTSpeaker/1.0"
    sys_version = ""

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: N802 - 覆盖标准库
        try:
            self.api.logger.debug("[http] " + fmt, *args)
        except Exception:  # pragma: no cover
            pass

    @property
    def api(self) -> Api:
        return self.server.api  # type: ignore[attr-defined]

    # -- 方法入口 -------------------------------------------------------- #
    def do_GET(self) -> None:  # noqa: N802
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._handle("PUT")

    def do_DELETE(self) -> None:  # noqa: N802
        self._handle("DELETE")

    def do_HEAD(self) -> None:  # noqa: N802
        self._handle("GET", head_only=True)

    def _handle(self, method: str, head_only: bool = False) -> None:
        try:
            parsed = urllib.parse.urlsplit(self.path)
            path = urllib.parse.unquote(parsed.path or "/")
            query = urllib.parse.parse_qs(parsed.query or "")
            if path == "/api" or path.startswith("/api/"):
                try:
                    if method == "POST" and path.rstrip("/") == "/api/library/upload":
                        # 文件上传：请求体是原始音频字节，不是 JSON
                        max_mb = clamp_int(self.api.cfg.get("sounds.max_upload_mb", 20), 1, 200, 20)
                        limit = min(MAX_UPLOAD_ABS_BYTES, max_mb * 1024 * 1024)
                        raw = self._read_raw_body(limit)
                        status, payload = self.api.dispatch(
                            method, path, query, {}, dict(self.headers.items()), raw_body=raw
                        )
                    else:
                        body = self._read_json_body()
                        status, payload = self.api.dispatch(method, path, query, body, dict(self.headers.items()))
                except ApiError as exc:
                    status, payload = exc.status, {"ok": False, "error": {"code": exc.code, "message": exc.message}}
                self._send_json(status, payload, head_only=head_only)
            elif method in ("GET", "HEAD"):
                self._send_static(path, head_only=head_only)
            else:
                self._send_json(405, {"ok": False, "error": {"code": "method_not_allowed", "message": "请求方法不支持"}}, head_only=head_only)
        except (BrokenPipeError, ConnectionResetError):  # pragma: no cover
            return
        except Exception:  # pragma: no cover - 兜底
            try:
                self._send_json(500, {"ok": False, "error": {"code": "internal", "message": "服务内部错误"}}, head_only=head_only)
            except Exception:
                pass

    # -- 请求体 ---------------------------------------------------------- #
    def _read_raw_body(self, limit: int) -> bytes:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        if length <= 0:
            return b""
        if length > limit:
            self.close_connection = True
            raise ApiError("payload_too_large", "文件超过上传上限（%d 字节）" % limit, 413)
        return self.rfile.read(length)

    def _read_json_body(self) -> Dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        if length <= 0:
            return {}
        if length > MAX_BODY_BYTES:
            raise ApiError("payload_too_large", "请求体超过 2MB 限制", 413)
        raw = self.rfile.read(length)
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            raise ApiError("bad_request", "请求体不是合法 JSON", 400)
        if not isinstance(data, dict):
            raise ApiError("bad_request", "请求体必须是 JSON 对象", 400)
        return data

    # -- 响应 ------------------------------------------------------------ #
    def _send_json(self, status: int, payload: Any, head_only: bool = False) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(int(status))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def _web_dir(self) -> str:
        return os.path.abspath(self.api.cfg.web_dir)

    def _send_static(self, path: str, head_only: bool = False) -> None:
        web_dir = self._web_dir()
        if path in ("/", ""):
            relative = "index.html"
        elif path == "/favicon.ico":
            relative = "favicon.svg"
        else:
            relative = path.lstrip("/")
        target = os.path.normpath(os.path.join(web_dir, relative))
        if not (target == web_dir or target.startswith(web_dir + os.sep)):
            self._send_plain(403, "403 Forbidden", head_only=head_only)
            return
        if not os.path.isfile(target):
            self._send_plain(404, "404 Not Found", head_only=head_only)
            return
        content_type = mimetypes.guess_type(target)[0] or "application/octet-stream"
        if target.endswith(".js"):
            content_type = "text/javascript"
        if content_type.startswith("text/") or content_type in ("application/javascript", "image/svg+xml"):
            content_type += "; charset=utf-8"
        try:
            with open(target, "rb") as handle:
                body = handle.read()
        except OSError:
            self._send_plain(500, "500 Internal Server Error", head_only=head_only)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def _send_plain(self, status: int, text: str, head_only: bool = False) -> None:
        body = ("%s\n" % text).encode("utf-8")
        self.send_response(int(status))
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not head_only:
            self.wfile.write(body)


class _ThreadingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address, RequestHandlerClass, api: Api):  # noqa: N803
        self.api = api
        super().__init__(server_address, RequestHandlerClass)

    def server_bind(self) -> None:  # noqa: D401
        # 不用 HTTPServer.server_bind：它会调用 socket.getfqdn()，而 OpenWrt/iStoreOS 的
        # 精简 Python（python3-light）默认不带 encodings.idna，会抛 LookupError 导致启动失败。
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = int(port)


class ApiServer:
    """HTTP 服务生命周期封装。"""

    def __init__(self, app: Any, logger=None) -> None:
        self.app = app
        self.api = Api(app)
        self.logger = logger or get_logger("http")
        self._httpd: Optional[_ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    def start(self) -> None:
        with self._lock:
            if self._httpd is not None:
                return
            host = self.app.cfg.host
            port = self.app.cfg.port
            try:
                httpd = _ThreadingHTTPServer((host, port), _RequestHandler, self.api)
            except OSError as exc:
                raise OSError("无法监听 %s:%s（%s）" % (host, port, exc))
            self._httpd = httpd
            self.api.started_ts = time.time()
            actual_port = int(httpd.server_address[1])
            if actual_port != self.app.cfg.port:
                # 端口传 0 时由系统分配，回写内存配置以便 /api/system/info 展示真实端口
                self.app.cfg.set("server.port", actual_port)
            self._thread = threading.Thread(target=httpd.serve_forever, name="http-server", daemon=True)
            self._thread.start()
            self.logger.info("HTTP 服务已启动: http://%s:%s", host, actual_port)

    def stop(self) -> None:
        with self._lock:
            httpd = self._httpd
            thread = self._thread
            self._httpd = None
            self._thread = None
        if httpd is not None:
            try:
                httpd.shutdown()
            except Exception:  # pragma: no cover
                pass
            try:
                httpd.server_close()
            except Exception:  # pragma: no cover
                pass
        if thread is not None:
            try:
                thread.join(timeout=3)
            except Exception:  # pragma: no cover
                pass
        self.logger.info("HTTP 服务已停止")

    @property
    def port(self) -> int:
        if self._httpd is not None:
            return int(self._httpd.server_address[1])
        return self.app.cfg.port
