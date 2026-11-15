# 蓝音管家 BTSpeaker

**在 iStoreOS（OpenWrt 系）x86_64 软路由上，用浏览器连接、播放和控制你的蓝牙音箱，并把「什么时候放什么声音」变成自动化规则。**

蓝音管家是一个**独立 Web 服务应用**：纯 Python 标准库实现，不依赖任何第三方包，不占用 Docker、不引入 Node.js 构建链。装好后打开 `http://<路由器IP>:8722` 即可使用；服务由 procd 守护、开机自启，配置与数据都在 `/etc/btspeaker`。

---

## 功能一览

| 模块 | 能力 |
| --- | --- |
| 蓝牙管理 | 扫描附近设备、配对、连接/断开、信任、设为默认音箱、删除配对；后台轮询连接状态 |
| 声音库 | 内置提示音（`chime.wav` / `welcome.wav` / `alarm.wav`）+ 用户声音目录（把音频文件放进 `/etc/btspeaker/sounds/`，刷新后自动出现在声音库下拉框） |
| 播放控制 | 播放本地文件 / 声音库文件名 / HTTP(S) URL；音量调节、停止、循环播放 |
| TTS 播报 | 在线 TTS 接口（推荐，中文音质好）、`espeak` 离线兜底、模拟模式；支持文本模板变量 |
| 定时任务 | 每天/指定星期几点几分、每 N 分钟执行；调度器每秒检查、不重复误触发 |
| 事件触发 | 蓝牙连接/断开/配对、开机后延迟、手动（网页按钮 / HTTP hook / 命令行） |
| 规则引擎 | 多动作顺序执行（播放、TTS、音量、连接、断开、命令、HTTP、等待、日志），条件判断、冷却时间 |
| Web 控制台 | 总览 / 音箱 / 自动化 / 设置 四个页面，归藏瑞士风（Swiss）视觉，全中文、纯离线（无 CDN） |
| HTTP API | JSON REST 接口，统一信封；可选 `api_token` 鉴权；可被 Home Assistant、脚本、crontab 调用 |
| 命令行 | `btspeaker status / devices / play / say / rules / logs / diag` 等，ssh 下脚本化 |
| 诊断 | Web `/api/diag`、`bt-diag.sh` 中文链路诊断、日志环形缓冲 |

## 界面预览

| 总览 | 音箱 |
| --- | --- |
| ![总览](docs/screenshots/dashboard.png) | ![音箱](docs/screenshots/devices.png) |

| 自动化 | 设置 |
| --- | --- |
| ![自动化](docs/screenshots/rules.png) | ![设置](docs/screenshots/settings.png) |

> 截图来自开发机**模拟模式**（`BTSPEAKER_SIM=1`，不接真实蓝牙硬件）；真机上连接真实音箱后，状态区会显示实际适配器、设备与播放状态。

## 架构

```mermaid
flowchart LR
    subgraph 使用入口
        WEB["浏览器<br/>Web 控制台"]
        CLI["ssh / 脚本<br/>btspeaker CLI"]
        HOOK["外部触发<br/>/api/hooks/{rule_id}"]
    end

    subgraph svc["Python 服务层 /usr/lib/btspeaker"]
        API["api.py<br/>HTTP 服务 + 鉴权"]
        SERVICE["service.py<br/>装配 / 生命周期"]
        ENGINE["engine.py<br/>自动化引擎"]
        SCHED["scheduler.py<br/>定时调度"]
        RULES["rules.py<br/>规则模型与校验"]
        TTS["tts.py<br/>TTS 合成"]
        PLAYER["audio.py<br/>播放器"]
        BTMOD["bluetooth.py<br/>蓝牙后端"]
        STATE["store.py<br/>运行状态 / 持久化"]
        CONF["config.py<br/>配置管理"]
    end

    subgraph sys["系统层"]
        BLUEZ["BlueZ<br/>bluetoothctl / bluetoothd"]
        BLUEALSA["BlueALSA<br/>bluealsa A2DP 桥接"]
        TOOLS["mpg123 / aplay / ffmpeg"]
        TTSSRV["在线 TTS 接口 / espeak"]
    end

    SPK[["蓝牙音箱<br/>A2DP"]]

    WEB -->|"HTTP JSON /api/*"| API
    CLI -->|"本机 HTTP"| API
    HOOK --> API
    API --> SERVICE
    SERVICE --> ENGINE & SCHED & RULES & TTS & PLAYER & BTMOD & STATE & CONF
    BTMOD -->|"配对 / 连接 / 状态"| BLUEZ
    PLAYER --> TOOLS
    TOOLS -->|"ALSA PCM<br/>bluealsa:HCI=...,DEV=..."| BLUEALSA
    BLUEALSA --> BLUEZ
    BLUEZ --> SPK
    TTS --> TTSSRV
    ENGINE -. "触发规则" .-> PLAYER & TTS & BTMOD
```

播放链路：`音频文件 → mpg123/aplay/ffmpeg 解码 → ALSA PCM "bluealsa:HCI=hci0,DEV=<MAC>,PROFILE=a2dp" → 蓝牙音箱`。

## 安装

目标环境：iStoreOS / OpenWrt x86_64（Python 3.9+，opkg、procd、BlueZ）。两种方式任选其一。

### 方式一：iStore 上传 ipk 安装（推荐）

```bash
# 在开发机 / 任意装有 Python 3 的机器上，于项目根目录构建
python scripts/build_ipk.py
# 产物：dist/btspeaker_1.0.0_all.ipk
```

把 `dist/btspeaker_1.0.0_all.ipk` 通过 iStoreOS 的 **iStore / 系统 → 软件包 → 手动安装** 上传安装；或 scp 到路由器后执行：

```bash
scp dist/btspeaker_1.0.0_all.ipk root@192.168.1.1:/tmp/
ssh root@192.168.1.1
opkg install /tmp/btspeaker_1.0.0_all.ipk
```

安装包会自动完成：创建程序目录与数据目录、`btspeaker seed` 初始化配置与 5 条示例规则、opkg 依赖安装、防火墙放行 TCP 8722、`/etc/init.d/btspeaker enable && start`。

### 方式二：ssh 运行 install.sh（源码方式）

```bash
# 把整个项目目录传到路由器（scp / git clone 均可），然后：
ssh root@192.168.1.1
cd /tmp/istoreos-btspeaker
sh install.sh                # 正常安装（可重复执行 = 升级覆盖）
sh install.sh --dry-run      # 只预览，不改动系统
sh install.sh --no-deps      # 依赖已自备时跳过 opkg 安装
```

`install.sh` 做的事情与 ipk 的 postinst 相同：检查 root 与 OpenWrt 环境 → `opkg install` 依赖 → 创建 `/usr/lib/btspeaker`、`/usr/share/btspeaker/sounds`、`/etc/btspeaker/{sounds,tmp}` → 拷贝 Python 包 / 启动器 `/usr/bin/btspeaker` / procd 启动脚本 → 安装内置声音库与诊断脚本 → `btspeaker seed` 初始化数据目录（已存在则不覆盖）→ uci 防火墙放行 TCP 8722 → 开机自启并启动 → 打印访问地址。

> 构建脚本还会生成 `dist/btspeaker-1.0.0-all.tar.gz` 离线安装包，解压后进入目录执行 `sh install.sh` 即可，适合内网离线部署。

详细依赖说明、手动安装步骤、iStore 上传流程、升级/回滚见 [docs/INSTALL.md](docs/INSTALL.md)。

## 使用入门

1. **打开控制台**：浏览器访问 `http://<路由器LAN IP>:8722/`（安装脚本结束时也会打印地址）。
2. **配对音箱**：进入「音箱」页 → 点击「扫描」→ 在列表里点「配对」并「连接」；也可以点「设为默认」，之后自动化规则会优先使用它。
   ```bash
   btspeaker devices --scan
   btspeaker pair AA:BB:CC:DD:EE:FF
   btspeaker connect AA:BB:CC:DD:EE:FF
   btspeaker default AA:BB:CC:DD:EE:FF
   ```
3. **试一下声音**：总览页点「播放提示音」，或命令行：
   ```bash
   btspeaker play chime.wav
   btspeaker volume 60
   btspeaker say 你好，我是蓝音管家
   ```
4. **配置 TTS**：进入「设置」页填写 TTS 服务地址（支持 `{text}` / `{lang}` 模板变量），或安装 `espeak` 作为离线兜底。TTS 未配置时，只有 `tts` 动作会报错，播放/规则其它功能不受影响。
5. **启用自动化**：5 条示例规则默认**全部停用**，确认音箱正常后到「自动化」页逐条启用，或直接新建规则。

### 内置示例规则（首次启动自动写入，默认停用）

| 规则 | 触发器 | 动作 |
| --- | --- | --- |
| 早安播报 | 每周一至周五 07:30 | 音量 60 → TTS「早上好，今天是{date}，{weekday}…」 |
| 回家迎宾 | 蓝牙连接任意音箱（冷却 300 秒） | 播放 `welcome.wav` → TTS 欢迎语 |
| 整点报时 | 每 60 分钟 | 播放 `chime.wav` → TTS「现在是{time}。」 |
| 深夜自动静音 | 每天 23:00 | 音量调到 15 → 记录日志 |
| 手动测试播报 | 手动触发 | 播放 `chime.wav` → TTS 测试播报 |

> 示例规则来自随包文件 `btspeaker/seed_rules.json`，由 `btspeaker seed` 写入 `/etc/btspeaker/rules.json`；已有规则文件时不会覆盖。恢复默认：删除或备份 `rules.json` 后再次执行 `btspeaker seed --data-dir /etc/btspeaker`。

### 规则长什么样

```json
{
  "name": "上班日八点新闻",
  "enabled": true,
  "cooldown": 60,
  "trigger": {"type": "schedule", "at": "08:00", "days": [0, 1, 2, 3, 4]},
  "conditions": [{"type": "connected", "value": true}],
  "actions": [
    {"type": "volume", "level": 70},
    {"type": "play", "source": "chime.wav", "label": "提示音"},
    {"type": "tts", "text": "现在是{time}，本地天气请查看手机。"}
  ]
}
```

触发器类型：`schedule`（`at` + `days`，或 `every_minutes`）、`bluetooth`（`connected` / `disconnected` / `paired`）、`boot`（开机延迟）、`manual`；动作类型：`play` `tts` `volume` `connect` `disconnect` `command` `http` `wait` `log`；模板变量：`{time}` `{date}` `{datetime}` `{weekday}` `{hostname}` `{bt_name}` `{bt_addr}` `{volume}` `{rule_name}` `{event}`。完整语法见 [docs/RULES.md](docs/RULES.md)。

### 常用命令

```bash
btspeaker status            # 服务状态与统计
btspeaker diag              # 蓝牙 / 音频 / TTS 链路自检
btspeaker logs -n 50        # 最近日志
btspeaker rules list        # 规则列表
btspeaker rules run <id>    # 立即执行一条规则
/etc/init.d/btspeaker restart|reload|status
sh /usr/share/btspeaker/scripts/bt-diag.sh   # 蓝牙链路诊断
```

## HTTP API 摘要

所有接口统一信封：成功 `{"ok": true, "data": ...}`，失败返回对应 HTTP 状态码与 `{"ok": false, "error": {"code": "...", "message": "中文"}}`。默认无鉴权；配置 `security.api_token` 后，除 `/api/health` 与静态资源外的 `/api/*` 需要请求头 `X-Api-Token: <token>` 或 `?token=<token>`。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | 健康检查（无需鉴权） |
| GET | `/api/status` | 总览状态：蓝牙 / 播放器 / 规则 / 引擎 / TTS |
| GET | `/api/system/info` | 版本、Python、平台、数据目录、运行时长 |
| GET / PUT | `/api/settings` | 读取（脱敏）/ 修改配置 |
| GET | `/api/bluetooth/devices` | 设备列表、当前连接、适配器信息 |
| POST / GET | `/api/bluetooth/scan` | 启动扫描 / 查询扫描结果 |
| POST | `/api/bluetooth/connect` · `disconnect` · `pair` | 连接 / 断开 / 配对 |
| POST | `/api/bluetooth/default` | 设置默认音箱 |
| DELETE | `/api/bluetooth/devices/{address}` | 删除配对记录 |
| GET | `/api/player` | 播放状态、音量、播放器自检 |
| POST | `/api/player/play` · `/stop` · `/volume` | 播放 / 停止 / 设置音量 |
| POST | `/api/tts/speak` | TTS 语音播报 |
| GET / POST | `/api/rules` | 规则列表 / 新建规则 |
| GET / PUT / DELETE | `/api/rules/{id}` | 查询 / 修改 / 删除规则 |
| POST | `/api/rules/{id}/run` · `/toggle` | 立即执行 / 启用停用 |
| GET / POST | `/api/rules/export` · `/import` | 规则导出 / 导入 |
| GET | `/api/library` | 声音库列表 |
| GET / DELETE | `/api/logs` | 日志查询 / 清空 |
| GET | `/api/events` | 最近规则执行记录 |
| GET | `/api/diag` | 系统自检结果与修复建议 |
| POST / GET | `/api/hooks/{rule_id}` | 外部触发某条 manual 规则（query 作为上下文） |

接口细节见 [docs/API.md](docs/API.md)。

## 目录结构

```
istoreos-btspeaker/
├── btspeaker/                 # Python 服务（安装到 /usr/lib/btspeaker/btspeaker）
│   ├── __init__.py            # 版本与产品常量
│   ├── __main__.py / cli.py   # 命令行入口（serve / seed / status / devices / play / say ...）
│   ├── api.py                 # HTTP 服务、路由、鉴权、静态资源托管
│   ├── service.py             # 组件装配与生命周期（App / run_service）
│   ├── bluetooth.py           # 蓝牙后端（BlueZ CLI + 模拟后端）与状态轮询
│   ├── audio.py               # 播放器（mpg123 / aplay / ffmpeg / mock）
│   ├── tts.py                 # TTS（HTTP / espeak / mock）
│   ├── rules.py               # 规则模型、校验、RuleStore
│   ├── scheduler.py           # 定时调度线程
│   ├── engine.py              # 自动化引擎（触发匹配、动作执行、冷却、持久化）
│   ├── config.py              # 配置默认值 / 加载 / 落盘
│   ├── bus.py                 # 事件总线
│   ├── store.py               # JSON 存储与运行状态
│   ├── util.py                # 日志、命令、模板、时间等公共工具
│   ├── seed_rules.json        # 5 条示例规则（默认停用）
│   └── web/                   # 纯静态 Web 控制台（无构建、无 CDN）
├── scripts/
│   ├── init.d-btspeaker       # procd 启动脚本（安装为 /etc/init.d/btspeaker）
│   ├── btspeaker-launcher.sh  # 启动器（安装为 /usr/bin/btspeaker）
│   ├── bt-diag.sh             # 蓝牙链路中文诊断脚本
│   └── build_ipk.py           # 纯标准库 .ipk 打包工具
├── assets/sounds/             # 内置声音库（chime / welcome / alarm）
├── install.sh                 # 一键安装 / 升级
├── uninstall.sh               # 卸载（默认保留配置，--purge 彻底删除）
├── docs/                      # INSTALL / FAQ / API / RULES / CONTRACTS
├── tests/                     # 标准库 unittest 测试
├── dist/                      # 构建产物（ipk / tar.gz，不入库）
└── VERSION
```

路由器上的安装布局：

```
/usr/bin/btspeaker                     启动器（PYTHONPATH=/usr/lib/btspeaker）
/usr/lib/btspeaker/btspeaker/          Python 包（含 web/）
/etc/init.d/btspeaker                  procd 启动脚本（开机自启）
/usr/share/btspeaker/sounds/           内置声音库
/usr/share/btspeaker/scripts/bt-diag.sh
/etc/btspeaker/                        数据目录：config.json、rules.json、state.json、
                                       btspeaker.log、sounds/、tmp/
```

## 排障

先跑一遍诊断，大部分问题能直接定位：

```bash
sh /usr/share/btspeaker/scripts/bt-diag.sh   # 蓝牙/音频/服务/端口/日志逐项检查
btspeaker diag                               # 服务内置自检（蓝牙/播放器/TTS）
tail -n 100 /etc/btspeaker/btspeaker.log     # 服务日志
logread | grep -i btspeaker                  # procd 系统日志
```

| 现象 | 先检查 |
| --- | --- |
| 打不开网页 | `pgrep -f 'btspeaker serve'`、`netstat -lnt | grep 8722`、`/etc/init.d/btspeaker status` |
| 没声音 | `bluetoothctl info <MAC>` 是否 Connected、`aplay -L` 是否有 bluealsa、mpg123 是否安装 |
| 配不上对 | 音箱是否处于配对模式、`bluetoothctl show` 是否 Powered: yes、`hciconfig` 是否有 hci0 |
| TTS 不响 | 设置页 TTS 地址/方法/模板是否正确，或安装 `espeak` 并切换后端 |
| 规则不触发 | 规则是否启用、`rules.enabled` 是否为 true、冷却时间、`btspeaker logs` 是否有 error |

更细的排查步骤见 [docs/FAQ.md](docs/FAQ.md)。开发机无蓝牙硬件时可用模拟模式：

```bash
BTSPEAKER_SIM=1 python -m btspeaker serve --data-dir .btsdata --port 8722
```

## 卸载

```bash
# 方式一（源码目录）
sh uninstall.sh            # 停止并删除程序文件，保留 /etc/btspeaker 配置与数据
sh uninstall.sh --purge    # 同时删除 /etc/btspeaker（配置、规则、日志、用户声音）

# 方式二（opkg）
opkg remove btspeaker
rm -rf /etc/btspeaker      # 需要彻底清理时手动删除
```

卸载会移除 `/usr/lib/btspeaker`、`/usr/bin/btspeaker`、`/etc/init.d/btspeaker`、`/usr/share/btspeaker` 以及防火墙放行规则；默认保留下 `/etc/btspeaker`，重新安装后配置依旧可用。

## 安全说明

- **面向局域网设计**：默认监听 `0.0.0.0:8722`，HTTP 明文传输，**不要直接暴露到公网**。安装脚本只放行 lan 区域；如需从 WAN 访问，请自行加反向代理与 TLS，并设置强 `api_token`。
- **API Token**：在「设置 → 安全」或 `config.json` 的 `security.api_token` 中设置后，除健康检查外的 API 都需要 `X-Api-Token`。命令行用 `--token` 或环境变量 `BTSPEAKER_TOKEN`。
- **命令执行默认关闭**：规则里的 `command` 动作只有在 `security.allow_commands=true` 时才允许，且 `security.command_allowlist` 非空时只允许前缀匹配的命令。开启前请确认路由器仅自己可管理。
- **服务以 root 运行**：蓝牙控制（`bluetoothctl`）与 ALSA 放音需要 root 权限；请为路由器设置强密码、关闭 WAN 侧 ssh，并限制 8722 只对可信网段开放。
- **TTS 地址与 headers 会保存在配置中**：如果 TTS 服务 URL 带密钥，请使用内网服务或注意 `config.json` 的备份/分享范围。
- 所有 API 返回配置时会对 `api_token` 脱敏（`******`）；日志与状态接口不返回密钥。

## 开发

```bash
python -m unittest discover -s tests -v    # 全部测试（无需硬件/外网）
BTSPEAKER_SIM=1 python -m btspeaker serve --data-dir .btsdata --port 8722
python scripts/build_ipk.py                # 构建 ipk + 离线 tar.gz
```

- 运行环境（路由器）：Python 3.11 `python3-light`；代码保持 Python 3.9 语法兼容、只用标准库。
- 开发机：Windows / Linux / macOS + Python 3.9+，模拟模式可完整跑通 Web、规则、TTS（mock）。
- 契约与模块边界见 [docs/CONTRACTS.md](docs/CONTRACTS.md)；测试从仓库根目录运行。

## 许可证

MIT，见 [LICENSE](LICENSE)。
