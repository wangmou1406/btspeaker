#!/bin/sh
# 蓝音管家 BTSpeaker —— 蓝牙链路诊断脚本（iStoreOS / OpenWrt x86_64）
#
# 用法：
#   sh /usr/lib/btspeaker/scripts/bt-diag.sh
#   sh bt-diag.sh --port 8722
#
# 逐项检查蓝牙/音频链路并给出中文修复建议；退出码 0=全部通过，1=存在失败项。

set -u

PORT=8722
API_TOKEN=""
DATA_DIR=/etc/btspeaker
CONFIG="$DATA_DIR/config.json"

while [ $# -gt 0 ]; do
	case "$1" in
		--port)
			shift
			PORT="${1:-8722}"
			;;
		--token)
			shift
			API_TOKEN="${1:-}"
			;;
		--data-dir)
			shift
			DATA_DIR="${1:-/etc/btspeaker}"
			CONFIG="$DATA_DIR/config.json"
			;;
		-h|--help)
			sed -n '2,12p' "$0"
			exit 0
			;;
		*)
			echo "未知参数：$1（可用 --port / --token / --data-dir / --help）"
			exit 2
			;;
	esac
	shift
done

FAILED=0

section() {
	printf '\n== %s ==\n' "$1"
}

ok() {
	printf '  [OK]   %s\n' "$*"
}

warn() {
	printf '  [WARN] %s\n' "$*"
}

fail() {
	printf '  [FAIL] %s\n' "$*"
	FAILED=$((FAILED + 1))
}

hint() {
	printf '         修复建议：%s\n' "$*"
}

have() {
	command -v "$1" >/dev/null 2>&1
}

# --------------------------------------------------------------------------- #
section "系统信息"
if [ -f /etc/openwrt_release ]; then
	# shellcheck disable=SC1091
	. /etc/openwrt_release
	ok "系统：${DISTRIB_DESCRIPTION:-OpenWrt}（${DISTRIB_ARCH:-unknown}）"
else
	warn "未检测到 /etc/openwrt_release：当前系统可能不是 OpenWrt/iStoreOS"
fi
ok "内核：$(uname -srm 2>/dev/null)"
ok "当前用户：$(id -un 2>/dev/null)（uid=$(id -u 2>/dev/null)）"

# --------------------------------------------------------------------------- #
section "BTSpeaker 服务"
if [ -x /etc/init.d/btspeaker ]; then
	STATUS="$(/etc/init.d/btspeaker status 2>&1 | head -n 2)"
	if printf '%s' "$STATUS" | grep -qi 'running'; then
		ok "procd 服务运行中：$STATUS"
	else
		warn "procd 服务状态：${STATUS:-未知}"
	fi
else
	fail "未找到 /etc/init.d/btspeaker（蓝音管家可能未安装）"
	hint "重新安装：sh install.sh，或上传 btspeaker_<版本>_x86_64.ipk 安装"
fi

if pgrep -f 'btspeaker serve' >/dev/null 2>&1; then
	ok "进程存在：$(pgrep -f 'btspeaker serve' | tr '\n' ' ')"
else
	fail "未发现 btspeaker serve 进程"
	hint "启动服务：/etc/init.d/btspeaker start；失败时看日志 /etc/btspeaker/btspeaker.log"
fi

LISTEN=""
if have netstat; then
	LISTEN="$(netstat -lnt 2>/dev/null | grep -E "[:.]${PORT}[[:space:]]" || true)"
elif have ss; then
	LISTEN="$(ss -lnt 2>/dev/null | grep -E "[:.]${PORT}[[:space:]]" || true)"
fi
if [ -n "$LISTEN" ]; then
	ok "端口 $PORT 正在监听"
else
	fail "端口 $PORT 未监听"
	hint "服务未启动或端口被改：检查 $CONFIG 中 server.port，并重启服务"
fi

# --------------------------------------------------------------------------- #
section "蓝牙适配器"
if have bluetoothctl; then
	ok "bluetoothctl：$(command -v bluetoothctl)"
	if timeout 6 bluetoothctl show >/tmp/btspeaker-diag-btctl.txt 2>&1; then
		ADAPTER_LINE="$(grep -E 'Controller [0-9A-Fa-f:]{17}' /tmp/btspeaker-diag-btctl.txt | head -n 1)"
		if [ -n "$ADAPTER_LINE" ]; then
			ok "适配器：$ADAPTER_LINE"
			if grep -qi 'Powered: yes' /tmp/btspeaker-diag-btctl.txt; then
				ok "适配器已上电（Powered: yes）"
			else
				fail "适配器未上电"
				hint "执行：bluetoothctl power on（或 hciconfig hci0 up）"
			fi
			if grep -qi 'Discovering: yes' /tmp/btspeaker-diag-btctl.txt; then
				warn "适配器正在扫描（可能影响连接稳定性）"
			fi
		else
			fail "bluetoothctl show 未返回 Controller，适配器可能未识别"
			hint "检查 dmesg | grep -i bluetooth、lsusb；确认 kmod-bluetooth / kmod-btusb 已安装"
		fi
	else
		fail "bluetoothctl show 执行失败"
		hint "确认 bluez-daemon 已安装并启动：/etc/init.d/bluetooth start"
	fi
else
	fail "未安装 bluetoothctl"
	hint "opkg update && opkg install bluez-daemon bluez-utils kmod-bluetooth kmod-btusb"
fi

if have hciconfig; then
	HCI="$(hciconfig 2>/dev/null | grep -E '^hci[0-9]+' | head -n 5)"
	if [ -n "$HCI" ]; then
		ok "hciconfig：$(printf '%s' "$HCI" | head -n 1)"
	else
		warn "hciconfig 未发现适配器（内核模块可能未加载）"
	fi
else
	warn "未安装 hciconfig（bluez-utils 提供，可选）"
fi

# --------------------------------------------------------------------------- #
section "配对与连接"
if have bluetoothctl; then
	DEVS="$(timeout 8 bluetoothctl devices 2>/dev/null || true)"
	if [ -n "$DEVS" ]; then
		printf '%s\n' "$DEVS" | sed 's/^/         /'
		CONNECTED_LINE="$(printf '%s\n' "$DEVS" | head -n 1 | awk '{print $2}')"
		if [ -n "$CONNECTED_LINE" ]; then
			INFO="$(timeout 8 bluetoothctl info "$CONNECTED_LINE" 2>/dev/null || true)"
			if printf '%s' "$INFO" | grep -qi 'Connected: yes'; then
				ok "音箱已连接：$CONNECTED_LINE"
			else
				warn "音箱未连接：$CONNECTED_LINE"
				hint "连接：bluetoothctl connect $CONNECTED_LINE（在 Web 界面 #/devices 页也可操作）"
			fi
			if printf '%s' "$INFO" | grep -qi 'Paired: yes'; then
				ok "已配对：$CONNECTED_LINE"
			else
				warn "未配对：$CONNECTED_LINE"
				hint "配对：bluetoothctl pair $CONNECTED_LINE && bluetoothctl trust $CONNECTED_LINE"
			fi
		fi
	else
		fail "没有已配对/已发现的设备"
		hint "先让音箱进入配对模式，再在 Web 界面 #/devices 页点击扫描并配对"
	fi
else
	warn "跳过配对检查：bluetoothctl 不可用"
fi

if [ -f "$CONFIG" ]; then
	DEFAULT_DEV="$(sed -n 's/.*"default_device"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$CONFIG" | head -n 1)"
	if [ -n "$DEFAULT_DEV" ]; then
		ok "默认音箱（配置）：$DEFAULT_DEV"
	else
		warn "未设置默认音箱（config.json 中 bluetooth.default_device 为空）"
		hint "在 Web 界面 #/devices 页把常用音箱设为默认，或调用 POST /api/bluetooth/default"
	fi
else
	warn "配置文件不存在：$CONFIG"
fi

# --------------------------------------------------------------------------- #
section "BlueALSA（蓝牙音频桥接）"
if pgrep -f bluealsa >/dev/null 2>&1; then
	ok "bluealsa 进程运行中：$(pgrep -f bluealsa | tr '\n' ' ')"
else
	fail "bluealsa 未运行"
	hint "启动：/etc/init.d/bluealsa start（或手工 /usr/bin/bluealsa -p a2dp-sink &）"
	hint "确认已安装：opkg install bluealsa"
fi

if [ -x /etc/init.d/bluealsa ]; then
	ok "bluealsa 服务脚本：/etc/init.d/bluealsa"
else
	warn "未找到 /etc/init.d/bluealsa（部分固件需要手工启动 bluealsa）"
fi

if have aplay; then
	if aplay -L 2>/dev/null | grep -qi bluealsa; then
		ok "ALSA 已识别 bluealsa PCM"
	else
		fail "aplay -L 中没有 bluealsa PCM"
		hint "确认 bluealsa 正在运行且已连接音箱；必要时重启 bluealsa 后重试"
	fi
else
	fail "未安装 aplay"
	hint "opkg install alsa-utils"
fi

# --------------------------------------------------------------------------- #
section "播放器工具"
if have mpg123; then
	ok "mpg123：$(command -v mpg123)"
	if mpg123 --list-modules 2>/dev/null | grep -qi alsa; then
		ok "mpg123 支持 alsa 输出模块"
	elif mpg123 --longhelp 2>&1 | grep -qi 'alsa'; then
		ok "mpg123 帮助信息中包含 alsa 输出（推测支持）"
	else
		warn "mpg123 未显示 alsa 输出模块"
		hint "若播放 mp3 无声：opkg install --force-reinstall mpg123，或改用 wav + aplay"
	fi
else
	fail "未安装 mpg123"
	hint "opkg install mpg123"
fi

if have ffmpeg; then
	ok "ffmpeg：$(command -v ffmpeg)（可用于 ogg/其它格式转码）"
else
	warn "未安装 ffmpeg（可选，仅非 mp3/wav 格式需要）"
	hint "opkg install ffmpeg"
fi

if have espeak; then
	ok "espeak：$(command -v espeak)（离线 TTS 兜底可用）"
else
	warn "未安装 espeak（可选；推荐配置在线 TTS 服务，音质更好）"
	hint "opkg install espeak，并在 Web 设置页把 TTS 后端设为 espeak"
fi

# --------------------------------------------------------------------------- #
section "声音库"
if [ -d /usr/share/btspeaker/sounds ]; then
	COUNT="$(ls -1 /usr/share/btspeaker/sounds 2>/dev/null | wc -l | tr -d ' ')"
	ok "安装内置声音库：/usr/share/btspeaker/sounds（${COUNT} 个文件）"
else
	warn "内置声音库目录不存在：/usr/share/btspeaker/sounds"
fi
if [ -d "$DATA_DIR/sounds" ]; then
	COUNT="$(ls -1 "$DATA_DIR/sounds" 2>/dev/null | wc -l | tr -d ' ')"
	ok "用户声音目录：$DATA_DIR/sounds（${COUNT} 个文件）"
else
	warn "用户声音目录不存在：$DATA_DIR/sounds"
fi

# --------------------------------------------------------------------------- #
section "HTTP 接口"
if have wget; then
	HEALTH="$(wget -q -O - "http://127.0.0.1:${PORT}/api/health" 2>/dev/null || true)"
elif have curl; then
	HEALTH="$(curl -fsS "http://127.0.0.1:${PORT}/api/health" 2>/dev/null || true)"
else
	HEALTH=""
fi
if printf '%s' "$HEALTH" | grep -q '"status"'; then
	ok "健康检查通过：$HEALTH"
else
	fail "无法访问 http://127.0.0.1:${PORT}/api/health"
	hint "服务未启动或端口不对；查看日志与端口监听"
fi
if [ -n "$API_TOKEN" ]; then
	ok "已提供 API Token（仅用于诊断提示，脚本不会打印 Token）"
fi

# --------------------------------------------------------------------------- #
section "日志（最近 30 行）"
if [ -f "$DATA_DIR/btspeaker.log" ]; then
	ok "日志文件：$DATA_DIR/btspeaker.log"
	tail -n 30 "$DATA_DIR/btspeaker.log" 2>/dev/null | sed 's/^/         /'
else
	warn "日志文件不存在：$DATA_DIR/btspeaker.log"
	hint "启动服务后日志会写入该文件；也可用 logread | grep -i btspeaker 查看"
fi
if have logread; then
	SYSLOG="$(logread -e btspeaker 2>/dev/null | tail -n 10)"
	if [ -n "$SYSLOG" ]; then
		printf '%s\n' "$SYSLOG" | sed 's/^/         /'
	fi
fi

# --------------------------------------------------------------------------- #
section "诊断结论"
if [ "$FAILED" -eq 0 ]; then
	echo "  未发现失败项，蓝牙播放链路看起来正常。"
	echo "  如果仍然没有声音：确认音箱音量、在 Web 界面点一次「播放提示音」测试。"
	exit 0
else
	echo "  共发现 $FAILED 个失败项，请按上面的修复建议逐条处理。"
	echo "  仍无法解决时，把本脚本输出和 $DATA_DIR/btspeaker.log 尾部一起提供。"
	exit 1
fi
