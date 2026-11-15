# OpenWrt / iStoreOS 归档（历史，不推荐）

> **这是历史归档，不推荐使用。** 项目当前主线是普通 Linux 应用（systemd + 一键安装），
> 见仓库根目录 `install.sh`、`packaging/systemd/btspeaker.service` 和 `README.md`。
> OpenWrt/iStoreOS 路线的代码自 1.0.0 起不再维护，仅保留在此供参考或怀旧使用。

## 归档内容

| 文件 | 原位置 | 作用 |
| --- | --- | --- |
| `install.sh` | 仓库根 `install.sh` | OpenWrt 一键安装（opkg 依赖、procd、uci 防火墙） |
| `uninstall.sh` | 仓库根 `uninstall.sh` | OpenWrt 卸载（停用 procd 服务、清防火墙规则） |
| `init.d-btspeaker` | `scripts/init.d-btspeaker` | procd 启动脚本（安装为 `/etc/init.d/btspeaker`） |
| `btspeaker-launcher.sh` | `scripts/btspeaker-launcher.sh` | 启动器（安装为 `/usr/bin/btspeaker`） |
| `build_ipk.py` | `scripts/build_ipk.py` | 纯标准库 .ipk 打包工具（现代 OpenWrt tar.gz 外层容器） |
| `bt-diag.sh` | `scripts/bt-diag.sh` | OpenWrt 版蓝牙链路诊断 |
| `install-deps.sh` | `scripts/install-deps.sh` | OpenWrt 版依赖安装（opkg） |
| `INSTALL.OpenWrt.md` | `docs/INSTALL.md` | 旧版安装文档 |
| `README.OpenWrt.md` | `README.md` | 旧版 README |

## 还能怎么用

归档脚本与程序包解耦：`build_ipk.py` 已适配归档目录，可直接在**仓库根目录**构建历史 ipk：

```bash
# 构建 OpenWrt ipk（产物在仓库根 dist/，安装逻辑使用本目录下的 procd 脚本）
python packaging/openwrt/build_ipk.py
```

安装产物结构为：程序包（`btspeaker/`，与 Linux 版共用同一份 Python 代码）+ OpenWrt 专用
启动脚本 + 内置声音库。旧版 `install.sh` 也做了归档兼容，可在 OpenWrt 目标机上执行：

```bash
sh packaging/openwrt/install.sh --dry-run   # 预览
sh packaging/openwrt/install.sh             # 真机安装（仅 OpenWrt/iStoreOS）
```

注意：

- 归档脚本假定 OpenWrt 环境（`opkg`、`procd`、`/etc/init.d`、`uci`），在普通 Linux 上无法运行；
- 相关文档中的端口（8722）、数据目录（`/etc/btspeaker`）与当前 Linux 版一致，但安装路径不同
  （OpenWrt: `/usr/lib/btspeaker`；Linux: `/opt/btspeaker`）；
- 如只是想在普通 Linux 上使用，请忽略本目录，直接看根目录 `README.md`。
