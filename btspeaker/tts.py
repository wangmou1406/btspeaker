"""TTS 引擎：优先 HTTP TTS 服务（中文质量好），espeak 离线兜底，mock 供开发测试。

后端选择（``cfg.tts.backend``）：
- ``auto``  ：有 ``cfg.tts.url`` → http；否则有 espeak → espeak；都没有 → 报中文错误；
- ``http``  ：调用 ``cfg.tts.url`` 模板；
- ``espeak``：调用本地 espeak；
- ``mock``  ：生成 0.6 秒静音 wav（sim 模式默认）。

HTTP 后端约定：
- ``GET``：URL 里的 ``{text}`` / ``{lang}`` 分别做 URL 编码替换；
- ``POST``：``body_template`` 做模板替换（空则直接发送原文）；headers 来自 ``cfg.tts.headers``；
- 按响应 Content-Type 落盘 .mp3/.wav/.ogg（其它 audio/* 用 ``cfg.tts.format``），非音频内容抛 TtsError。

所有临时文件放在 ``cfg.data_dir/tmp/``，``cleanup()`` 清理 1 小时前的 ``tts_*`` 文件。
"""

from __future__ import annotations

import os
import re
import time
import urllib.error
import uuid
import wave
from typing import Any, Dict, Optional, Tuple
from urllib import parse as urlparse
from urllib import request as urlrequest

from .util import get_logger, run_cmd, which

TMP_PREFIX = "tts_"
CLEANUP_AGE = 3600.0
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MOCK_DURATION = 0.6
MOCK_RATE = 22050
_SAFE_EXT_RE = re.compile(r"^[a-z0-9]{1,5}$")


class TtsError(Exception):
    """TTS 合成失败（配置缺失 / 服务异常 / 返回非音频）。"""


def _no_tts_error() -> TtsError:
    return TtsError("未配置 TTS：请在设置里填写 TTS 服务地址，或安装 espeak 作为离线兜底")


class TtsEngine:
    """TTS 合成引擎，返回本地音频文件路径。"""

    def __init__(self, cfg, logger: Optional[Any] = None) -> None:
        self.cfg = cfg
        self.logger = logger or get_logger("tts")

    # ------------------------------------------------------------------ #
    # 配置读取
    # ------------------------------------------------------------------ #
    @property
    def tmp_dir(self) -> str:
        data_dir = str(getattr(self.cfg, "data_dir", "") or ".")
        return os.path.join(data_dir, "tmp")

    def _opt(self, path: str, default: Any) -> Any:
        try:
            return self.cfg.get(path, default)
        except Exception:  # pragma: no cover - 防御性
            return default

    def _backend(self) -> str:
        backend = str(self._opt("tts.backend", "auto") or "auto").strip().lower()
        if backend not in ("auto", "http", "espeak", "mock", "none"):
            self.logger.warning("未知 TTS 后端 %s，按 auto 处理", backend)
            backend = "auto"
        return backend

    def _url(self) -> str:
        return str(self._opt("tts.url", "") or "").strip()

    def _espeak_cmd(self) -> str:
        return str(self._opt("tts.espeak", "espeak") or "espeak").strip() or "espeak"

    def _timeout(self, default: float = 20.0) -> float:
        try:
            value = float(self._opt("tts.timeout", default) or default)
        except (TypeError, ValueError):
            value = default
        return max(1.0, min(300.0, value))

    # ------------------------------------------------------------------ #
    # 探测
    # ------------------------------------------------------------------ #
    def probe(self) -> dict:
        """返回 {"backend","ready","hint"}，供设置页 / 诊断展示。"""
        backend = self._backend()
        url = self._url()
        if backend == "mock":
            return {
                "backend": "mock",
                "ready": True,
                "hint": "模拟模式：只生成静音音频，不产生真实语音（BTSPEAKER_SIM=1）",
            }
        if backend == "none":
            return {"backend": "none", "ready": False, "hint": str(_no_tts_error())}
        if backend in ("auto", "http") and url:
            return {"backend": "http", "ready": True, "hint": "使用 HTTP TTS 服务：%s" % url}
        if backend == "http":
            return {
                "backend": "http",
                "ready": False,
                "hint": "未配置 TTS 服务地址：请在设置页填写 tts.url（支持 {text}/{lang} 占位）",
            }
        espeak_cmd = self._espeak_cmd()
        espeak_path = which(espeak_cmd)
        if espeak_path:
            return {
                "backend": "espeak",
                "ready": True,
                "hint": "离线 espeak 合成（音质机械，中文建议配置 HTTP TTS）",
            }
        if backend == "espeak":
            return {
                "backend": "espeak",
                "ready": False,
                "hint": "未找到 espeak：请安装（opkg install espeak）或改用 HTTP TTS 服务",
            }
        return {"backend": "none", "ready": False, "hint": str(_no_tts_error())}

    # ------------------------------------------------------------------ #
    # 合成
    # ------------------------------------------------------------------ #
    def synthesize(self, text: str, voice: Optional[str] = None) -> str:
        """把文本合成为本地音频文件，返回文件路径。"""
        content = "" if text is None else str(text).strip()
        if not content:
            raise TtsError("TTS 文本不能为空")
        backend = self._resolve_backend()
        voice_name = self._voice(voice)
        self._ensure_tmp_dir()
        try:
            if backend == "http":
                path = self._synthesize_http(content, voice_name)
            elif backend == "espeak":
                path = self._synthesize_espeak(content, voice_name)
            else:
                path = self._synthesize_mock(content, voice_name)
        except TtsError:
            raise
        except Exception as exc:  # pragma: no cover - 兜底防御
            self.logger.exception("TTS 合成异常（%s）", backend)
            raise TtsError("TTS 合成失败（%s）：%s" % (backend, exc))
        self.logger.info("TTS 合成完成（%s）：%d 字 → %s", backend, len(content), path)
        return path

    def _resolve_backend(self) -> str:
        """决定本次实际使用的后端，不可用时抛中文错误。"""
        backend = self._backend()
        url = self._url()
        if backend == "mock":
            return "mock"
        if backend == "none":
            raise _no_tts_error()
        if url:
            return "http"
        if backend == "http":
            raise TtsError(
                "未配置 TTS 服务地址：请在设置页填写 tts.url，或安装 espeak 作为离线兜底"
            )
        if which(self._espeak_cmd()):
            return "espeak"
        if backend == "espeak":
            raise TtsError(
                "未找到 espeak：请先安装（opkg install espeak），或改用 HTTP TTS 服务"
            )
        raise _no_tts_error()

    def _voice(self, voice: Optional[str]) -> str:
        explicit = str(voice).strip() if voice is not None else ""
        return explicit or str(self._opt("tts.voice", "zh") or "zh")

    # ------------------------------------------------------------------ #
    # 各后端实现
    # ------------------------------------------------------------------ #
    def _synthesize_http(self, text: str, voice: str) -> str:
        url_template = self._url()
        method = str(self._opt("tts.method", "GET") or "GET").strip().upper()
        if method not in ("GET", "POST"):
            self.logger.warning("TTS method=%s 不受支持，按 GET 处理", method)
            method = "GET"
        encoded_text = urlparse.quote(text, safe="")
        encoded_lang = urlparse.quote(str(voice), safe="")
        url = url_template.replace("{text}", encoded_text).replace("{lang}", encoded_lang)

        headers = self._opt("tts.headers", {}) or {}
        if not isinstance(headers, dict):
            headers = {}
        header_map = {str(k): str(v) for k, v in headers.items() if str(k).strip()}

        data = None  # type: Optional[bytes]
        if method == "POST":
            body_template = str(self._opt("tts.body_template", "") or "")
            if body_template:
                body = body_template.replace("{text}", text).replace("{lang}", str(voice))
            else:
                body = text
            data = body.encode("utf-8")
            if not any(key.lower() == "content-type" for key in header_map):
                header_map["Content-Type"] = "text/plain; charset=utf-8"

        request = urlrequest.Request(url, data=data, headers=header_map, method=method)
        response = self._open(request)
        try:
            content_type = self._response_content_type(response)
            payload = response.read(MAX_RESPONSE_BYTES + 1)
        finally:
            try:
                response.close()
            except Exception:  # pragma: no cover - 防御性
                pass

        if not payload:
            raise TtsError("TTS 服务返回空内容（检查 tts.url 与请求参数）")
        if len(payload) > MAX_RESPONSE_BYTES:
            raise TtsError("TTS 服务返回内容过大（超过 8MB），请检查 tts.url")
        extension = self._extension_for(content_type)
        if extension is None:
            preview = payload[:360].decode("utf-8", "replace").strip()[:120]
            raise TtsError("TTS 服务返回了非音频内容: %s" % (preview or "(空)"))
        path = self._new_tmp_path(extension)
        self._write_bytes(path, payload)
        self.logger.info(
            "HTTP TTS 完成：%s（Content-Type=%s，%d 字节）",
            path,
            content_type or "未知",
            len(payload),
        )
        return path

    def _open(self, request: "urlrequest.Request"):
        timeout = self._timeout()
        try:
            return urlrequest.urlopen(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                body = exc.read(240)
                detail = body.decode("utf-8", "replace").strip()[:120]
            except Exception:  # pragma: no cover - 读取错误体失败
                detail = ""
            raise TtsError(
                "TTS 服务返回错误：HTTP %s %s%s"
                % (exc.code, getattr(exc, "reason", "") or "", ("：" + detail) if detail else "")
            )
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            raise TtsError("无法连接 TTS 服务：%s（请检查 tts.url 与网络）" % (reason,))
        except OSError as exc:
            raise TtsError("TTS 请求失败：%s" % (exc,))

    @staticmethod
    def _response_content_type(response: Any) -> str:
        try:
            headers = getattr(response, "headers", None)
            if headers is not None and hasattr(headers, "get"):
                return str(headers.get("Content-Type", "") or "")
            getheader = getattr(response, "getheader", None)
            if callable(getheader):
                return str(getheader("Content-Type", "") or "")
        except Exception:  # pragma: no cover - 防御性
            return ""
        return ""

    def _extension_for(self, content_type: str) -> Optional[str]:
        """按 Content-Type 决定落盘扩展名；None 表示非音频。"""
        ctype = str(content_type or "").split(";")[0].strip().lower()
        if ctype in ("audio/mpeg", "audio/mp3", "audio/mpeg3", "audio/x-mpeg"):
            return "mp3"
        if ctype in ("audio/wav", "audio/x-wav", "audio/wave", "audio/vnd.wave"):
            return "wav"
        if ctype in ("audio/ogg", "application/ogg", "audio/opus", "audio/x-ogg"):
            return "ogg"
        if ctype.startswith("audio/"):
            return self._format_extension()
        if not ctype:
            # 部分自建服务不返回 Content-Type，按配置格式尝试
            self.logger.warning("TTS 响应缺少 Content-Type，按 tts.format 落盘")
            return self._format_extension()
        return None

    def _format_extension(self) -> str:
        fmt = str(self._opt("tts.format", "mp3") or "mp3").strip().lower().lstrip(".")
        return fmt if _SAFE_EXT_RE.match(fmt) else "mp3"

    def _synthesize_espeak(self, text: str, voice: str) -> str:
        espeak_cmd = self._espeak_cmd()
        path = self._new_tmp_path("wav")
        speed = self._opt("tts.speed", 150)
        try:
            speed = max(80, min(450, int(speed)))
        except (TypeError, ValueError):
            speed = 150
        timeout = self._timeout()
        result = run_cmd(
            [espeak_cmd, "-v", voice or "zh", "-s", str(speed), "-w", path, text],
            timeout=timeout,
        )
        if not result.ok:
            detail = result.text() or ("退出码 %d" % result.returncode)
            raise TtsError("espeak 合成失败：%s" % detail[:200])
        if not os.path.isfile(path) or os.path.getsize(path) <= 0:
            raise TtsError("espeak 合成失败：未生成音频文件（检查 espeak 安装与语音包）")
        self.logger.info("espeak 合成完成：%s（voice=%s，speed=%d）", path, voice, speed)
        return path

    def _synthesize_mock(self, text: str, voice: str) -> str:
        path = self._new_tmp_path("wav")
        frames = int(MOCK_RATE * MOCK_DURATION)
        with wave.open(path, "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(MOCK_RATE)
            handle.writeframes(b"\x00\x00" * frames)
        self.logger.info("模拟 TTS 播报（voice=%s）：%s", voice, text)
        return path

    # ------------------------------------------------------------------ #
    # 临时文件
    # ------------------------------------------------------------------ #
    def _ensure_tmp_dir(self) -> None:
        try:
            os.makedirs(self.tmp_dir, exist_ok=True)
        except OSError as exc:
            raise TtsError("无法创建 TTS 临时目录 %s：%s" % (self.tmp_dir, exc))

    def _new_tmp_path(self, extension: str) -> str:
        ext = str(extension or "wav").lower().lstrip(".")
        if not _SAFE_EXT_RE.match(ext):
            ext = "wav"
        return os.path.join(self.tmp_dir, "%s%s.%s" % (TMP_PREFIX, uuid.uuid4().hex[:8], ext))

    @staticmethod
    def _write_bytes(path: str, payload: bytes) -> None:
        tmp_path = "%s.part" % path
        with open(tmp_path, "wb") as handle:
            handle.write(payload)
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:  # pragma: no cover - 部分文件系统不支持
                pass
        os.replace(tmp_path, path)

    def cleanup(self) -> int:
        """删除 1 小时前的 TTS 临时文件，返回删除数量。"""
        tmp_dir = self.tmp_dir
        if not os.path.isdir(tmp_dir):
            return 0
        deadline = time.time() - CLEANUP_AGE
        removed = 0
        try:
            names = os.listdir(tmp_dir)
        except OSError:  # pragma: no cover - 权限问题
            return 0
        for name in names:
            if not name.startswith(TMP_PREFIX):
                continue  # 只清理本模块生成的文件，避免误删下载缓存
            path = os.path.join(tmp_dir, name)
            try:
                if os.path.isfile(path) and os.path.getmtime(path) < deadline:
                    os.remove(path)
                    removed += 1
            except OSError:
                continue
        if removed:
            self.logger.info("已清理 %d 个过期 TTS 临时文件", removed)
        return removed


__all__ = ["TtsError", "TtsEngine"]
