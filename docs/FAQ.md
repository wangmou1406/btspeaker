# 常见问题 FAQ —— 蓝音管家 BTSpeaker

> 本文针对 **Linux / systemd** 安装方式；安装步骤与依赖说明见 [INSTALL.md](INSTALL.md)。
> OpenWrt/iStoreOS 旧路线已归档在 `packaging/openwrt/`，不推荐继续使用。

先记住三条排障命令，本文所有小节都从它们开始：

```bash
sudo sh /opt/btspeaker/scripts/bt-diag.sh     # 服务/端口/蓝牙/BlueALSA/播放器逐项中文诊断
btspeaker diag                                # 服务内置自检（蓝牙/播放器/TTS）
journalctl -u btspeaker -n 100 --no-pager     # systemd 日志（服务日志在 /etc/btspeaker/btspeaker.log）
```

目录：

1. [播放了但音箱没有声音](#1-播放了但音箱没有声音)
2. [扫描得到但配不上对 / 连接失败](#2-扫描得到但配不上对--连接失败)
3. [bluealsa 未启动 / 没有 bluealsa PCM](#3-bluealsa-未启动--没有-bluealsa-pcm)
4. [mpg123 不支持 alsa 输出](#4-mpg123-不支持-alsa-输出)
5. [TTS 怎么配置 / TTS 不响](#5-tts-怎么配置--tts-不响)
6. [端口冲突 / 8722 打不开](#6-端口冲突--8722-打不开)
7. [服务起不来，怎么看日志](#7-服务起不来怎么看日志)
8. [如何恢复出厂设置](#8-如何恢复出厂设置)
9. [卸载残留怎么清理](#9-卸载残留怎么清理)
10. [其它：401、规则不触发、命令动作被拒绝、依赖缺失](#10-其它常见问题)

---

## 1. 播放了但音箱没有声音

**现象**：页面提示播放成功，音箱无声音；或日志里有 `player.failed`。

**逐层排查**：

```bash
# ① 是否真的有已连接音箱
bluetoothctl info <MAC> | grep -E 'Name|Connected|Paired'   # 期望 Connected: yes
btspeaker status                                             # bluetooth.connected 不为 null

# ② 系统是否把 bluealsa 当成可用 PCM
aplay -L | grep -i bluealsa

# ③ 播放器自检结果（会直接给出中文 hint）
btspeaker diag

# ④ 直接测试 ALSA 链路（音箱需已连接）
aplay -D bluealsa:HCI=hci0,DEV=<MAC>,PROFILE=a2dp /usr/share/btspeaker/sounds/chime.wav
```

**常见原因与处理**：

| 原因 | 处理 |
| --- | --- |
| 音箱没连接（只是配对） | 页面「音箱」页点「连接」，或 `btspeaker connect <MAC>` |
| 默认音箱不是当前连的那台 | 页面「设为默认」，或 `btspeaker default <MAC>` |
| 音量太低 / 被静音 | `btspeaker volume 70`；规则「深夜自动静音」可能已把音量降到 15 |
| ALSA 设备模板与实际不符 | 修改 `/etc/btspeaker/config.json` 的 `player.alsa_device`（默认 `bluealsa:DEV={mac},PROFILE=a2dp`；BlueALSA 4.x 不接受 HCI 参数） |
| 音箱输入源没切到蓝牙 | 在音箱上切到蓝牙输入，或重新连接 |
| bluealsa 未运行 | 见 [第 3 节](#3-bluealsa-未启动--没有-bluealsa-pcm) |
| 格式不受支持 | mp3 装 `mpg123`；其它格式装 `ffmpeg`；或改用 wav |

> 播放动作的失败信息会写明原因（如「mpg123 未安装」「未连接蓝牙音箱」），优先看
> `journalctl -u btspeaker -n 50`、`/etc/btspeaker/btspeaker.log` 与 `/api/events`。

## 2. 扫描得到但配不上对 / 连接失败

**现象**：扫描能看到设备，点配对无反应或报错；或配对成功但连接失败。

**排查**：

```bash
bluetoothctl show                  # 确认 Powered: yes，记下 hci0 的 MAC
bluetoothctl devices               # 已发现/已配对设备
bluetoothctl info <MAC>            # Paired / Trusted / Connected 状态
systemctl status bluetooth         # BlueZ 服务状态
rfkill list bluetooth              # 是否被软/硬开关禁用
dmesg | tail -50 | grep -i bluetooth
```

**处理**：

```bash
sudo systemctl enable --now bluetooth
bluetoothctl power on
bluetoothctl agent on
bluetoothctl default-agent
bluetoothctl scan on               # 保持一会，等音箱出现在扫描列表
# 另开终端：
bluetoothctl pair <MAC>
bluetoothctl trust <MAC>
bluetoothctl connect <MAC>
```

- 音箱需进入**配对模式**（多数音箱长按蓝牙键直到指示灯快速闪烁）；
- 之前配对过但失败，先在页面删除设备，或在音箱端清除配对记录后重试；
- USB 蓝牙适配器不识别：`lsusb | grep -i bluetooth`、`dmesg | tail -30`；内核模块通常随发行版内核自带，极老内核需装 `btusb`；
- 蓝牙被禁用：`sudo rfkill unblock bluetooth`；笔记本硬开关/BIOS 关闭时 `Hard blocked: yes` 需物理开启；
- 已连接手机的音箱通常拒绝新主机连接，先在手机端断开。

## 3. bluealsa 未启动 / 没有 bluealsa PCM

**现象**：`aplay -L` 里没有 `bluealsa`；日志提示「未检测到 BlueALSA PCM」；`bt-diag.sh` 报 `bluealsa 未运行`。

**检查**：

```bash
pgrep -a bluealsa
aplay -L | grep -i bluealsa
systemctl list-unit-files | grep -i -E 'bluealsa|bluez-alsa'
```

**处理**：

```bash
# 安装（发行版包名不同，按需选择）
sudo apt install bluez-alsa-utils          # Debian / Ubuntu
sudo dnf install bluez-alsa                # Fedora
sudo pacman -S bluez-alsa                  # Arch
sudo zypper install bluez-alsa             # openSUSE

# 启动服务（服务名可能是 bluealsa 或 bluez-alsa）
sudo systemctl enable --now bluealsa
# 若提示 unit 不存在，看看实际名称：
systemctl list-unit-files | grep -i -E 'bluealsa|bluez-alsa'
```

如果 bluealsa 未提供 systemd 服务，可手工以 A2DP sink 模式启动（临时）：

```bash
sudo /usr/bin/bluealsa -p a2dp-sink &
```

如果确认 bluealsa 在运行但 `aplay -L` 仍无输出，通常是**音箱未连接**：BlueALSA 只有在一台 A2DP 设备连接后才暴露对应 PCM。先去「音箱」页连接设备，再执行 `aplay -L | grep bluealsa`。

另外检查 `config.json` 的 `player.backend`：`auto`（默认）会在检测不到 bluealsa 时给出提示；不要误设成 `mock`，否则服务只“假装”播放、不会出声。

## 4. mpg123 不支持 alsa 输出

**现象**：mp3 播放失败，日志类似 `mpg123: no output module named alsa` / `Output module 'alsa' not found`；wav（走 `aplay`）却正常。

**检查 mpg123 的输出模块**：

```bash
mpg123 --list-modules 2>/dev/null | grep -i alsa
# 或
mpg123 --longhelp 2>&1 | grep -i alsa
```

**处理**：

1. 重新安装带 alsa 输出的版本：`sudo apt install --reinstall mpg123`（Fedora: `sudo dnf reinstall mpg123`）；
2. 若发行版的 mpg123 确实未编译 alsa 输出，改用 wav 或让服务走 `aplay`：
   - 在设置里把提示音换成 `.wav`（内置 `chime.wav`、`welcome.wav`、`alarm.wav` 都是 wav，aplay 直接播放）；
   - 或安装 `ffmpeg`，把其它格式转成 wav 后播放；
3. 也可以自建一个“mp3 → wav”的 TTS/预处理脚本，但推荐优先解决 mpg123 的 alsa 输出模块。

## 5. TTS 怎么配置 / TTS 不响

推荐使用**在线 TTS 服务**（中文音质最好），其次 `espeak-ng` 离线兜底，最后还有 `mock`（只用于开发测试，不会出声）。

在「设置 → TTS」或 `config.json` 的 `tts` 段配置：

```json
{
  "tts": {
    "backend": "auto",
    "url": "http://192.168.1.10:8080/tts?text={text}&lang={lang}",
    "method": "GET",
    "headers": {},
    "body_template": "",
    "voice": "zh",
    "speed": 150,
    "espeak": "espeak",
    "timeout": 20,
    "format": "mp3"
  }
}
```

- **`url`**：支持 `{text}`（自动 URL 编码）与 `{lang}`（取自 `voice`）模板变量；`method=POST` 时用 `body_template`（空则直接发送原文），headers 用 `headers`；
- **返回格式**：根据响应 `Content-Type` 决定后缀（`audio/mpeg`→mp3、`audio/wav`→wav、`audio/ogg`→ogg，其它按 `format`）；
- **backend=auto**：`url` 非空 → HTTP；否则有 `espeak` → espeak；否则报错并提示；
- 安装离线兜底：`sudo sh /opt/btspeaker/scripts/install-deps.sh --optional`（装 `espeak-ng`）；然后 `btspeaker say 测试一下`；
- 若发行版只装了 `espeak-ng` 而配置里 `tts.espeak` 仍是 `espeak`，把它改成 `espeak-ng` 即可。

**排查 TTS 不响**：

```bash
btspeaker diag | grep -A2 -i tts
journalctl -u btspeaker -n 50 --no-pager | grep -i tts
# 用 curl 直接测你的 TTS 地址返回的是不是音频
curl -s -D - "http://你的TTS地址" -o /tmp/tts.out; file /tmp/tts.out
```

非音频响应（如 HTML 错误页）会被判为失败，日志提示「TTS 服务返回了非音频内容」。

## 6. 端口冲突 / 8722 打不开

**检查**：

```bash
ss -lntp | grep 8722            # 或 netstat -lntp | grep 8722
systemctl status btspeaker
curl -s http://127.0.0.1:8722/api/health
```

**处理**：

- 若 8722 被其它程序占用：改配置 `server.port`（如 `18800`），然后 `sudo systemctl restart btspeaker`，
  同时更新防火墙放行规则（端口从配置读取，不需要改 systemd unit）；
- 若本机 `127.0.0.1` 能通、局域网 IP 不通：检查防火墙（见 [INSTALL.md 第 7 节](INSTALL.md#7-防火墙)），放行 TCP 端口；
- 若服务没起来：见 [第 7 节](#7-服务起不来怎么看日志)；
- 浏览器打不开但 `curl` 正常：清理浏览器缓存 / 换浏览器，确认访问的是 `http://` 而不是 `https://`。

## 7. 服务起不来，怎么看日志

**按顺序看三处日志**：

```bash
# ① systemd 状态与日志（启动失败、崩溃重启、退出码）
systemctl status btspeaker --no-pager
journalctl -u btspeaker -n 100 --no-pager

# ② 服务日志（业务运行、异常堆栈）
tail -n 200 /etc/btspeaker/btspeaker.log

# ③ 手工前台启动看实时输出（先停服务）
sudo systemctl stop btspeaker
sudo PYTHONPATH=/opt/btspeaker python3 -m btspeaker serve --data-dir /etc/btspeaker --log-level DEBUG
```

**常见原因**：

| 日志/现象 | 原因与处理 |
| --- | --- |
| `python3: command not found` / `No module named btspeaker` | 依赖缺失或程序目录不完整：安装 `python3`，或重新执行 `sudo bash install.sh` |
| `Address already in use` | 端口被占用，见 [第 6 节](#6-端口冲突--8722-打不开) |
| `Permission denied`（/etc/btspeaker） | 用 root 运行；检查数据目录权限 `ls -ld /etc/btspeaker` |
| 进程反复重启 | `journalctl -u btspeaker -n 200 --no-pager` 看退出原因；多为配置 JSON 写坏，备份后删掉 `config.json` 再启动（会重新生成） |
| `systemd` 报 unit 不存在 | 先 `sudo cp packaging/systemd/btspeaker.service /etc/systemd/system/`，再 `sudo systemctl daemon-reload` |
| 升级后起不来 | 旧进程未退出：`pkill -f 'btspeaker serve'` 后 `sudo systemctl restart btspeaker` |
| 容器/WSL 无 systemd | 改为手动前台运行：`sudo /usr/local/bin/btspeaker serve --data-dir /etc/btspeaker` |

服务启动后会写 `state.json` 与 `btspeaker.log`；如果数据目录只读，服务仍可运行但日志落盘会失败，建议保证 `/etc/btspeaker` 可写。

## 8. 如何恢复出厂设置

「恢复出厂」= 清空配置、规则与运行状态，保留程序与声音库：

```bash
# 1) 停止服务
sudo systemctl stop btspeaker

# 2) 备份（可选，后悔时能救回来）
sudo tar -czf ~/btspeaker-backup-$(date +%Y%m%d-%H%M).tar.gz /etc/btspeaker

# 3) 清空数据但保留目录结构
sudo rm -rf /etc/btspeaker/config.json /etc/btspeaker/rules.json /etc/btspeaker/state.json
sudo rm -rf /etc/btspeaker/sounds/* /etc/btspeaker/tmp/*

# 4) 重新初始化（写入默认配置 + 5 条默认停用的示例规则）
sudo PYTHONPATH=/opt/btspeaker python3 -m btspeaker seed --data-dir /etc/btspeaker

# 5) 启动
sudo systemctl start btspeaker
```

只想恢复默认规则：删除 `rules.json` 后执行第 4 步；只想恢复默认配置：删除 `config.json` 后执行第 4 步（服务启动时也会自动补齐缺失配置）。

## 9. 卸载残留怎么清理

```bash
# 仓库目录内执行：
sudo bash uninstall.sh            # 标准卸载（保留 /etc/btspeaker）
sudo bash uninstall.sh --purge    # 彻底清理（含配置、规则、日志、用户声音）
```

**确认没有残留**：

```bash
pgrep -f 'btspeaker serve'                                  # 应无输出
ls /usr/local/bin/btspeaker /etc/systemd/system/btspeaker.service 2>&1   # 应报 No such file
ls -d /opt/btspeaker /usr/share/btspeaker 2>&1              # 应报 No such file
systemctl status btspeaker 2>&1 | head -3                    # 应报 not found / inactive
```

若进程仍在：`pkill -f 'btspeaker serve'`，再确认自启已取消：`systemctl is-enabled btspeaker`。
`/etc/btspeaker` 默认保留是刻意设计，避免误删用户规则与声音文件。

## 10. 其它常见问题

### 页面提示 401 Unauthorized

说明配置了 `security.api_token`。在页面弹出的 token 输入框填入正确 token（会存到浏览器 localStorage），或用请求头：

```bash
curl -s -H 'X-Api-Token: 你的token' http://127.0.0.1:8722/api/status
btspeaker --token 你的token status
```

忘记 token：`sudo grep api_token /etc/btspeaker/config.json`，或清空该字段后 `sudo systemctl restart btspeaker`（仅建议在可信局域网内无 token）。

### 规则不触发

- 规则是否启用（示例规则默认**全部停用**）：`btspeaker rules list`，或页面「自动化」页开关；
- 全局规则开关 `rules.enabled` 是否为 true；
- 定时类规则检查当前时间、星期（`days` 为空表示每天）、是否在冷却期内（`cooldown` 秒）；
- 蓝牙事件类规则要求事件发生（如连接音箱）且 `device` 为空或匹配该 MAC；
- 条件不满足也会跳过（如 `connected` 条件要求已连接音箱）；
- 用「立即执行」按钮或 `btspeaker rules run <id>` 绕过触发器直接验证动作链路；执行结果看 `/api/events` 或日志。

### 命令动作被拒绝

规则里 `command` 动作默认禁用：需要 `security.allow_commands=true`，且若 `security.command_allowlist` 非空，命令必须以白名单前缀开头。开启后请务必保证主机管理面（ssh/Web）不对不可信网络开放。

### 依赖缺失 / 装不上

一键安装时依赖安装失败不影响程序文件部署，可稍后补装：

```bash
sudo sh /opt/btspeaker/scripts/install-deps.sh              # 必需依赖
sudo sh /opt/btspeaker/scripts/install-deps.sh --optional   # 再加 espeak-ng / ffmpeg
sudo systemctl restart btspeaker
```

各发行版包名对照见 [INSTALL.md 第 2 节](INSTALL.md#2-依赖包说明)。若 `bluez-alsa` 始终装不上，
可先从 [BlueALSA 上游](https://github.com/arkq/bluez-alsa) 编译安装，再启动 `bluealsa` 服务。

### 关于 OpenWrt / iStoreOS 旧问题

FAQ 旧版中的 ipk 打包格式、opkg 软件源失效、`Architecture: all` 等排障内容已随 OpenWrt 路线归档
（`packaging/openwrt/`），普通 Linux 用户无需关心。

---

还有问题？收集以下信息后反馈，通常能快速定位：

```bash
sudo sh /opt/btspeaker/scripts/bt-diag.sh > /tmp/bt-diag.txt 2>&1
btspeaker status > /tmp/status.json 2>&1
journalctl -u btspeaker -n 200 --no-pager > /tmp/btspeaker-journal.log
cat /etc/os-release
```
