#!/bin/sh
# 蓝音管家 BTSpeaker —— 运行时依赖安装脚本（幂等，可重复执行）
#
# 用途：ipk 的 control 不再声明硬依赖（避免 iStore 本地安装沙箱解析依赖失败），
#       由 postinst 在真机上尽力安装；若当时失败（如刚开机网络未就绪），
#       可 ssh 上来手动执行本脚本重试。
#
# 用法：sh /usr/share/btspeaker/scripts/install-deps.sh
#       sh install-deps.sh --optional   # 额外安装 espeak / ffmpeg（离线 TTS 与多格式解码）

set -u

REQUIRED="python3-light python3-urllib python3-openssl python3-email python3-logging python3-uuid bluez-daemon bluez-utils alsa-utils mpg123 kmod-bluetooth"
OPTIONAL="bluealsa espeak ffmpeg"

if [ "$(id -u)" != "0" ]; then
	echo "请用 root 执行：sh $0" >&2
	exit 1
fi

if ! command -v opkg >/dev/null 2>&1; then
	echo "找不到 opkg（不是 OpenWrt/iStoreOS 环境？）" >&2
	exit 1
fi

echo "== 蓝音管家依赖安装 =="
echo "[1/3] 更新软件包索引（失败不致命）..."
opkg update >/dev/null 2>&1 || echo "  [警告] opkg update 失败，将用现有索引继续"

echo "[2/3] 检查并安装必需依赖..."
missing=""
for dep in $REQUIRED; do
	if opkg list-installed 2>/dev/null | grep -q "^$dep "; then
		echo "  [已装] $dep"
	else
		missing="$missing $dep"
	fi
done

failed=""
if [ -n "$missing" ]; then
	echo "  [安装] 缺失：$missing"
	for dep in $missing; do
		if opkg install "$dep" >/dev/null 2>&1; then
			echo "  [完成] $dep"
		else
			echo "  [失败] $dep"
			failed="$failed $dep"
		fi
	done
else
	echo "  必需依赖已全部安装。"
fi

echo "[3/3] 可选组件（不影响核心功能）..."
for dep in $OPTIONAL; do
	if opkg list-installed 2>/dev/null | grep -q "^$dep "; then
		echo "  [已装] $dep"
	elif [ "${1:-}" = "--optional" ]; then
		opkg install "$dep" >/dev/null 2>&1 && echo "  [完成] $dep" || echo "  [跳过] $dep 安装失败"
	else
		echo "  [未装] $dep（可用 --optional 安装）"
	fi
done

if [ -n "$failed" ]; then
	echo ""
	echo "部分必需依赖安装失败：$failed"
	echo "常见原因："
	echo "  1) 软件源不可用（iStoreOS 默认 mirrors.istoreos.com / downloads.istoreos.com 域名已失效），"
	echo "     请改用可用镜像：官方 https://downloads.openwrt.org 或国内 https://mirrors.ustc.edu.cn/openwrt/；"
	echo "  2) 固件版本与软件源版本不匹配（看 /etc/openwrt_release 的 DISTRIB_RELEASE）；"
	echo "  3) 网络/DNS 不通。"
	echo ""
	echo "修复软件源后执行：opkg update && sh $0"
	exit 1
fi

echo ""
echo "提示：bluealsa 用于蓝牙 A2DP 放音，OpenWrt 官方源没有这个包；"
echo "      若未安装（本脚本第 3 步会显示），请确认固件是否已内置，或改装第三方预编译包。"
echo "依赖就绪。重启服务以生效：/etc/init.d/btspeaker restart"
echo "随后可运行诊断：sh /usr/share/btspeaker/scripts/bt-diag.sh"
exit 0
