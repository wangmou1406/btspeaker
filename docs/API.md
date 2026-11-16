# 蓝音管家 BTSpeaker · REST API 文档

> 适用版本：`1.0.0`。本文档与 `docs/CONTRACTS.md` §12 保持一致，是 Web 界面与外部脚本调用后端的唯一依据。
> 所有接口返回 UTF-8 JSON（`ensure_ascii=False`，中文直接可读）。**不使用 SSE / WebSocket**，前端通过轮询 `GET /api/status` 获取实时状态。

---

## 1. 基础信息

| 项目 | 说明 |
| --- | --- |
| 服务地址 | `http://<路由器地址>:<server.port>`，默认端口 **8722**，监听 `server.host`（默认 `0.0.0.0`） |
| API 前缀 | `/api` |
| 数据格式 | 请求与响应均为 JSON，`Content-Type: application/json; charset=utf-8` |
| 静态资源 | `GET /`、`/index.html`、`/app.js`、`/style.css`、`/favicon.svg` 由服务直接托管，不需要鉴权 |
| 时间格式 | ISO 8601 本地时间，如 `2026-11-15T20:00:00+08:00` |
| 状态轮询 | Web 界面每 **3 秒**请求一次 `GET /api/status` |

默认端口可在配置中修改：`server.port`。所有示例使用：

```bash
BASE=http://192.168.1.1:8722
```

---

## 2. 鉴权

- 当配置 `security.api_token` **为空**时：除静态资源外的所有 `/api/*` 接口都可以直接访问。
- 当 `security.api_token` **非空**时：除 `/api/health` 与静态资源外，所有 `/api/*` 接口都必须携带令牌，二选一：
  - 请求头：`X-Api-Token: <token>`
  - 查询参数：`?token=<token>`
- 缺少或令牌错误时返回 **HTTP 401**：

```json
{
  "ok": false,
  "error": { "code": "unauthorized", "message": "需要访问令牌" }
}
```

> Web 界面在收到 401 时会弹出令牌输入框，令牌保存在浏览器 `localStorage` 的 `bts_token` 键，后续请求自动附带 `X-Api-Token` 请求头。
> 令牌在 `GET /api/settings` 中始终脱敏为 `"******"`；`PUT /api/settings` 时如果不修改令牌，请**不要**传入 `api_token` 字段（传入 `"******"` 会被视为新令牌值）。

带鉴权的 curl 示例：

```bash
curl -s -H "X-Api-Token: my-secret-token" "$BASE/api/status"
curl -s "$BASE/api/status?token=my-secret-token"
```

---

## 3. 统一响应信封

### 成功

```json
{ "ok": true, "data": { "...": "接口各自的 data 内容" } }
```

### 失败

HTTP 4xx / 5xx，同时返回：

```json
{
  "ok": false,
  "error": {
    "code": "bad_request",
    "message": "参数 level 必须是 0-100 的整数"
  }
}
```

约定：

- `error.code` 为机器可读的短标识（见「错误码」一节），供程序判断；
- `error.message` 为**面向用户的中文**说明，Web 界面直接展示；
- 前端判断成功与否看 HTTP 状态码与 `ok` 字段，`data` 只在成功时存在。

---

## 4. 接口总览

| # | Method | Path | 说明 |
| --- | --- | --- | --- |
| 1 | GET | `/api/health` | 健康检查（无需鉴权） |
| 2 | GET | `/api/status` | 运行状态快照（轮询入口） |
| 3 | GET | `/api/system/info` | 系统信息 |
| 4 | GET | `/api/settings` | 读取配置（脱敏） |
| 5 | PUT | `/api/settings` | 修改配置（部分更新） |
| 6 | GET | `/api/bluetooth/devices` | 设备列表、当前连接、适配器信息 |
| 7 | POST | `/api/bluetooth/scan` | 启动扫描（异步任务） |
| 8 | GET | `/api/bluetooth/scan` | 查询扫描进度与结果 |
| 9 | POST | `/api/bluetooth/connect` | 连接设备 |
| 10 | POST | `/api/bluetooth/disconnect` | 断开设备 |
| 11 | POST | `/api/bluetooth/pair` | 配对设备 |
| 12 | POST | `/api/bluetooth/default` | 设置默认设备 |
| 13 | DELETE | `/api/bluetooth/devices/{address}` | 删除（取消配对）设备 |
| 14 | GET | `/api/player` | 播放器状态 |
| 15 | POST | `/api/player/play` | 播放声音（异步任务） |
| 16 | POST | `/api/player/stop` | 停止播放 |
| 17 | POST | `/api/player/volume` | 设置音量（持久化） |
| 18 | POST | `/api/tts/speak` | 语音播报（异步任务） |
| 19 | GET | `/api/rules` | 规则列表与统计 |
| 20 | POST | `/api/rules` | 新建规则 |
| 21 | GET | `/api/rules/{id}` | 读取单条规则 |
| 22 | PUT | `/api/rules/{id}` | 修改规则（部分字段） |
| 23 | DELETE | `/api/rules/{id}` | 删除规则 |
| 24 | POST | `/api/rules/{id}/run` | 立即执行规则（异步任务） |
| 25 | POST | `/api/rules/{id}/toggle` | 启用 / 停用规则 |
| 26 | GET | `/api/rules/export` | 导出全部规则 JSON |
| 27 | POST | `/api/rules/import` | 导入规则 JSON |
| 28 | GET | `/api/library` | 声音库文件列表 |
| 29 | GET | `/api/logs` | 读取服务日志 |
| 30 | DELETE | `/api/logs` | 清空日志 |
| 31 | GET | `/api/events` | 最近规则执行记录 |
| 32 | GET | `/api/diag` | 运行诊断 |
| 33 | GET/POST | `/api/hooks/{rule_id}` | 外部钩子触发规则 |
| 34 | POST | `/api/library/upload` | 上传音频文件保存到声音库（原始字节，非 multipart） |
| 35 | GET | `/api/backup` | 导出配置备份（config + 规则） |

> 路径参数中的 MAC 地址建议做 URL 编码（`:` → `%3A`），例如 `/api/bluetooth/devices/AA%3ABB%3ACC%3A11%3A22%3A33`。后端也接受未编码的冒号写法。

---

## 5. 基础与状态接口

### 5.1 `GET /api/health`

健康检查，**不需要鉴权**，可用于进程存活探测。

响应：

```json
{
  "ok": true,
  "data": { "status": "ok", "version": "1.0.0" }
}
```

### 5.2 `GET /api/status`

返回运行状态快照，Web 界面每 3 秒轮询一次。数据均来自内存中的 `RuntimeState`，响应很快。

响应示例：

```json
{
  "ok": true,
  "data": {
    "version": "1.0.0",
    "app": "蓝音管家",
    "uptime": 123,
    "started_at": "2026-11-15T20:00:00+08:00",
    "hostname": "iStoreOS",
    "sim": true,
    "bluetooth": {
      "available": true,
      "powered": true,
      "adapter": { "address": "00:1A:7D:DA:71:13", "name": "iStoreOS BT", "hci": "hci0" },
      "scanning": false,
      "connected": null,
      "devices": []
    },
    "player": {
      "playing": false,
      "current": null,
      "volume": 70,
      "backend": "mock",
      "probe": { "ready": true, "tools": {}, "hint": "" }
    },
    "rules": { "total": 3, "enabled": 2 },
    "engine": {
      "running": 0,
      "queued": 0,
      "next_runs": [
        { "rule_id": "r_seed_morning", "name": "早安播报", "at": "2026-11-16T07:30:00+08:00", "in": 41160 }
      ]
    },
    "tts": { "backend": "mock", "ready": true },
    "last_event": null
  }
}
```

字段说明：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `version` / `app` | string | 版本号与产品名 |
| `uptime` | int | 服务已运行秒数 |
| `started_at` | string | 服务启动时间（ISO 8601） |
| `hostname` | string | 主机名 |
| `sim` | bool | 是否模拟模式（`BTSPEAKER_SIM=1`） |
| `bluetooth.available` / `powered` | bool | 蓝牙后端是否可用 / 适配器是否上电 |
| `bluetooth.adapter` | object | `{address, name, hci}` |
| `bluetooth.scanning` | bool | 是否正在扫描 |
| `bluetooth.connected` | object\|null | 当前连接的设备 |
| `bluetooth.devices` | array | 设备列表（元素为 device 对象） |
| `player.playing` | bool | 是否正在播放 |
| `player.current` | object\|null | 当前播放信息 `{playing, source, label, address, volume, backend, started_at, elapsed}` |
| `player.volume` | int | 当前音量 0-100 |
| `player.backend` | string | `bluealsa` / `mock` / `none` |
| `player.probe` | object | `{ready, tools, hint}`，播放链路探测结果 |
| `rules.total` / `enabled` | int | 规则总数 / 启用数 |
| `engine.running` / `queued` | int | 执行中的规则数 / 排队数 |
| `engine.next_runs` | array | `[{rule_id, name, at, in}]`，按时间排序的下一次定时触发（`in` 为剩余秒数） |
| `tts.backend` / `ready` | string / bool | TTS 后端与是否就绪 |
| `last_event` | object\|null | 最近一次总线事件 |

### 5.3 `GET /api/system/info`

响应：

```json
{
  "ok": true,
  "data": {
    "version": "1.0.0",
    "python": "3.11.6",
    "platform": "Linux-5.15.0-x86_64-with-glibc2.36",
    "data_dir": "/etc/btspeaker",
    "uptime": 3600,
    "sim": false,
    "hostname": "iStoreOS",
    "started_at": "2026-11-15T20:00:00+08:00",
    "project": "蓝音管家 BTSpeaker"
  }
}
```

---

## 6. 配置接口

### 6.1 `GET /api/settings`

读取完整配置（**脱敏**：`security.api_token` 返回 `"******"`，空令牌返回 `""`）。

```json
{
  "ok": true,
  "data": {
    "config": {
      "version": 1,
      "server": { "host": "0.0.0.0", "port": 8722, "title": "蓝音管家" },
      "security": { "api_token": "******", "allow_commands": false, "command_timeout": 20, "command_allowlist": [] },
      "bluetooth": {
        "backend": "cli", "cli": "bluetoothctl", "adapter": "hci0", "poll_interval": 5,
        "scan_timeout": 12, "auto_reconnect": true, "default_device": "", "trust_on_connect": true
      },
      "player": {
        "backend": "auto", "volume": 70,
        "alsa_device": "bluealsa:DEV={mac},PROFILE=a2dp",
        "mpg123": "mpg123", "aplay": "aplay", "ffmpeg": "ffmpeg",
        "max_duration": 900, "loop_gap": 1, "volume_command": ""
      },
      "tts": {
        "backend": "auto", "url": "", "method": "GET", "headers": {},
        "body_template": "", "voice": "zh", "speed": 150,
        "espeak": "espeak", "timeout": 20, "format": "mp3"
      },
      "rules": { "enabled": true, "max_concurrent": 1, "default_cooldown": 0 },
      "logging": { "level": "INFO", "max_records": 500 },
      "sounds": { "dirs": ["/usr/share/btspeaker/sounds"], "max_upload_mb": 20 },
      "sim": { "devices_file": "" }
    }
  }
}
```

### 6.2 `PUT /api/settings`

部分更新配置：采用**递归合并**（`dict` 深度合并、列表整体替换），只需要传要修改的分组/字段。

请求体：

```json
{
  "config": {
    "player": { "volume": 80 },
    "tts": { "url": "http://192.168.1.2:8080/tts?text={text}&lang={lang}", "method": "GET" }
  }
}
```

响应：

```json
{
  "ok": true,
  "data": {
    "config": { "...": "更新后的完整配置（脱敏）" },
    "restart_required": false
  }
}
```

说明：

- `restart_required=true` 表示部分设置（如监听地址/端口）需要重启服务才能生效，其他设置即时生效。
- 修改 `security.api_token` 后，后续请求必须使用新令牌；Web 界面保存后会把新令牌写入本机 `localStorage`。
- 不修改令牌时省略 `api_token` 字段；传空字符串表示**清除令牌**（关闭鉴权）。

### 6.3 `GET /api/backup`

导出配置备份：一次性返回「配置 + 全部规则」的 JSON 内容，前端用 Blob 下载为文件。

响应：

```json
{
  "ok": true,
  "data": {
    "filename": "btspeaker-backup-20261115-223500.json",
    "content": {
      "version": 1,
      "exported_at": "2026-11-15T22:35:00+08:00",
      "config": { "...": "完整配置（api_token 已脱敏）" },
      "rules": { "version": 1, "rules": [ { "...": "rule" } ] }
    }
  }
}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `filename` | string | 建议的下载文件名，格式 `btspeaker-backup-YYYYmmdd-HHMMSS.json` |
| `content` | object | 备份内容，至少包含 `config`（完整配置）与 `rules`（规则文件结构） |

说明：

- `content.config` 中的 `security.api_token` 为脱敏值（`******`），恢复备份后需要重新填写令牌。
- 备份可直接保存归档；建议在升级/迁移前导出一份。
- 鉴权同其它接口，需要 `X-Api-Token` 或 `?token=`。

```bash
curl -s -H "X-Api-Token: my-secret-token" "$BASE/api/backup" -o btspeaker-backup.json
```

---

## 7. 蓝牙接口

### 7.1 `GET /api/bluetooth/devices`

响应：

```json
{
  "ok": true,
  "data": {
    "items": [
      {
        "address": "AA:BB:CC:11:22:33",
        "name": "客厅小爱音箱",
        "paired": true,
        "connected": true,
        "trusted": true,
        "icon": "audio-card"
      }
    ],
    "connected": {
      "address": "AA:BB:CC:11:22:33",
      "name": "客厅小爱音箱",
      "paired": true,
      "connected": true,
      "trusted": true,
      "icon": "audio-card"
    },
    "adapter": { "address": "00:1A:7D:DA:71:13", "name": "iStoreOS BT", "hci": "hci0" }
  }
}
```

device 对象：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `address` | string | 大写 MAC，形如 `AA:BB:CC:DD:EE:FF` |
| `name` | string | 设备名，可能为空 |
| `paired` | bool | 是否已配对 |
| `connected` | bool | 是否已连接 |
| `trusted` | bool | 是否已信任 |
| `icon` | string | BlueZ 图标名，可能为空 |

### 7.2 `POST /api/bluetooth/scan`

启动一次异步扫描。扫描在后台线程执行，接口立即返回。

请求体：

```json
{ "timeout": 12 }
```

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `timeout` | number | 否 | 扫描秒数，默认取 `bluetooth.scan_timeout` |

响应：

```json
{
  "ok": true,
  "data": { "job_id": "job_7f3a1c", "scanning": true }
}
```

### 7.3 `GET /api/bluetooth/scan`

查询扫描状态与结果。首次扫描未结束时 `scanning=true`，结束后返回发现的设备。Web 界面在扫描期间每 1.5 秒轮询一次。

```json
{
  "ok": true,
  "data": {
    "scanning": false,
    "items": [
      {
        "address": "AA:BB:CC:77:88:99",
        "name": "卧室新音箱",
        "paired": false,
        "connected": false,
        "trusted": false,
        "icon": "audio-card"
      }
    ],
    "started_at": "2026-11-15T20:10:00+08:00",
    "error": ""
  }
}
```

`error` 非空表示扫描失败（中文原因），例如 `"bluetoothctl 不可用"`。

### 7.4 `POST /api/bluetooth/connect`

请求体：

```json
{ "address": "AA:BB:CC:11:22:33" }
```

响应：

```json
{ "ok": true, "data": { "address": "AA:BB:CC:11:22:33", "connected": true } }
```

### 7.5 `POST /api/bluetooth/disconnect`

请求体：`{ "address": "AA:BB:CC:11:22:33" }`

响应：

```json
{ "ok": true, "data": { "address": "AA:BB:CC:11:22:33", "connected": false } }
```

### 7.6 `POST /api/bluetooth/pair`

请求体：`{ "address": "AA:BB:CC:77:88:99" }`

响应：

```json
{ "ok": true, "data": { "address": "AA:BB:CC:77:88:99", "paired": true } }
```

### 7.7 `POST /api/bluetooth/default`

设置默认音箱（连接/播放未指定设备时优先使用），持久化到 `bluetooth.default_device`。

请求体：`{ "address": "AA:BB:CC:11:22:33" }`

响应：

```json
{ "ok": true, "data": { "default_device": "AA:BB:CC:11:22:33" } }
```

### 7.8 `DELETE /api/bluetooth/devices/{address}`

移除配对记录。

```bash
curl -s -X DELETE "$BASE/api/bluetooth/devices/AA%3ABB%3ACC%3A11%3A22%3A33"
```

响应：

```json
{ "ok": true, "data": { "removed": true } }
```

---

## 8. 播放接口

### 8.1 `GET /api/player`

```json
{
  "ok": true,
  "data": {
    "playing": false,
    "current": null,
    "volume": 70,
    "probe": {
      "ready": true,
      "backend": "bluealsa",
      "tools": { "mpg123": true, "aplay": true, "ffmpeg": true },
      "bluealsa_pcm": true,
      "hint": ""
    }
  }
}
```

### 8.2 `POST /api/player/play`

异步播放：立即返回任务 ID，实际播放由后台线程执行。

请求体：

```json
{
  "source": "chime.wav",
  "label": "提示音",
  "volume": 70,
  "loop": false,
  "address": "AA:BB:CC:11:22:33"
}
```

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `source` | string | 是 | 本地绝对路径 / 声音库文件名 / `http(s)` URL |
| `label` | string | 否 | 展示名称，缺省用文件名 |
| `volume` | int | 否 | 0-100，缺省用 `player.volume` |
| `loop` | bool | 否 | 是否循环播放直到停止 |
| `address` | string | 否 | 目标音箱 MAC，缺省用当前连接/默认设备 |

响应：

```json
{ "ok": true, "data": { "accepted": true, "job_id": "job_2b9d0e" } }
```

### 8.3 `POST /api/player/stop`

停止当前播放（打断阻塞中的播放任务）。

响应：

```json
{ "ok": true, "data": { "stopped": true } }
```

### 8.4 `POST /api/player/volume`

请求体：`{ "level": 65 }`，取值 0-100，会持久化到配置 `player.volume`。

响应：

```json
{ "ok": true, "data": { "volume": 65 } }
```

---

## 9. TTS 接口

### 9.1 `POST /api/tts/speak`

异步语音播报：先合成音频，再交给播放器播放。

请求体：

```json
{
  "text": "吃饭啦，快下来。",
  "voice": "zh",
  "volume": 70
}
```

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `text` | string | 是 | 播报文本 |
| `voice` | string | 否 | 语音/语言，缺省用 `tts.voice` |
| `volume` | int | 否 | 0-100，缺省用 `player.volume` |

响应：

```json
{ "ok": true, "data": { "accepted": true, "job_id": "job_tts_01" } }
```

TTS 未配置时返回 4xx，`message` 为可操作的中文提示，例如：
`"未配置 TTS：请在设置里填写 TTS 服务地址，或安装 espeak 作为离线兜底"`。

---

## 10. 规则接口

规则数据结构见 `docs/CONTRACTS.md` §9，摘要如下：

```json
{
  "id": "r_ab12cd34",
  "name": "早安播报",
  "enabled": true,
  "created_at": "2026-11-15T20:00:00+08:00",
  "updated_at": "2026-11-15T20:00:00+08:00",
  "cooldown": 0,
  "trigger": { "type": "schedule", "at": "07:30", "days": [0, 1, 2, 3, 4] },
  "conditions": [],
  "actions": [{ "type": "tts", "text": "早上好，今天是{date}，星期{weekday}。" }]
}
```

触发器 `trigger.type`：

| 类型 | 结构 | 说明 |
| --- | --- | --- |
| `schedule` | `{"type":"schedule","at":"HH:MM","days":[0..6]}` | 每天/指定星期；`days` 省略或空数组＝每天（0=周一，6=周日） |
| `schedule` | `{"type":"schedule","every_minutes":30}` | 每 N 分钟（1-1440） |
| `bluetooth` | `{"type":"bluetooth","event":"connected","device":""}` | `event` 取 `connected` / `disconnected` / `paired`；`device` 为空＝任意设备 |
| `boot` | `{"type":"boot","delay":5}` | 服务启动后延迟 N 秒（0-3600） |
| `manual` | `{"type":"manual"}` | 仅手动/HTTP 钩子触发 |

条件 `conditions`（全部满足才执行）：

| 类型 | 结构 | 说明 |
| --- | --- | --- |
| `connected` | `{"type":"connected","value":true}` | 是否存在已连接音箱 |
| `time_between` | `{"type":"time_between","start":"08:00","end":"22:00"}` | 时间段，支持跨夜（start > end） |
| `weekday_in` | `{"type":"weekday_in","days":[0,4]}` | 星期范围 |

动作 `actions`（按顺序执行，单条失败不中断后续）：

| 类型 | 结构 | 必填字段 |
| --- | --- | --- |
| `play` | `{"type":"play","source":"chime.wav","label":"提示音","volume":70,"loop":false}` | `source` |
| `tts` | `{"type":"tts","text":"...","voice":"zh","volume":70}` | `text` |
| `volume` | `{"type":"volume","level":70}` | `level` |
| `connect` | `{"type":"connect","address":"MAC 或空"}` | — |
| `disconnect` | `{"type":"disconnect"}` | — |
| `http` | `{"type":"http","url":"http://...","method":"GET","headers":{},"body":"","timeout":10}` | `url` |
| `wait` | `{"type":"wait","seconds":2}` | `seconds`（0-300） |
| `log` | `{"type":"log","message":"..."}` | `message` |
| `command` | `{"type":"command","command":"sh /root/x.sh","timeout":20}` | `command`（需 `security.allow_commands=true`） |

模板变量（`tts.text` / `http.url` / `http.body` / `log.message` 均支持）：
`{time}` `{date}` `{datetime}` `{weekday}` `{hostname}` `{bt_name}` `{bt_addr}` `{volume}` `{rule_name}` `{event}`。

### 10.1 `GET /api/rules`

```json
{
  "ok": true,
  "data": {
    "items": [ { "...": "rule" } ],
    "counts": { "total": 3, "enabled": 2 }
  }
}
```

### 10.2 `POST /api/rules`

新建规则。请求体为规则字段（`name`、`trigger`、`actions` 等；`id` 由服务生成，可不传）。校验失败返回 400 与中文错误列表。

响应：创建后的完整规则对象。

### 10.3 `GET /api/rules/{id}`

返回单条规则；不存在时 404。

### 10.4 `PUT /api/rules/{id}`

部分更新规则字段（只传要改的字段即可）。返回更新后的规则。

### 10.5 `DELETE /api/rules/{id}`

```json
{ "ok": true, "data": { "deleted": true } }
```

### 10.6 `POST /api/rules/{id}/run`

立即执行规则（忽略定时触发，仍会检查启用状态、冷却与前置条件）。

响应：

```json
{ "ok": true, "data": { "accepted": true, "job_id": "job_run_01" } }
```

规则被禁用/冷却中时，`accepted=false` 且带 `reason` 中文原因。

### 10.7 `POST /api/rules/{id}/toggle`

请求体：

```json
{ "enabled": false }
```

响应：更新后的规则对象。

### 10.8 `GET /api/rules/export`

导出全部规则（备份/迁移用）：

```json
{
  "ok": true,
  "data": {
    "version": 1,
    "rules": [ { "...": "rule" } ]
  }
}
```

```bash
curl -s "$BASE/api/rules/export" -o rules-backup.json
```

### 10.9 `POST /api/rules/import`

请求体：

```json
{
  "rules": [ { "...": "rule" } ],
  "mode": "merge"
}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `rules` | array | 规则数组（也接受导出文件里的 `rules` 字段内容） |
| `mode` | string | `merge`（默认，合并并覆盖同 ID 规则）或 `replace`（清空后导入） |

响应：

```json
{ "ok": true, "data": { "imported": 3 } }
```

```bash
curl -s -X POST "$BASE/api/rules/import" \
  -H "Content-Type: application/json" \
  -d '{"rules":[{"name":"测试","trigger":{"type":"manual"},"actions":[{"type":"tts","text":"测试"}]}],"mode":"merge"}'
```

---

## 11. 声音库、日志与事件

### 11.1 `GET /api/library`

```json
{
  "ok": true,
  "data": {
    "items": [
      {
        "name": "chime.wav",
        "path": "/usr/share/btspeaker/sounds/chime.wav",
        "size": 48000,
        "size_text": "46.9 KB",
        "source": "system"
      }
    ],
    "dirs": ["/usr/share/btspeaker/sounds"]
  }
}
```

`source` 表示来源（如 `system` / `user`）；`path` 可直接作为播放接口的 `source`。

### 11.2 `POST /api/library/upload`

把本地音频文件保存到声音库。请求体是**原始音频字节**（`Content-Type: <file.type>`，**不是 multipart/form-data**），文件名通过查询参数传入。

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| `name` | query | string | 是 | 保存的文件名，需要带受支持的音频扩展名（如 `.wav` / `.mp3` / `.ogg`） |

```bash
curl -s -X POST --data-binary @chime.wav \
  -H "X-Api-Token: my-secret-token" \
  -H "Content-Type: audio/wav" \
  "$BASE/api/library/upload?name=chime.wav"
```

成功响应（`data` 可直接用于播放接口的 `source`）：

```json
{
  "ok": true,
  "data": {
    "name": "chime.wav",
    "path": "/etc/btspeaker/sounds/chime.wav",
    "size": 48000,
    "size_text": "46.9 KB",
    "source": "user"
  }
}
```

错误：

| HTTP | `error.code` | 场景 |
| --- | --- | --- |
| 400 | `bad_request` | 缺少 `name`、扩展名不支持、内容为空或不是音频 |
| 413 | `payload_too_large` | 文件超过 `settings.sounds.max_upload_mb`（默认 20MB） |
| 401 | `unauthorized` | 令牌缺失或错误 |

说明：

- 上传成功后 `GET /api/library` 会返回新文件；Web 界面会上传后自动刷新声音库下拉。
- 上传不会中断当前正在播放的音频。
- 保存目录为 `settings.sounds.dirs` 中的用户目录（通常为 `<数据目录>/sounds/`）。

### 11.3 `GET /api/logs`

查询参数：

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `limit` | int | 返回条数，默认 200，最大为环形缓冲容量 |
| `level` | string | 可选，最低级别过滤：`DEBUG` / `INFO` / `WARNING` / `ERROR` |

```json
{
  "ok": true,
  "data": {
    "items": [
      { "time": "2026-11-15T20:00:01+08:00", "level": "INFO", "module": "api", "message": "HTTP 服务已启动: http://0.0.0.0:8722" }
    ],
    "total": 128,
    "capacity": 500
  }
}
```

```bash
curl -s "$BASE/api/logs?limit=50&level=WARNING"
```

### 11.4 `DELETE /api/logs`

```json
{ "ok": true, "data": { "cleared": true } }
```

### 11.5 `GET /api/events`

最近规则执行记录，倒序。`limit` 默认 50。

```json
{
  "ok": true,
  "data": {
    "items": [
      {
        "ok": true,
        "rule_id": "r_ab12cd34",
        "rule_name": "早安播报",
        "trigger": "schedule",
        "started_at": "2026-11-15T07:30:00+08:00",
        "duration": 1.2,
        "actions": [ { "type": "tts", "ok": true, "message": "播放完成" } ],
        "error": ""
      }
    ]
  }
}
```

---

## 12. 诊断接口

### 12.1 `GET /api/diag`

逐项检查运行环境，`ok=false` 的项带可操作的中文 `hint`。

```json
{
  "ok": true,
  "data": {
    "checks": [
      { "name": "服务运行", "ok": true, "detail": "蓝音管家 v1.0.0 已运行 83 秒", "hint": "" },
      { "name": "蓝牙工具", "ok": false, "detail": "未找到 bluetoothctl", "hint": "opkg install bluez-utils" },
      { "name": "mpg123 解码器", "ok": false, "detail": "未安装", "hint": "opkg install mpg123" },
      { "name": "BlueALSA 音频输出", "ok": false, "detail": "未检测到 bluealsa PCM", "hint": "opkg install bluealsa，并确认 /etc/init.d/bluealsa 已启动" }
    ]
  }
}
```

典型检查项：服务运行、数据目录可写、蓝牙工具、蓝牙适配器、音箱连接、mpg123、aplay、BlueALSA、TTS、规则调度器等。

---

## 13. 外部钩子

### `GET|POST /api/hooks/{rule_id}`

用外部系统（Home Assistant、快捷指令、网页按钮等）触发指定规则。**URL 查询参数会作为上下文传入规则**，可在模板变量中引用。

```bash
curl -s "$BASE/api/hooks/r_ab12cd34?from=door&name=home"
curl -s -X POST "$BASE/api/hooks/r_ab12cd34" \
  -H "Content-Type: application/json" \
  -d '{"from":"door"}'
```

响应：

```json
{
  "ok": true,
  "data": { "fired": true, "rule_id": "r_ab12cd34", "job_id": "job_hook_01" }
}
```

规则不存在或未启用时返回 4xx（`accepted=false` / `fired=false` 及中文原因）。

---

## 14. 错误码

| HTTP | `error.code` | 含义 |
| --- | --- | --- |
| 400 | `bad_request` | 参数缺失或格式错误（如音量越界、规则校验失败） |
| 400 | `rule_invalid` | 规则校验未通过（`message` 含具体字段与原因） |
| 401 | `unauthorized` | 缺少或错误的 API 令牌 |
| 403 | `forbidden` | 操作被安全配置禁止（如未开启命令执行权限） |
| 404 | `not_found` | 资源不存在（规则 ID、设备地址、接口路径） |
| 405 | `method_not_allowed` | 请求方法不被支持 |
| 409 | `conflict` | 状态冲突（如设备忙、重复扫描） |
| 413 | `payload_too_large` | 上传内容超过大小上限（`sounds.max_upload_mb`，默认 20MB） |
| 500 | `internal_error` | 服务内部错误（详情见日志） |
| 503 | `unavailable` | 蓝牙/播放等后端暂时不可用 |

> 前端只依赖 `error.code` 做逻辑分支，展示一律使用 `error.message`。新增错误码时保持 `code` 稳定、`message` 中文可操作。

---

## 15. 前端对接说明

Web 界面（`btspeaker/web/`）的调用方式：

1. 启动时 `GET /api/status` 判断在线状态，之后每 3 秒轮询；
2. 所有请求带 `Accept: application/json`，写操作带 `Content-Type: application/json`；
3. 令牌保存在 `localStorage.bts_token`，每次请求附加 `X-Api-Token`；
4. 收到 401 弹出令牌输入框；网络异常展示错误条与「重试」，不会白屏；
5. 扫描、播放、TTS、规则执行均为「提交任务 + 轮询状态/事件」的异步模型，界面据此展示 loading 与后续状态；
6. **不使用 SSE / WebSocket**：所有实时性由轮询保证（状态 3 秒、扫描 1.5 秒、日志 5 秒）。
7. 声音库上传使用 `POST /api/library/upload?name=<文件名>` 的**原始字节**请求体（非 multipart，`Content-Type` 为文件的 MIME 类型），Web 界面用隐藏 `<input type="file" accept="audio/*">` 选择文件后直接 `fetch`；
8. 配置备份 `GET /api/backup` 返回 `{filename, content}`，Web 界面用 `Blob` + `a[download]` 下载，文件名直接取返回值。

### 常用 curl 速查

```bash
BASE=http://192.168.1.1:8722
TOKEN=my-secret-token

# 健康检查（无需令牌）
curl -s "$BASE/api/health"

# 状态 / 设备 / 播放器
curl -s -H "X-Api-Token: $TOKEN" "$BASE/api/status"
curl -s -H "X-Api-Token: $TOKEN" "$BASE/api/bluetooth/devices"
curl -s -H "X-Api-Token: $TOKEN" "$BASE/api/player"

# 连接音箱 + 播放提示音
curl -s -X POST "$BASE/api/bluetooth/connect" -H "X-Api-Token: $TOKEN" -H "Content-Type: application/json" \
  -d '{"address":"AA:BB:CC:11:22:33"}'
curl -s -X POST "$BASE/api/player/play" -H "X-Api-Token: $TOKEN" -H "Content-Type: application/json" \
  -d '{"source":"chime.wav","label":"提示音","volume":70}'

# 语音播报 + 停止
curl -s -X POST "$BASE/api/tts/speak" -H "X-Api-Token: $TOKEN" -H "Content-Type: application/json" \
  -d '{"text":"吃饭啦"}'
curl -s -X POST "$BASE/api/player/stop" -H "X-Api-Token: $TOKEN" -H "Content-Type: application/json" -d '{}'

# 规则：列表 / 新建 / 立即执行 / 导出
curl -s -H "X-Api-Token: $TOKEN" "$BASE/api/rules"
curl -s -X POST "$BASE/api/rules" -H "X-Api-Token: $TOKEN" -H "Content-Type: application/json" \
  -d '{"name":"测试播报","trigger":{"type":"manual"},"actions":[{"type":"tts","text":"测试成功"}]}'
curl -s -X POST "$BASE/api/rules/r_ab12cd34/run" -H "X-Api-Token: $TOKEN" -H "Content-Type: application/json" -d '{}'
curl -s -H "X-Api-Token: $TOKEN" "$BASE/api/rules/export" -o rules.json

# 日志与诊断
curl -s -H "X-Api-Token: $TOKEN" "$BASE/api/logs?limit=20"
curl -s -H "X-Api-Token: $TOKEN" "$BASE/api/diag"

# 声音库上传（原始字节，非 multipart）与配置备份
curl -s -X POST --data-binary @chime.wav \
  -H "X-Api-Token: $TOKEN" -H "Content-Type: audio/wav" \
  "$BASE/api/library/upload?name=chime.wav"
curl -s -H "X-Api-Token: $TOKEN" "$BASE/api/backup" -o btspeaker-backup.json
```

---

## 16. 版本与兼容性

- 当前 API 版本：`1.0.0`（对应 `VERSION` 文件）。
- 新增字段向后兼容：客户端应忽略未知字段；删除/改名字段会提升主版本并同步更新本文档与契约。
- 所有接口路径与信封格式由 `docs/CONTRACTS.md` §12 定义；若实现与契约冲突，以契约为主并同步修正。
