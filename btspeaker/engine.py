"""自动化引擎：规则执行器。

职责：
- worker 队列（上限 100，线程数 ``max(1, cfg.rules.max_concurrent)``）异步执行规则；
- ``execute_rule`` 同步执行：进入条件（启用 / 全局开关 / 冷却 / conditions）→
  动作顺序执行（单条失败不中断，整体 ok=false）→ 发布事件 → 记录 recent / 持久化；
- 订阅 ``bluetooth.connected/disconnected/paired`` 与 ``system.boot`` 事件，
  boot 规则按 ``delay`` 用 ``threading.Timer`` 延迟入队（``start()`` 本身不发布 system.boot）；
- 模板变量渲染（tts.text / http.url / http.body / log.message 等）；
- 命令动作安全开关（``security.allow_commands`` + ``command_allowlist`` 前缀）。

所有异常不外抛，记日志并写入 recent。
"""

from __future__ import annotations

import copy
import queue
import shlex
import threading
import time
import urllib.error
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Deque, Dict, List, Optional, Tuple
from urllib import request as urlrequest

from . import bus as bus_events
from .ai import AIClient
from .rules import WEEKDAY_CN, evaluate_conditions
from .scheduler import Scheduler
from .util import clamp_int, get_logger, hostname, iso_now, new_id, parse_bool, render_template, run_cmd

try:  # 优先复用 audio.PlayRequest；bt-audio 模块未就绪时用等价本地结构兜底
    from .audio import PlayRequest  # type: ignore
except Exception:  # pragma: no cover - 仅在 audio 模块不可用时走到

    @dataclass
    class PlayRequest:  # type: ignore
        """与 audio.PlayRequest 字段一致的兜底结构。"""

        source: str
        label: str = ""
        volume: Optional[int] = None
        loop: bool = False
        address: Optional[str] = None
        rule_id: Optional[str] = None


MAX_QUEUE_SIZE = 100
MAX_RECENT = 50
_MISSING = object()


def _clip(text: Any, limit: int = 120) -> str:
    value = str(text or "").strip()
    return value if len(value) <= limit else value[:limit] + "…"


class AutomationEngine:
    """规则自动化引擎（线程安全）。"""

    def __init__(self, cfg, bus, player, tts, bt, rules, state, persist=None, logger=None, ai=None):
        self.cfg, self.bus, self.player, self.tts, self.bt = cfg, bus, player, tts, bt
        self.rules, self.state, self.persist = rules, state, persist
        self.logger = logger or get_logger("engine")
        # AI 播报客户端：默认由配置构造，测试可注入假实现
        self.ai = ai if ai is not None else AIClient(cfg, self.logger)
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._queue = queue.Queue(maxsize=MAX_QUEUE_SIZE)  # type: queue.Queue
        self._workers = []  # type: List[threading.Thread]
        self._started = False
        self._running = 0
        self._recent = deque(maxlen=MAX_RECENT)  # type: Deque[Dict[str, Any]]
        self._bus_tokens = []  # type: List[str]
        self._timers = []  # type: List[threading.Timer]
        self._last_runs = {}  # type: Dict[str, Dict[str, Any]]
        self._counters = {"fired": 0, "ok": 0, "failed": 0, "rejected": 0}
        self._hostname_cache = ""
        self._scheduler = None  # type: Optional[Scheduler]
        self._scheduler_used = False
        self._load_persist()
        self._scheduler = self._make_scheduler()

    # -- 生命周期 -------------------------------------------------------- #
    def _make_scheduler(self) -> Scheduler:
        return Scheduler(
            self.cfg, self._scheduler_rules, self._on_schedule_due,
            state=self.state, logger=get_logger("scheduler"),
        )

    def start(self) -> None:
        """启动 worker 线程 + 订阅 bus + 启动调度器（本身不发布 system.boot）。"""
        with self._lock:
            if self._started:
                return
            self._stop_event.clear()
            if self._scheduler is None or self._scheduler_used:
                self._scheduler = self._make_scheduler()
            self._workers = []
            for index in range(self._concurrency()):
                thread = threading.Thread(
                    target=self._worker_loop, name="btspeaker-engine-%d" % (index + 1), daemon=True
                )
                thread.start()
                self._workers.append(thread)
            self._subscribe_bus()
            scheduler = self._scheduler
            if scheduler is not None:
                try:
                    scheduler.start()
                except Exception:  # pragma: no cover - 防御性
                    self.logger.exception("调度器启动失败")
            self._scheduler_used = True
            self._started = True
        self.logger.info(
            "自动化引擎已启动（worker=%d，规则 %d 条）", self._concurrency(), len(self._all_rules())
        )

    def stop(self) -> None:
        """停止调度器 / worker / 延时定时器，及时退出。"""
        with self._lock:
            was_started = self._started
            self._stop_event.set()
            self._started = False
            timers, self._timers = list(self._timers), []
            workers, self._workers = list(self._workers), []
            tokens, self._bus_tokens = list(self._bus_tokens), []
            scheduler = self._scheduler
        for timer in timers:
            try:
                timer.cancel()
            except Exception:  # pragma: no cover - 防御性
                pass
        if scheduler is not None:
            try:
                scheduler.stop()
            except Exception:  # pragma: no cover - 防御性
                self.logger.exception("停止调度器失败")
        for token in tokens:
            try:
                self.bus.unsubscribe(token)
            except Exception:  # pragma: no cover - 防御性
                pass
        for _ in workers:  # 唤醒阻塞在 queue.get 的 worker
            try:
                self._queue.put_nowait(None)
            except queue.Full:
                break
        for thread in workers:
            if thread is not threading.current_thread():
                try:
                    thread.join(timeout=2.0)
                except Exception:  # pragma: no cover - 防御性
                    pass
        while True:  # 丢弃队列里未执行的残留任务
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
            try:
                self._queue.task_done()
            except ValueError:  # pragma: no cover - 防御性
                pass
        if was_started:
            self.logger.info("自动化引擎已停止")

    @property
    def started(self) -> bool:
        return self._started

    def _concurrency(self) -> int:
        try:
            value = int(self.cfg.get("rules.max_concurrent", 1) or 1)
        except (TypeError, ValueError):
            value = 1
        return max(1, value)

    def _subscribe_bus(self) -> None:
        handlers = [
            (bus_events.EV_BT_CONNECTED, self._on_bluetooth_bus_event),
            (bus_events.EV_BT_DISCONNECTED, self._on_bluetooth_bus_event),
            (bus_events.EV_BT_PAIRED, self._on_bluetooth_bus_event),
            (bus_events.EV_SYSTEM_BOOT, self._on_boot_bus_event),
        ]
        for event, handler in handlers:
            try:
                self._bus_tokens.append(self.bus.subscribe(event, handler))
            except Exception:  # pragma: no cover - 防御性
                self.logger.exception("订阅事件失败: %s", event)

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                item = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                if item is None:
                    return
                with self._lock:
                    self._running += 1
                try:
                    self.execute_rule(item.get("rule"), item.get("context") or {})
                finally:
                    with self._lock:
                        self._running = max(0, self._running - 1)
            except Exception:  # pragma: no cover - execute_rule 内部已兜底
                self.logger.exception("worker 执行规则异常")
            finally:
                try:
                    self._queue.task_done()
                except ValueError:  # pragma: no cover - 防御性
                    pass

    # -- 异步 / 同步执行 -------------------------------------------------- #
    def run_rule(self, rule_id: Any, context: Optional[Dict[str, Any]] = None) -> dict:
        """异步入队执行，返回 {"accepted","job_id","reason"}。"""
        rule = self._get_rule(rule_id)
        if rule is None:
            return {"accepted": False, "job_id": "", "reason": "规则不存在：%s" % (rule_id or "")}
        if not parse_bool(self._cfg_get("rules.enabled", True), True):
            return {"accepted": False, "job_id": "", "reason": "自动化规则已在设置中关闭（rules.enabled=false）"}
        if not rule.get("enabled", True):
            return {"accepted": False, "job_id": "", "reason": "规则已停用：%s" % (rule.get("name") or rule_id)}
        ctx = dict(context or {})
        job_id = new_id("job")
        ctx["job_id"] = job_id
        ctx.setdefault("trigger", "manual")
        item = {"job_id": job_id, "rule": copy.deepcopy(rule), "context": ctx, "queued_at": time.time()}
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            self._bump("rejected")
            self.logger.warning("规则执行队列已满（上限 %d），拒绝：%s", MAX_QUEUE_SIZE, rule.get("name"))
            return {"accepted": False, "job_id": "", "reason": "执行队列已满（上限 %d），请稍后再试" % MAX_QUEUE_SIZE}
        self.logger.info("规则已入队：%s（job=%s，trigger=%s）", rule.get("name") or rule_id, job_id, ctx.get("trigger"))
        return {"accepted": True, "job_id": job_id, "reason": ""}

    def execute_rule(self, rule: Any, context: Optional[Dict[str, Any]] = None) -> dict:
        """同步执行规则，返回契约 §11 结构。"""
        context = dict(context or {})
        started_ts = time.time()
        rule_data = self._resolve_rule(rule)
        result = {
            "ok": False, "rule_id": "", "rule_name": "", "trigger": str(context.get("trigger") or ""),
            "started_at": iso_now(), "duration": 0.0, "actions": [], "error": "",
        }  # type: Dict[str, Any]
        if rule_data is None:
            result["error"] = "规则不存在：%s" % (rule if isinstance(rule, str) else "")
            return result
        rule_id = str(rule_data.get("id") or "")
        trigger = rule_data.get("trigger")
        job_id = str(context.get("job_id") or "")
        result["rule_id"] = rule_id
        result["rule_name"] = str(rule_data.get("name") or "")
        if not result["trigger"]:
            result["trigger"] = str((trigger or {}).get("type") or "") if isinstance(trigger, dict) else ""

        rejection = self._check_entry(rule_data)  # 进入条件
        if rejection:
            self._bump("rejected")
            result["error"] = rejection
            result["duration"] = round(time.time() - started_ts, 3)
            self.logger.info("规则未执行：%s（%s）", result["rule_name"] or rule_id, rejection)
            self._record_recent(result, job_id)
            return result

        self._record_last_run(rule_id)  # 冷却从现在开始计时
        render_ctx = self.render_context(rule_data, context)
        self._bump("fired")
        self._publish(bus_events.EV_RULE_FIRED, {
            "rule_id": rule_id, "rule_name": result["rule_name"],
            "trigger": result["trigger"], "job_id": job_id,
        })

        actions = rule_data.get("actions") if isinstance(rule_data.get("actions"), list) else []
        all_ok = True
        for index, action in enumerate(actions):
            if self._stop_event.is_set():
                result["actions"].append({
                    "type": "abort", "ok": False,
                    "message": "引擎正在停止，剩余 %d 个动作未执行" % (len(actions) - index),
                })
                all_ok = False
                break
            if not isinstance(action, dict):
                result["actions"].append({"type": "", "ok": False, "message": "第 %d 个动作格式不正确" % (index + 1)})
                all_ok = False
                continue
            action_type = str(action.get("type") or "").strip().lower()
            try:
                ok, message = self._run_action(action, render_ctx, rule_data, context)
            except Exception as exc:  # pragma: no cover - 单动作异常不中断
                self.logger.exception("规则 %s 的动作 %s 执行异常", rule_id, action_type)
                ok, message = False, "动作执行异常：%s" % exc
            result["actions"].append({"type": action_type, "ok": bool(ok), "message": str(message)})
            if ok:
                self.logger.info("  动作 %d/%d %s 成功：%s", index + 1, len(actions), action_type, message)
            else:
                all_ok = False
                self.logger.warning("  动作 %d/%d %s 失败：%s", index + 1, len(actions), action_type, message)

        duration = round(time.time() - started_ts, 3)
        result["ok"] = bool(all_ok)
        result["duration"] = duration
        if all_ok:
            self._bump("ok")
            self._publish(bus_events.EV_RULE_FINISHED, {
                "rule_id": rule_id, "rule_name": result["rule_name"], "trigger": result["trigger"],
                "job_id": job_id, "ok": True, "duration": duration,
            })
        else:
            failures = [item["message"] for item in result["actions"] if not item["ok"]]
            result["error"] = failures[0] if failures else "部分动作执行失败"
            self._bump("failed")
            self._publish(bus_events.EV_RULE_ERROR, {
                "rule_id": rule_id, "rule_name": result["rule_name"], "trigger": result["trigger"],
                "job_id": job_id, "error": result["error"],
            })
        self._record_recent(result, job_id)
        self.logger.info(
            "规则执行%s：%s（%.3fs，%d 个动作）", "成功" if all_ok else "失败",
            result["rule_name"] or rule_id, duration, len(result["actions"]),
        )
        return result

    # -- 进入条件 / 冷却 / 连接状态 --------------------------------------- #
    def _check_entry(self, rule: dict) -> str:
        """返回拒绝原因（空字符串表示可以执行）。"""
        if not rule.get("enabled", True):
            return "规则已停用"
        if not parse_bool(self._cfg_get("rules.enabled", True), True):
            return "自动化规则已在设置中关闭（rules.enabled=false）"
        cooldown = self._cooldown(rule)
        if cooldown > 0:
            last = self._last_runs.get(str(rule.get("id") or ""))
            if isinstance(last, dict):
                try:
                    elapsed = time.time() - float(last.get("ts") or 0)
                except (TypeError, ValueError):
                    elapsed = cooldown
                remaining = cooldown - elapsed
                if remaining > 0:
                    return "规则冷却中，还需等待 %d 秒" % max(1, int(remaining))
        conditions = rule.get("conditions")
        if isinstance(conditions, list) and conditions:
            ok, detail = evaluate_conditions(conditions, self._is_bt_connected())
            if not ok:
                return "条件不满足：%s" % detail
        return ""

    def _cooldown(self, rule: dict) -> int:
        """规则冷却秒数；规则未配置（0）时回退到 rules.default_cooldown。"""
        value = rule.get("cooldown")
        try:
            cooldown = int(float(value))
        except (TypeError, ValueError):
            cooldown = 0
        if cooldown <= 0:
            try:
                cooldown = int(float(self._cfg_get("rules.default_cooldown", 0) or 0))
            except (TypeError, ValueError):
                cooldown = 0
        return max(0, cooldown)

    def _is_bt_connected(self) -> bool:
        device, has_key = self._state_connected()
        if has_key:
            return device is not None
        try:  # state 没有维护连接信息时回退查询蓝牙后端
            for item in self.bt.list_devices() or []:
                if (item.get("connected") if isinstance(item, dict) else getattr(item, "connected", False)):
                    return True
        except Exception:  # pragma: no cover - 后端不可用视为未连接
            return False
        return False

    # -- 动作执行 -------------------------------------------------------- #
    def _run_action(self, action: dict, ctx: Dict[str, Any], rule: dict, context: Dict[str, Any]) -> Tuple[bool, str]:
        atype = str(action.get("type") or "").strip().lower()
        handlers = {
            "play": lambda: self._act_play(action, ctx, rule),
            "tts": lambda: self._act_tts(action, ctx, rule),
            "volume": lambda: self._act_volume(action),
            "connect": lambda: self._act_connect(action, ctx),
            "disconnect": lambda: self._act_disconnect(action, ctx),
            "command": lambda: self._act_command(action, ctx),
            "http": lambda: self._act_http(action, ctx),
            "wait": lambda: self._act_wait(action),
            "log": lambda: self._act_log(action, ctx, rule),
            "ai": lambda: self._act_ai(action, ctx, rule),
        }
        handler = handlers.get(atype)
        if handler is None:
            return False, "未知动作类型：%s" % (atype or "空")
        return handler()

    def _act_play(self, action: dict, ctx: Dict[str, Any], rule: dict) -> Tuple[bool, str]:
        source = self._render(action.get("source"), ctx)
        if not source:
            return False, "play 动作缺少 source"
        label = self._render(action.get("label"), ctx) or source
        request = self._make_play_request(
            source=source, label=label, volume=self._opt_volume(action.get("volume")),
            loop=parse_bool(action.get("loop"), False),
            address=self._render(action.get("address"), ctx).upper() or None,
            rule_id=str(rule.get("id") or ""),
        )
        try:
            result = self.player.play(request)
        except Exception as exc:
            return False, "播放失败：%s" % exc
        if bool(getattr(result, "ok", False)):
            return True, "已播放：%s" % _clip(label, 60)
        error = str(getattr(result, "error", "") or "")
        skipped = bool(getattr(result, "skipped", False))
        return False, "播放失败：%s" % (error or ("播放被中断" if skipped else "未知错误"))

    def _act_tts(self, action: dict, ctx: Dict[str, Any], rule: dict) -> Tuple[bool, str]:
        text = self._render(action.get("text"), ctx).strip()
        if not text:
            return False, "tts 动作缺少 text"
        if self.tts is None:
            return False, "TTS 未配置（请先安装 espeak 或配置 TTS 服务）"
        voice = str(action.get("voice") or "").strip() or None
        try:
            path = self.tts.synthesize(text, voice)
        except Exception as exc:
            return False, "TTS 合成失败：%s" % exc
        request = self._make_play_request(
            source=str(path), label=_clip(text, 60), volume=self._opt_volume(action.get("volume")),
            loop=False, address=None, rule_id=str(rule.get("id") or ""),
        )
        try:
            result = self.player.play(request)
        except Exception as exc:
            return False, "TTS 播放失败：%s" % exc
        if not bool(getattr(result, "ok", False)):
            return False, "TTS 播放失败：%s" % (str(getattr(result, "error", "") or "") or "未知错误")
        return True, "已播报：%s" % _clip(text, 40)

    def _act_ai(self, action: dict, ctx: Dict[str, Any], rule: dict) -> Tuple[bool, str]:
        """AI 播报：大模型生成文本 → TTS 合成 → 播放。"""
        prompt = self._render(action.get("prompt"), ctx).strip()
        if not prompt:
            return False, "ai 动作缺少 prompt"
        if self.ai is None:  # pragma: no cover - 理论上不会为 None
            return False, "AI 未初始化"
        system = self._render(action.get("system"), ctx).strip() or None
        model = str(action.get("model") or "").strip() or None
        try:
            text = self.ai.complete(prompt, system=system, model=model)
        except Exception as exc:
            return False, "AI 调用失败：%s" % exc
        text = str(text or "").strip()
        if not text:
            return False, "AI 返回了空文本"
        if self.tts is None:
            return False, "AI 已生成文本，但 TTS 未配置，无法播报"
        voice = str(action.get("voice") or "").strip() or None
        try:
            path = self.tts.synthesize(text, voice)
        except Exception as exc:
            return False, "AI 语音合成失败：%s" % exc
        request = self._make_play_request(
            source=str(path), label=_clip(text, 60), volume=self._opt_volume(action.get("volume")),
            loop=False, address=None, rule_id=str(rule.get("id") or ""),
        )
        try:
            result = self.player.play(request)
        except Exception as exc:
            return False, "AI 播报播放失败：%s" % exc
        if not bool(getattr(result, "ok", False)):
            return False, "AI 播报播放失败：%s" % (str(getattr(result, "error", "") or "") or "未知错误")
        return True, "AI 播报：%s" % _clip(text, 60)

    def _act_volume(self, action: dict) -> Tuple[bool, str]:
        try:
            level = int(float(action.get("level")))
        except (TypeError, ValueError):
            return False, "volume 动作的 level 必须是 0~100 的整数"
        if level < 0 or level > 100:
            return False, "volume 动作的 level 必须在 0~100 之间（当前 %d）" % level
        try:
            actual = self.player.set_volume(level)
        except Exception as exc:
            return False, "设置音量失败：%s" % exc
        try:
            actual = int(actual)
        except (TypeError, ValueError):
            actual = level
        if self.state is not None:
            try:
                self.state.set("player.volume", actual)
            except Exception:  # pragma: no cover - 防御性
                pass
        try:  # 持久化到 config.json，重启后保持
            self.cfg.set("player.volume", actual)
            self.cfg.save()
        except Exception:  # pragma: no cover - 配置保存失败不影响执行
            self.logger.warning("保存音量到配置失败", exc_info=True)
        return True, "音量已设为 %d" % actual

    def _act_connect(self, action: dict, ctx: Dict[str, Any]) -> Tuple[bool, str]:
        address = self._resolve_address(action, ctx)
        if not address:
            return False, "connect 动作未指定 address，且没有默认设备（bluetooth.default_device）"
        if self.bt is None:
            return False, "蓝牙后端不可用"
        try:
            ok = bool(self.bt.connect(address))
        except Exception as exc:
            return False, "连接蓝牙音箱失败：%s" % exc
        return (True, "已连接 %s" % address) if ok else (
            False, "连接蓝牙音箱失败：%s（请确认音箱已配对且在范围内）" % address)

    def _act_disconnect(self, action: dict, ctx: Dict[str, Any]) -> Tuple[bool, str]:
        address = self._render(action.get("address"), ctx).strip().upper()
        if not address:
            device, _ = self._state_connected()
            if isinstance(device, dict):
                address = str(device.get("address") or "").upper()
            elif device is not None:
                address = str(getattr(device, "address", "") or "").upper()
        if not address:
            address = str(self._cfg_get("bluetooth.default_device", "") or "").strip().upper()
        if not address:
            return False, "disconnect 动作未指定 address，且没有已连接/默认设备"
        if self.bt is None:
            return False, "蓝牙后端不可用"
        try:
            ok = bool(self.bt.disconnect(address))
        except Exception as exc:
            return False, "断开蓝牙音箱失败：%s" % exc
        return (True, "已断开 %s" % address) if ok else (False, "断开蓝牙音箱失败：%s" % address)

    def _act_command(self, action: dict, ctx: Dict[str, Any]) -> Tuple[bool, str]:
        if not parse_bool(self._cfg_get("security.allow_commands", False), False):
            return False, "未开启命令执行权限（security.allow_commands=false）"
        command = self._render(action.get("command"), ctx).strip()
        if not command:
            return False, "command 动作缺少 command"
        prefixes = self._allowlist()
        if prefixes and not any(command.startswith(prefix) for prefix in prefixes):
            return False, "命令不在允许列表内（command_allowlist）：%s" % _clip(command, 100)
        try:
            argv = shlex.split(command, posix=True)
        except ValueError as exc:
            return False, "命令解析失败：%s" % exc
        if not argv:
            return False, "命令为空"
        try:
            result = run_cmd(argv, timeout=self._command_timeout(action))
        except Exception as exc:  # pragma: no cover - run_cmd 内部已兜底
            return False, "命令执行异常：%s" % exc
        if not result.ok:
            detail = (result.stderr or result.stdout or "").strip()
            return False, "命令执行失败（退出码 %d）：%s" % (
                result.returncode, _clip(detail, 200) if detail else "退出码 %d" % result.returncode)
        stdout = (result.stdout or "").strip()
        return True, "命令执行成功" + ("：%s" % _clip(stdout, 100) if stdout else "")

    def _act_http(self, action: dict, ctx: Dict[str, Any]) -> Tuple[bool, str]:
        url = self._render(action.get("url"), ctx).strip()
        if not url:
            return False, "http 动作缺少 url"
        if not (url.startswith("http://") or url.startswith("https://")):
            return False, "http 动作的 url 必须以 http:// 或 https:// 开头"
        method = str(action.get("method") or "GET").strip().upper() or "GET"
        headers = {}  # type: Dict[str, str]
        raw_headers = action.get("headers")
        if isinstance(raw_headers, dict):
            for key, value in raw_headers.items():
                if str(key).strip():
                    headers[str(key)] = self._render(value, ctx)
        data = None  # type: Optional[bytes]
        body = self._render(action.get("body"), ctx)
        if body and method not in ("GET", "HEAD"):
            data = body.encode("utf-8")
            if not any(key.lower() == "content-type" for key in headers):
                headers["Content-Type"] = "text/plain; charset=utf-8"
        request = urlrequest.Request(url, data=data, headers=headers, method=method)
        try:
            response = urlrequest.urlopen(request, timeout=self._http_timeout(action))
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = _clip(exc.read(240).decode("utf-8", "replace").strip(), 100)
            except Exception:  # pragma: no cover - 读取错误体失败
                detail = ""
            return False, "HTTP 请求失败：HTTP %d %s%s" % (
                exc.code, getattr(exc, "reason", "") or "", ("：" + detail) if detail else "")
        except urllib.error.URLError as exc:
            return False, "HTTP 请求失败：%s" % (getattr(exc, "reason", exc),)
        except OSError as exc:
            return False, "HTTP 请求失败：%s" % exc
        try:
            status = int(getattr(response, "status", 200) or 200)
        except Exception:  # pragma: no cover - 防御性
            status = 200
        try:
            response.read(4096)
        except Exception:  # pragma: no cover - 响应体读取失败不影响判定
            pass
        finally:
            try:
                response.close()
            except Exception:  # pragma: no cover - 防御性
                pass
        if status >= 400:
            return False, "HTTP 请求失败：HTTP %d" % status
        return True, "HTTP %s 请求成功（HTTP %d）" % (method, status)

    def _act_wait(self, action: dict) -> Tuple[bool, str]:
        try:
            seconds = float(action.get("seconds"))
        except (TypeError, ValueError):
            return False, "wait 动作的 seconds 必须是数字"
        seconds = max(0.0, min(300.0, seconds))
        if seconds <= 0:
            return True, "无需等待"
        if self._stop_event.wait(seconds):
            return True, "等待被中断（引擎正在停止）"
        return True, "已等待 %.1f 秒" % seconds

    def _act_log(self, action: dict, ctx: Dict[str, Any], rule: dict) -> Tuple[bool, str]:
        message = self._render(action.get("message"), ctx)
        if not message:
            return False, "log 动作缺少 message"
        self.logger.info("规则日志[%s]：%s", rule.get("name") or rule.get("id"), message)
        return True, "已记录日志：%s" % _clip(message, 100)

    # -- 动作辅助 -------------------------------------------------------- #
    @staticmethod
    def _make_play_request(**kwargs: Any) -> Any:
        try:
            return PlayRequest(**kwargs)
        except TypeError:  # pragma: no cover - 兼容字段更少的旧版 PlayRequest
            kwargs.pop("rule_id", None)
            return PlayRequest(**kwargs)

    @staticmethod
    def _opt_volume(value: Any) -> Optional[int]:
        if value is None:
            return None
        try:
            return max(0, min(100, int(float(value))))
        except (TypeError, ValueError):
            return None

    def _resolve_address(self, action: dict, ctx: Dict[str, Any]) -> str:
        address = self._render(action.get("address"), ctx).strip().upper()
        return address or str(self._cfg_get("bluetooth.default_device", "") or "").strip().upper()

    def _allowlist(self) -> List[str]:
        raw = self._cfg_get("security.command_allowlist", [])
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, list):
            return []
        return [str(item).strip() for item in raw if str(item).strip()]

    def _command_timeout(self, action: dict) -> float:
        raw = action.get("timeout")
        if raw is None:
            raw = self._cfg_get("security.command_timeout", 20)
        return float(clamp_int(raw, 1, 300, 20))

    def _http_timeout(self, action: dict) -> float:
        return float(clamp_int(action.get("timeout", 10), 1, 120, 10))

    def _render(self, value: Any, ctx: Dict[str, Any]) -> str:
        return "" if value is None else render_template(str(value), ctx)

    # -- 模板上下文 ------------------------------------------------------ #
    def render_context(self, rule: Optional[dict] = None, payload: Optional[Dict[str, Any]] = None) -> dict:
        """构造模板变量上下文，见 docs/RULES.md「模板变量」。"""
        data = dict(payload or {})
        now = datetime.now()
        bt_name, bt_addr = self._connected_info(data)
        ctx = {str(key): value for key, value in data.items()
               if isinstance(value, (str, int, float, bool))}  # type: Dict[str, Any]
        ctx.update({
            "time": now.strftime("%H:%M"),
            "date": now.strftime("%Y-%m-%d"),
            "datetime": now.strftime("%Y-%m-%d %H:%M:%S"),
            "weekday": WEEKDAY_CN[now.weekday()],
            "hostname": self._hostname(),
            "bt_name": bt_name,
            "bt_addr": bt_addr,
            "volume": self._current_volume(),
            "rule_name": str((rule or {}).get("name") or "") if isinstance(rule, dict) else "",
            "event": str(data.get("event") or data.get("trigger") or ""),
        })
        return ctx

    def _hostname(self) -> str:
        if not self._hostname_cache:
            self._hostname_cache = hostname()
        return self._hostname_cache

    def _current_volume(self) -> int:
        try:
            return int(self.player.get_volume())
        except Exception:
            return int(clamp_int(self._cfg_get("player.volume", 70), 0, 100, 70))

    def _state_connected(self) -> Tuple[Any, bool]:
        """返回 (连接设备, state 是否有该键)。"""
        if self.state is None:
            return None, False
        try:
            snapshot = self.state.snapshot() if hasattr(self.state, "snapshot") else {}
        except Exception:  # pragma: no cover - 防御性
            snapshot = None
        if isinstance(snapshot, dict) and "bluetooth.connected" in snapshot:
            return snapshot.get("bluetooth.connected"), True
        try:
            device = self.state.get("bluetooth.connected", _MISSING)
        except Exception:  # pragma: no cover - 防御性
            device = _MISSING
        return (None, False) if device is _MISSING else (device, True)

    def _connected_info(self, payload: Optional[Dict[str, Any]] = None) -> Tuple[str, str]:
        data = payload or {}
        device, _ = self._state_connected()
        name = address = ""
        if isinstance(device, dict):
            name, address = str(device.get("name") or ""), str(device.get("address") or "")
        elif device is not None:
            name = str(getattr(device, "name", "") or "")
            address = str(getattr(device, "address", "") or "")
        name = name or str(data.get("name") or "")
        address = address or str(data.get("address") or "")
        return name, address.upper()

    # -- 事件订阅 -------------------------------------------------------- #
    def _on_bluetooth_bus_event(self, event: str, payload: Dict[str, Any]) -> None:
        event_name = str(event or "")
        suffix = event_name.rsplit(".", 1)[-1] if "." in event_name else event_name
        data = dict(payload or {})
        address = str(data.get("address") or "").strip().upper()
        name = str(data.get("name") or "").strip()
        matched = 0
        for rule in self._all_rules():
            if not rule.get("enabled", True):
                continue
            trigger = rule.get("trigger")
            if not isinstance(trigger, dict) or str(trigger.get("type")) != "bluetooth":
                continue
            if str(trigger.get("event") or "").strip().lower() != suffix:
                continue
            filter_device = str(trigger.get("device") or "").strip().upper()
            if filter_device and (not address or filter_device != address):
                continue
            context = {"trigger": "bluetooth", "event": suffix, "address": address, "name": name}
            self.run_rule(str(rule.get("id") or ""), context)
            matched += 1
        if matched:
            self.logger.info("蓝牙事件 %s 触发 %d 条规则", event_name, matched)

    def _on_boot_bus_event(self, event: str, payload: Dict[str, Any]) -> None:
        matched = 0
        for rule in self._all_rules():
            if not rule.get("enabled", True):
                continue
            trigger = rule.get("trigger")
            if not isinstance(trigger, dict) or str(trigger.get("type")) != "boot":
                continue
            rule_id = str(rule.get("id") or "")
            try:
                delay = int(float(trigger.get("delay") or 0))
            except (TypeError, ValueError):
                delay = 0
            delay = max(0, min(3600, delay))
            context = {"trigger": "boot", "event": "system.boot"}
            if delay <= 0:
                self.run_rule(rule_id, context)
            else:
                timer = threading.Timer(delay, self._boot_fire, args=(rule_id, context))
                timer.daemon = True
                with self._lock:
                    self._timers = [item for item in self._timers if item.is_alive()]
                    self._timers.append(timer)
                timer.start()
                self.logger.info("开机规则 %s 将在 %d 秒后触发", rule.get("name") or rule_id, delay)
            matched += 1
        self.logger.info("system.boot 事件匹配 %d 条开机规则", matched)

    def _boot_fire(self, rule_id: str, context: Dict[str, Any]) -> None:
        if self._stop_event.is_set():
            return
        rule = self._get_rule(rule_id)
        if rule is not None and rule.get("enabled", True):
            self.run_rule(rule_id, context)

    def _on_schedule_due(self, rule: dict, source: str) -> None:
        rule_id = str((rule or {}).get("id") or "")
        if rule_id:
            self.run_rule(rule_id, {"trigger": source or "schedule"})

    def _scheduler_rules(self) -> List[dict]:
        return [rule for rule in self._all_rules() if rule.get("enabled", True)]

    # -- 查询 / 管理 ----------------------------------------------------- #
    def _all_rules(self) -> List[dict]:
        source = self.rules
        if source is None:
            return []
        items = None
        if callable(getattr(source, "list", None)):
            try:
                items = source.list()
            except Exception:  # pragma: no cover - 防御性
                self.logger.exception("读取规则列表失败")
                return []
        elif isinstance(source, list):
            items = source
        elif callable(source):  # pragma: no cover - 允许注入 provider
            try:
                items = source()
            except Exception:
                return []
        return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []

    def _get_rule(self, rule_id: Any) -> Optional[dict]:
        target = str(rule_id or "")
        if not target:
            return None
        source = self.rules
        if source is not None and callable(getattr(source, "get", None)):
            try:
                rule = source.get(target)
            except Exception:  # pragma: no cover - 防御性
                rule = None
            if isinstance(rule, dict):
                return rule
        for item in self._all_rules():
            if str(item.get("id") or "") == target:
                return item
        return None

    def _resolve_rule(self, rule: Any) -> Optional[dict]:
        if isinstance(rule, dict):
            return rule
        return self._get_rule(rule) if isinstance(rule, str) else None

    def reload_rules(self) -> None:
        scheduler = self._scheduler
        if scheduler is not None:
            try:
                scheduler.reload()
            except Exception:  # pragma: no cover - 防御性
                self.logger.exception("调度器重载失败")
        self.logger.info("规则已重载（共 %d 条）", len(self._all_rules()))

    def status(self) -> dict:
        scheduler = self._scheduler
        next_runs = []  # type: List[dict]
        if scheduler is not None:
            try:
                next_runs = list(scheduler.next_runs(5) or [])
            except Exception:  # pragma: no cover - 防御性
                next_runs = []
        with self._lock:
            running, queued = self._running, self._queue.qsize()
            workers = len([thread for thread in self._workers if thread.is_alive()])
            counters = dict(self._counters)
        return {
            "started": self._started, "running": int(running), "queued": int(queued),
            "workers": workers,
            "scheduler_running": bool(scheduler is not None and scheduler.running),
            "next_runs": next_runs, "recent": self.recent(5), "counters": counters,
        }

    def recent(self, limit: int = 20) -> List[dict]:
        """最近执行记录，最新的在前。"""
        try:
            count = max(1, int(limit))
        except (TypeError, ValueError):
            count = 20
        with self._lock:
            items = list(self._recent)
        return copy.deepcopy(items[-count:][::-1])

    # -- 记录 / 持久化 --------------------------------------------------- #
    def _record_recent(self, result: Dict[str, Any], job_id: str) -> None:
        entry = {
            "job_id": job_id, "rule_id": str(result.get("rule_id") or ""),
            "rule_name": str(result.get("rule_name") or ""), "trigger": str(result.get("trigger") or ""),
            "ok": bool(result.get("ok")), "started_at": str(result.get("started_at") or ""),
            "finished_at": iso_now(), "duration": float(result.get("duration") or 0.0),
            "error": str(result.get("error") or ""),
            "actions": [dict(item) for item in (result.get("actions") or []) if isinstance(item, dict)],
        }
        with self._lock:
            self._recent.append(entry)

    def _record_last_run(self, rule_id: str) -> None:
        if not rule_id:
            return
        entry = {"at": iso_now(), "ts": time.time()}
        with self._lock:
            self._last_runs[rule_id] = entry
            counters, persist = dict(self._counters), self.persist
        if persist is None:
            return

        def _mutate(data: Dict[str, Any]) -> None:
            if not isinstance(data, dict):
                return
            last_runs = data.get("last_runs")
            if not isinstance(last_runs, dict):
                last_runs = {}
                data["last_runs"] = last_runs
            last_runs[rule_id] = dict(entry)
            data["counters"] = dict(counters)

        try:
            persist.update(_mutate)
        except Exception:  # pragma: no cover - 持久化失败不影响执行
            self.logger.exception("持久化规则执行时间失败")

    def _load_persist(self) -> None:
        if self.persist is None:
            return
        try:
            data = self.persist.read() if hasattr(self.persist, "read") else None
        except Exception:  # pragma: no cover - 防御性
            self.logger.exception("读取持久化状态失败")
            return
        if not isinstance(data, dict):
            return
        last_runs = data.get("last_runs")
        if isinstance(last_runs, dict):
            self._last_runs = {str(key): dict(value) for key, value in last_runs.items()
                               if isinstance(value, dict)}
        counters = data.get("counters")
        if isinstance(counters, dict):
            for key in self._counters:
                try:
                    self._counters[key] = int(counters.get(key) or 0)
                except (TypeError, ValueError):
                    pass

    def _bump(self, key: str) -> None:
        with self._lock:
            self._counters[key] = int(self._counters.get(key, 0)) + 1

    def _cfg_get(self, path: str, default: Any) -> Any:
        try:
            return self.cfg.get(path, default)
        except Exception:  # pragma: no cover - 防御性
            return default

    def _publish(self, event: str, payload: Dict[str, Any]) -> None:
        try:
            self.bus.publish(event, payload)
        except Exception:  # pragma: no cover - 事件总线异常不影响执行
            self.logger.exception("发布事件失败: %s", event)


__all__ = ["AutomationEngine", "PlayRequest"]
