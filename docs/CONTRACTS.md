# 模块契约 v1 —— 蓝音管家 BTSpeaker

> 本文件是并行开发的**唯一事实源**。所有实现必须严格按此签名/数据结构编写。
> 任何想改动契约的地方，先在本文件提出并通知 Lead，不允许私自改接口。
> 文件写入一律用原生 `write` / `edit` 工具（`ops_write_file` 在本会话不可用）。

## 1. 项目目标与运行环境

- 产品：**蓝音管家 BTSpeaker** —— iStoreOS（OpenWrt 系）x86_64 上的独立 Web 服务应用。
- 功能：扫描/配对/连接蓝牙音箱（A2DP 播放）、声音库播放、TTS 语音播报、定时任务、蓝牙事件触发、HTTP/命令行外部触发。
- 运行环境：iStoreOS x86_64，`python3-light`（Python 3.11 左右）。**只允许标准库**，不得引入任何第三方依赖。
- 开发机：Windows + Python 3.13（测试用）。代码需保持 **Python 3.9 语法兼容**：
  - 禁止 `match`、`X | Y` 运行时类型联合（注解用 `Optional[...]`/`Union[...]`）、`dataclass(slots=True)`、`tomllib`、`zoneinfo`；
  - 每个文件开头 `from __future__ import annotations`；
  - 时间一律用 `time`/`datetime` 本地时间（路由器无 tzdata）。
- 传输/存储：JSON，UTF-8；接口返回 `ensure_ascii=False` 的中文。

## 2. 代码规范

- 中文日志、中文面向用户的错误信息；异常在模块内抛自定义异常，由 API 层统一转 JSON。
- 单文件不超过 ~900 行；函数保持短小；关键逻辑写中文注释。
- 不 `print`，统一 `from .util import get_logger` → `logger = get_logger("模块名")`。
- 子进程一律 `util.run_cmd([...])`（不经 shell）或用 `subprocess.Popen` + 参数列表；禁止字符串拼 shell。
- 所有阻塞操作（播放、扫描、命令）不得在 HTTP 请求线程里直接跑，必须丢后台线程（`api.py` 负责）。
- 测试：标准库 `unittest`，从仓库根目录 `python -m unittest discover -s tests -v` 可跑。测试不得依赖硬件/外网/第三方包。

## 3. 文件归属（写范围，禁止越界）

| 文件 | 负责人 | 说明 |
| --- | --- | --- |
| `btspeaker/__init__.py` `util.py` `config.py` `bus.py` `store.py` | Lead | 公共层，已写好 |
| `btspeaker/bluetooth.py` `btspeaker/audio.py` | bt-audio | 蓝牙后端 + 播放器 |
| `btspeaker/tts.py` `btspeaker/rules.py` `btspeaker/scheduler.py` `btspeaker/engine.py` | flow | TTS、规则模型、调度、自动化引擎 |
| `btspeaker/web/index.html` `app.js` `style.css` `favicon.svg` | face | 独立 Web 界面（无构建步骤、无 CDN） |
| `btspeaker/api.py` `btspeaker/service.py` `btspeaker/cli.py` `btspeaker/__main__.py` | Lead | HTTP 服务、装配、命令行 |
| `tests/test_bluetooth.py` `tests/test_audio.py` | bt-audio | |
| `tests/test_rules.py` `tests/test_scheduler.py` `tests/test_engine.py` | flow | |
| `tests/test_api.py` `tests/test_integration.py` `tests/test_config.py` `tests/helpers.py` | Lead | |
| `docs/RULES.md` | flow | 规则语法与示例 |
| `docs/API.md` | face | REST API 文档 |
| `README.md` `docs/INSTALL.md` `docs/FAQ.md` `install.sh` `uninstall.sh` `scripts/*` | deploy | 打包安装与部署文档 |
| `assets/sounds/*` `dist/*` | Lead | 声音库与构建产物 |

## 4. 数据目录与配置

数据目录：`$BTSPEAKER_DATA_DIR`，路由器默认 `/etc/btspeaker`，开发机默认 `./.btsdata`。
文件：`config.json`（配置）、`rules.json`（规则）、`state.json`（运行状态持久化）、`btspeaker.log`、`sounds/`（用户音频）、`tmp/`（下载/TTS 临时文件）、`sim_devices.json`（模拟设备）。

`Config`（`btspeaker/config.py`，已实现）：
```python
cfg = Config.load(data_dir=None)          # 读取/合并默认值
cfg.get("player.volume", 70)              # 点号路径读取
cfg.set("player.volume", 80); cfg.save()  # 修改并落盘
cfg.to_dict() / cfg.as_public_dict()      # 全量 / 脱敏（api_token → ******）
cfg.data_dir, cfg.port, cfg.host, cfg.state_path, cfg.rules_path, cfg.log_file,
cfg.web_dir, cfg.sounds_dirs(), cfg.sim_devices_path, cfg.is_sim(), cfg.ensure_dirs()
```
默认配置项（键名不可改）：
```json
{
  "version": 1,
  "server": {"host": "0.0.0.0", "port": 8722, "title": "蓝音管家"},
  "security": {"api_token": "", "allow_commands": false, "command_timeout": 20, "command_allowlist": []},
  "bluetooth": {"backend": "cli", "cli": "bluetoothctl", "adapter": "hci0", "poll_interval": 5,
                "scan_timeout": 12, "auto_reconnect": true, "default_device": "", "trust_on_connect": true},
  "player": {"backend": "auto", "volume": 70,
             "alsa_device": "bluealsa:HCI={hci},DEV={mac},PROFILE=a2dp",
             "mpg123": "mpg123", "aplay": "aplay", "ffmpeg": "ffmpeg",
             "max_duration": 900, "loop_gap": 1, "volume_command": ""},
  "tts": {"backend": "auto", "url": "", "method": "GET", "headers": {}, "body_template": "",
          "voice": "zh", "speed": 150, "espeak": "espeak", "timeout": 20, "format": "mp3"},
  "rules": {"enabled": true, "max_concurrent": 1, "default_cooldown": 0},
  "logging": {"level": "INFO", "max_records": 500},
  "sounds": {"dirs": ["/usr/share/btspeaker/sounds"], "max_upload_mb": 20},
  "sim": {"devices_file": ""}
}
```

## 5. 公共层 API（已实现，直接用）

`util.py`
```python
get_logger(name="") -> logging.Logger             # btspeaker.<name>
setup_logging(level, log_file=None, capacity=500) -> LogRingHandler
run_cmd(argv, timeout=20.0, env=None, input_text=None, cwd=None) -> CmdResult
CmdResult(returncode, stdout, stderr, timed_out, argv).ok / .text()
which(name) -> Optional[str]
iso_now(dt=None) -> "2026-11-15T20:00:00+08:00"
local_minute(dt=None) -> "20:00";  local_weekday(dt=None) -> 0=周一..6=周日
render_template(text, ctx) -> str                 # {var} 替换，未知保留
json_dumps(obj) -> str;  read_json(path, default);  write_json(path, obj)
atomic_write_text(path, text);  parse_bool / parse_int / clamp_int
human_size / human_duration / new_id(prefix) / mask_secret / wait_or_stop(stop_event, sec)
get_log_ring() -> LogRingHandler | None           # .records(limit, level) / .clear() / .total()
```
`bus.py`
```python
from .bus import EventBus, EV_BT_CONNECTED, EV_PLAYER_STARTED, ...
bus = EventBus()
token = bus.subscribe("bluetooth.connected", handler)   # handler(event: str, payload: dict)
bus.unsubscribe(token);  bus.publish("player.finished", {"source": "..."})
```
事件名：`bluetooth.connected` `bluetooth.disconnected` `bluetooth.paired` `bluetooth.scan_done`
`player.started` `player.stopped` `player.finished` `player.failed`
`rule.fired` `rule.finished` `rule.error` `system.boot` `system.shutdown`；支持 `"*"` 通配。
`store.py`
```python
JsonStore(path, default_factory=dict).read()/get(key)/write(dict)/update(mutator)/set(key, value)
RuntimeState().set(k, v) / get(k, default) / update(mapping) / incr(k) / snapshot()
```
`RuntimeState` 扁平点号键（可新增键，但以下为约定键）：
`bluetooth.available` `bluetooth.powered` `bluetooth.adapter`(dict) `bluetooth.connected`(device|null)
`bluetooth.devices`(list) `bluetooth.scanning`(bool) `player.playing`(bool) `player.current`(dict|null)
`player.volume`(int) `last_event`(dict|null) `started_at`(iso) `pid`(int)

## 6. 蓝牙模块 `btspeaker/bluetooth.py`（bt-audio）

```python
class BluetoothError(Exception)

@dataclass
class Device:
    address: str                 # 大写 MAC，AA:BB:CC:DD:EE:FF
    name: str = ""
    paired: bool = False
    connected: bool = False
    trusted: bool = False
    icon: str = ""
    def to_dict(self) -> dict    # {"address","name","paired","connected","trusted","icon"}

def normalize_address(value: str) -> str          # 非法则原样 strip 返回

class BluetoothBackend:      # 抽象基类
    name: str
    def available(self) -> bool                   # 后端工具/适配器是否可用
    def adapter_info(self) -> dict                 # {"address","name","powered","discovering","hci"}
    def power_on(self) -> bool
    def list_devices(self, paired_only: bool = False) -> List[Device]
    def device_info(self, address: str) -> Optional[Device]
    def scan(self, timeout: float = 12.0) -> List[Device]     # 阻塞；返回发现的全部设备
    def pair(self, address: str) -> bool
    def connect(self, address: str) -> bool
    def disconnect(self, address: str) -> bool
    def remove(self, address: str) -> bool
    def trust(self, address: str) -> bool
    def diag(self) -> dict                         # {"tool": "...", "ok": bool, "detail": "..."}

class CliBackend(BluetoothBackend):      # 用 bluetoothctl 实现（OpenWrt 默认）
    def __init__(self, cfg, logger=None)
class SimBackend(BluetoothBackend):      # 模拟后端（开发/测试，读写 cfg.sim_devices_path）
    def __init__(self, cfg, logger=None)
def create_backend(cfg, logger=None) -> BluetoothBackend   # 按 cfg.bluetooth.backend 选择 cli/sim

class BluetoothMonitor(threading.Thread):
    def __init__(self, cfg, backend, bus, state, interval: Optional[float] = None, logger=None)
    def stop(self) -> None
    def poll_once(self, publish: bool = True) -> dict    # 返回本次快照，供测试
    # run(): 每 interval 秒轮询；检测到状态迁移时 publish：
    #   bluetooth.connected    {"address","name","device":{...},"at": iso}
    #   bluetooth.disconnected {"address","name","device":{...},"at": iso}
    # 并更新 RuntimeState 的 bluetooth.* 键
```
要求：
- `CliBackend` 用 `bluetoothctl` 非交互命令：`--timeout <t> scan on`（扫描）、`devices`、`info <mac>`、`pair/connect/disconnect/remove/trust <mac>`、`show`（适配器）。解析要容忍 BlueZ 版本差异，失败返回 `False`/空列表并记日志，不抛异常（只有明显参数错误抛 `BluetoothError`）。
- `SimBackend` 设备文件格式（数组）：
```json
[{"address": "AA:BB:CC:11:22:33", "name": "客厅小爱音箱", "paired": true,
  "connected": true, "trusted": true, "icon": "audio-card"}]
```
  `scan()` 返回文件中 `"discoverable": true` 的设备加上已配对设备；`connect()` 把该设备 connected=true 且其它设备 connected=false 并写回文件；`pair/trust/disconnect/remove` 同理写回。文件缺失时用内置 3 个示例设备（2 个已配对、1 个可发现）自动创建。
- `BluetoothMonitor.poll_once` 必须在 30 秒内完成（`bluetoothctl` 命令都带超时）；首次轮询不产生 connected 事件（避免启动即误触发），只建立基线（除非基线就是已连接且有 `--emit-initial` 需求？不需要）。

## 7. 音频模块 `btspeaker/audio.py`（bt-audio）

播放链路：`音频文件 → mpg123/aplay/ffmpeg 解码 → ALSA PCM "bluealsa:HCI=hci0,DEV=<MAC>,PROFILE=a2dp" → 蓝牙音箱`。
设备串由 `cfg.player.alsa_device` 模板渲染（`{mac}` 大写 MAC、`{hci}` 适配器名）；`cfg.player.volume_command` 非空时可用于设置硬件音量（`{level}` 占位）。

```python
class AudioError(Exception)

@dataclass
class PlayRequest:
    source: str                  # 本地绝对路径 / 声音库文件名 / http(s) URL
    label: str = ""
    volume: Optional[int] = None     # 0-100；None 用 cfg.player.volume
    loop: bool = False
    address: Optional[str] = None    # 目标音箱 MAC；None 用当前连接/默认设备
    rule_id: Optional[str] = None

@dataclass
class PlayResult:
    ok: bool
    source: str = ""
    backend: str = ""
    duration: float = 0.0
    error: str = ""
    skipped: bool = False        # stop() 打断时为 True

class Player:
    def __init__(self, cfg, bus, logger=None)
    def probe(self) -> dict
        # {"ready": bool, "backend": "bluealsa"|"mock"|"none",
        #  "tools": {"mpg123": bool, "aplay": bool, "ffmpeg": bool},
        #  "bluealsa_pcm": bool, "hint": "缺什么/怎么装"}
    def ready(self) -> bool
    def play(self, req: PlayRequest) -> PlayResult   # 阻塞直到播完/被 stop()/失败；内部串行锁
    def stop(self) -> bool
    def is_playing(self) -> bool
    def current(self) -> Optional[dict]
        # {"playing","source","label","address","volume","backend","started_at","elapsed"}
    def get_volume(self) -> int
    def set_volume(self, level: int) -> int          # 限制 0-100；返回生效值
    def resolve_source(self, source: str) -> str     # URL 下载到 data_dir/tmp/、声音库相对名补全、本地文件校验
    def diag(self) -> dict                            # 诊断用，见 API /api/diag
```
行为要求：
- `probe()`：`player.backend == "mock"` 或 `cfg.is_sim()` → mock（ready=True）；
  `auto` 时若 `which(aplay)` 且 `aplay -L` 输出含 `bluealsa` → bluealsa ready；否则 `none` + 中文 hint（提示安装 `bluealsa alsa-utils mpg123`）。
- 播放：mp3 → `mpg123 -q -o alsa -a <pcm> -f <scale>`（volume 100 时省略 `-f`）；wav → 可选 Python 软增益（`wave`+`array`，仅 16bit）后 `aplay -q -D <pcm>`；其它格式 → `ffmpeg` 转 wav 管道给 aplay（Popen）。全部带 `cfg.player.max_duration` 看门狗。
- 发布事件：`player.started {source,label,address,rule_id,backend,volume}`、`player.finished {source,duration,reason}`、`player.stopped {source,reason}`、`player.failed {source,error}`；同时维护内部 current 状态。
- `mock` 后端：不发真实音频，播放 `min(2.5, 估计时长)` 秒（可被 stop 打断），正常发事件，用于开发/测试。`resolve_source` 对 URL 在 mock 下直接返回 `"mock://url"`，不联网。
- 任何失败都给**可操作的中文错误**（如「mpg123 未安装：opkg install mpg123」「未连接蓝牙音箱」）。

## 8. TTS `btspeaker/tts.py`（flow）

```python
class TtsError(Exception)

class TtsEngine:
    def __init__(self, cfg, logger=None)
    def probe(self) -> dict
        # {"backend": "http"|"espeak"|"mock"|"none", "ready": bool, "hint": str}
    def synthesize(self, text: str, voice: Optional[str] = None) -> str   # 返回本地 wav/mp3 路径
    def cleanup(self) -> int            # 清理过期临时文件，返回删除数量
```
- `http` 后端（默认推荐，中文质量最好）：`cfg.tts.url` 作为模板，`{text}` 做 URL 编码替换（GET），`{lang}` 替换为 `cfg.tts.voice`；`method=POST` 时用 `body_template`（同为模板替换，空则直接发送原文），headers 来自 `cfg.tts.headers`。响应按 Content-Type 落盘（audio/mpeg→.mp3，audio/wav|audio/x-wav→.wav，audio/ogg→.ogg，其它用 `cfg.tts.format`）；非音频响应抛 `TtsError("TTS 服务返回了非音频内容: <前120字>")`。
- `espeak` 后端：`espeak -v <voice> -s <speed> -w <out.wav> <text>`（离线，音质机械，作为离线兜底）。
- `mock`：生成 0.6 秒静音 wav（`wave` 模块，22050Hz/16bit/单声道），日志记录文本，供开发测试。
- `auto`：`cfg.tts.url` 非空 → http；否则 `which(espeak)` → espeak；否则报错 `TtsError("未配置 TTS：请在设置里填写 TTS 服务地址，或安装 espeak 作为离线兜底")`。
- 临时文件放 `cfg.data_dir/tmp/`；`cleanup()` 删除 1 小时前的文件。

## 9. 规则模型 `btspeaker/rules.py`（flow）

```python
TRIGGER_TYPES = ("schedule", "bluetooth", "boot", "manual")
ACTION_TYPES  = ("play", "tts", "volume", "connect", "disconnect", "command", "http", "wait", "log")
class RuleError(Exception)
def new_rule(name="", trigger=None, actions=None) -> dict
def normalize_rule(rule: dict) -> dict        # 补 id/created_at/updated_at/enabled/cooldown/conditions 等默认
def validate_rule(rule: dict) -> List[str]    # 中文错误列表，空列表=合法
class RuleStore:
    def __init__(self, path, logger=None)     # 文件 {"version":1,"rules":[...]}
    def list(self) -> List[dict]
    def get(self, rule_id) -> Optional[dict]
    def add(self, rule) -> dict
    def update(self, rule_id, patch) -> dict       # 非法抛 RuleError
    def delete(self, rule_id) -> bool
    def set_enabled(self, rule_id, enabled) -> dict
    def replace_all(self, rules) -> List[dict]
    def counts(self) -> dict                       # {"total": n, "enabled": n}
```
规则 JSON：
```json
{
  "id": "r_ab12cd34", "name": "早安播报", "enabled": true,
  "created_at": "...", "updated_at": "...", "cooldown": 0,
  "trigger": {"type": "schedule", "at": "07:30", "days": [0,1,2,3,4]},
  "conditions": [],
  "actions": [{"type": "tts", "text": "早上好，今天是{date}，星期{weekday}。"}]
}
```
触发器（`trigger.type`）：
- `schedule`：二选一
  - `{"type":"schedule","at":"HH:MM","days":[0..6]}` —— days 可省略/空数组=每天；`at` 必填且 `^([01]\d|2[0-3]):[0-5]\d$`
  - `{"type":"schedule","every_minutes":30}` —— 1..1440 的整数
- `bluetooth`：`{"type":"bluetooth","event":"connected"|"disconnected"|"paired","device":"MAC或空"}`（device 为空=任意设备）
- `boot`：`{"type":"boot","delay":5}` —— 开机 delay 秒后触发（0..3600）
- `manual`：`{"type":"manual"}` —— 只手动/HTTP 触发
条件（`conditions`，全部满足才执行）：
- `{"type":"connected","value":true}` —— 是否有音箱已连接
- `{"type":"time_between","start":"08:00","end":"22:00"}` —— 支持跨夜（start>end）
- `{"type":"weekday_in","days":[0..6]}`
动作（`actions`，按顺序执行，单条失败不中断后续，但整体 `ok=false`）：
- `play`：`{"type":"play","source":"chime.wav","label":"提示音","volume":70,"loop":false}`
- `tts`：`{"type":"tts","text":"...","voice":"zh","volume":70}`
- `volume`：`{"type":"volume","level":70}`
- `connect`：`{"type":"connect","address":"MAC或空(默认设备)"}`
- `disconnect`：`{"type":"disconnect"}`
- `command`：`{"type":"command","command":"sh /root/xxx.sh","timeout":20}`（需 `security.allow_commands=true`，且受 `command_allowlist` 前缀限制）
- `http`：`{"type":"http","url":"http://...","method":"GET","headers":{},"body":"","timeout":10}`（支持模板变量）
- `wait`：`{"type":"wait","seconds":2}`（0..300）
- `log`：`{"type":"log","message":"..."}`
校验规则：`name` 1~64 字符；至少 1 个动作；未知 type 报错；`play.source`、`tts.text`、`http.url`、`command.command` 必填；数值范围检查。错误信息中文、指明字段。

## 10. 调度器 `btspeaker/scheduler.py`（flow）

```python
class Scheduler(threading.Thread):
    def __init__(self, cfg, rules_provider, on_due, state=None, logger=None, clock=None)
        # rules_provider: Callable[[], List[dict]] 取当前启用规则
        # on_due: Callable[[dict, str], None]      触发规则；第二参为触发来源 "schedule"
        # clock: Callable[[], datetime] 可注入（测试）
    def start(self) / stop(self)
    def reload(self) -> None                 # 规则变更后调用
    def tick(self, now=None) -> int          # 同步执行一次匹配，返回触发数量（单测直接调用）
    def next_runs(self, limit=5) -> List[dict]   # [{"rule_id","name","at": iso,"in": 秒}]
```
匹配逻辑：
- `at` 型：当前 `HH:MM` 等于 at、星期匹配（days 空=任意）、且本分钟未触发过（内存去重键 `rule_id + "YYYY-MM-DD HH:MM"`）→ 触发。
- `every_minutes` 型：内存维护 next_epoch（启动时=now+interval），`now >= next` 触发并把 next 顺延；不追补多次。
- 只处理 `trigger.type == "schedule"`；其余类型由 engine 处理。
- 线程每 1 秒 tick 一次；异常必须捕获记日志，线程不能死。
- `next_runs` 供 UI 展示，计算下一次触发时间，按时间排序。

## 11. 自动化引擎 `btspeaker/engine.py`（flow）

```python
class AutomationEngine:
    def __init__(self, cfg, bus, player, tts, bt, rules, state, persist=None, logger=None)
        # persist: Optional[JsonStore]，保存 {"last_runs": {rule_id: {"at": iso, "ts": epoch}}, "counters": {...}}
    def start(self) -> None              # 启动 worker 线程 + 订阅 bus + 启动 scheduler + 发布 system.boot 处理
    def stop(self) -> None
    def run_rule(self, rule_id, context=None) -> dict
        # 异步入队：{"accepted": bool, "job_id": str, "reason": str}
    def execute_rule(self, rule, context=None) -> dict
        # 同步执行：{"ok","rule_id","rule_name","trigger","started_at","duration",
        #            "actions":[{"type","ok","message"}],"error":""}
    def status(self) -> dict             # {"running","queued","scheduler_running","next_runs":[...],"recent":[...]}
    def reload_rules(self) -> None
    def recent(self, limit=20) -> List[dict]   # 最近执行记录，倒序
    def render_context(self, rule=None, payload=None) -> dict
```
模板变量（tts.text / http.url / http.body / log.message 都渲染）：`{time}` `{date}` `{datetime}` `{weekday}`
`{hostname}` `{bt_name}` `{bt_addr}` `{volume}` `{rule_name}` `{event}`。
执行语义：
- 进入条件：规则存在且启用；`rules.enabled` 为 false 则拒绝；冷却期（`cooldown` 秒，基于上次执行时间）；`conditions` 全部满足。
- 动作顺序执行；`play`/`tts` 调 player 阻塞播放，`tts` 先 `tts.synthesize` 再 `player.play`。
- `command` 安全检查：未开启 → 该动作失败并记「未开启命令执行权限」；allowlist 非空时前缀不匹配 → 失败。
- 发布 event：开始 `rule.fired {rule_id,rule_name,trigger,job_id}`；结束 `rule.finished {...}` 或 `rule.error {rule_id,rule_name,error}`。
- 持久化 last_runs（供 UI 显示上次执行时间/冷却判断）；`_recent` 内存 deque 保留 50 条。
- bus 订阅：`bluetooth.connected/disconnected/paired` → 匹配 `trigger.type=="bluetooth"` 的启用规则；`system.boot` → 匹配 boot 规则（delay 由 engine 用 threading.Timer 延迟入队）。
- worker 线程数 = `max(1, cfg.rules.max_concurrent)`；队列入队满（>100）时拒绝并返回 reason。
- 所有异常不外抛，记日志 + 写 recent。

## 12. REST API（Lead 实现 `api.py`，face 按此写 UI）

统一信封：成功 `{"ok":true,"data":...}`；失败 HTTP 4xx/5xx + `{"ok":false,"error":{"code":"bad_request","message":"中文"}}`。
鉴权：`cfg.security.api_token` 非空时，除 `/api/health` 和静态资源外的 `/api/*` 都需要 `X-Api-Token: <token>` 或 `?token=`；否则 401 `unauthorized`。
端口：`cfg.server.port`（默认 8722），监听 `cfg.server.host`。

| Method | Path | Body / Query | data |
| --- | --- | --- | --- |
| GET | `/api/health` | — | `{"status":"ok","version":"1.0.0"}` |
| GET | `/api/status` | — | 见下方 Status |
| GET | `/api/system/info` | — | `{"version","python","platform","data_dir","uptime","sim","hostname","started_at","project"}` |
| GET | `/api/settings` | — | `{"config": {...脱敏}}` |
| PUT | `/api/settings` | `{"config": {...partial}}` | `{"config", "restart_required": bool}` |
| GET | `/api/bluetooth/devices` | — | `{"items":[device],"connected":device\|null,"adapter":{...}}` |
| POST | `/api/bluetooth/scan` | `{"timeout":12}` | `{"job_id","scanning":true}` |
| GET | `/api/bluetooth/scan` | — | `{"scanning":bool,"items":[device],"started_at","error":""}` |
| POST | `/api/bluetooth/connect` | `{"address":"MAC"}` | `{"address","connected":true}` |
| POST | `/api/bluetooth/disconnect` | `{"address":"MAC"}` | `{"address","connected":false}` |
| POST | `/api/bluetooth/pair` | `{"address":"MAC"}` | `{"address","paired":true}` |
| POST | `/api/bluetooth/default` | `{"address":"MAC"}` | `{"default_device":"MAC"}` |
| DELETE | `/api/bluetooth/devices/{address}` | — | `{"removed":true}` |
| GET | `/api/player` | — | `{"playing","current","volume","probe":{...}}` |
| POST | `/api/player/play` | `{"source","label","volume","loop","address"}` | `{"accepted":true,"job_id"}` |
| POST | `/api/player/stop` | — | `{"stopped":true}` |
| POST | `/api/player/volume` | `{"level":0-100}` | `{"volume":70}`（持久化到 config） |
| POST | `/api/tts/speak` | `{"text","voice","volume"}` | `{"accepted":true,"job_id"}` |
| GET | `/api/rules` | — | `{"items":[rule],"counts":{"total","enabled"}}` |
| POST | `/api/rules` | rule 字段 | rule |
| GET | `/api/rules/{id}` | — | rule |
| PUT | `/api/rules/{id}` | 部分字段 | rule |
| DELETE | `/api/rules/{id}` | — | `{"deleted":true}` |
| POST | `/api/rules/{id}/run` | — | `{"accepted":true,"job_id"}` |
| POST | `/api/rules/{id}/toggle` | `{"enabled":bool}` | rule |
| GET | `/api/rules/export` | — | `{"version":1,"rules":[...]}` |
| POST | `/api/rules/import` | `{"rules":[...],"mode":"merge"\|"replace"}` | `{"imported":n}` |
| GET | `/api/library` | — | `{"items":[{"name","path","size","size_text","source"}],"dirs":[...]}` |
| GET | `/api/logs` | `?limit=200&level=` | `{"items":[{"time","level","module","message"}],"total","capacity"}` |
| DELETE | `/api/logs` | — | `{"cleared":true}` |
| GET | `/api/events` | `?limit=50` | `{"items":[执行记录]}` |
| GET | `/api/diag` | — | `{"checks":[{"name","ok","detail","hint"}]}` |
| POST/GET | `/api/hooks/{rule_id}` | 任意 query 作为上下文 | `{"fired":true,"rule_id","job_id"}` |

Status `data`：
```json
{
  "version": "1.0.0", "app": "蓝音管家", "uptime": 123, "started_at": "...", "hostname": "iStoreOS", "sim": true,
  "bluetooth": {"available": true, "powered": true,
                "adapter": {"address": "...", "name": "...", "hci": "hci0"},
                "scanning": false, "connected": null, "devices": []},
  "player": {"playing": false, "current": null, "volume": 70,
             "backend": "mock", "probe": {"ready": true, "tools": {}, "hint": ""}},
  "rules": {"total": 3, "enabled": 2},
  "engine": {"running": 0, "queued": 0, "next_runs": [{"rule_id","name","at","in"}]},
  "tts": {"backend": "mock", "ready": true},
  "last_event": null
}
```
device：`{"address","name","paired","connected","trusted","icon"}`。

## 13. Web UI（face）

静态文件：`btspeaker/web/index.html` + `app.js` + `style.css` + `favicon.svg`，由 api.py 直接托管（`/`、`/app.js`…），**不允许任何 CDN / 外链字体 / 构建步骤**，必须支持纯离线局域网使用。
单页应用，hash 路由 4 页：
1. `#/dashboard` 总览：适配器/连接状态卡、当前播放卡（含音量滑块与停止）、快捷动作（扫描、TTS 播报、播放提示音）、下一批定时任务、最近日志、规则统计。
2. `#/devices` 音箱：已配对列表（连接/断开/设为默认/删除）、扫描（含发现列表与配对按钮）、音量、播放控制。
3. `#/rules` 自动化：规则列表（开关/立即执行/编辑/删除）、新建与编辑表单（触发器四类 + 条件 + 多动作动态增删）、导出/导入 JSON。
4. `#/settings` 设置：TTS、播放器、蓝牙、安全；系统信息；「模拟模式」显著提示。
要求：全中文；每 3 秒轮询 `/api/status`；操作有 loading/toast 反馈；空状态有引导文案；桌面 ≥1200px 三列栅格、手机 375px 单列可用；401 时弹出 token 输入并存 localStorage（`bts_token`），之后所有请求带 `X-Api-Token`；键盘可达（button/输入框语义正确）。
视觉方向：**v2.0 起改为归藏瑞士风（Swiss · 克莱因蓝 ikb）**——冷灰纸面（页面 `#f0f0ee`、卡片 `#fafaf8`，用背景色分层而非边框）、系统无衬线、唯一强调色 `#002FA7`、全直角、零渐变零阴影、mono 微标签、表格化列表、单页最多一个 accent 实底主按钮。**完整规范见 `docs/DESIGN-SWISS.md`（唯一视觉验收依据）**。不要深色背景、霓虹、玻璃拟态、投影、彩色状态色，不要 emoji 当图标（用内联 SVG）。

## 14. 开发与模拟模式

- 无硬件即可全功能运行：
  ```bat
  set BTSPEAKER_SIM=1
  python -m btspeaker serve --data-dir .btsdata --port 8722
  ```
  （`BTSPEAKER_SIM=1` 时 config 自动切 `bluetooth.backend=sim`、`player.backend=mock`、`tts.backend=mock`，模拟设备来自 `.btsdata/sim_devices.json`，缺失时 `SimBackend` 自动创建示例。）
- 测试命令（仓库根目录）：`python -m unittest discover -s tests -v`
- 单模块测试：`python -m unittest tests.test_rules -v`
- 新增文件必须放进归属表；测试文件必须能独立运行。
- 提交前自查：`python -m compileall btspeaker tests -q` 无错误、自己的测试全绿。

## 15. 内置示例规则（install 时种子，也用于 UI 演示）

1. 早安播报（`schedule` 07:30，周一至周五，tts + 音量 60）
2. 回家迎宾（`bluetooth` connected，play 欢迎音 + tts 欢迎语，冷却 300 秒）
3. 整点报时（`schedule` every_minutes=60，tts「现在是{time}」+ chime）
4. 深夜自动静音（`schedule` 23:00，volume 15）
5. 手动测试播报（`manual`，tts + chime）
