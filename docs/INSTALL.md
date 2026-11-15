# 安装指南 —— 蓝音管家 BTSpeaker

本文面向 iStoreOS / OpenWrt x86_64 用户，覆盖三种安装方式、依赖说明、手动安装、开机自启、防火墙、验证、升级与回滚。

- 产品：蓝音管家 BTSpeaker（纯 Python 标准库 Web 服务）
- 目标系统：iStoreOS（或 OpenWrt 23.05+）x86_64
- 默认端口：`8722`（TCP，监听 `0.0.0.0`）
- 安装路径：`/usr/lib/btspeaker`；启动器 `/usr/bin/btspeaker`；服务名 `btspeaker`
- 数据目录：`/etc/btspeaker`

---

## 1. 系统要求

| 项目 | 要求 | 检查命令 |
| --- | --- | --- |
| 系统 | iStoreOS / OpenWrt x86_64（opkg + procd） | `cat /etc/openwrt_release` |
| Python | 3.9 及以上（`python3-light`） | `python3 --version` |
| 蓝牙 | 板载/外接蓝牙适配器（USB 蓝牙需 `kmod-btusb`） | `lsusb`、`hciconfig -a` |
| 音频 | BlueALSA + ALSA 工具（A2DP 接收侧桥接） | `pgrep bluealsa`、`aplay -L` |
| 磁盘 | 约 5 MB 程序 + 声音库空间 | `df -h /overlay` |
| 权限 | root（安装、蓝牙控制、ALSA 放音） | `id -u` |

> Python 依赖全部来自 OpenWrt 的 python3 标准库拆分包，**不需要 pip，不需要任何第三方库**。

## 2. 依赖包说明

ipk 的 control **刻意不声明 `Depends`**：iStoreOS 的 iStore 本地安装沙箱无法解析 feeds 依赖，硬依赖会导致 `cannot find dependency` / `incompatible with the architectures configured` 安装失败。

依赖分三处处理：

1. **离线包 `install.sh`**：安装前统一 `opkg update && opkg install <下列包>`，失败会逐个安装并跳过固件源中不存在的包；
2. **ipk 的 `postinst`**：安装后在真机上检查缺失依赖并尽力安装（失败不阻断安装）；
3. **`/usr/share/btspeaker/scripts/install-deps.sh`**：以上都没装上时手动重试（幂等）：
   ```sh
   sh /usr/share/btspeaker/scripts/install-deps.sh          # 必需依赖
   sh /usr/share/btspeaker/scripts/install-deps.sh --optional  # 附带 espeak / ffmpeg
   ```

| 包名 | 作用 | 缺失影响 |
| --- | --- | --- |
| `python3-light` | Python 3 解释器与核心标准库 | 服务无法启动 |
| `python3-urllib` | HTTP 客户端（TTS 请求、URL 音频下载、CLI） | TTS / URL 播放 / 命令行不可用 |
| `python3-openssl` | HTTPS/TLS（HTTPS 的 TTS 接口） | HTTPS 资源不可用 |
| `python3-email` | 标准库 email 解析（HTTP 头/编码处理） | 部分 TTS 响应解析异常 |
| `python3-logging` | 日志模块 | 服务无法启动 |
| `python3-uuid` | UUID（规则/任务 ID） | 服务无法启动 |
| `bluez-daemon` | BlueZ 守护进程 `bluetoothd`、`bluetoothctl` | 无法扫描/配对/连接 |
| `bluez-utils` | `hciconfig` / `btmon` 等调试工具 | 诊断受限（可选性较强） |
| `bluealsa` | BlueALSA：把蓝牙 A2DP 音频桥接为 ALSA PCM（**OpenWrt 官方源无此包**，需固件内置或第三方源提供） | 无法通过蓝牙音箱放音（其余功能可用） |
| `alsa-utils` | `aplay` 等 ALSA 工具 | 无法放音 / 无法检测 bluealsa PCM |
| `mpg123` | MP3 解码播放（需带 alsa 输出模块） | mp3 无法播放；wav 仍可用 aplay |
| `kmod-bluetooth` | 蓝牙核心内核模块（在 targets/core 源里） | 无蓝牙适配器 |
| `kmod-btusb` | USB 蓝牙适配器驱动（**x86_64 内核已内建，官方源没有此包**，无需安装） | — |

可选增强（安装脚本只提示、不强制安装）：

| 包名 | 作用 | 安装建议 |
| --- | --- | --- |
| `espeak` | 离线 TTS 兜底（音质机械） | 无外网 TTS 服务时安装：`opkg install espeak` |
| `ffmpeg` | 非 mp3/wav 格式（ogg/flac 等）转码 | 需要播放其它格式时安装 |

## 3. 方式一：iStore 上传 ipk 安装（推荐）

### 3.1 构建 ipk

在开发机（Windows / Linux / macOS，装有 Python 3.9+）的项目根目录执行：

```bash
python scripts/build_ipk.py
```

产物：

- `dist/btspeaker_<版本>_all.ipk` —— opkg 安装包
- `dist/btspeaker-<版本>-all.tar.gz` —— 离线安装包（解压后执行 `install.sh`）

构建脚本只用标准库，会自动扫描仓库中真实存在的文件；构建完成后会重新解析 ipk 自校验结构（外层 tar.gz 容器与 3 个成员、control 字段、data 文件清单、可执行权限），失败会以非 0 退出。
> 注意：新版 OpenWrt / iStoreOS 的 opkg 要求 ipk 外层是 **tar.gz**（成员 `./debian-binary`、`./data.tar.gz`、`./control.tar.gz`）。旧式 ar 容器会被拒绝并报 `Malformed package file`，请务必用本仓库脚本构建，不要用老工具手工 ar 打包。

### 3.2 上传安装（iStoreOS 图形界面）

1. 浏览器登录 iStoreOS（LuCI）后台；
2. 打开 **iStore 应用商店 → 手动安装 / 上传安装**（不同固件版本菜单名略有差异，也可能是 **系统 → 软件包 → 上传软件包**）；
3. 选择 `dist/btspeaker_<版本>_all.ipk` 上传并安装；
4. 安装完成后刷新页面，左侧或服务列表中出现「蓝音管家 BTSpeaker」，或直接访问 `http://<路由器IP>:8722/`。

命令行等价操作：

```bash
scp dist/btspeaker_1.0.0_all.ipk root@192.168.1.1:/tmp/
ssh root@192.168.1.1
opkg update
opkg install /tmp/btspeaker_1.0.0_all.ipk
```

查看安装结果：

```bash
opkg list-installed | grep btspeaker
ls -l /usr/bin/btspeaker /etc/init.d/btspeaker
```

ipk 的 `postinst` 会自动完成：创建 `/etc/btspeaker/{sounds,tmp}` 等目录 → `btspeaker seed --data-dir /etc/btspeaker`（写入默认 `config.json` 与 5 条默认停用的示例规则）→ uci 放行 TCP 8722（lan 区域）→ `/etc/init.d/btspeaker enable && start`。

## 4. 方式二：ssh 运行 install.sh（源码方式）

适合没有 iStore 上传入口、或想直接跑源码的场景。

```bash
# 开发机：把整个项目目录传到路由器（任选其一）
scp -r istoreos-btspeaker root@192.168.1.1:/tmp/
# 或路由器上支持 git 时：git clone <仓库地址> /tmp/istoreos-btspeaker
```

```bash
ssh root@192.168.1.1
cd /tmp/istoreos-btspeaker
sh install.sh --dry-run     # 建议先预览：只打印将执行的操作，不改动系统
sh install.sh               # 正式安装；重复执行即升级覆盖
```

`install.sh` 选项：

| 选项 | 说明 |
| --- | --- |
| `--dry-run` / `-n` | 只预览将执行的操作，不修改系统（非 root 下也可运行） |
| `--no-deps` | 跳过 opkg 依赖安装（依赖已自备 / 离线环境） |
| `--help` / `-h` | 显示帮助 |

执行流程（全部幂等，可重复执行）：

1. 检查 root 权限，检测 `/etc/openwrt_release`（不匹配只警告并继续）；
2. `opkg update` + 安装依赖包（`--no-deps` 时跳过）；
3. 创建 `/usr/lib/btspeaker`、`/usr/share/btspeaker/sounds`、`/etc/btspeaker/{sounds,tmp}`；
4. 拷贝 `btspeaker/` 包（含 `web/`）到 `/usr/lib/btspeaker/btspeaker`，清理 `__pycache__`；
5. 安装启动器 `/usr/bin/btspeaker`、procd 启动脚本 `/etc/init.d/btspeaker`、诊断脚本 `/usr/share/btspeaker/scripts/bt-diag.sh` 并 `chmod 0755`；
6. 拷贝内置声音库（`assets/sounds/*.wav`）到 `/usr/share/btspeaker/sounds/`；
7. `btspeaker seed --data-dir /etc/btspeaker` 初始化配置与示例规则（**已存在则不覆盖**）；
8. uci 防火墙放行 TCP 8722（lan 区域）；
9. `/etc/init.d/btspeaker enable && start`；
10. 访问 `http://<LAN IP>:8722/api/health` 做一次健康检查，最后打印访问地址与后续步骤。

## 5. 方式三：离线 tar.gz 安装包

`dist/btspeaker-<版本>-all.tar.gz` 是自包含离线安装包（源码 + 安装脚本 + 声音库 + 文档）：

```bash
tar -xzf btspeaker-1.0.0-all.tar.gz
cd btspeaker-1.0.0
sh install.sh --no-deps        # 离线环境：依赖需事先自行准备好
```

## 6. 手动安装（不使用任何脚本）

需要完全掌控每一步时，可以按下面流程手工安装：

```bash
# 1) 依赖
opkg update
opkg install python3-light python3-urllib python3-openssl python3-email \
             python3-logging python3-uuid bluez-daemon bluez-utils bluealsa \
             alsa-utils mpg123 kmod-bluetooth kmod-btusb

# 2) 目录
mkdir -p /usr/lib/btspeaker /usr/share/btspeaker/{sounds,scripts} /etc/btspeaker/{sounds,tmp}

# 3) 程序文件（在项目根目录执行）
cp -R btspeaker /usr/lib/btspeaker/
cp scripts/btspeaker-launcher.sh /usr/bin/btspeaker
cp scripts/init.d-btspeaker      /etc/init.d/btspeaker
cp scripts/bt-diag.sh            /usr/share/btspeaker/scripts/bt-diag.sh
cp assets/sounds/*.wav           /usr/share/btspeaker/sounds/ 2>/dev/null || true
chmod 0755 /usr/bin/btspeaker /etc/init.d/btspeaker /usr/share/btspeaker/scripts/bt-diag.sh

# 4) 初始化配置与示例规则（幂等，已存在不覆盖）
/usr/bin/btspeaker seed --data-dir /etc/btspeaker

# 5) 防火墙放行 TCP 8722（lan 区域）
uci set firewall.btspeaker=rule
uci set firewall.btspeaker.name='btspeaker'
uci set firewall.btspeaker.src='lan'
uci set firewall.btspeaker.dest_port='8722'
uci set firewall.btspeaker.proto='tcp'
uci set firewall.btspeaker.target='ACCEPT'
uci commit firewall
/etc/init.d/firewall reload

# 6) 开机自启并启动
/etc/init.d/btspeaker enable
/etc/init.d/btspeaker start
```

> 升级时只需替换 `/usr/lib/btspeaker/btspeaker` 与 `/usr/bin/btspeaker`、`/etc/init.d/btspeaker`，再 `restart`；`/etc/btspeaker` 不要删除。

## 7. 开机自启与服务管理

服务由 OpenWrt procd 守护（init 脚本 `USE_PROCD=1`、`START=95`），崩溃自动拉起（`respawn 3600 5 0`）。

```bash
/etc/init.d/btspeaker enable     # 开机自启（创建 /etc/rc.d/S95btspeaker）
/etc/init.d/btspeaker disable    # 取消开机自启
/etc/init.d/btspeaker start
/etc/init.d/btspeaker stop
/etc/init.d/btspeaker restart
/etc/init.d/btspeaker reload     # 配置变更后重启（config.json / rules.json 变化时 procd 也会自动重启）
/etc/init.d/btspeaker status
```

检查自启是否生效：

```bash
ls -l /etc/rc.d/ | grep btspeaker
```

## 8. 防火墙

默认规则（postinst / install.sh 自动创建，脚本名 `btspeaker`）：

```bash
# 查看
uci show firewall | grep -i btspeaker

# 手动添加（若自动创建失败）
uci set firewall.btspeaker=rule
uci set firewall.btspeaker.name='btspeaker'
uci set firewall.btspeaker.src='lan'
uci set firewall.btspeaker.dest_port='8722'
uci set firewall.btspeaker.proto='tcp'
uci set firewall.btspeaker.target='ACCEPT'
uci commit firewall && /etc/init.d/firewall reload

# 删除
uci delete firewall.btspeaker && uci commit firewall && /etc/init.d/firewall reload
```

说明：

- 该规则只放行 `lan` 区域到路由器的 TCP 8722，符合「只在内网使用」的设计；
- OpenWrt 默认 lan 区域 input 为 ACCEPT，部分固件其实不加规则也能访问，但显式放行更稳妥；
- **不要把 8722 暴露到 WAN**。如确需远程访问，请使用 VPN / 反向代理 + TLS，并配置 `security.api_token`。

## 9. 验证安装

安装完成后按顺序执行：

```bash
# 1) 服务状态
/etc/init.d/btspeaker status
pgrep -f 'btspeaker serve'

# 2) 端口监听
netstat -lnt | grep 8722        # 或 ss -lnt | grep 8722

# 3) HTTP 健康检查（无需 token）
wget -qO- http://127.0.0.1:8722/api/health
# 期望：{"ok": true, "data": {"status": "ok", "version": "1.0.0", ...}}

# 4) 运行状态
wget -qO- http://127.0.0.1:8722/api/status

# 5) 服务内置自检（蓝牙/播放器/TTS）
btspeaker diag

# 6) 蓝牙链路诊断（逐项中文建议）
sh /usr/share/btspeaker/scripts/bt-diag.sh

# 7) 数据目录
ls -l /etc/btspeaker
# 期望：config.json、rules.json、btspeaker.log、sounds/、tmp/

# 8) 示例规则（应为 5 条且全部停用）
btspeaker rules list
```

浏览器打开 `http://<路由器LAN IP>:8722/`，能看到「蓝音管家」总览页即安装成功。

## 10. 升级

### ipk 升级

```bash
# 备份数据目录（推荐，便于回滚）
tar -czf /root/btspeaker-data-backup-$(date +%Y%m%d).tar.gz /etc/btspeaker

opkg install /tmp/btspeaker_<新版本>_all.ipk
/etc/init.d/btspeaker restart
```

- 数据目录 `/etc/btspeaker` 不在升级删除范围内；`btspeaker seed` 只在配置/规则缺失时写入，**升级不会覆盖用户配置与规则**；
- 升级后如页面仍有旧缓存，浏览器强制刷新（Ctrl+F5）即可；
- 服务在升级过程中会短暂重启（prerm 停止、postinst 启动）。

### 源码方式升级

```bash
cd /tmp/istoreos-btspeaker   # 更新代码后
sh install.sh                # 重复执行即覆盖程序文件，保留数据
/etc/init.d/btspeaker restart
```

### 回滚

```bash
# 方式一：重新安装旧版本 ipk（opkg 支持降级时需要 --force-downgrade）
opkg install --force-downgrade /tmp/btspeaker_<旧版本>_all.ipk
/etc/init.d/btspeaker restart

# 方式二：恢复备份的配置（如果新版本改坏了配置）
tar -xzf /root/btspeaker-data-backup-YYYYMMDD.tar.gz -C /
/etc/init.d/btspeaker restart
```

## 11. 卸载

```bash
# 源码方式：保留配置与数据
sh uninstall.sh

# 源码方式：彻底删除（含 /etc/btspeaker 配置、规则、日志、用户声音）
sh uninstall.sh --purge

# opkg 方式
opkg remove btspeaker
rm -rf /etc/btspeaker          # 可选：彻底清理数据
```

卸载会停止并禁用服务、删除 `/usr/lib/btspeaker`、`/usr/bin/btspeaker`、`/etc/init.d/btspeaker`、`/usr/share/btspeaker` 并移除防火墙 `btspeaker` 规则。默认保留 `/etc/btspeaker`，重新安装后可直接沿用。

## 12. 安装常见问题

| 问题 | 原因与处理 |
| --- | --- |
| `opkg install` 报依赖缺失 | 先 `opkg update`；确认固件源里有 `bluealsa`、`python3-light` 等包；也可用 `sh install.sh --no-deps` 后手动补依赖 |
| 提示 `python3: not found` | 未装 `python3-light`：`opkg install python3-light` |
| 安装成功但打不开网页 | `/etc/init.d/btspeaker status` 与 `tail -n 100 /etc/btspeaker/btspeaker.log`；常见是端口被占用或 Python 依赖缺失 |
| `uci set firewall...` 报错 | 当前固件防火墙配置不标准，可跳过脚本自行在 LuCI 防火墙页面放行 8722/tcp |
| iStore 上传后找不到入口 | 蓝音管家是独立 Web 服务，不在 LuCI 菜单里；直接访问 `http://<路由器IP>:8722/` |
| `opkg remove` 后配置还在 | 这是有意设计（默认保留 `/etc/btspeaker`）；彻底清理执行 `rm -rf /etc/btspeaker` |
| 升级后规则/配置丢失 | 检查是否误删了 `/etc/btspeaker`；升级本身不会覆盖，可从备份恢复 |

蓝牙、声音、TTS 相关的问题请见 [FAQ.md](FAQ.md)。
