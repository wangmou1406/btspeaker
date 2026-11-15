# 安装指南 —— 蓝音管家 BTSpeaker（Linux）

本文覆盖普通 Linux（systemd 发行版）的安装、依赖说明、手动步骤、systemd 管理、防火墙、验证、升级与回滚。

- 产品：蓝音管家 BTSpeaker（纯 Python 标准库 Web 服务）
- 目标系统：Debian / Ubuntu、Fedora、Arch Linux、openSUSE 等 systemd 发行版
- 默认端口：`8722`（TCP，监听 `0.0.0.0`）
- 安装路径：`/opt/btspeaker`；启动器 `/usr/local/bin/btspeaker`；服务名 `btspeaker`
- 数据目录：`/etc/btspeaker`
- 服务单元：`/etc/systemd/system/btspeaker.service`

---

## 1. 系统要求

| 项目 | 要求 | 检查命令 |
| --- | --- | --- |
| 系统 | 使用 systemd 的 Linux 发行版（x86_64 / ARM 均可） | `systemctl --version` |
| Python | 3.9 及以上（`python3`） | `python3 --version` |
| 蓝牙 | 板载或 USB 蓝牙适配器，BlueZ 已启用 | `bluetoothctl show`、`rfkill list bluetooth` |
| 音频 | BlueALSA + ALSA 工具（A2DP 接收侧桥接） | `pgrep bluealsa`、`aplay -L` |
| 磁盘 | 约 5MB 程序 + 声音库空间 | `df -h /opt /etc` |
| 权限 | root（安装、蓝牙控制、ALSA 放音） | `id -u` |

> Python 依赖全部来自系统 python3 标准库，**不需要 pip，不需要任何第三方 Python 包**。

## 2. 依赖包说明

一键安装脚本会自动安装；也可手动按发行版安装。

### 2.1 各发行版命令

| 发行版 | 必需依赖 | 可选增强 |
| --- | --- | --- |
| Debian / Ubuntu | `apt install python3 bluez bluez-alsa-utils alsa-utils mpg123` | `apt install espeak-ng ffmpeg` |
| Fedora | `dnf install python3 bluez bluez-alsa alsa-utils mpg123` | `dnf install espeak-ng ffmpeg` |
| Arch Linux | `pacman -S python bluez bluez-alsa alsa-utils mpg123` | `pacman -S espeak-ng ffmpeg` |
| openSUSE | `zypper install python3 bluez bluez-alsa alsa-utils mpg123` | `zypper install espeak-ng ffmpeg` |

> `bluez-alsa` 包名在发行版间不一致：Debian/Ubuntu 为 `bluez-alsa-utils`，Fedora/Arch/openSUSE 为
> `bluez-alsa`，个别旧仓库叫 `bluealsa`。一键安装脚本会按顺序自动尝试。

### 2.2 每个包的作用

| 包名 | 作用 | 缺失影响 |
| --- | --- | --- |
| `python3` | Python 3 解释器与标准库 | 服务无法启动 |
| `bluez` | BlueZ 守护进程 `bluetoothd`、`bluetoothctl` | 无法扫描/配对/连接 |
| `bluez-alsa`（`bluez-alsa-utils`） | BlueALSA：把蓝牙 A2DP 音频桥接为 ALSA PCM | 无法通过蓝牙音箱放音 |
| `alsa-utils` | `aplay` 等 ALSA 工具 | 无法放音 / 无法检测 bluealsa PCM |
| `mpg123` | MP3 解码播放（需带 alsa 输出模块） | mp3 无法播放；wav 仍可用 aplay |
| `espeak-ng`（可选） | 离线 TTS 兜底（音质机械） | 无外网 TTS 服务时 TTS 不可用 |
| `ffmpeg`（可选） | 非 mp3/wav 格式（ogg/flac 等）转码 | 这些格式无法播放 |

## 3. 方式一：一键安装（推荐）

```bash
curl -fsSL https://raw.githubusercontent.com/wangmou1406/btspeaker/main/install.sh | sudo bash
```

脚本流程：

1. **检测环境**：root、发行版（`/etc/os-release`）、包管理器（apt / dnf / pacman / zypper）；
2. **安装依赖**：python3、bluez、bluez-alsa（多包名自动尝试）、alsa-utils、mpg123；bluez-alsa 装不上不中断，会提示手动安装；
3. **部署程序**：`/opt/btspeaker/btspeaker`（含 `web/` 与 `seed_rules.json`）、`/opt/btspeaker/scripts`、启动器 `/usr/local/bin/btspeaker`；
4. **安装服务**：`/etc/systemd/system/btspeaker.service`；
5. **初始化数据**：`/etc/btspeaker`（`config.json` + 5 条默认停用的示例规则，幂等，不覆盖已有配置）；
6. **启动服务**：`systemctl daemon-reload` + `systemctl enable --now btspeaker`；
7. **自检**：访问 `http://127.0.0.1:8722/api/health`，打印访问地址与后续步骤。

可选参数（通过 `bash -s --` 传给远程脚本）：

```bash
# 跳过系统依赖安装（离线/依赖已自备）
curl -fsSL .../install.sh | sudo bash -s -- --no-deps

# 额外安装 espeak-ng 与 ffmpeg
curl -fsSL .../install.sh | sudo bash -s -- --optional

# 只预览、不改动系统（可不加 sudo）
curl -fsSL .../install.sh | bash -s -- --dry-run

# 组合
curl -fsSL .../install.sh | sudo bash -s -- --no-deps --optional
```

> 单行模式需要能访问 GitHub 以下载源码包。国内网络直连 `github.com` 常超时，脚本会自动依次
> 尝试 `ghfast.top` / `ghproxy.net` / `gh-proxy.com` / `hub.gitmirror.com` 四个加速镜像，
> 全部失败再尝试镜像 `git clone`。也可手动指定：
>
> ```bash
> # 自定义源码包地址（内网镜像 / 反代 / file://）
> curl -fsSL .../install.sh | sudo env BTSPEAKER_TARBALL_URL='https://<镜像>/btspeaker-main.tar.gz' bash
> # 或只给镜像前缀
> curl -fsSL .../install.sh | sudo env BTSPEAKER_MIRROR='https://ghfast.top/' bash
> # 脚本本身也拉不到时，用 jsDelivr 通道（可能滞后几分钟）
> curl -fsSL https://cdn.jsdelivr.net/gh/wangmou1406/btspeaker@main/install.sh | sudo bash
> ```
>
> 内网环境请用「方式二」，或设置 `BTSPEAKER_SRC_DIR=/path/to/source` 指向本地源码目录。

## 4. 方式二：克隆仓库安装

```bash
git clone https://github.com/wangmou1406/btspeaker.git
cd btspeaker
sudo bash install.sh                  # 安装 / 重复执行即升级覆盖
sudo bash install.sh --dry-run        # 先预览计划
sudo bash install.sh --no-deps        # 依赖已自备
sudo bash install.sh --optional       # 额外安装 espeak-ng / ffmpeg
```

`--dry-run` 会在本地打印完整计划：检测到的源码目录、要创建的目录、要拷贝的文件、要初始化的数据目录、要执行的 systemd 命令；不会修改系统，也无需 root。

## 5. 方式三：手动分步安装（不使用脚本）

```bash
# 1) 依赖（按发行版选一条）
sudo apt install python3 bluez bluez-alsa-utils alsa-utils mpg123
# sudo dnf install python3 bluez bluez-alsa alsa-utils mpg123
# sudo pacman -S python bluez bluez-alsa alsa-utils mpg123
# sudo zypper install python3 bluez bluez-alsa alsa-utils mpg123

# 2) 目录
sudo mkdir -p /opt/btspeaker /usr/share/btspeaker/sounds /etc/btspeaker/sounds /etc/btspeaker/tmp

# 3) 程序文件（在仓库根目录执行）
sudo cp -R btspeaker /opt/btspeaker/
sudo cp -R scripts /opt/btspeaker/scripts
sudo cp packaging/bin/btspeaker /usr/local/bin/btspeaker
sudo chmod 0755 /usr/local/bin/btspeaker
sudo cp packaging/systemd/btspeaker.service /etc/systemd/system/btspeaker.service
sudo cp assets/sounds/*.wav /usr/share/btspeaker/sounds/

# 4) 初始化配置与示例规则（幂等，已存在不覆盖）
sudo PYTHONPATH=/opt/btspeaker python3 -m btspeaker seed --data-dir /etc/btspeaker

# 5) 注册并启动
sudo systemctl daemon-reload
sudo systemctl enable --now btspeaker
sudo systemctl status btspeaker
```

> 升级时替换 `/opt/btspeaker/btspeaker`、`/opt/btspeaker/scripts`、`/usr/local/bin/btspeaker` 与 unit 文件，
> 再 `sudo systemctl daemon-reload && sudo systemctl restart btspeaker`；`/etc/btspeaker` 不要删除。

## 6. systemd 管理

服务单元关键配置（`/etc/systemd/system/btspeaker.service`）：

```ini
[Unit]
After=network-online.target bluetooth.target bluealsa.service
Wants=network-online.target bluealsa.service

[Service]
Type=simple
User=root
ExecStart=/usr/local/bin/btspeaker serve --data-dir /etc/btspeaker
Restart=always
RestartSec=3
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

> `bluealsa.service` 在部分发行版可能不存在或叫 `bluez-alsa.service`；unit 使用 `Wants=`
> 而非 `Requires=`，因此不会阻塞启动。

常用命令：

```bash
sudo systemctl daemon-reload              # 修改 unit 后重载
sudo systemctl enable --now btspeaker     # 开机自启 + 启动
sudo systemctl status btspeaker           # 状态
sudo systemctl restart btspeaker          # 重启（改配置/升级后）
sudo systemctl stop btspeaker             # 停止
sudo systemctl disable btspeaker          # 取消开机自启
journalctl -u btspeaker -f                # 实时日志
journalctl -u btspeaker --since today     # 今天的日志
```

检查开机自启是否生效：

```bash
systemctl is-enabled btspeaker
```

## 7. 防火墙

服务监听 TCP 8722。若开启防火墙，需要放行（**建议只放行局域网/可信网段**）。

```bash
# ufw（Debian/Ubuntu）
sudo ufw allow from 192.168.1.0/24 to any port 8722 proto tcp
sudo ufw status

# firewalld（Fedora/openSUSE）
sudo firewall-cmd --permanent --add-port=8722/tcp
sudo firewall-cmd --reload
# 或仅放行某个区域/网段：
sudo firewall-cmd --permanent --zone=home --add-port=8722/tcp

# nftables / iptables（示例：仅允许 192.168.1.0/24 访问）
sudo nft add rule inet filter input ip saddr 192.168.1.0/24 tcp dport 8722 accept
sudo iptables -A INPUT -p tcp -s 192.168.1.0/24 --dport 8722 -j ACCEPT
```

说明：

- 本机 `127.0.0.1:8722` 不受防火墙影响，可用于排障；
- **不要把 8722 暴露到公网**；远程访问请用 VPN 或反向代理 + TLS，并配置 `security.api_token`；
- 如果修改了 `server.port`，记得同步修改防火墙规则与 systemd 无冲突（端口从配置读取，无需改 unit）。

## 8. 验证安装

按顺序执行：

```bash
# 1) 服务与自启
systemctl is-active btspeaker
systemctl is-enabled btspeaker

# 2) 端口监听
ss -lntp | grep 8722          # 或 netstat -lntp | grep 8722

# 3) HTTP 健康检查（无需 token）
curl -s http://127.0.0.1:8722/api/health
# 期望：{"ok": true, "data": {"status": "ok", "version": "1.0.0", ...}}

# 4) 运行状态
curl -s http://127.0.0.1:8722/api/status

# 5) 服务内置自检（蓝牙/播放器/TTS）
btspeaker diag

# 6) 蓝牙链路诊断（systemd / bluealsa / ALSA 逐项中文建议）
sudo sh /opt/btspeaker/scripts/bt-diag.sh

# 7) 数据目录与示例规则
ls -l /etc/btspeaker
btspeaker rules list          # 应为 5 条且全部停用
```

浏览器打开 `http://<主机IP>:8722/`，能看到「蓝音管家」总览页即安装成功。

## 9. 升级与回滚

### 升级

```bash
# 1) 备份数据目录（推荐）
sudo tar -czf ~/btspeaker-backup-$(date +%Y%m%d).tar.gz /etc/btspeaker

# 2) 重新执行安装（幂等，只覆盖程序，不动配置）
curl -fsSL https://raw.githubusercontent.com/wangmou1406/btspeaker/main/install.sh | sudo bash

# 或者仓库内：
cd btspeaker && git pull && sudo bash install.sh
```

- 升级不会覆盖 `/etc/btspeaker`：`btspeaker seed` 仅在配置/规则缺失时写入；
- 升级会自动 `daemon-reload` 并重启服务；页面如显示旧版本，强制刷新浏览器（Ctrl+F5）。

### 回滚

```bash
# 1) 回退代码到上一个版本（仓库方式）
cd btspeaker
git log --oneline -5
git checkout <上一个版本的 commit 或 tag>
sudo bash install.sh

# 2) 恢复数据备份（如果新版本改坏了配置）
sudo tar -xzf ~/btspeaker-backup-YYYYMMDD.tar.gz -C /
sudo systemctl restart btspeaker
```

## 10. 卸载

```bash
# 仓库目录内执行
sudo bash uninstall.sh            # 停止并删除程序，保留 /etc/btspeaker
sudo bash uninstall.sh --purge    # 同时删除 /etc/btspeaker（配置、规则、日志、用户声音）
sudo bash uninstall.sh --dry-run  # 先预览
```

卸载会停止并禁用 systemd 服务、删除 unit 文件、`/opt/btspeaker`、`/usr/local/bin/btspeaker`、`/usr/share/btspeaker`；默认保留下 `/etc/btspeaker`，重新安装后可直接沿用。

## 11. 安装常见问题

| 问题 | 原因与处理 |
| --- | --- |
| `python3: command not found` | 手动安装依赖：Debian `apt install python3`；Fedora `dnf install python3` |
| `bluez-alsa` 装不上 | 各发行版包名不同：Debian `bluez-alsa-utils`、Fedora/Arch/openSUSE `bluez-alsa`；装不上不影响服务启动，但播放会提示「未检测到 BlueALSA PCM」 |
| 单行安装卡在下载 | 无法访问 GitHub；改用 `git clone` 后在仓库执行 `sudo bash install.sh`，或 `BTSPEAKER_SRC_DIR=<本地目录> sudo -E bash install.sh` |
| `systemctl enable` 报 unit 不存在 | 先确认 `/etc/systemd/system/btspeaker.service` 已拷贝，再 `sudo systemctl daemon-reload` |
| 服务启动即失败 | `journalctl -u btspeaker -n 80 --no-pager` 查看 Python 异常；常见是缺 python3 标准库模块或程序目录不完整 |
| 安装成功但浏览器打不开 | 检查 `ss -lntp | grep 8722`、防火墙规则、是否访问的是 `http://` 而非 `https://` |
| 升级后规则/配置丢失 | 检查是否误删 `/etc/btspeaker`；升级本身不会覆盖，可从备份恢复 |
| 容器/WSL 没有 systemd | 服务无法注册为 systemd 服务；可手动前台运行：`sudo /usr/local/bin/btspeaker serve --data-dir /etc/btspeaker` |

蓝牙、声音、TTS 相关问题见 [FAQ.md](FAQ.md)。
