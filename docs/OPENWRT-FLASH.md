# 官方 OpenWrt 刷机 + 蓝牙音频（bluez-alsa）安装指南

> 适用：iStoreOS 的 x86 软路由（你的机器：Intel 蓝牙 `8087:0a2a`、内核 6.6.144）。
>
> **为什么必须换固件**：iStoreOS 24.10 的 x86 内核配置里 `# CONFIG_BT is not set`，
> 蓝牙子系统整个没编译进内核（没有 `/sys/class/bluetooth`，任何 kmod/Docker 都无效）。
> 官方 OpenWrt 24.10 x86_64 有蓝牙内核模块，配合本仓库自动编译的 bluez-alsa 即可实现 A2DP 放音。
>
> ⚠️ 刷机会**清空原系统**（iStoreOS 及其应用/配置）。刷前备份 `/etc/config`、Docker 数据、
> 重要文件到 U 盘或另一台电脑。确认你能接受再继续。

---

## 1. 准备固件

官方 24.10.4 x86_64 固件（EFI 启动，绝大多数 x86 软路由/小主机适用）：

```text
下载页：https://downloads.openwrt.org/releases/24.10.4/targets/x86/64/
镜像：  openwrt-24.10.4-x86-64-generic-ext4-combined-efi.img.gz
校验：  sha256sums
```

国内可用镜像（把 `downloads.openwrt.org` 换成 `mirrors.ustc.edu.cn/openwrt` 或 `mirrors.tuna.tsinghua.edu.cn/openwrt`）：

```text
https://mirrors.ustc.edu.cn/openwrt/releases/24.10.4/targets/x86/64/openwrt-24.10.4-x86-64-generic-ext4-combined-efi.img.gz
```

> 纯 BIOS（非 UEFI）老机器请用不带 `-efi` 的 `generic-ext4-combined.img.gz`。
> 写盘前先校验 sha256。

## 2. 写盘

**Linux 下（推荐，可以在另一台机器上把盘接到 USB 转接器）：**

```bash
gzip -dc openwrt-24.10.4-x86-64-generic-ext4-combined-efi.img.gz | sudo dd of=/dev/sdX bs=4M status=progress conv=fsync
sync
```

（`/dev/sdX` 是整个磁盘设备，不是分区；写错会毁数据，务必确认。）

**Windows：** Rufus / balenaEtcher，选择镜像写入 U 盘或目标磁盘。

## 3. 首次启动与联网

1. 显示器+键盘，或直接把网线接到设备的 LAN 口；
2. 默认地址 `http://192.168.1.1`，用户名 `root`，密码为空 → 先设置密码；
3. 网络 → 接口：WAN 设为 DHCP 或 PPPoE（按你的宽带），LAN 保持 `192.168.1.1`（与主路由不冲突即可）；
4. 用网线连到主路由，或确认设备能 `ping` 通外网。

## 4. 换国内软件源（可选但推荐）

```sh
cp /etc/opkg/distfeeds.conf /etc/opkg/distfeeds.conf.bak
cat > /etc/opkg/distfeeds.conf <<'EOF'
src/gz openwrt_core https://mirrors.ustc.edu.cn/openwrt/releases/24.10.4/targets/x86/64/packages
src/gz openwrt_base https://mirrors.ustc.edu.cn/openwrt/releases/24.10.4/packages/x86_64/base
src/gz openwrt_luci https://mirrors.ustc.edu.cn/openwrt/releases/24.10.4/packages/x86_64/luci
src/gz openwrt_packages https://mirrors.ustc.edu.cn/openwrt/releases/24.10.4/packages/x86_64/packages
src/gz openwrt_routing https://mirrors.ustc.edu.cn/openwrt/releases/24.10.4/packages/x86_64/routing
src/gz openwrt_telephony https://mirrors.ustc.edu.cn/openwrt/releases/24.10.4/packages/x86_64/telephony
EOF
opkg update
```

> **内核模块源**：官方 24.10 的 kmod 独立在 `targets/x86/64/kmods/<内核版本哈希>/`。
> 官方固件默认的 `distfeeds.conf` 已包含对应 `kmods` 源；如果你改源后 `opkg install kmod-bluetooth`
> 报找不到，请把下面这行加回 `distfeeds.conf`（内核版本以 `uname -r` 为准）：
>
> ```text
> src/gz openwrt_kmods https://downloads.openwrt.org/releases/24.10.4/targets/x86/64/kmods/6.6.110-1-484466e2719a743506c36b4bb2103582
> ```

## 5. 安装蓝牙与 Python 依赖

```sh
opkg update
opkg install kmod-bluetooth kmod-usb-core bluez-daemon bluez-utils bluez-utils-extra \
             alsa-utils mpg123 \
             python3-light python3-urllib python3-openssl python3-email python3-logging python3-uuid
```

验证蓝牙硬件：

```sh
ls /sys/class/bluetooth           # 应出现 hci0（没有就 modprobe bluetooth 再试）
bluetoothctl list                 # 应列出 Controller
/etc/init.d/bluetooth enable
/etc/init.d/bluetooth start
```

## 6. 安装预编译 ipk（bluez-alsa + 蓝音管家）

本仓库的 GitHub Actions 会自动编译并提交到 `dist-openwrt/`（也可在 Release `openwrt-latest` 下载）。
`raw.githubusercontent.com` 在国内通常可达：

```sh
cd /tmp
BASE="https://raw.githubusercontent.com/wangmou1406/btspeaker/main/dist-openwrt"
wget -O bluez-alsa.ipk        "$BASE/bluez-alsa_4.3.1-1_x86_64.ipk"
wget -O bluez-alsa-utils.ipk  "$BASE/bluez-alsa-utils_4.3.1-1_x86_64.ipk"
wget -O btspeaker.ipk         "$BASE/btspeaker_1.0.0_all.ipk"
wget -O SHA256SUMS            "$BASE/SHA256SUMS"
sha256sum -c SHA256SUMS 2>/dev/null || true

opkg install bluez-alsa.ipk bluez-alsa-utils.ipk btspeaker.ipk
```

> 如果 `raw.githubusercontent.com` 也慢，可加镜像前缀：
> `https://ghfast.top/https://raw.githubusercontent.com/wangmou1406/btspeaker/main/dist-openwrt/...`
>
> 具体文件名以 `dist-openwrt/` 目录实际为准（`bluez-alsa` 版本号固定 4.3.1-1，`btspeaker` 为 `all` 架构）。
> ipk 若暂未生成（Actions 还没跑完），可在 GitHub Actions 页面手动触发 `Build OpenWrt packages`。

## 7. 启动服务

```sh
/etc/init.d/bluealsa enable
/etc/init.d/bluealsa start
/etc/init.d/btspeaker enable
/etc/init.d/btspeaker start

ls -l /usr/bin/bluealsa /usr/bin/bluetoothctl /usr/bin/btspeaker
aplay -L | grep -i bluealsa          # 应看到 bluealsa PCM
btspeaker status                     # 或 curl -s http://127.0.0.1:8722/api/health
```

浏览器打开 `http://<路由器IP>:8722/` → 「音箱」页扫描 → 配对 → 连接 → 播放内置提示音。

## 8. 常见问题

| 现象 | 检查 |
| --- | --- |
| `opkg install kmod-bluetooth` 找不到包 | kmods 源没配好；见第 4 节末尾，确认内核版本与源路径一致 |
| `kmod-bluetooth` 装完仍无 `hci0` | `dmesg | grep -i blue`；`modprobe bluetooth btusb`；`ls /lib/modules/$(uname -r)/kernel/drivers/bluetooth` |
| `aplay -L` 没有 bluealsa | `bluealsa` 进程是否在：`pgrep bluealsa`；`/etc/init.d/bluealsa restart`；查 `/etc/alsa/conf.d/20-bluealsa.conf` |
| 连上音箱但没声音 | 看 `/api/diag`；确认播放器后端是 bluealsa；`btspeaker logs`；必要时 `btspeaker devices --scan` 重新连接 |
| bluealsa 启动失败 | `logread | grep -i bluealsa`；确认 `/etc/dbus-1/system.d/bluealsa.conf` 已安装、dbus 正常 |
| 网页打不开 | `/etc/init.d/btspeaker status`、`netstat -lntp | grep 8722`、防火墙放行 8722 |

一键诊断（本应用自带）：

```sh
sh /usr/share/btspeaker/scripts/bt-diag.sh
```

## 9. 回滚

刷机前原 iStoreOS 若有备份，可按官方方式重新刷回；本应用卸载：

```sh
opkg remove btspeaker bluez-alsa-utils bluez-alsa
```
