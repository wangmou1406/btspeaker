"""通用工具：日志、时间、子进程、模板、JSON 读写。

本模块是依赖图的叶子：不允许 import 本包内其它模块，避免循环依赖。
"""

from __future__ import annotations

import json
import logging
import os
import re
import socket
import subprocess
import threading
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Deque, Dict, List, Optional, Sequence

LOGGER_NAME = "btspeaker"

_LOG_RING = None  # type: Optional["LogRingHandler"]
_RING_LOCK = threading.Lock()


# --------------------------------------------------------------------------- #
# 日志
# --------------------------------------------------------------------------- #
class LogRingHandler(logging.Handler):
    """把日志记录保存在内存环形缓冲里，供 Web 界面「日志」页读取。"""

    def __init__(self, capacity: int = 500) -> None:
        super().__init__()
        self.capacity = max(10, int(capacity))
        self._items = deque(maxlen=self.capacity)  # type: Deque[Dict[str, Any]]
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            item = {
                "time": datetime.fromtimestamp(record.created).strftime("%Y-%m-%d %H:%M:%S"),
                "level": record.levelname,
                "module": record.name.split(".")[-1],
                "message": record.getMessage(),
            }
            with self._lock:
                self._items.append(item)
        except Exception:  # pragma: no cover - 日志本身不应抛错
            self.handleError(record)

    def records(self, limit: int = 200, level: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._lock:
            items = list(self._items)
        if level:
            want = str(level).upper()
            items = [i for i in items if i["level"] == want]
        if limit and int(limit) > 0:
            items = items[-int(limit):]
        return items

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    def total(self) -> int:
        with self._lock:
            return len(self._items)


def setup_logging(level: str = "INFO", log_file: Optional[str] = None, capacity: int = 500) -> LogRingHandler:
    """初始化根日志器（幂等），返回内存日志环形缓冲。"""
    global _LOG_RING
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)

    fmt = logging.Formatter(
        "%(asctime)s [%(name)s] %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S"
    )
    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    logger.addHandler(stream)

    if log_file:
        try:
            parent = os.path.dirname(os.path.abspath(log_file))
            if parent:
                os.makedirs(parent, exist_ok=True)
            file_handler = logging.FileHandler(log_file, encoding="utf-8")
            file_handler.setFormatter(fmt)
            logger.addHandler(file_handler)
        except OSError:
            pass

    with _RING_LOCK:
        _LOG_RING = LogRingHandler(capacity)
        logger.addHandler(_LOG_RING)
    return _LOG_RING


def get_log_ring() -> Optional[LogRingHandler]:
    return _LOG_RING


def get_logger(name: str = "") -> logging.Logger:
    """获取带命名空间的日志器，如 get_logger('engine') -> btspeaker.engine。"""
    if not name or name == LOGGER_NAME:
        return logging.getLogger(LOGGER_NAME)
    if name.startswith(LOGGER_NAME + "."):
        return logging.getLogger(name)
    return logging.getLogger("%s.%s" % (LOGGER_NAME, name))


# --------------------------------------------------------------------------- #
# 子进程
# --------------------------------------------------------------------------- #
@dataclass
class CmdResult:
    """子进程执行结果。"""

    returncode: int = 0
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    argv: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    def text(self) -> str:
        parts = []
        if self.stdout:
            parts.append(self.stdout.strip())
        if self.stderr:
            parts.append(self.stderr.strip())
        return "\n".join(parts)


def _to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def run_cmd(
    argv: Sequence[str],
    timeout: float = 20.0,
    env: Optional[Dict[str, str]] = None,
    input_text: Optional[str] = None,
    cwd: Optional[str] = None,
) -> CmdResult:
    """执行外部命令（不经过 shell，避免注入），捕获超时与缺失命令。"""
    cmd = [str(a) for a in argv]
    if not cmd:
        return CmdResult(returncode=2, stderr="空命令", argv=[])
    full_env = None
    if env:
        full_env = dict(os.environ)
        full_env.update({str(k): str(v) for k, v in env.items()})
    try:
        proc = subprocess.run(
            cmd,
            input=input_text.encode("utf-8") if input_text is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=max(0.5, float(timeout)),
            env=full_env,
            cwd=cwd,
            shell=False,
        )
        return CmdResult(
            returncode=int(proc.returncode),
            stdout=_to_text(proc.stdout),
            stderr=_to_text(proc.stderr),
            argv=cmd,
        )
    except subprocess.TimeoutExpired as exc:
        return CmdResult(
            returncode=124,
            stdout=_to_text(exc.stdout),
            stderr=_to_text(exc.stderr) or "命令执行超时",
            timed_out=True,
            argv=cmd,
        )
    except FileNotFoundError:
        return CmdResult(returncode=127, stderr="找不到命令: %s" % cmd[0], argv=cmd)
    except OSError as exc:
        return CmdResult(returncode=126, stderr="命令执行失败: %s" % exc, argv=cmd)


def which(name: str) -> Optional[str]:
    """在 PATH 中查找可执行文件，返回绝对路径或 None。"""
    if not name:
        return None
    path = os.environ.get("PATH", os.defpath)
    exts = [""]
    if os.name == "nt":
        exts += os.environ.get("PATHEXT", ".EXE;.BAT;.CMD").split(";")
    for directory in path.split(os.pathsep):
        directory = directory.strip('"')
        if not directory:
            continue
        for ext in exts:
            candidate = os.path.join(directory, name + ext)
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
    return None


# --------------------------------------------------------------------------- #
# 时间 / 文本 / JSON
# --------------------------------------------------------------------------- #
def _local_dt(dt: Optional[datetime] = None) -> datetime:
    return dt if dt is not None else datetime.now()


def local_minute(dt: Optional[datetime] = None) -> str:
    """本地时间 "HH:MM"，用于定时规则匹配。"""
    return _local_dt(dt).strftime("%H:%M")


def local_weekday(dt: Optional[datetime] = None) -> int:
    """本地星期，0=周一 ... 6=周日（与 datetime.weekday 一致）。"""
    return _local_dt(dt).weekday()


def iso_now(dt: Optional[datetime] = None) -> str:
    """ISO8601 本地时间，如 2026-11-15T20:00:00+08:00。"""
    value = _local_dt(dt)
    if value.tzinfo is None:
        value = value.astimezone()
    return value.replace(microsecond=0).isoformat()


def now_hm(dt: Optional[datetime] = None) -> str:
    return local_minute(dt)


def hostname() -> str:
    try:
        return socket.gethostname() or "iStoreOS"
    except OSError:  # pragma: no cover
        return "iStoreOS"


_TEMPLATE_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def render_template(text: str, ctx: Dict[str, Any]) -> str:
    """把 {var} 替换为 ctx[var]；未知变量原样保留。"""
    if not text:
        return ""
    ctx = ctx or {}

    def _replace(match: "re.Match[str]") -> str:
        key = match.group(1)
        if key in ctx and ctx[key] is not None:
            return str(ctx[key])
        return match.group(0)

    return _TEMPLATE_RE.sub(_replace, str(text))


def json_dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False)


def read_json(path: str, default: Any = None) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def atomic_write_text(path: str, text: str, encoding: str = "utf-8") -> None:
    """原子写入文本：先写临时文件、fsync，再 os.replace。"""
    target = os.path.abspath(path)
    parent = os.path.dirname(target)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp_path = "%s.tmp.%d.%d" % (target, os.getpid(), threading.get_ident() % 100000)
    with open(tmp_path, "w", encoding=encoding, newline="\n") as handle:
        handle.write(text)
        handle.flush()
        try:
            os.fsync(handle.fileno())
        except OSError:  # pragma: no cover - 部分文件系统不支持
            pass
    os.replace(tmp_path, target)


def write_json(path: str, obj: Any) -> None:
    atomic_write_text(path, json.dumps(obj, ensure_ascii=False, indent=2))


def parse_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on", "是", "开")


def parse_int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def clamp_int(value: Any, low: int, high: int, default: int = 0) -> int:
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def human_size(num: float) -> str:
    size = float(num or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024.0 or unit == "GB":
            if unit == "B":
                return "%d %s" % (int(size), unit)
            return "%.1f %s" % (size, unit)
        size /= 1024.0
    return "%.1f GB" % size


def human_duration(seconds: float) -> str:
    total = int(max(0, float(seconds or 0)))
    if total < 60:
        return "%d 秒" % total
    minutes, sec = divmod(total, 60)
    if minutes < 60:
        return "%d 分 %d 秒" % (minutes, sec) if sec else "%d 分钟" % minutes
    hours, minutes = divmod(minutes, 60)
    return "%d 小时 %d 分" % (hours, minutes)


def new_id(prefix: str = "r") -> str:
    return "%s_%s" % (str(prefix).strip() or "id", uuid.uuid4().hex[:8])


def mask_secret(value: str, keep: int = 4) -> str:
    raw = str(value or "")
    if not raw:
        return ""
    if len(raw) <= keep * 2:
        return "*" * len(raw)
    return raw[:keep] + "*" * (len(raw) - keep * 2) + raw[-keep:]


def wait_or_stop(stop_event: threading.Event, seconds: float) -> bool:
    """等待 seconds 秒，期间若 stop_event 被置位则立即返回 True。"""
    try:
        return bool(stop_event.wait(max(0.0, float(seconds))))
    except Exception:  # pragma: no cover
        return True
