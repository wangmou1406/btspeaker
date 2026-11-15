#!/bin/sh
# 蓝音管家 BTSpeaker —— 蓝牙链路诊断脚本（通用 Linux / systemd）
#
# 用法：
#   sh scripts/bt-diag.sh
#   sh /opt/btspeaker/scripts/bt-diag.sh --port 8722 --token <API_TOKEN>
#
# 逐项检查 systemd 服务、端口、蓝牙适配器、BlueALSA、播放工具、声音库与日志，
# 每项给出中文修复建议。退出码 0=未发现失败项，1=存在失败项。

set -u

PORT=8722
API_TOKEN=""
DATA_DIR="/etc/btspeaker"
INSTALL_DIR="/opt/btspeaker"
LAUNCHER="/usr/local/bin/btspeaker"

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
			;;
		--install-dir)
			shift
			INSTALL_DIR="${1:-/opt/btspeaker}"
			;;
		-h|--help)
			sed -n '2,10p' "$0"
			exit 0
			;;
		*)
			echo "未知参数：$1（可用 --port / --token / --data-dir / --install-dir / --help）"
			exit 2
			;;
	esac
	shift
done

FAILED=0

section() { printf '\n== %s ==\n' "$1"; }
ok()   { printf '  [OK]   %s\n' "$*"; }
warn() { printf '  [WARN] %s\n' "$*"; }
fail() { printf '  [FAIL] %s\n' "$*"; FAILED=$((FAILED + 1)); }
hint() { printf '         修复建议：%s\n' "$*"; }
have() { command -v "$1" >/dev/null 2>&1; }

# --------------------------------------------------------------------------- #
section "系统信息"
if [ -r /etc/os-release ]; then
	# shellcheck disable=SC1091
	. /etc/os-release
	ok "系统：${PRETTY_NAME:-$ID}"
else
	warn "未找到 /etc/os-release，无法识别发行版"
fi
ok "内核：$(uname -srm 2>/dev/null)"
ok "当前用户：$(id -un 2>/dev/null)（uid=$(id -u 2>/dev/null)）"
if [ "$(id -u 2>/dev/null || echo 1)" -ne 0 ]; then
	warn "当前不是 root：蓝牙控制、服务状态查看可能需要 sudo"
fi
if ! have systemctl; then
	warn "未检测到 systemd（本服务按 systemd 单元设计，容器/WSL 中可能需要手动前台运行）"
fi

# --------------------------------------------------------------------------- #
section "程序安装"
if [ -f "$INSTALL_DIR/btspeaker/__init__.py" ]; then
	ok "程序目录：$INSTALL_DIR/btspeaker"
else
	fail "程序目录不存在或缺少 __init__.py：$INSTALL_DIR/btspeaker"
	hint "重新安装：sudo bash install.sh"
fi
if [ -x "$LAUNCHER" ]; then
	ok "启动器：$LAUNCHER"
else
	fail "启动器不存在：$LAUNCHER"
	hint "重新安装：sudo bash install.sh"
fi
if [ -f /etc/systemd/system/btspeaker.service ]; then
	ok "systemd 单元：/etc/systemd/system/btspeaker.service"
else
	fail "systemd 单元不存在：/etc/systemd/system/btspeaker.service"
	hint "重新安装：sudo bash install.sh（或手动 cp packaging/systemd/btspeaker.service /etc/systemd/system/）"
fi
if have python3; then
	ok "python3：$(python3 --version 2>&1)"
	if PYTHONPATH="$INSTALL_DIR" python3 -c "import btspeaker; print(btspeaker.__version__)" >/tmp/btspeaker-diag-import.txt 2>/dev/null; then
		ok "Python 包导入正常（btspeaker $(cat /tmp/btspeaker-diag-import.txt)）"
	else
		fail "无法 import btspeaker"
		hint "检查 $INSTALL_DIR/btspeaker 是否完整；缺标准库模块时按发行版安装 python3"
	fi
else
	fail "未安装 python3"
	hint "Debian/Ubuntu: apt install python3；Fedora: dnf install python3；Arch: pacman -S python"
fi

# --------------------------------------------------------------------------- #
section "systemd 服务状态"
if have systemctl; then
	if systemctl is-active --quiet btspeaker 2>/dev/null; then
		ok "服务运行中（active）；开机自启：$(systemctl is-enabled btspeaker 2>/dev/null || echo '未启用')"
	else
		fail "服务未运行（systemctl is-active btspeaker 非 active）"
		hint "启动：sudo systemctl start btspeaker；失败原因看：journalctl -u btspeaker -n 50 --no-pager"
	fi
	if systemctl is-enabled --quiet btspeaker 2>/dev/null; then
		ok "已设置开机自启"
	else
		warn "未设置开机自启"
		hint "sudo systemctl enable btspeaker"
	fi
	STATUS_HEAD="$(systemctl status btspeaker --no-pager -n 0 2>/dev/null | head -n 4)"
	[ -n "$STATUS_HEAD" ] && printf '%s\n' "$STATUS_HEAD" | sed 's/^/         /'
else
	warn "跳过 systemd 检查（无 systemctl）"
fi
if pgrep -f 'btspeaker serve' >/dev/null 2>&1; then
	ok "进程存在：$(pgrep -f 'btspeaker serve' | tr '\n' ' ')"
else
	fail "未发现 btspeaker serve 进程"
	hint "查看日志：journalctl -u btspeaker -n 80 --no-pager；数据日志：$DATA_DIR/btspeaker.log"
fi

# --------------------------------------------------------------------------- #
section "端口与 HTTP 接口"
LISTEN=""
if have ss; then
	LISTEN="$(ss -lntp 2>/dev/null | grep -E "[:.]${PORT}[[:space:]]" || true)"
elif have netstat; then
	LISTEN="$(netstat -lntp 2>/dev/null | grep -E "[:.]${PORT}[[:space:]]" || true)"
fi
if [ -n "$LISTEN" ]; then
	ok "端口 $PORT 正在监听"
else
	fail "端口 $PORT 未监听"
	hint "服务未启动或端口被修改：检查 $DATA_DIR/config.json 的 server.port，然后 sudo systemctl restart btspeaker"
fi
HEALTH=""
if have curl; then
	if [ -n "$API_TOKEN" ]; then
		HEALTH="$(curl -fsS -H "X-Api-Token: $API_TOKEN" "http://127.0.0.1:${PORT}/api/health" 2>/dev/null || true)"
	else
		HEALTH="$(curl -fsS "http://127.0.0.1:${PORT}/api/health" 2>/dev/null || true)"
	fi
elif have wget; then
	HEALTH="$(wget -q -O - "http://127.0.0.1:${PORT}/api/health" 2>/dev/null || true)"
fi
if printf '%s' "$HEALTH" | grep -q '"status"'; then
	ok "健康检查通过：$HEALTH"
else
	fail "无法访问 http://127.0.0.1:${PORT}/api/health"
	hint "服务未启动、端口不对或被防火墙拦截；先看 journalctl -u btspeaker -n 80 --no-pager"
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
				hint "sudo bluetoothctl power on（或 sudo hciconfig hci0 up）"
			fi
		else
			fail "bluetoothctl show 未返回 Controller，适配器可能未被识别"
			hint "检查：lsusb | grep -i bluetooth、dmesg | tail -30；确认 bluez 已安装且 bluetooth 服务已启动"
		fi
	else
		fail "bluetoothctl show 执行失败"
		hint "sudo systemctl start bluetooth；再 sudo systemctl status bluetooth"
	fi
else
	fail "未安装 bluetoothctl（bluez 未安装）"
	hint "Debian/Ubuntu: apt install bluez；Fedora: dnf install bluez；Arch: pacman -S bluez"
fi
if have systemctl; then
	BT_STATE="$(systemctl is-active bluetooth 2>/dev/null || true)"
	if [ "$BT_STATE" = "active" ]; then
		ok "bluetooth 服务运行中"
	else
		fail "bluetooth 服务未运行（状态：${BT_STATE:-unknown}）"
		hint "sudo systemctl enable --now bluetooth"
	fi
fi
if have rfkill; then
	RF="$(rfkill list bluetooth 2>/dev/null | head -n 6)"
	if [ -n "$RF" ]; then
		printf '%s\n' "$RF" | sed 's/^/         /'
		if printf '%s' "$RF" | grep -qi 'Soft blocked: yes'; then
			warn "蓝牙被软件禁用（Soft blocked: yes）"
			hint "sudo rfkill unblock bluetooth"
		fi
		if printf '%s' "$RF" | grep -qi 'Hard blocked: yes'; then
			fail "蓝牙被硬件开关/BIOS 禁用（Hard blocked: yes）"
			hint "检查笔记本蓝牙开关、BIOS 设置或 USB 适配器"
		fi
	fi
fi

# --------------------------------------------------------------------------- #
section "配对与连接"
if have bluetoothctl; then
	DEVS="$(timeout 8 bluetoothctl devices 2>/dev/null || true)"
	if [ -n "$DEVS" ]; then
		printf '%s\n' "$DEVS" | sed 's/^/         /'
		FIRST_ADDR="$(printf '%s\n' "$DEVS" | head -n 1 | awk '{print $2}')"
		if [ -n "$FIRST_ADDR" ]; then
			INFO="$(timeout 8 bluetoothctl info "$FIRST_ADDR" 2>/dev/null || true)"
			if printf '%s' "$INFO" | grep -qi 'Connected: yes'; then
				ok "音箱已连接：$FIRST_ADDR"
			else
				warn "首个音箱未连接：$FIRST_ADDR"
				hint "sudo bluetoothctl connect $FIRST_ADDR（或在 Web 界面「音箱」页操作）"
			fi
			if printf '%s' "$INFO" | grep -qi 'Paired: yes'; then
				ok "已配对：$FIRST_ADDR"
			else
				warn "未配对：$FIRST_ADDR"
				hint "sudo bluetoothctl pair $FIRST_ADDR && sudo bluetoothctl trust $FIRST_ADDR"
			fi
		fi
	else
		fail "没有已配对/已发现的蓝牙设备"
		hint "让音箱进入配对模式，然后在 Web 界面「音箱」页点扫描并配对"
	fi
else
	warn "跳过配对检查（bluetoothctl 不可用）"
fi
if [ -f "$DATA_DIR/config.json" ] && have python3; then
	DEFAULT_DEV="$(python3 - "$DATA_DIR/config.json" <<'PY' 2>/dev/null || true
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as f:
        cfg = json.load(f)
    print(((cfg.get("bluetooth") or {}).get("default_device") or ""))
except Exception:
    pass
PY
)"
	if [ -n "$DEFAULT_DEV" ]; then
		ok "默认音箱（配置）：$DEFAULT_DEV"
	else
		warn "未设置默认音箱（bluetooth.default_device 为空）"
		hint "在 Web 界面「音箱」页设为默认，或：btspeaker default <MAC>"
	fi
fi

# --------------------------------------------------------------------------- #
section "BlueALSA（蓝牙音频桥接）"
if pgrep -f bluealsa >/dev/null 2>&1; then
	ok "bluealsa 进程运行中：$(pgrep -f bluealsa | tr '\n' ' ')"
else
	fail "bluealsa 未运行"
	hint "启动：sudo systemctl enable --now bluealsa（部分发行版服务名为 bluez-alsa.service）"
	hint "Debian/Ubuntu 包名 bluez-alsa-utils；Fedora/Arch/openSUSE 包名 bluez-alsa"
fi
if have systemctl; then
	BA_STATE="$(systemctl is-active bluealsa 2>/dev/null || systemctl is-active bluez-alsa 2>/dev/null || true)"
	if [ "$BA_STATE" = "active" ]; then
		ok "bluealsa systemd 服务运行中"
	elif pgrep -f bluealsa >/dev/null 2>&1; then
		warn "bluealsa 进程存在但不是由 systemd 管理（可接受）"
	else
		warn "bluealsa systemd 服务未运行（状态：${BA_STATE:-不存在该服务}）"
		hint "查找服务：systemctl list-unit-files | grep -i -E 'bluealsa|bluez-alsa'"
	fi
fi
if have aplay; then
	if aplay -L 2>/dev/null | grep -qi bluealsa; then
		ok "ALSA 已识别 bluealsa PCM"
	else
		fail "aplay -L 中没有 bluealsa PCM"
		hint "① 确认 bluealsa 正在运行；② 确认蓝牙音箱已连接（BlueALSA 仅在 A2DP 设备连接后暴露 PCM）"
	fi
else
	fail "未安装 aplay（alsa-utils）"
	hint "Debian/Ubuntu: apt install alsa-utils；Fedora: dnf install alsa-utils；Arch: pacman -S alsa-utils"
fi

# --------------------------------------------------------------------------- #
section "播放器工具"
if have mpg123; then
	ok "mpg123：$(command -v mpg123)"
	if mpg123 --list-modules 2>/dev/null | grep -qi alsa; then
		ok "mpg123 支持 alsa 输出模块"
	elif mpg123 --longhelp 2>&1 | grep -qi 'alsa'; then
		ok "mpg123 帮助信息包含 alsa（推测支持）"
	else
		warn "mpg123 未显示 alsa 输出模块"
		hint "重新安装：apt install --reinstall mpg123（Fedora: dnf reinstall mpg123）；或改用 wav + aplay 播放"
	fi
else
	fail "未安装 mpg123"
	hint "Debian/Ubuntu: apt install mpg123；Fedora: dnf install mpg123；Arch: pacman -S mpg123"
fi
if have ffmpeg; then
	ok "ffmpeg 已安装（非 mp3/wav 格式转码可用）"
else
	warn "未安装 ffmpeg（可选，仅 ogg/flac 等格式需要）"
	hint "sudo sh $INSTALL_DIR/scripts/install-deps.sh --optional"
fi
if have espeak-ng || have espeak; then
	ok "espeak-ng/espeak 已安装（离线 TTS 兜底）"
else
	warn "未安装 espeak-ng（可选；推荐配置在线 TTS，音质更好）"
	hint "sudo sh $INSTALL_DIR/scripts/install-deps.sh --optional"
fi

# --------------------------------------------------------------------------- #
section "数据目录与声音库"
if [ -d "$DATA_DIR" ]; then
	ok "数据目录：$DATA_DIR"
	if [ -w "$DATA_DIR" ]; then
		ok "数据目录可写"
	else
		fail "数据目录不可写（服务以 root 运行，普通用户查看时属正常）"
		hint "sudo chown root:root $DATA_DIR && sudo chmod 755 $DATA_DIR"
	fi
	for f in config.json rules.json; do
		if [ -f "$DATA_DIR/$f" ]; then
			ok "存在 $DATA_DIR/$f"
		else
			warn "缺少 $DATA_DIR/$f"
			hint "初始化：sudo PYTHONPATH=$INSTALL_DIR python3 -m btspeaker seed --data-dir $DATA_DIR"
		fi
	done
else
	fail "数据目录不存在：$DATA_DIR"
	hint "重新安装：sudo bash install.sh（会自动创建并初始化）"
fi
if [ -d /usr/share/btspeaker/sounds ]; then
	COUNT="$(ls -1 /usr/share/btspeaker/sounds 2>/dev/null | wc -l | tr -d ' ')"
	ok "内置声音库：/usr/share/btspeaker/sounds（${COUNT} 个文件）"
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
section "日志（最近 30 行）"
if have journalctl; then
	JLOG="$(journalctl -u btspeaker -n 30 --no-pager 2>/dev/null | tail -n 30)"
	if [ -n "$JLOG" ]; then
		ok "journalctl -u btspeaker："
		printf '%s\n' "$JLOG" | sed 's/^/         /'
	fi
fi
if [ -f "$DATA_DIR/btspeaker.log" ]; then
	ok "服务日志：$DATA_DIR/btspeaker.log（尾部 20 行）"
	tail -n 20 "$DATA_DIR/btspeaker.log" 2>/dev/null | sed 's/^/         /'
else
	warn "服务日志不存在：$DATA_DIR/btspeaker.log"
fi

# --------------------------------------------------------------------------- #
section "诊断结论"
if [ "$FAILED" -eq 0 ]; then
	echo "  未发现失败项，服务与蓝牙播放链路看起来正常。"
	echo "  如仍无声：确认音箱音量/输入源，在 Web 界面点一次「播放提示音」测试。"
	exit 0
else
	echo "  共发现 $FAILED 个失败项，请按上面的修复建议逐条处理。"
	echo "  仍未解决时，附上本脚本输出与：journalctl -u btspeaker -n 100 --no-pager"
	exit 1
fi
