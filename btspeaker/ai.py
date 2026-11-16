"""AI 播报：调用 OpenAI 兼容的 Chat Completions 接口生成播报文本。

配置（``cfg.ai.*``）：

- ``enabled``      ：是否启用（关闭时动作会直接报错）
- ``base_url``     ：接口地址，三种写法都支持：
  - ``https://api.deepseek.com/v1``（自动补 ``/chat/completions``）
  - ``https://api.openai.com/v1/chat/completions``（原样使用）
  - 自建服务的完整地址
- ``api_key``      ：Bearer 令牌，留空则不可用
- ``model``        ：模型名，如 ``deepseek-chat`` / ``gpt-4o-mini``
- ``system_prompt``：系统提示词，默认要求"适合语音朗读"
- ``timeout`` / ``max_tokens`` / ``temperature``

本模块只依赖标准库，与音频、TTS、规则引擎解耦；由规则动作 ``ai`` 调用。
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

from .util import get_logger

DEFAULT_SYSTEM_PROMPT = (
    "你是蓝音管家（BTSpeaker），负责用简洁自然的中文生成适合语音朗读的内容。"
    "回答要口语化、一句话到三句话，不要使用 Markdown、列表符号、表情或括号注释。"
)


class AIError(Exception):
    """AI 相关错误（消息为可直接展示的中文）。"""


class AIClient:
    """OpenAI 兼容 Chat Completions 客户端。"""

    def __init__(self, cfg, logger: Optional[Any] = None) -> None:
        self.cfg = cfg
        self.logger = logger or get_logger("ai")

    # -- 配置读取 -------------------------------------------------------- #
    def _opt(self, path: str, default: Any) -> Any:
        try:
            value = self.cfg.get(path, default)
        except Exception:  # pragma: no cover - 防御性
            value = default
        return default if value is None else value

    def _base_url(self) -> str:
        return str(self._opt("ai.base_url", "") or "").strip()

    def _api_key(self) -> str:
        return str(self._opt("ai.api_key", "") or "").strip()

    def _model(self) -> str:
        return str(self._opt("ai.model", "deepseek-chat") or "deepseek-chat").strip() or "deepseek-chat"

    def _system_prompt(self) -> str:
        value = str(self._opt("ai.system_prompt", "") or "").strip()
        return value or DEFAULT_SYSTEM_PROMPT

    def _timeout(self) -> float:
        try:
            value = float(self._opt("ai.timeout", 30) or 30)
        except (TypeError, ValueError):
            value = 30.0
        return max(3.0, min(300.0, value))

    def endpoint(self) -> str:
        """把 base_url 归一化成完整的 chat/completions 地址。"""
        url = self._base_url()
        if not url:
            return ""
        if "chat/completions" in url:
            return url
        url = url.rstrip("/")
        # 兼容各家版本路径：/v1（OpenAI/DeepSeek）与 /v4（智谱）等
        if re.search(r"/v\d+$", url):
            return url + "/chat/completions"
        return url + "/v1/chat/completions"

    # -- 探测 ------------------------------------------------------------ #
    def ready(self) -> bool:
        return bool(self.endpoint() and self._api_key())

    def probe(self) -> Dict[str, Any]:
        """返回 {"ready", "backend", "model", "base_url", "hint"}，供设置页/诊断使用。"""
        url = self.endpoint()
        if not url:
            return {
                "ready": False,
                "backend": "none",
                "model": self._model(),
                "base_url": "",
                "hint": "未配置 AI：请在设置页填写接口地址与 API Key",
            }
        if not self._api_key():
            return {
                "ready": False,
                "backend": "openai",
                "model": self._model(),
                "base_url": url,
                "hint": "已填写接口地址，但缺少 API Key",
            }
        enabled = bool(self._opt("ai.enabled", False))
        return {
            "ready": enabled,
            "backend": "openai",
            "model": self._model(),
            "base_url": url,
            "hint": "" if enabled else "AI 接口已配置，但「启用 AI」开关是关闭的",
        }

    # -- 调用 ------------------------------------------------------------ #
    def complete(self, prompt: str, system: Optional[str] = None, model: Optional[str] = None) -> str:
        """调用模型生成文本；失败抛 AIError（中文消息）。"""
        text = "" if prompt is None else str(prompt).strip()
        if not text:
            raise AIError("AI 提示词不能为空")
        if not bool(self._opt("ai.enabled", False)):
            raise AIError("AI 未启用：请在设置页打开「启用 AI」开关")
        url = self.endpoint()
        key = self._api_key()
        if not url or not key:
            raise AIError("未配置 AI：请在设置页填写接口地址与 API Key")

        system_text = str(system or "").strip() or self._system_prompt()
        messages = [
            {"role": "system", "content": system_text},
            {"role": "user", "content": text},
        ]
        payload = {
            "model": str(model or "").strip() or self._model(),
            "messages": messages,
            "stream": False,
            "max_tokens": self._max_tokens(),
            "temperature": self._temperature(),
        }
        request = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Authorization": "Bearer " + key,
            },
            method="POST",
        )
        data = self._post(request)
        try:
            parsed = json.loads(data.decode("utf-8", "replace"))
        except ValueError:
            preview = data[:200].decode("utf-8", "replace").strip()
            raise AIError("AI 接口返回的不是 JSON：%s" % (preview or "(空)"))
        answer = self._extract_text(parsed)
        if not answer:
            raise AIError("AI 返回了空内容")
        self.logger.info("AI 生成完成：%d 字（模型 %s）", len(answer), payload["model"])
        return answer

    def _post(self, request: "urllib.request.Request") -> bytes:
        try:
            response = urllib.request.urlopen(request, timeout=self._timeout())
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read(400).decode("utf-8", "replace").strip()[:160]
            except Exception:  # pragma: no cover - 读取错误体失败
                detail = ""
            hint = ""
            if exc.code == 401:
                hint = "（请检查 API Key 是否正确、是否过期；到服务商控制台重新复制完整 Key）"
            raise AIError(
                "AI 接口返回错误：HTTP %s%s%s"
                % (exc.code, ("：" + detail) if detail else "", hint)
            )
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            raise AIError("无法连接 AI 接口：%s" % (reason,))
        except OSError as exc:
            raise AIError("AI 请求失败：%s" % (exc,))
        try:
            return response.read()
        finally:
            try:
                response.close()
            except Exception:  # pragma: no cover - 防御性
                pass

    @staticmethod
    def _extract_text(parsed: Any) -> str:
        """从 OpenAI 兼容响应中取 assistant 文本。"""
        if not isinstance(parsed, dict):
            return ""
        choices = parsed.get("choices")
        if not isinstance(choices, list) or not choices:
            error = parsed.get("error")
            if isinstance(error, dict) and error.get("message"):
                raise AIError("AI 接口报错：%s" % str(error.get("message"))[:200])
            return ""
        first = choices[0] if isinstance(choices[0], dict) else {}
        message = first.get("message") if isinstance(first.get("message"), dict) else {}
        content = message.get("content")
        if isinstance(content, list):  # 多段内容
            parts = []
            for item in content:
                if isinstance(item, dict) and item.get("text"):
                    parts.append(str(item["text"]))
            content = "".join(parts)
        return str(content or "").strip()

    def _max_tokens(self) -> int:
        try:
            value = int(self._opt("ai.max_tokens", 300) or 300)
        except (TypeError, ValueError):
            value = 300
        return max(32, min(4096, value))

    def _temperature(self) -> float:
        try:
            value = float(self._opt("ai.temperature", 0.7) or 0.7)
        except (TypeError, ValueError):
            value = 0.7
        return max(0.0, min(2.0, value))
