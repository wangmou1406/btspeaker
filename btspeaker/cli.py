"""命令行入口：`btspeaker serve|seed|status|devices|play|say|rules|logs|diag ...`

除 `serve` / `seed` 外，其余子命令通过本机 HTTP API 操作运行中的服务，
方便 ssh 下脚本化（也可配合 crontab / 自动化平台调用）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from . import APP_TITLE, DEFAULT_PORT, __version__
from .config import ENV_SIM, Config, default_data_dir
from .util import clamp_int, json_dumps

EXIT_OK = 0
EXIT_ERROR = 1


# --------------------------------------------------------------------------- #
# HTTP 客户端
# --------------------------------------------------------------------------- #
def _base_url(args: argparse.Namespace) -> str:
    port = getattr(args, "port", None) or int(os.environ.get("BTSPEAKER_PORT", DEFAULT_PORT) or DEFAULT_PORT)
    host = getattr(args, "host", None) or "127.0.0.1"
    return "http://%s:%d" % (host, int(port))


def _request(args: argparse.Namespace, method: str, path: str, body: Optional[Dict[str, Any]] = None, timeout: float = 20.0) -> Dict[str, Any]:
    url = _base_url(args) + path
    data = json_dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, method=str(method).upper())
    request.add_header("Content-Type", "application/json; charset=utf-8")
    token = getattr(args, "token", None) or os.environ.get("BTSPEAKER_TOKEN", "")
    if token:
        request.add_header("X-Api-Token", token)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        payload: Dict[str, Any] = {}
        try:
            payload = json.loads(exc.read().decode("utf-8", "replace"))
        except Exception:
            payload = {}
        message = ((payload.get("error") or {}) if isinstance(payload, dict) else {}).get("message") or ("HTTP %s" % exc.code)
        print("错误: %s" % message, file=sys.stderr)
        raise SystemExit(EXIT_ERROR)
    except urllib.error.URLError as exc:
        print("无法连接服务 %s：%s" % (url, exc.reason), file=sys.stderr)
        print("提示：确认服务已启动（/etc/init.d/btspeaker status），或用 --port 指定端口。", file=sys.stderr)
        raise SystemExit(EXIT_ERROR)
    except OSError as exc:
        print("请求失败：%s" % exc, file=sys.stderr)
        raise SystemExit(EXIT_ERROR)
    if not payload.get("ok"):
        error = payload.get("error") or {}
        print("错误: %s" % (error.get("message") or "未知错误"), file=sys.stderr)
        raise SystemExit(EXIT_ERROR)
    return payload.get("data") or {}


def _print_json(args: argparse.Namespace, data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def _print_table(headers: List[str], rows: List[List[str]]) -> None:
    widths = [len(h) for h in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(str(cell)))
    line = "  ".join(h.ljust(widths[index]) for index, h in enumerate(headers))
    print(line)
    print("-" * len(line))
    for row in rows:
        print("  ".join(str(cell).ljust(widths[index]) for index, cell in enumerate(row)))
    if not rows:
        print("（空）")


# --------------------------------------------------------------------------- #
# 子命令实现
# --------------------------------------------------------------------------- #
def _cmd_serve(args: argparse.Namespace) -> int:
    if args.sim:
        os.environ[ENV_SIM] = "1"
    if args.log_level:
        os.environ["BTSPEAKER_LOG_LEVEL"] = args.log_level
    from .service import run_service

    return run_service(data_dir=args.data_dir, host=args.serve_host, port=args.serve_port)


def _cmd_seed(args: argparse.Namespace) -> int:
    from .service import seed_data_dir

    cfg = Config.load(data_dir=args.data_dir)
    info = seed_data_dir(cfg)
    if args.json:
        _print_json(args, info)
    else:
        print("数据目录: %s" % info["data_dir"])
        print("配置文件: %s %s" % (info["config_path"], "（已创建）" if info["config_created"] else "（已存在，保持不变）"))
        print("规则文件: %s %s" % (info["rules_path"], "（已写入 %d 条示例规则，默认停用）" % info["rules_created"] if info["rules_created"] else "（已存在，保持不变）"))
    return EXIT_OK


def _cmd_status(args: argparse.Namespace) -> int:
    data = _request(args, "GET", "/api/status")
    if args.json:
        _print_json(args, data)
        return EXIT_OK
    bt = data.get("bluetooth") or {}
    player = data.get("player") or {}
    engine = data.get("engine") or {}
    connected = bt.get("connected") or {}
    current = player.get("current") or {}
    print("%s v%s%s" % (data.get("app", APP_TITLE), data.get("version", ""), "（模拟模式）" if data.get("sim") else ""))
    print("主机: %s   运行: %s" % (data.get("hostname", ""), data.get("uptime", 0)))
    print("蓝牙: %s  适配器: %s" % ("已就绪" if bt.get("available") else "不可用", (bt.get("adapter") or {}).get("address", "-")))
    print("音箱: %s" % (connected.get("name") or "-" if not connected else "%s (%s)" % (connected.get("name") or "未命名", connected.get("address"))))
    print("播放: %s" % ("播放中: %s" % (current.get("label") or current.get("source") or "未知") if player.get("playing") else "空闲"))
    print("音量: %s%%" % player.get("volume", "-"))
    print("规则: 共 %s 条，启用 %s 条" % ((data.get("rules") or {}).get("total", 0), (data.get("rules") or {}).get("enabled", 0)))
    print("待执行: %s 条，运行中: %s" % (len(engine.get("next_runs") or []), engine.get("running", 0)))
    return EXIT_OK


def _cmd_devices(args: argparse.Namespace) -> int:
    if args.scan:
        _request(args, "POST", "/api/bluetooth/scan", {"timeout": clamp_int(args.timeout, 3, 60, 12)}, timeout=30)
        print("扫描中…")
        deadline = time.time() + clamp_int(args.timeout, 3, 60, 12) + 15
        data: Dict[str, Any] = {"scanning": True, "items": []}
        while time.time() < deadline:
            data = _request(args, "GET", "/api/bluetooth/scan")
            if not data.get("scanning"):
                break
            time.sleep(0.5)
        print("发现 %d 个设备：" % len(data.get("items") or []))
        for item in data.get("items") or []:
            print("  %s  %s%s" % (item.get("address"), item.get("name") or "未命名", "（已配对）" if item.get("paired") else ""))
        return EXIT_OK
    data = _request(args, "GET", "/api/bluetooth/devices")
    items = data.get("items") or []
    if args.json:
        _print_json(args, data)
        return EXIT_OK
    rows = []
    for item in items:
        rows.append(
            [
                item.get("address", ""),
                item.get("name") or "未命名",
                "已连接" if item.get("connected") else "未连接",
                "已配对" if item.get("paired") else "未配对",
            ]
        )
    _print_table(["地址", "名称", "连接", "配对"], rows)
    return EXIT_OK


def _cmd_connect(args: argparse.Namespace) -> int:
    data = _request(args, "POST", "/api/bluetooth/connect", {"address": args.address}, timeout=45)
    print("已连接: %s" % (data.get("address") or args.address))
    return EXIT_OK


def _cmd_disconnect(args: argparse.Namespace) -> int:
    if args.address:
        data = _request(args, "POST", "/api/bluetooth/disconnect", {"address": args.address}, timeout=30)
    else:
        devices = (_request(args, "GET", "/api/bluetooth/devices").get("items") or [])
        connected = [item for item in devices if item.get("connected")]
        if not connected:
            print("当前没有已连接的音箱。")
            return EXIT_OK
        data = _request(args, "POST", "/api/bluetooth/disconnect", {"address": connected[0]["address"]}, timeout=30)
    print("已断开: %s" % (data.get("address") or "-"))
    return EXIT_OK


def _cmd_pair(args: argparse.Namespace) -> int:
    data = _request(args, "POST", "/api/bluetooth/pair", {"address": args.address}, timeout=45)
    print("配对成功: %s" % (data.get("address") or args.address))
    return EXIT_OK


def _cmd_default(args: argparse.Namespace) -> int:
    data = _request(args, "POST", "/api/bluetooth/default", {"address": args.address})
    print("默认音箱: %s" % (data.get("default_device") or args.address))
    return EXIT_OK


def _cmd_play(args: argparse.Namespace) -> int:
    body: Dict[str, Any] = {"source": args.source, "label": args.label or "", "loop": args.loop}
    if args.volume is not None:
        body["volume"] = clamp_int(args.volume, 0, 100, 70)
    _request(args, "POST", "/api/player/play", body)
    print("已开始播放: %s" % args.source)
    return EXIT_OK


def _cmd_stop(args: argparse.Namespace) -> int:
    _request(args, "POST", "/api/player/stop")
    print("已停止播放。")
    return EXIT_OK


def _cmd_volume(args: argparse.Namespace) -> int:
    data = _request(args, "POST", "/api/player/volume", {"level": clamp_int(args.level, 0, 100, 70)})
    print("音量: %s%%" % data.get("volume", args.level))
    return EXIT_OK


def _cmd_say(args: argparse.Namespace) -> int:
    text = " ".join(args.text).strip()
    if not text:
        print("错误: 播报文本不能为空", file=sys.stderr)
        return EXIT_ERROR
    body: Dict[str, Any] = {"text": text}
    if args.volume is not None:
        body["volume"] = clamp_int(args.volume, 0, 100, 70)
    _request(args, "POST", "/api/tts/speak", body)
    print("已提交语音播报：%s" % text)
    return EXIT_OK


def _cmd_rules(args: argparse.Namespace) -> int:
    if args.action in (None, "list"):
        data = _request(args, "GET", "/api/rules")
        items = data.get("items") or []
        if args.json:
            _print_json(args, data)
            return EXIT_OK
        rows = []
        for rule in items:
            trigger = rule.get("trigger") or {}
            trigger_text = trigger.get("type", "-")
            if trigger.get("type") == "schedule":
                trigger_text = ("每天 %s" % trigger.get("at")) if trigger.get("at") else ("每 %s 分钟" % trigger.get("every_minutes"))
                if trigger.get("days"):
                    trigger_text += " 周%s" % ",".join(str(day + 1) for day in trigger["days"])
            elif trigger.get("type") == "bluetooth":
                trigger_text = "蓝牙%s" % {"connected": "连接", "disconnected": "断开", "paired": "配对"}.get(trigger.get("event", ""), "")
            elif trigger.get("type") == "boot":
                trigger_text = "开机后 %s 秒" % trigger.get("delay", 0)
            elif trigger.get("type") == "manual":
                trigger_text = "手动/API"
            rows.append([rule.get("id", ""), rule.get("name", ""), trigger_text, "启用" if rule.get("enabled") else "停用"])
        _print_table(["ID", "名称", "触发", "状态"], rows)
        print("共 %d 条，启用 %d 条" % (len(items), (data.get("counts") or {}).get("enabled", 0)))
        return EXIT_OK
    if args.action == "run":
        data = _request(args, "POST", "/api/rules/%s/run" % args.rule_id, {})
        print("已触发规则 %s（job: %s）" % (args.rule_id, data.get("job_id", "")))
        return EXIT_OK
    if args.action in ("enable", "disable"):
        data = _request(args, "POST", "/api/rules/%s/toggle" % args.rule_id, {"enabled": args.action == "enable"})
        print("规则 %s 已%s" % (data.get("name") or args.rule_id, "启用" if data.get("enabled") else "停用"))
        return EXIT_OK
    if args.action == "export":
        data = _request(args, "GET", "/api/rules/export")
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return EXIT_OK
    print("错误: 未知操作 %s" % args.action, file=sys.stderr)
    return EXIT_ERROR


def _cmd_logs(args: argparse.Namespace) -> int:
    data = _request(args, "GET", "/api/logs?limit=%d" % clamp_int(args.lines, 1, 1000, 100))
    items = data.get("items") or []
    if args.json:
        _print_json(args, data)
        return EXIT_OK
    for item in items:
        print("%s [%s] %s: %s" % (item.get("time", ""), item.get("level", ""), item.get("module", ""), item.get("message", "")))
    if not items:
        print("（暂无日志）")
    return EXIT_OK


def _cmd_diag(args: argparse.Namespace) -> int:
    data = _request(args, "GET", "/api/diag", timeout=30)
    checks = data.get("checks") or []
    if args.json:
        _print_json(args, data)
        return EXIT_OK
    for item in checks:
        mark = "OK  " if item.get("ok") else "FAIL"
        print("[%s] %s：%s" % (mark, item.get("name", ""), item.get("detail", "")))
        if item.get("hint") and not item.get("ok"):
            print("       建议: %s" % item.get("hint"))
    failed = [item for item in checks if not item.get("ok")]
    print("")
    print("诊断完成：%d 项通过，%d 项异常" % (len(checks) - len(failed), len(failed)))
    return EXIT_OK


def _cmd_info(args: argparse.Namespace) -> int:
    data = _request(args, "GET", "/api/system/info")
    if args.json:
        _print_json(args, data)
    else:
        for key, value in data.items():
            print("%s: %s" % (key, value))
    return EXIT_OK


# --------------------------------------------------------------------------- #
# 参数解析
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="btspeaker",
        description="%s（BTSpeaker）—— iStoreOS 蓝牙音箱连接与自动化服务" % APP_TITLE,
        epilog="示例: btspeaker status / btspeaker devices --scan / btspeaker say 你好 / btspeaker rules list",
    )
    parser.add_argument("--port", type=int, default=None, help="服务端口（默认 8722，可用 BTSPEAKER_PORT 覆盖）")
    parser.add_argument("--host", default=None, help="服务地址（默认 127.0.0.1）")
    parser.add_argument("--token", default=None, help="API 访问令牌（也可用 BTSPEAKER_TOKEN）")
    parser.add_argument("--json", action="store_true", help="输出原始 JSON，便于脚本处理")
    parser.add_argument("--version", action="version", version="%(prog)s " + __version__)
    subparsers = parser.add_subparsers(dest="command")

    serve = subparsers.add_parser("serve", help="前台运行服务（由 procd 守护）")
    serve.add_argument("--data-dir", default=None, help="数据目录（默认 /etc/btspeaker）")
    serve.add_argument("--host", dest="serve_host", default=None, help="监听地址")
    serve.add_argument("--port", dest="serve_port", type=int, default=None, help="监听端口")
    serve.add_argument("--sim", action="store_true", help="模拟模式（无需蓝牙硬件）")
    serve.add_argument("--log-level", default=None, help="日志级别 DEBUG/INFO/WARNING")
    serve.set_defaults(func=_cmd_serve)

    seed = subparsers.add_parser("seed", help="初始化数据目录（安装脚本调用）")
    seed.add_argument("--data-dir", default=None, help="数据目录（默认 /etc/btspeaker）")
    seed.set_defaults(func=_cmd_seed)

    status = subparsers.add_parser("status", help="查看服务状态")
    status.set_defaults(func=_cmd_status)

    devices = subparsers.add_parser("devices", help="列出/扫描蓝牙设备")
    devices.add_argument("--scan", action="store_true", help="先扫描附近设备")
    devices.add_argument("--timeout", type=int, default=12, help="扫描秒数（默认 12）")
    devices.set_defaults(func=_cmd_devices)

    connect = subparsers.add_parser("connect", help="连接音箱")
    connect.add_argument("address", help="设备 MAC，例如 AA:BB:CC:DD:EE:FF")
    connect.set_defaults(func=_cmd_connect)

    disconnect = subparsers.add_parser("disconnect", help="断开音箱（不填地址则断开当前连接）")
    disconnect.add_argument("address", nargs="?", default=None)
    disconnect.set_defaults(func=_cmd_disconnect)

    pair = subparsers.add_parser("pair", help="配对设备")
    pair.add_argument("address")
    pair.set_defaults(func=_cmd_pair)

    default = subparsers.add_parser("default", help="设置默认音箱")
    default.add_argument("address")
    default.set_defaults(func=_cmd_default)

    play = subparsers.add_parser("play", help="播放音频（本地路径 / 声音库文件名 / URL）")
    play.add_argument("source")
    play.add_argument("--label", default=None, help="显示名称")
    play.add_argument("--volume", type=int, default=None, help="音量 0-100")
    play.add_argument("--loop", action="store_true", help="循环播放")
    play.set_defaults(func=_cmd_play)

    stop = subparsers.add_parser("stop", help="停止播放")
    stop.set_defaults(func=_cmd_stop)

    volume = subparsers.add_parser("volume", help="设置音量")
    volume.add_argument("level", type=int)
    volume.set_defaults(func=_cmd_volume)

    say = subparsers.add_parser("say", help="TTS 语音播报")
    say.add_argument("text", nargs="+")
    say.add_argument("--volume", type=int, default=None)
    say.set_defaults(func=_cmd_say)

    rules = subparsers.add_parser("rules", help="自动化规则")
    rules.add_argument("action", nargs="?", default="list", choices=["list", "run", "enable", "disable", "export"])
    rules.add_argument("rule_id", nargs="?", default="")
    rules.set_defaults(func=_cmd_rules)

    logs = subparsers.add_parser("logs", help="查看日志")
    logs.add_argument("-n", "--lines", type=int, default=100)
    logs.set_defaults(func=_cmd_logs)

    diag = subparsers.add_parser("diag", help="自检诊断（蓝牙/音频/TTS 链路）")
    diag.set_defaults(func=_cmd_diag)

    info = subparsers.add_parser("info", help="系统信息")
    info.set_defaults(func=_cmd_info)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        args.command = "serve"
        args.data_dir = None
        args.serve_host = None
        args.serve_port = None
        args.sim = False
        args.log_level = None
        args.func = _cmd_serve
    func = getattr(args, "func", None)
    if func is None:
        parser.print_help()
        return EXIT_ERROR
    return int(func(args) or EXIT_OK)
