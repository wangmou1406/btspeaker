"""规则模型：触发器 / 条件 / 动作的规范化、校验与持久化。

规则文件格式（rules.json）::

    {"version": 1, "rules": [ {...}, {...} ]}

设计要点：
- ``normalize_rule`` 只补默认值与类型归一，尽量保留原始非法值，
- ``validate_rule`` 负责给出中文错误列表（空列表 = 合法），错误信息必须指名字段；
- ``RuleStore`` 所有写操作先校验再原子落盘，非法时抛 ``RuleError``。
"""

from __future__ import annotations

import copy
import json
import os
import re
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from .util import (
    atomic_write_text,
    get_logger,
    iso_now,
    new_id,
    parse_bool,
    read_json,
)

TRIGGER_TYPES = ("schedule", "bluetooth", "boot", "manual")
CONDITION_TYPES = ("connected", "time_between", "weekday_in")
ACTION_TYPES = ("play", "tts", "ai", "volume", "connect", "disconnect", "command", "http", "wait", "log")
BLUETOOTH_EVENTS = ("connected", "disconnected", "paired")

RULES_VERSION = 1
MAX_NAME_LEN = 64
WEEKDAY_CN = ("一", "二", "三", "四", "五", "六", "日")

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_INT_RE = re.compile(r"^-?\d+$")
_MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")
_HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD")

logger = get_logger("rules")


class RuleError(Exception):
    """规则非法 / 规则文件读写失败。"""


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def _as_dict(value: Any) -> Dict[str, Any]:
    return copy.deepcopy(value) if isinstance(value, dict) else {}


def _as_text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip()


def _as_int(value: Any, default: Any = None) -> Any:
    """尽量转 int；失败时保留原值（交给 validate 报错）。"""
    if value is None:
        return default
    if isinstance(value, bool):
        return int(value)
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return value


def _int_in_range(value: Any, low: int, high: int) -> bool:
    """判断值是否为 [low, high] 内的整数（接受数字字符串，拒绝 bool/小数）。"""
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        number = value
    elif isinstance(value, float) and float(value).is_integer():
        number = int(value)
    elif isinstance(value, str) and _INT_RE.match(value.strip()):
        number = int(value.strip())
    else:
        return False
    return low <= number <= high


def _is_time_text(value: Any) -> bool:
    return isinstance(value, str) and bool(_TIME_RE.match(value.strip()))


def _normalize_days(value: Any) -> Any:
    """星期列表归一：None -> []，列表内元素尽量转 int。"""
    if value is None:
        return []
    if not isinstance(value, list):
        return value
    return [_as_int(item, item) for item in value]


# --------------------------------------------------------------------------- #
# 规范化
# --------------------------------------------------------------------------- #
def normalize_rule(rule: dict) -> dict:
    """补齐规则默认字段（不改动传入对象）。

    默认：id/created_at/updated_at/enabled=True/cooldown=0/
    trigger={"type":"manual"}/conditions=[]/actions=[]。
    """
    data = _as_dict(rule)
    now = iso_now()

    data["id"] = _as_text(data.get("id")) or new_id("r")
    data["name"] = _as_text(data.get("name"))
    data["enabled"] = parse_bool(data.get("enabled"), True)
    cooldown = data.get("cooldown")
    data["cooldown"] = 0 if cooldown is None else _as_int(cooldown, cooldown)
    data["created_at"] = _as_text(data.get("created_at")) or now
    data["updated_at"] = _as_text(data.get("updated_at")) or now
    data["trigger"] = _normalize_trigger(data.get("trigger"))

    conditions = data.get("conditions")
    if not isinstance(conditions, list):
        conditions = []
    data["conditions"] = [
        _normalize_condition(item) if isinstance(item, dict) else item for item in conditions
    ]

    actions = data.get("actions")
    if not isinstance(actions, list):
        actions = []
    data["actions"] = [
        _normalize_action(item) if isinstance(item, dict) else item for item in actions
    ]
    return data


def _normalize_trigger(raw: Any) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        return {"type": "manual"}
    trigger = copy.deepcopy(raw)
    ttype = _as_text(trigger.get("type")).lower()
    if ttype:
        trigger["type"] = ttype

    if ttype == "schedule":
        if trigger.get("at") is not None:
            trigger["at"] = _as_text(trigger.get("at"))
        if trigger.get("every_minutes") is not None:
            trigger["every_minutes"] = _as_int(trigger.get("every_minutes"))
        if "days" in trigger or ttype == "schedule":
            trigger["days"] = _normalize_days(trigger.get("days"))
    elif ttype == "bluetooth":
        trigger["event"] = _as_text(trigger.get("event")).lower()
        device = _as_text(trigger.get("device")).upper()
        trigger["device"] = device
    elif ttype == "boot":
        trigger["delay"] = _as_int(trigger.get("delay", 5), 5)
    return trigger


def _normalize_condition(raw: Dict[str, Any]) -> Dict[str, Any]:
    cond = copy.deepcopy(raw)
    ctype = _as_text(cond.get("type")).lower()
    if ctype:
        cond["type"] = ctype
    if ctype == "connected":
        cond["value"] = parse_bool(cond.get("value"), True)
    elif ctype == "time_between":
        cond["start"] = _as_text(cond.get("start"))
        cond["end"] = _as_text(cond.get("end"))
    elif ctype == "weekday_in":
        cond["days"] = _normalize_days(cond.get("days"))
    return cond


def _normalize_action(raw: Dict[str, Any]) -> Dict[str, Any]:
    action = copy.deepcopy(raw)
    atype = _as_text(action.get("type")).lower()
    if atype:
        action["type"] = atype

    if atype == "play":
        action["source"] = _as_text(action.get("source"))
        action["label"] = _as_text(action.get("label"))
        action["loop"] = parse_bool(action.get("loop"), False)
        if action.get("volume") is not None:
            action["volume"] = _as_int(action.get("volume"))
        if action.get("address") is not None:
            action["address"] = _as_text(action.get("address")).upper()
    elif atype == "tts":
        action["text"] = _as_text(action.get("text"))
        action["voice"] = _as_text(action.get("voice"))
        if action.get("volume") is not None:
            action["volume"] = _as_int(action.get("volume"))
    elif atype == "ai":
        action["prompt"] = _as_text(action.get("prompt"))
        action["system"] = _as_text(action.get("system"))
        action["model"] = _as_text(action.get("model"))
        if action.get("volume") is not None:
            action["volume"] = _as_int(action.get("volume"))
    elif atype == "volume":
        action["level"] = _as_int(action.get("level"))
    elif atype in ("connect", "disconnect"):
        action["address"] = _as_text(action.get("address")).upper()
    elif atype == "command":
        action["command"] = _as_text(action.get("command"))
        if action.get("timeout") is not None:
            action["timeout"] = _as_int(action.get("timeout"))
    elif atype == "http":
        action["url"] = _as_text(action.get("url"))
        action["method"] = _as_text(action.get("method")).upper() or "GET"
        # 非对象/非字符串的非法值保持原样，交给 validate 报中文错误
        headers = action.get("headers")
        action["headers"] = copy.deepcopy(headers) if headers is not None else {}
        body = action.get("body")
        action["body"] = "" if body is None else body
        action["timeout"] = _as_int(action.get("timeout", 10), 10)
    elif atype == "wait":
        action["seconds"] = _as_int(action.get("seconds"))
    elif atype == "log":
        action["message"] = _as_text(action.get("message"))
    return action


# --------------------------------------------------------------------------- #
# 校验
# --------------------------------------------------------------------------- #
def validate_rule(rule: dict) -> List[str]:
    """校验规则，返回中文错误列表（空列表 = 合法）。"""
    errors: List[str] = []  # type: List[str]
    if not isinstance(rule, dict):
        return ["规则必须是一个 JSON 对象"]

    # ---- 名称 ---------------------------------------------------------- #
    name = rule.get("name")
    if not isinstance(name, str) or not name.strip():
        errors.append("规则名称 name 不能为空（1~64 个字符）")
    elif len(name.strip()) > MAX_NAME_LEN:
        errors.append(
            "规则名称 name 过长（最多 %d 个字符，当前 %d）" % (MAX_NAME_LEN, len(name.strip()))
        )

    cooldown = rule.get("cooldown", 0)
    if cooldown is not None and not _int_in_range(cooldown, 0, 86400 * 7):
        errors.append("冷却时间 cooldown 必须是 0~604800 的整数（秒），当前：%s" % (cooldown,))

    # ---- 触发器 -------------------------------------------------------- #
    trigger = rule.get("trigger")
    if not isinstance(trigger, dict):
        errors.append("触发器 trigger 必须是对象")
    else:
        ttype = _as_text(trigger.get("type")).lower()
        if ttype not in TRIGGER_TYPES:
            errors.append(
                "触发器类型 trigger.type 必须是 %s 之一（当前：%s）"
                % ("/".join(TRIGGER_TYPES), ttype or "空")
            )
        elif ttype == "schedule":
            errors.extend(_validate_schedule_trigger(trigger))
        elif ttype == "bluetooth":
            errors.extend(_validate_bluetooth_trigger(trigger))
        elif ttype == "boot":
            delay = trigger.get("delay", 0)
            if not _int_in_range(delay, 0, 3600):
                errors.append(
                    "开机触发器 trigger.delay 必须是 0~3600 的整数（秒），当前：%s" % (delay,)
                )

    # ---- 条件 ---------------------------------------------------------- #
    conditions = rule.get("conditions", [])
    if not isinstance(conditions, list):
        errors.append("条件列表 conditions 必须是数组")
    else:
        for index, cond in enumerate(conditions):
            errors.extend(_validate_condition(cond, index))

    # ---- 动作 ---------------------------------------------------------- #
    actions = rule.get("actions")
    if not isinstance(actions, list) or not actions:
        errors.append("动作列表 actions 至少需要 1 个动作")
    else:
        for index, action in enumerate(actions):
            errors.extend(_validate_action(action, index))
    return errors


def _validate_schedule_trigger(trigger: Dict[str, Any]) -> List[str]:
    errors: List[str] = []
    has_at = trigger.get("at") is not None and _as_text(trigger.get("at")) != ""
    has_every = trigger.get("every_minutes") is not None and _as_text(trigger.get("every_minutes")) != ""
    if has_at and has_every:
        errors.append("定时触发器 trigger 不能同时设置 at 和 every_minutes，请二选一")
    elif not has_at and not has_every:
        errors.append("定时触发器需要 at（如 07:30）或 every_minutes（如 30）")

    if has_at:
        at = trigger.get("at")
        if not _is_time_text(at):
            errors.append("定时触发器 trigger.at 格式必须是 HH:MM（如 07:30），当前：%s" % (at,))
        days = trigger.get("days", [])
        if days is None:
            days = []
        if not isinstance(days, list):
            errors.append("定时触发器 trigger.days 必须是数组，元素为 0~6 的整数（0=周一）")
        else:
            for day in days:
                if not _int_in_range(day, 0, 6):
                    errors.append(
                        "定时触发器 trigger.days 元素必须是 0~6 的整数（0=周一，6=周日），当前：%s"
                        % (day,)
                    )
                    break

    if has_every:
        every = trigger.get("every_minutes")
        if not _int_in_range(every, 1, 1440):
            errors.append(
                "定时触发器 trigger.every_minutes 必须是 1~1440 的整数（分钟），当前：%s" % (every,)
            )
    return errors


def _validate_bluetooth_trigger(trigger: Dict[str, Any]) -> List[str]:
    errors: List[str] = []
    event = _as_text(trigger.get("event")).lower()
    if event not in BLUETOOTH_EVENTS:
        errors.append(
            "蓝牙触发器 trigger.event 必须是 %s 之一（当前：%s）"
            % ("/".join(BLUETOOTH_EVENTS), event or "空")
        )
    device = _as_text(trigger.get("device"))
    if device and not _MAC_RE.match(device):
        errors.append(
            "蓝牙触发器 trigger.device 不是合法 MAC 地址（如 AA:BB:CC:DD:EE:FF），当前：%s" % device
        )
    return errors


def _validate_days(days: Any, label: str) -> List[str]:
    errors: List[str] = []
    if not isinstance(days, list) or not days:
        return ["%s 必须是非空数组，元素为 0~6 的整数（0=周一）" % label]
    for day in days:
        if not _int_in_range(day, 0, 6):
            errors.append("%s 元素必须是 0~6 的整数（0=周一，6=周日），当前：%s" % (label, day))
            break
    return errors


def _validate_condition(cond: Any, index: int) -> List[str]:
    prefix = "第 %d 个条件" % (index + 1)
    if not isinstance(cond, dict):
        return ["%s 必须是对象" % prefix]
    errors: List[str] = []
    ctype = _as_text(cond.get("type")).lower()
    if ctype not in CONDITION_TYPES:
        errors.append(
            "%s 的类型 type 必须是 %s 之一（当前：%s）"
            % (prefix, "/".join(CONDITION_TYPES), ctype or "空")
        )
    elif ctype == "connected":
        value = cond.get("value", True)
        if not isinstance(value, bool):
            errors.append("%s（connected）的 value 必须是 true 或 false" % prefix)
    elif ctype == "time_between":
        for field, label in (("start", "开始时间"), ("end", "结束时间")):
            value = cond.get(field)
            if not _is_time_text(value):
                errors.append(
                    "%s（time_between）的 %s 格式必须是 HH:MM（如 08:00），当前：%s"
                    % (prefix, field, value)
                )
    elif ctype == "weekday_in":
        errors.extend(_validate_days(cond.get("days"), "%s（weekday_in）的 days" % prefix))
    return errors


def _validate_volume(value: Any, prefix: str) -> List[str]:
    if value is None:
        return []
    if not _int_in_range(value, 0, 100):
        return ["%s 的 volume 必须是 0~100 的整数，当前：%s" % (prefix, value)]
    return []


def _validate_address(value: Any, prefix: str) -> List[str]:
    text = _as_text(value)
    if text and not _MAC_RE.match(text):
        return ["%s 的 address 不是合法 MAC 地址（如 AA:BB:CC:DD:EE:FF），当前：%s" % (prefix, text)]
    return []


def _validate_action(action: Any, index: int) -> List[str]:
    prefix = "第 %d 个动作" % (index + 1)
    if not isinstance(action, dict):
        return ["%s 必须是对象" % prefix]
    errors: List[str] = []
    atype = _as_text(action.get("type")).lower()
    if atype not in ACTION_TYPES:
        errors.append(
            "%s 的类型 type 必须是 %s 之一（当前：%s）"
            % (prefix, "/".join(ACTION_TYPES), atype or "空")
        )
        return errors

    if atype == "play":
        if not _as_text(action.get("source")):
            errors.append("%s（play）缺少 source（音频文件名、路径或 URL）" % prefix)
        if action.get("label") is not None and not isinstance(action.get("label"), str):
            errors.append("%s（play）的 label 必须是字符串" % prefix)
        errors.extend(_validate_volume(action.get("volume"), "%s（play）" % prefix))
        if action.get("loop") is not None and not isinstance(action.get("loop"), bool):
            errors.append("%s（play）的 loop 必须是 true 或 false" % prefix)
        errors.extend(_validate_address(action.get("address"), "%s（play）" % prefix))
    elif atype == "tts":
        if not _as_text(action.get("text")):
            errors.append("%s（tts）缺少 text（要播报的文本）" % prefix)
        if action.get("voice") is not None and not isinstance(action.get("voice"), str):
            errors.append("%s（tts）的 voice 必须是字符串" % prefix)
        errors.extend(_validate_volume(action.get("volume"), "%s（tts）" % prefix))
    elif atype == "ai":
        if not _as_text(action.get("prompt")):
            errors.append("%s（ai）缺少 prompt（要交给 AI 生成播报内容的提示词）" % prefix)
        if action.get("system") is not None and not isinstance(action.get("system"), str):
            errors.append("%s（ai）的 system 必须是字符串" % prefix)
        if action.get("model") is not None and not isinstance(action.get("model"), str):
            errors.append("%s（ai）的 model 必须是字符串" % prefix)
        errors.extend(_validate_volume(action.get("volume"), "%s（ai）" % prefix))
    elif atype == "volume":
        level = action.get("level")
        if level is None:
            errors.append("%s（volume）缺少 level（0~100）" % prefix)
        elif not _int_in_range(level, 0, 100):
            errors.append("%s（volume）的 level 必须是 0~100 的整数，当前：%s" % (prefix, level))
    elif atype in ("connect", "disconnect"):
        errors.extend(_validate_address(action.get("address"), "%s（%s）" % (prefix, atype)))
    elif atype == "command":
        if not _as_text(action.get("command")):
            errors.append("%s（command）缺少 command（要执行的命令）" % prefix)
        if action.get("timeout") is not None and not _int_in_range(action.get("timeout"), 1, 300):
            errors.append(
                "%s（command）的 timeout 必须是 1~300 的整数（秒），当前：%s"
                % (prefix, action.get("timeout"))
            )
    elif atype == "http":
        url = _as_text(action.get("url"))
        if not url:
            errors.append("%s（http）缺少 url" % prefix)
        elif not (url.startswith("http://") or url.startswith("https://")):
            errors.append("%s（http）的 url 必须以 http:// 或 https:// 开头，当前：%s" % (prefix, url))
        method = _as_text(action.get("method")).upper()
        if method and method not in _HTTP_METHODS:
            errors.append(
                "%s（http）的 method 必须是 %s 之一（当前：%s）"
                % (prefix, "/".join(_HTTP_METHODS), method)
            )
        if action.get("headers") is not None and not isinstance(action.get("headers"), dict):
            errors.append("%s（http）的 headers 必须是对象" % prefix)
        if action.get("body") is not None and not isinstance(action.get("body"), str):
            errors.append("%s（http）的 body 必须是字符串" % prefix)
        if action.get("timeout") is not None and not _int_in_range(action.get("timeout"), 1, 120):
            errors.append(
                "%s（http）的 timeout 必须是 1~120 的整数（秒），当前：%s"
                % (prefix, action.get("timeout"))
            )
    elif atype == "wait":
        seconds = action.get("seconds")
        if seconds is None:
            errors.append("%s（wait）缺少 seconds（0~300 秒）" % prefix)
        elif not _int_in_range(seconds, 0, 300):
            errors.append("%s（wait）的 seconds 必须是 0~300 的整数，当前：%s" % (prefix, seconds))
    elif atype == "log":
        if not _as_text(action.get("message")):
            errors.append("%s（log）缺少 message" % prefix)
    return errors


# --------------------------------------------------------------------------- #
# 条件求值（规则语义，供引擎执行前判断）
# --------------------------------------------------------------------------- #
def _hm_minutes(value: Any) -> Optional[int]:
    """把 "HH:MM" 转成当天分钟数；非法返回 None。"""
    text = str(value or "").strip()
    if len(text) != 5 or text[2] != ":":
        return None
    try:
        hour, minute = int(text[:2]), int(text[3:])
    except ValueError:
        return None
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return hour * 60 + minute
    return None


def evaluate_conditions(
    conditions: List[Any], connected: bool, now: Optional[datetime] = None
) -> Tuple[bool, str]:
    """对规则 conditions 求值（全部满足才通过），返回 (是否满足, 中文原因)。

    ``connected``：当前是否有蓝牙音箱已连接；``now``：可注入当前时间（测试用）。
    """
    current = now if isinstance(now, datetime) else datetime.now()
    current_min = current.hour * 60 + current.minute
    for index, cond in enumerate(conditions or []):
        if not isinstance(cond, dict):
            return False, "第 %d 个条件格式不正确" % (index + 1)
        ctype = str(cond.get("type") or "").strip().lower()
        if ctype == "connected":
            expected = parse_bool(cond.get("value", True), True)
            if bool(connected) == expected:
                continue
            return False, ("当前没有已连接的蓝牙音箱" if expected else "当前已有蓝牙音箱连接")
        if ctype == "time_between":
            start = _hm_minutes(cond.get("start"))
            end = _hm_minutes(cond.get("end"))
            if start is None or end is None:
                return False, "time_between 条件的时间格式不合法（需要 HH:MM）"
            if start == end:
                inside = True
            elif start < end:
                inside = start <= current_min < end
            else:  # 跨夜，如 22:00~06:00
                inside = current_min >= start or current_min < end
            if inside:
                continue
            return False, "当前时间 %s 不在 %s~%s 之间" % (
                current.strftime("%H:%M"),
                cond.get("start"),
                cond.get("end"),
            )
        if ctype == "weekday_in":
            days = set()
            for item in cond.get("days") or []:
                if isinstance(item, bool):
                    continue
                try:
                    value = int(item)
                except (TypeError, ValueError):
                    continue
                if 0 <= value <= 6:
                    days.add(value)
            if not days:
                return False, "weekday_in 条件的 days 为空"
            today = current.weekday()
            if today in days:
                continue
            return False, "今天是星期%s，不在条件允许的星期内" % WEEKDAY_CN[today]
        return False, "未知条件类型：%s" % (ctype or "空")
    return True, ""


# --------------------------------------------------------------------------- #
# 规则构造
# --------------------------------------------------------------------------- #
def new_rule(name: str = "", trigger: Optional[Dict[str, Any]] = None, actions: Optional[List[Dict[str, Any]]] = None) -> dict:
    """构造一条新规则（已规范化，未校验）。"""
    rule = normalize_rule(
        {
            "name": name,
            "trigger": trigger if isinstance(trigger, dict) else {"type": "manual"},
            "actions": list(actions or []),
        }
    )
    rule["created_at"] = iso_now()
    rule["updated_at"] = rule["created_at"]
    return rule


# --------------------------------------------------------------------------- #
# 规则存储
# --------------------------------------------------------------------------- #
class RuleStore:
    """rules.json 的线程安全读写封装，所有写操作原子落盘。"""

    def __init__(self, path: str, logger: Optional[Any] = None) -> None:
        self.path = os.path.abspath(path)
        self._logger = logger or get_logger("rules")
        self._lock = threading.RLock()
        self._rules = []  # type: List[Dict[str, Any]]
        self._load()

    # -- 读写 ------------------------------------------------------------ #
    def _load(self) -> None:
        raw = read_json(self.path, None)
        items: Any = None
        if isinstance(raw, dict) and isinstance(raw.get("rules"), list):
            items = raw.get("rules")
        elif isinstance(raw, list):
            items = raw
            self._logger.info("规则文件为旧版数组格式，已按新格式加载: %s", self.path)
        elif raw is not None:
            self._logger.warning("规则文件格式异常，已忽略: %s", self.path)
        if not isinstance(items, list):
            self._rules = []
            return
        loaded = []  # type: List[Dict[str, Any]]
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                self._logger.warning("忽略第 %d 条规则（不是对象）", index + 1)
                continue
            rule = normalize_rule(item)
            errors = validate_rule(rule)
            if errors:
                self._logger.warning(
                    "忽略非法规则 %s：%s", rule.get("id") or ("第 %d 条" % (index + 1)), "；".join(errors)
                )
                continue
            loaded.append(rule)
        self._rules = loaded

    def _save_locked(self) -> None:
        payload = {"version": RULES_VERSION, "rules": self._rules}
        try:
            atomic_write_text(self.path, json.dumps(payload, ensure_ascii=False, indent=2))
        except OSError as exc:
            self._logger.error("保存规则文件失败 %s: %s", self.path, exc)
            raise RuleError("保存规则文件失败：%s" % exc)

    def _find_index(self, rule_id: Any) -> int:
        target = str(rule_id or "")
        for index, rule in enumerate(self._rules):
            if str(rule.get("id") or "") == target:
                return index
        return -1

    def _unique_id(self) -> str:
        existing = {str(rule.get("id") or "") for rule in self._rules}
        while True:
            candidate = new_id("r")
            if candidate not in existing:
                return candidate

    # -- 查询 ------------------------------------------------------------ #
    def list(self) -> List[dict]:
        with self._lock:
            return copy.deepcopy(self._rules)

    def list_enabled(self) -> List[dict]:
        with self._lock:
            return [copy.deepcopy(rule) for rule in self._rules if rule.get("enabled")]

    def get(self, rule_id: Any) -> Optional[dict]:
        with self._lock:
            index = self._find_index(rule_id)
            if index < 0:
                return None
            return copy.deepcopy(self._rules[index])

    def counts(self) -> dict:
        with self._lock:
            enabled = sum(1 for rule in self._rules if rule.get("enabled"))
            return {"total": len(self._rules), "enabled": enabled}

    # -- 写入 ------------------------------------------------------------ #
    def add(self, rule: Any) -> dict:
        with self._lock:
            candidate = normalize_rule(rule)
            existing = {str(item.get("id") or "") for item in self._rules}
            if not _as_text(candidate.get("id")) or _as_text(candidate.get("id")) in existing:
                candidate["id"] = self._unique_id()
            now = iso_now()
            candidate["created_at"] = _as_text(candidate.get("created_at")) or now
            candidate["updated_at"] = now
            errors = validate_rule(candidate)
            if errors:
                raise RuleError("；".join(errors))
            self._rules.append(candidate)
            self._save_locked()
            return copy.deepcopy(candidate)

    def update(self, rule_id: Any, patch: Any) -> dict:
        with self._lock:
            index = self._find_index(rule_id)
            if index < 0:
                raise RuleError("规则不存在：%s" % (rule_id or ""))
            base = copy.deepcopy(self._rules[index])
            if not isinstance(patch, dict):
                raise RuleError("规则更新内容必须是对象")
            for key, value in patch.items():
                if key in ("id", "created_at"):
                    continue
                base[key] = copy.deepcopy(value)
            candidate = normalize_rule(base)
            candidate["id"] = base.get("id")
            candidate["created_at"] = base.get("created_at")
            candidate["updated_at"] = iso_now()
            errors = validate_rule(candidate)
            if errors:
                raise RuleError("；".join(errors))
            self._rules[index] = candidate
            self._save_locked()
            return copy.deepcopy(candidate)

    def delete(self, rule_id: Any) -> bool:
        with self._lock:
            index = self._find_index(rule_id)
            if index < 0:
                return False
            self._rules.pop(index)
            self._save_locked()
            return True

    def set_enabled(self, rule_id: Any, enabled: Any) -> dict:
        with self._lock:
            index = self._find_index(rule_id)
            if index < 0:
                raise RuleError("规则不存在：%s" % (rule_id or ""))
            rule = copy.deepcopy(self._rules[index])
            rule["enabled"] = parse_bool(enabled, True)
            rule["updated_at"] = iso_now()
            errors = validate_rule(rule)
            if errors:
                raise RuleError("；".join(errors))
            self._rules[index] = rule
            self._save_locked()
            return copy.deepcopy(rule)

    def replace_all(self, rules: Any) -> List[dict]:
        """整体替换规则列表（导入 replace 模式）。非法时抛 RuleError 且不落盘。"""
        if not isinstance(rules, list):
            raise RuleError("规则列表必须是数组")
        prepared = []  # type: List[Dict[str, Any]]
        errors = []  # type: List[str]
        used_ids = set()
        for index, raw in enumerate(rules):
            if not isinstance(raw, dict):
                errors.append("第 %d 条规则不是对象" % (index + 1))
                continue
            candidate = normalize_rule(raw)
            rule_id = _as_text(candidate.get("id"))
            if not rule_id or rule_id in used_ids:
                while True:
                    rule_id = new_id("r")
                    if rule_id not in used_ids:
                        break
                candidate["id"] = rule_id
            used_ids.add(rule_id)
            now = iso_now()
            candidate["created_at"] = _as_text(candidate.get("created_at")) or now
            candidate["updated_at"] = now
            rule_errors = validate_rule(candidate)
            if rule_errors:
                errors.append(
                    "第 %d 条（%s）：%s"
                    % (index + 1, candidate.get("name") or "未命名", "；".join(rule_errors))
                )
                continue
            prepared.append(candidate)
        if errors:
            raise RuleError("；".join(errors[:5]))
        with self._lock:
            self._rules = prepared
            self._save_locked()
            return copy.deepcopy(self._rules)

    # -- 其它 ------------------------------------------------------------ #
    def reload(self) -> List[dict]:
        with self._lock:
            self._load()
            return copy.deepcopy(self._rules)

    def path_for_log(self) -> str:
        return self.path


__all__ = [
    "TRIGGER_TYPES",
    "CONDITION_TYPES",
    "ACTION_TYPES",
    "BLUETOOTH_EVENTS",
    "WEEKDAY_CN",
    "RuleError",
    "new_rule",
    "normalize_rule",
    "validate_rule",
    "evaluate_conditions",
    "RuleStore",
]
