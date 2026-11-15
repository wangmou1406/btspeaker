# 规则语法与示例（蓝音管家 BTSpeaker）

规则文件：`$BTSPEAKER_DATA_DIR/rules.json`，UTF-8 JSON：

```json
{"version": 1, "rules": [ { ... }, { ... } ]}
```

Web 界面「自动化」页、`/api/rules` 接口、`POST /api/hooks/<rule_id>` 外部触发都使用本文档的结构。
所有写操作（新建 / 修改 / 导入）都会先校验，非法时返回中文错误并指名字段，不会落盘。

---

## 1. 规则结构

```json
{
  "id": "r_ab12cd34",
  "name": "早安播报",
  "enabled": true,
  "created_at": "2026-11-15T20:00:00+08:00",
  "updated_at": "2026-11-15T20:00:00+08:00",
  "cooldown": 0,
  "trigger": {"type": "schedule", "at": "07:30", "days": [0, 1, 2, 3, 4]},
  "conditions": [],
  "actions": [
    {"type": "tts", "text": "早上好，今天是{date}，星期{weekday}。"}
  ]
}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | string | 规则 ID，新建时自动生成（`r_` + 8 位十六进制），导入时可沿用 |
| `name` | string | 规则名称，**必填**，1~64 个字符 |
| `enabled` | bool | 是否启用，默认 `true`；停用的规则不会被调度 / 事件触发，但可手动执行（会返回停用错误） |
| `cooldown` | int | 冷却秒数（0~604800），默认 `0`；为 0 时使用全局设置 `rules.default_cooldown` |
| `trigger` | object | 触发器，**必填**，四类之一，见下文 |
| `conditions` | array | 条件列表，全部满足才执行，可为空 |
| `actions` | array | 动作列表，**至少 1 个**，按顺序执行 |
| `created_at` / `updated_at` | string | ISO8601 本地时间，自动维护 |
| `enabled` 之外的未知字段 | — | 原样保留（便于界面扩展 / 导入导出） |

---

## 2. 触发器 `trigger`

### 2.1 定时 `schedule`（二选一）

**固定时间（`at`）**：

```json
{"type": "schedule", "at": "07:30", "days": [0, 1, 2, 3, 4]}
```

- `at`：`HH:MM`（24 小时制，`^([01]\d|2[0-3]):[0-5]\d$`），必填；
- `days`：星期数组，`0=周一 … 6=周日`；省略或空数组 = 每天；
- 同一分钟内只触发一次（按「规则 ID + 日期时间」去重）。

**周期（`every_minutes`）**：

```json
{"type": "schedule", "every_minutes": 60}
```

- 整数 1~1440（分钟）；
- 服务启动 / 规则变更后从当前时刻起完整等一个周期才触发；
- 若设备休眠等原因错过多个周期，只补触发一次，不会连续补发。

### 2.2 蓝牙事件 `bluetooth`

```json
{"type": "bluetooth", "event": "connected", "device": "AA:BB:CC:DD:EE:FF"}
```

- `event`：`connected`（已连接）/ `disconnected`（已断开）/ `paired`（已配对）；
- `device`：MAC 地址，可省略或留空 = 任意设备。

### 2.3 开机 `boot`

```json
{"type": "boot", "delay": 5}
```

- 服务启动后由服务层发布 `system.boot` 事件，引擎匹配开机规则；
- `delay`：0~3600 秒，默认 5；`threading.Timer` 延迟入队，适合等蓝牙适配器就绪（如 `delay: 10`）；
- **`engine.start()` 本身不触发开机规则**，只有收到 `system.boot` 才触发。

### 2.4 手动 / 外部触发 `manual`

```json
{"type": "manual"}
```

- 只通过 Web 界面「立即执行」、`POST /api/rules/{id}/run` 或外部钩子
  `GET/POST /api/hooks/{rule_id}?key=value` 触发；
- 钩子的查询参数会作为模板上下文变量（如 `?event=door` 可用 `{event}`）。

---

## 3. 条件 `conditions`

所有条件必须同时满足（AND）。条件在动作执行前求值，不满足时规则记一次「被拒绝」，
不执行任何动作，也不占用冷却时间。

### 3.1 `connected` —— 是否有音箱已连接

```json
{"type": "connected", "value": true}
```

- `value: true`：当前有蓝牙音箱连接才执行；
- `value: false`：当前没有连接才执行（如「断开后自动暂停」）。

### 3.2 `time_between` —— 时间段（支持跨夜）

```json
{"type": "time_between", "start": "08:00", "end": "22:00"}
```

- `HH:MM` 格式；`start <= 当前时间 < end`；
- `start > end` 表示跨夜，例如 `22:00 ~ 06:00` 在 23:30 与 05:00 都满足；
- `start == end` 视为全天满足。

### 3.3 `weekday_in` —— 星期

```json
{"type": "weekday_in", "days": [0, 1, 2, 3, 4]}
```

- `0=周一 … 6=周日`；`days` 不能为空。

---

## 4. 动作 `actions`

动作按数组顺序执行；**单条动作失败不会中断后续动作**，但整条规则的执行结果 `ok=false`，
并在 `recent` / `rule.error` 事件中记录第一条失败原因。

### 4.1 `play` —— 播放声音库 / 本地文件 / URL

```json
{"type": "play", "source": "chime.wav", "label": "提示音", "volume": 70, "loop": false}
```

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `source` | 是 | 声音库文件名（如 `chime.wav`）、本地绝对路径或 `http(s)://` URL |
| `label` | 否 | 界面展示名称 |
| `volume` | 否 | 0~100；省略 = 使用当前音量 |
| `loop` | 否 | 是否循环播放，默认 `false`（**谨慎使用**，会一直播放到停止） |
| `address` | 否 | 目标音箱 MAC；省略 = 当前连接 / 默认设备 |

### 4.2 `tts` —— 语音播报

```json
{"type": "tts", "text": "现在是{time}，室内温度适宜。", "voice": "zh", "volume": 60}
```

- `text` 必填，支持模板变量；
- 先 `TtsEngine.synthesize()` 合成到 `data_dir/tmp/`，再交给播放器播放（阻塞到播完）；
- `voice` 省略 = 使用设置里的 `tts.voice`。

### 4.3 `volume` —— 调整音量

```json
{"type": "volume", "level": 15}
```

- `level` 必填，0~100 整数；
- 同时更新播放器音量、运行状态与 `config.json` 的 `player.volume`（持久化）。

### 4.4 `connect` / `disconnect` —— 连接 / 断开音箱

```json
{"type": "connect", "address": "AA:BB:CC:DD:EE:FF"}
{"type": "disconnect"}
```

- `connect.address` 省略 = 使用 `bluetooth.default_device`，都没有则失败；
- `disconnect.address` 省略 = 当前已连接设备，其次默认设备。

### 4.5 `command` —— 执行本机命令（受限）

```json
{"type": "command", "command": "sh /root/btspeaker/notify.sh hello", "timeout": 20}
```

- **安全开关**：必须在设置中开启 `security.allow_commands=true`，否则该动作失败并提示「未开启命令执行权限」；
- **白名单**：`security.command_allowlist` 非空时，命令字符串必须以其中一项为**前缀**，否则拒绝；
- 命令经 `shlex` 拆分后以参数数组直接执行（**不经过 shell**），因此 `|`、`&&`、`>` 等 shell 语法无效；
  需要复杂逻辑请写成脚本文件（如 `sh /root/xxx.sh 参数`）；
- `timeout` 1~300 秒，默认 `security.command_timeout`（20 秒）。

### 4.6 `http` —— 请求 HTTP 接口

```json
{"type": "http", "url": "http://192.168.1.10:8080/hook?msg={rule_name}",
 "method": "POST", "headers": {"X-Token": "abc"}, "body": "{\"text\":\"{time}\"}", "timeout": 10}
```

- `url` 必填，必须以 `http://` 或 `https://` 开头，支持模板变量；
- `method`：`GET/POST/PUT/PATCH/DELETE/HEAD`，默认 `GET`；`GET/HEAD` 不发送 body；
- `headers` 对象，值也支持模板变量；`body` 支持模板变量；
- 2xx/3xx 视为成功，4xx/5xx 视为失败（记录 HTTP 状态码）；
- `timeout` 1~120 秒，默认 10。

### 4.7 `wait` —— 等待

```json
{"type": "wait", "seconds": 2}
```

- 0~300 秒；用于连续动作之间留间隔（如先播提示音、等 2 秒再播报）。

### 4.8 `log` —— 写日志

```json
{"type": "log", "message": "规则 {rule_name} 于 {datetime} 执行完成"}
```

- `message` 必填，支持模板变量，写入服务日志（`data_dir/btspeaker.log` 与 Web「日志」页）。

### 4.9 动作一览

| type | 必填字段 | 可选字段 |
| --- | --- | --- |
| `play` | `source` | `label` `volume` `loop` `address` |
| `tts` | `text` | `voice` `volume` |
| `volume` | `level` | — |
| `connect` | — | `address` |
| `disconnect` | — | `address` |
| `command` | `command` | `timeout` |
| `http` | `url` | `method` `headers` `body` `timeout` |
| `wait` | `seconds` | — |
| `log` | `message` | — |

---

## 5. 模板变量

以下变量可用于 `tts.text`、`http.url`、`http.body`、`log.message`、`play.source/label`、
`connect/disconnect.address` 等文本字段；未知变量原样保留（如 `{unknown}`）。

| 变量 | 示例 | 说明 |
| --- | --- | --- |
| `{time}` | `07:30` | 当前时间（时:分） |
| `{date}` | `2026-11-16` | 当前日期 |
| `{datetime}` | `2026-11-16 07:30:00` | 日期 + 时间 |
| `{weekday}` | `一` | 星期（一/二/三/四/五/六/日，配合「星期{weekday}」使用） |
| `{hostname}` | `iStoreOS` | 设备主机名 |
| `{bt_name}` | `客厅小爱音箱` | 当前连接音箱名称（未连接为空） |
| `{bt_addr}` | `AA:BB:CC:11:22:33` | 当前连接音箱 MAC（未连接为空） |
| `{volume}` | `70` | 当前音量 |
| `{rule_name}` | `早安播报` | 当前规则名称 |
| `{event}` | `connected` / `boot` / `manual` | 触发来源（蓝牙事件名 / 触发方式） |

外部钩子的查询参数会额外作为变量注入，例如 `/api/hooks/r_xxx?event=door&who=mom`
可在动作里使用 `{event}`、`{who}`。

---

## 6. 执行语义

1. **进入条件**（按顺序）：
   - 规则存在且 `enabled=true`；
   - 全局 `rules.enabled=true`（设置页「自动化」总开关）；
   - 不在冷却期（`cooldown` 秒，从上次开始执行算起；`cooldown=0` 时用 `rules.default_cooldown`）；
   - `conditions` 全部满足。
2. 通过后记录执行时间（供冷却判断与界面展示），发布 `rule.fired`；
3. 顺序执行动作，单条失败不中断；全部成功发布 `rule.finished`，否则发布 `rule.error`；
4. 执行记录写入 `recent`（内存保留最近 50 条，倒序），`last_runs` 与计数持久化到 `state.json`；
5. 异步执行队列上限 100 条，线程数 = `max(1, rules.max_concurrent)`（默认 1，串行执行）。

---

## 7. 示例规则

### 7.1 早安播报（工作日 07:30）

```json
{
  "name": "早安播报",
  "enabled": true,
  "cooldown": 0,
  "trigger": {"type": "schedule", "at": "07:30", "days": [0, 1, 2, 3, 4]},
  "conditions": [{"type": "connected", "value": true}],
  "actions": [
    {"type": "volume", "level": 60},
    {"type": "tts", "text": "早上好，今天是{date}，星期{weekday}。今天也要元气满满哦。"}
  ]
}
```

### 7.2 回家迎宾（蓝牙连上音箱时，冷却 5 分钟）

```json
{
  "name": "回家迎宾",
  "enabled": true,
  "cooldown": 300,
  "trigger": {"type": "bluetooth", "event": "connected"},
  "conditions": [{"type": "time_between", "start": "17:00", "end": "23:00"}],
  "actions": [
    {"type": "play", "source": "welcome.wav", "label": "欢迎音", "volume": 60},
    {"type": "wait", "seconds": 1},
    {"type": "tts", "text": "欢迎回家，{bt_name} 已连接。", "volume": 65}
  ]
}
```

### 7.3 整点报时（每 60 分钟）

```json
{
  "name": "整点报时",
  "enabled": true,
  "cooldown": 0,
  "trigger": {"type": "schedule", "every_minutes": 60},
  "conditions": [
    {"type": "time_between", "start": "08:00", "end": "22:00"},
    {"type": "connected", "value": true}
  ],
  "actions": [
    {"type": "play", "source": "chime.wav", "volume": 50},
    {"type": "tts", "text": "现在是{time}。", "volume": 55}
  ]
}
```

### 7.4 深夜自动静音（每天 23:00 降到 15，次日 07:00 恢复）

```json
{
  "name": "深夜自动静音",
  "enabled": true,
  "cooldown": 0,
  "trigger": {"type": "schedule", "at": "23:00"},
  "conditions": [],
  "actions": [
    {"type": "volume", "level": 15},
    {"type": "log", "message": "已进入深夜静音模式（{datetime}）"}
  ]
}
```

配套恢复规则（可另建一条）：

```json
{
  "name": "早晨恢复音量",
  "enabled": true,
  "trigger": {"type": "schedule", "at": "07:00", "days": [0, 1, 2, 3, 4]},
  "conditions": [],
  "actions": [{"type": "volume", "level": 60}]
}
```

### 7.5 手动测试播报（一次性验证 TTS 与音箱）

```json
{
  "name": "手动测试播报",
  "enabled": true,
  "cooldown": 10,
  "trigger": {"type": "manual"},
  "conditions": [],
  "actions": [
    {"type": "play", "source": "chime.wav", "volume": 60},
    {"type": "wait", "seconds": 1},
    {"type": "tts", "text": "这是一条测试播报，当前时间{time}，音量{volume}。", "volume": 60}
  ]
}
```

---

## 8. 校验与排错

常见中文错误与处理：

| 错误信息 | 原因 / 处理 |
| --- | --- |
| `规则名称 name 不能为空（1~64 个字符）` | `name` 缺失或全空格 |
| `动作列表 actions 至少需要 1 个动作` | `actions` 为空数组 |
| `定时触发器需要 at（如 07:30）或 every_minutes（如 30）` | `schedule` 触发器两个字段都没给 |
| `定时触发器 trigger.at 格式必须是 HH:MM（如 07:30）` | 时间写成 `7:30` / `24:00` 等 |
| `蓝牙触发器 trigger.device 不是合法 MAC 地址` | 写成 `AA:BB:CC:DD:EE:FF` 格式 |
| `第 N 个动作（play）缺少 source` 等 | 按提示补齐对应字段 |
| `第 N 个条件的类型 type 必须是 ...` | 条件 `type` 拼写错误 |
| `规则冷却中，还需等待 N 秒`（执行记录） | 冷却未结束，属正常保护 |
| `未开启命令执行权限（security.allow_commands=false）` | 设置页打开「允许执行命令」 |
| `命令不在允许列表内（command_allowlist）` | 补白名单前缀或开启后清空白名单 |

排错入口：

- Web「自动化」页查看规则与最近执行记录；
- `GET /api/events`（等价于引擎 `recent()`）看失败原因与动作明细；
- Web「日志」页 / `data_dir/btspeaker.log` 看详细中文日志；
- `GET /api/diag` 检查 TTS、播放器、蓝牙、调度器是否就绪。
