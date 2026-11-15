# 常见问题 FAQ —— 蓝音管家 BTSpeaker

先记住三条排障命令，本文所有小节都从它们开始：

```bash
sh /usr/share/btspeaker/scripts/bt-diag.sh    # 蓝牙/音频/服务/端口逐项中文诊断
btspeaker diag                                # 服务内置自检（蓝牙/播放器/TTS）
tail -n 100 /etc/btspeaker/btspeaker.log      # 服务日志
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
10. [其它：401、规则不触发、命令动作被拒绝](#10-其它常见问题)

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
| ALSA 设备模板与实际不符 | 修改 `/etc/btspeaker/config.json` 的 `player.alsa_device`（默认 `bluealsa:HCI={hci},DEV={mac},PROFILE=a2dp`） |
| 音箱输入源没切到蓝牙 | 在音箱上切到蓝牙输入，或重新连接 |
| bluealsa 未运行 | 见 [第 3 节](#3-bluealsa-未启动--没有-bluealsa-pcm) |
| 格式不受支持 | mp3 装 `mpg123`；其它格式装 `ffmpeg`；或改用 wav |

> 播放动作的失败信息会写明原因（如「mpg123 未安装：opkg install mpg123」「未连接蓝牙音箱」），优先看 `/etc/btspeaker/btspeaker.log` 与 `/api/events`。

## 2. 扫描得到但配不上对 / 连接失败

**现象**：扫描能看到设备，点配对无反应或报错；或配对成功但连接失败。

**排查**：

```bash
bluetoothctl show                  # 确认 Powered: yes，记下 hci0 的 MAC
bluetoothctl devices               # 已发现/已配对设备
bluetoothctl info <MAC>            # Paired / Trusted / Connected 状态
dmesg | tail -50 | grep -i bluetooth
```

**处理**：

```bash
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
- USB 蓝牙适配器需 `kmod-btusb`，检查 `lsusb` 与 `hciconfig -a`，没有 `hci0` 说明驱动/供电问题（部分适配器需要外接供电或 USB 延长线）；
- 已连接手机的音箱通常拒绝新主机连接，先在手机端断开。

## 3. bluealsa 未启动 / 没有 bluealsa PCM

**现象**：`aplay -L` 里没有 `bluealsa`；日志提示「未检测到 BlueALSA PCM」；`bt-diag.sh` 报 `bluealsa 未运行`。

**检查**：

```bash
pgrep -a bluealsa
aplay -L | grep -i bluealsa
opkg list-installed | grep -E 'bluealsa|alsa-utils'
```

**处理**：

```bash
# 安装（若未安装）
opkg update && opkg install bluealsa alsa-utils

# 有 init 脚本的固件：
/etc/init.d/bluealsa enable
/etc/init.d/bluealsa start
# 没有 init 脚本时手工启动（A2DP sink 模式）：
/usr/bin/bluealsa -p a2dp-sink &
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

1. 重新安装带 alsa 输出的版本：`opkg update && opkg install --force-reinstall mpg123`；
2. 若固件源的 mpg123 确实未编译 alsa 输出，改用 wav 或让服务走 `aplay`：
   - 在设置里把提示音换成 `.wav`（内置 `chime.wav`、`welcome.wav`、`alarm.wav` 都是 wav，aplay 直接播放）；
   - 或安装 `ffmpeg`，把其它格式转成 wav 后播放；
3. 也可以自建一个“mp3 → wav”的 TTS/预处理脚本，但推荐优先解决 mpg123 的 alsa 输出模块。

## 5. TTS 怎么配置 / TTS 不响

推荐使用**在线 TTS 服务**（中文音质最好），其次 `espeak` 离线兜底，最后还有 `mock`（只用于开发测试，不会出声）。

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
- 配置 `espeak`：`opkg install espeak`，然后 `btspeaker say 测试一下`；
- 用 `btspeaker say 你好` 或总览页「TTS 播报」验证；失败原因会写进日志与规则执行记录。

**排查 TTS 不响**：

```bash
btspeaker diag | grep -A2 tts
tail -n 50 /etc/btspeaker/btspeaker.log | grep -i tts
# 用 curl 直接测你的 TTS 地址返回的是不是音频
curl -s -D - "http://你的TTS地址" -o /tmp/tts.out; file /tmp/tts.out
```

非音频响应（如 HTML 错误页）会被判为失败，日志提示「TTS 服务返回了非音频内容」。

## 6. 端口冲突 / 8722 打不开

**检查**：

```bash
netstat -lnt | grep 8722        # 或 ss -lnt | grep 8722
/etc/init.d/btspeaker status
wget -qO- http://127.0.0.1:8722/api/health
```

**处理**：

- 若 8722 被其它程序占用：改配置 `server.port`（如 `18800`），同步修改 `/etc/init.d/btspeaker` 与防火墙规则对应的端口，然后 `restart`；
- 若本机 `127.0.0.1` 能通、局域网 IP 不通：检查防火墙（见 [INSTALL.md 第 8 节](INSTALL.md#8-防火墙)），确认放行 TCP 端口、源区域为 lan；
- 若服务没起来：见 [第 7 节](#7-服务起不来怎么看日志)；
- 浏览器打不开但 `wget` 正常：清理浏览器缓存 / 换浏览器，确认访问的是 `http://` 而不是 `https://`。

## 7. 服务起不来，怎么看日志

**按顺序看三处日志**：

```bash
# ① procd / 系统日志（启动失败、崩溃重启）
logread | grep -i btspeaker | tail -30

# ② 服务日志（业务运行、异常堆栈）
tail -n 200 /etc/btspeaker/btspeaker.log

# ③ 手工前台启动看实时输出（先停服务）
/etc/init.d/btspeaker stop
/usr/bin/btspeaker serve --data-dir /etc/btspeaker --log-level DEBUG
```

**常见原因**：

| 日志/现象 | 原因与处理 |
| --- | --- |
| `python3: not found` / `No module named btspeaker` | 依赖缺失或程序目录不完整：`opkg install python3-light`，或重装应用 |
| `Address already in use` | 端口被占用，见 [第 6 节](#6-端口冲突--8722-打不开) |
| `No module named 'urllib'` / `logging` / `uuid` | 缺 python3 拆分包：`opkg install python3-urllib python3-logging python3-uuid` |
| `Permission denied`（/etc/btspeaker） | 用 root 运行；检查数据目录权限 `ls -ld /etc/btspeaker` |
| 进程反复重启 | `logread | grep procd` 查看退出码；通常是配置 JSON 写坏导致启动异常，备份后删掉 `config.json` 再启动（会重新生成） |
| 升级后起不来 | 旧进程未退出：`pkill -f 'btspeaker serve'` 后 `/etc/init.d/btspeaker start` |

服务启动后会写 `state.json` 与 `btspeaker.log`；如果数据目录只读，服务仍可运行但日志落盘会失败，建议保证 `/etc/btspeaker` 可写。

## 8. 如何恢复出厂设置

「恢复出厂」= 清空配置、规则与运行状态，保留程序与声音库：

```bash
# 1) 停止服务
/etc/init.d/btspeaker stop

# 2) 备份（可选，后悔时能救回来）
tar -czf /root/btspeaker-backup-$(date +%Y%m%d-%H%M).tar.gz /etc/btspeaker

# 3) 清空数据但保留目录结构
rm -rf /etc/btspeaker/config.json /etc/btspeaker/rules.json /etc/btspeaker/state.json
rm -rf /etc/btspeaker/sounds/* /etc/btspeaker/tmp/*

# 4) 重新初始化（写入默认配置 + 5 条默认停用的示例规则）
/usr/bin/btspeaker seed --data-dir /etc/btspeaker

# 5) 启动
/etc/init.d/btspeaker start
```

只想恢复默认规则：删除 `rules.json` 后执行第 4 步；只想恢复默认配置：删除 `config.json` 后执行第 4 步（服务启动时也会自动补齐缺失配置）。

## 9. 卸载残留怎么清理

```bash
# 标准卸载（保留 /etc/btspeaker）
sh uninstall.sh                    # 源码方式
opkg remove btspeaker              # opkg 方式

# 彻底清理（含配置、规则、日志、用户声音）
sh uninstall.sh --purge
rm -rf /etc/btspeaker
```

**确认没有残留**：

```bash
pgrep -f 'btspeaker serve'                  # 应无输出
ls /usr/bin/btspeaker /etc/init.d/btspeaker 2>&1   # 应报 No such file
ls -d /usr/lib/btspeaker /usr/share/btspeaker 2>&1
uci show firewall | grep -i btspeaker       # 应无输出
ls -l /etc/rc.d/ | grep btspeaker           # 应无输出
```

若进程仍在：`pkill -f 'btspeaker serve'`，再检查 `/etc/rc.d` 里的自启软链是否删除（`/etc/init.d/btspeaker disable`）。opkg 卸载不会自动删除 `/etc/btspeaker`，这是为了避免误删用户规则与声音文件。

## 10. 其它常见问题

### 页面提示 401 Unauthorized

说明配置了 `security.api_token`。在页面弹出的 token 输入框填入正确 token（会存到浏览器 localStorage），或用请求头：

```bash
wget -qO- --header='X-Api-Token: 你的token' http://127.0.0.1:8722/api/status
btspeaker --token 你的token status
```

忘记 token：`grep api_token /etc/btspeaker/config.json`（root 可读），或清空该字段后重启服务（局域网环境才建议无 token）。

### 规则不触发

- 规则是否启用（示例规则默认**全部停用**）：`btspeaker rules list`，或页面「自动化」页开关；
- 全局规则开关 `rules.enabled` 是否为 true；
- 定时类规则检查当前时间、星期（`days` 为空表示每天）、是否在冷却期内（`cooldown` 秒）；
- 蓝牙事件类规则要求事件发生（如连接音箱）且 `device` 为空或匹配该 MAC；
- 条件不满足也会跳过（如 `connected` 条件要求已连接音箱）；
- 用「立即执行」按钮或 `btspeaker rules run <id>` 绕过触发器直接验证动作链路；执行结果看 `/api/events` 或日志。

### 命令动作被拒绝

规则里 `command` 动作默认禁用：需要 `security.allow_commands=true`，且若 `security.command_allowlist` 非空，命令必须以白名单前缀开头。开启后请务必保证路由器管理面（ssh/Web）不对不可信网络开放。

### 安装报 `pkg_init_from_file: Malformed package file`

说明这个 ipk 的外层容器格式不对。新版 OpenWrt / iStoreOS 的 opkg 只接受 **tar.gz 外层**（成员为 `./debian-binary`、`./data.tar.gz`、`./control.tar.gz`），旧式 ar 容器会被直接拒绝。

- 用本仓库 `python scripts/build_ipk.py` 重新构建（v1.0.0 修复后产物已改为 tar.gz 格式），不要用老的 `ar` / `ipkg-build` 手工打包；
- 或改用离线包绕过 opkg 格式问题：`scp dist/btspeaker-1.0.0-all.tar.gz` 到路由器，解压后 `sh install.sh`（效果等价）；
- 重新安装可加 `--force-reinstall`：`opkg install --force-reinstall /tmp/btspeaker_1.0.0_all.ipk`。

### 安装报 `cannot find dependency python3-light` / `incompatible with the architectures configured`

这两个报错都来自 iStore 的**本地 ipk 安装沙箱**，不是包本身损坏：

- 沙箱不能解析软件源依赖，所以旧包 control 里的 `Depends` 全部报 `cannot find dependency`；
- 旧包声明 `Architecture: x86_64`，与沙箱允许的架构列表不匹配时报 `incompatible with the architectures configured`。

当前修复版包已改为 **`Architecture: all`（纯 Python + 脚本，跨架构）且 control 不写 `Depends`**：安装不再被依赖/架构拦截，依赖由 `postinst` 安装后自动补装。若安装后服务没起来（依赖没装全）：

```sh
sh /usr/share/btspeaker/scripts/install-deps.sh    # 幂等，重试安装 11 个必需依赖（bluealsa 为可选）
/etc/init.d/btspeaker restart
/etc/init.d/btspeaker status
python3 --version                                   # 确认 python3 可用
logread | grep btspeaker | tail -20
```

如果设备本身 `opkg update` 都失败（软件源不可用），直接改用离线包：解压后 `sh install.sh`，它会做同样的依赖安装并输出逐项结果。

### 依赖全部安装失败（`opkg update` 失败 / 找不到任何包）

如果 13 个依赖**全部**失败，通常不是包名问题，而是设备软件源不可用。iStoreOS 旧默认源
`mirrors.istoreos.com` / `downloads.istoreos.com` 的域名已经失效（DNS 解析不到），
`opkg update` 静默失败后自然什么都装不上。

先在路由器上确认：

```sh
cat /etc/openwrt_release | head -8     # 看 DISTRIB_RELEASE 与 DISTRIB_TARGET
cat /etc/opkg/distfeeds.conf
opkg update 2>&1 | tail -20            # 正常应显示各源下载索引成功
```

换可用镜像（**版本号要与 `DISTRIB_RELEASE` 一致**，下面以 23.05.5 / x86_64 为例，国内可用 USTC）：

```sh
cp /etc/opkg/distfeeds.conf /etc/opkg/distfeeds.conf.bak
cat > /etc/opkg/distfeeds.conf <<'EOF'
src/gz openwrt_core https://mirrors.ustc.edu.cn/openwrt/releases/23.05.5/targets/x86/64/packages
src/gz openwrt_base https://mirrors.ustc.edu.cn/openwrt/releases/23.05.5/packages/x86_64/base
src/gz openwrt_luci https://mirrors.ustc.edu.cn/openwrt/releases/23.05.5/packages/x86_64/luci
src/gz openwrt_packages https://mirrors.ustc.edu.cn/openwrt/releases/23.05.5/packages/x86_64/packages
src/gz openwrt_routing https://mirrors.ustc.edu.cn/openwrt/releases/23.05.5/packages/x86_64/routing
src/gz openwrt_telephony https://mirrors.ustc.edu.cn/openwrt/releases/23.05.5/packages/x86_64/telephony
EOF
opkg update
sh /usr/share/btspeaker/scripts/install-deps.sh
```

> 注意：`bluealsa` 在 OpenWrt 官方源中**不存在**，脚本会把它列为可选失败；A2DP 放音依赖它，若固件未内置，需要第三方预编译包或自行编译。

---

还有问题？收集以下信息后反馈，通常能快速定位：

```bash
sh /usr/share/btspeaker/scripts/bt-diag.sh > /tmp/bt-diag.txt 2>&1
btspeaker status > /tmp/status.json 2>&1
tail -n 200 /etc/btspeaker/btspeaker.log > /tmp/btspeaker.log
cat /etc/openwrt_release
```
