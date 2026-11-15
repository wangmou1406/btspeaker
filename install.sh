#!/bin/sh
# 蓝音管家 BTSpeaker —— iStoreOS / OpenWrt x86_64 一键安装脚本
#
# 用法（在项目根目录、以 root 执行）：
#   sh install.sh                 正常安装 / 重复执行即升级覆盖
#   sh install.sh --dry-run       仅预览将执行的操作，不改动系统
#   sh install.sh --no-deps       跳过 opkg 依赖安装（依赖已自备时使用）
#   sh install.sh --help          显示帮助
#
# 安装内容：
#   /usr/lib/btspeaker/btspeaker    Python 包（含 web/ 静态界面）
#   /usr/bin/btspeaker              启动器（PYTHONPATH=/usr/lib/btspeaker）
#   /etc/init.d/btspeaker           procd 启动脚本（开机自启）
#   /usr/share/btspeaker/sounds     内置声音库
#   /usr/share/btspeaker/scripts/bt-diag.sh 蓝牙链路诊断脚本
#   /etc/btspeaker/                 数据目录（由 btspeaker seed 初始化）
#   防火墙放行 TCP 8722（lan 区域）

set -u

APP_NAME="蓝音管家 BTSpeaker"
PKG_NAME="btspeaker"
VERSION="unknown"

PREFIX_LIB="/usr/lib/btspeaker"
PREFIX_BIN="/usr/bin/btspeaker"
SHARE_DIR="/usr/share/btspeaker"
SOUND_DIR="$SHARE_DIR/sounds"
SCRIPTS_DIR="$SHARE_DIR/scripts"
DATA_DIR="/etc/btspeaker"
INIT_SCRIPT="/etc/init.d/btspeaker"
PORT="8722"

# 运行依赖（python3 标准库子包 + BlueZ/BlueALSA + ALSA 播放工具 + 蓝牙内核模块）
REQUIRED_PKGS="python3-light python3-urllib python3-openssl python3-email python3-logging python3-uuid bluez-daemon bluez-utils bluealsa alsa-utils mpg123 kmod-bluetooth kmod-btusb"

DRY_RUN=0
NO_DEPS=0

# --------------------------------------------------------------------------- #
info() { printf '[信息] %s\n' "$*"; }
ok()   { printf '[完成] %s\n' "$*"; }
warn() { printf '[警告] %s\n' "$*" >&2; }
err()  { printf '[错误] %s\n' "$*" >&2; }
die()  { err "$*"; exit 1; }

usage() {
	cat <<'EOF'
蓝音管家 BTSpeaker 一键安装脚本

用法：
  sh install.sh [选项]

选项：
  --dry-run, -n   只预览将要执行的操作，不修改系统（可在非 root 下运行）
  --no-deps       跳过 opkg 依赖安装（适用于依赖已自行安装 / 离线环境）
  --help, -h      显示本帮助

示例：
  sudo sh install.sh
  sudo sh install.sh --no-deps
  sh install.sh --dry-run
EOF
}

# 执行命令；dry-run 时只打印
run() {
	if [ "$DRY_RUN" = 1 ]; then
		printf '  [dry-run] %s\n' "$*"
		return 0
	fi
	"$@"
}

# 判断服务进程是否在运行
service_running() {
	if command -v pgrep >/dev/null 2>&1; then
		pgrep -f 'btspeaker serve' >/dev/null 2>&1
	else
		ps 2>/dev/null | grep -v grep | grep -q 'btspeaker serve'
	fi
}

# 获取局域网访问地址
lan_ip() {
	_lan_ip=""
	if command -v uci >/dev/null 2>&1; then
		_lan_ip="$(uci -q get network.lan.ipaddr 2>/dev/null || true)"
	fi
	if [ -z "$_lan_ip" ] && command -v ip >/dev/null 2>&1; then
		_lan_ip="$(ip -4 addr show br-lan 2>/dev/null | awk '/inet /{print $2}' | cut -d/ -f1 | head -n 1)"
	fi
	if [ -z "$_lan_ip" ]; then
		_lan_ip="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
	fi
	[ -n "$_lan_ip" ] || _lan_ip="<路由器LAN IP>"
	printf '%s' "$_lan_ip"
}

# --------------------------------------------------------------------------- #
# 参数解析
while [ $# -gt 0 ]; do
	case "$1" in
		--dry-run|-n)
			DRY_RUN=1
			;;
		--no-deps)
			NO_DEPS=1
			;;
		--help|-h)
			usage
			exit 0
			;;
		*)
			err "未知参数：$1"
			usage >&2
			exit 2
			;;
	esac
	shift
done

# 定位源码目录（install.sh 所在目录）
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"

if [ -f "$SCRIPT_DIR/VERSION" ]; then
	VERSION="$(tr -d ' \t\r\n' < "$SCRIPT_DIR/VERSION")"
fi

# --------------------------------------------------------------------------- #
# 前置检查
if [ ! -d "$SCRIPT_DIR/$PKG_NAME" ]; then
	die "在 $SCRIPT_DIR 下找不到 $PKG_NAME 程序目录，请在项目根目录运行本脚本"
fi
if [ ! -f "$SCRIPT_DIR/scripts/init.d-btspeaker" ]; then
	die "缺少 scripts/init.d-btspeaker，安装包不完整"
fi
if [ ! -f "$SCRIPT_DIR/scripts/btspeaker-launcher.sh" ]; then
	die "缺少 scripts/btspeaker-launcher.sh，安装包不完整"
fi

if [ "$(id -u 2>/dev/null || echo 1)" -ne 0 ]; then
	if [ "$DRY_RUN" = 1 ]; then
		warn "当前不是 root，--dry-run 仅预览，不会修改系统"
	else
		die "请以 root 身份运行：sudo sh install.sh"
	fi
fi

info "蓝音管家 BTSpeaker 安装程序 v$VERSION"
if [ "$DRY_RUN" = 1 ]; then
	info "当前为 --dry-run 预览模式，不会改动系统"
fi

if [ -f /etc/openwrt_release ]; then
	# shellcheck disable=SC1091
	. /etc/openwrt_release
	info "检测到系统：${DISTRIB_DESCRIPTION:-OpenWrt}（${DISTRIB_ARCH:-unknown}）"
else
	warn "未检测到 /etc/openwrt_release：当前系统可能不是 iStoreOS/OpenWrt"
	warn "脚本会继续，但 opkg / uci / procd 相关步骤可能失败"
fi

# 依赖安装
install_dependencies() {
	if [ "$NO_DEPS" = 1 ]; then
		info "已指定 --no-deps，跳过依赖安装"
		info "如运行缺依赖，请手动执行：opkg install $REQUIRED_PKGS"
		return 0
	fi
	if ! command -v opkg >/dev/null 2>&1; then
		warn "未找到 opkg，跳过依赖安装（非 OpenWrt 系统可忽略）"
		warn "OpenWrt 上请手动安装：opkg install $REQUIRED_PKGS"
		return 0
	fi
	if [ "$DRY_RUN" = 1 ]; then
		printf '  [dry-run] opkg update\n'
		printf '  [dry-run] opkg install %s\n' "$REQUIRED_PKGS"
		return 0
	fi

	info "更新 opkg 软件源索引 ..."
	opkg update || warn "opkg update 失败，继续尝试安装（可能使用旧索引）"

	info "安装依赖包：$REQUIRED_PKGS"
	if opkg install $REQUIRED_PKGS; then
		ok "依赖安装完成"
	else
		warn "批量安装失败，改为逐个安装并跳过固件源中不存在的包 ..."
		for pkg in $REQUIRED_PKGS; do
			if opkg install "$pkg" >/tmp/btspeaker-opkg.log 2>&1; then
				ok "已安装 $pkg"
			else
				warn "未能安装 $pkg（可能当前固件源没有该包），可稍后手动安装"
			fi
		done
	fi
}

check_optional_tools() {
	info "检查可选增强工具 ..."
	if command -v espeak >/dev/null 2>&1; then
		ok "espeak 已安装（离线 TTS 兜底可用）"
	else
		info "可选：未安装 espeak（离线 TTS 兜底，opkg install espeak）"
	fi
	if command -v ffmpeg >/dev/null 2>&1; then
		ok "ffmpeg 已安装（非 mp3/wav 格式转码可用）"
	else
		info "可选：未安装 ffmpeg（仅非 mp3/wav 格式需要，opkg install ffmpeg）"
	fi
}

create_dirs() {
	info "创建目录 ..."
	for dir in "$PREFIX_LIB" "$SHARE_DIR" "$SOUND_DIR" "$SCRIPTS_DIR" \
		"$DATA_DIR" "$DATA_DIR/sounds" "$DATA_DIR/tmp"; do
		run mkdir -p "$dir"
	done
}

install_package() {
	info "安装 Python 包到 $PREFIX_LIB/$PKG_NAME ..."
	if [ "$DRY_RUN" = 1 ]; then
		printf '  [dry-run] rm -rf %s\n' "$PREFIX_LIB/$PKG_NAME"
	else
		rm -rf "$PREFIX_LIB/$PKG_NAME"
	fi
	run cp -R "$SCRIPT_DIR/$PKG_NAME" "$PREFIX_LIB/"
	if [ "$DRY_RUN" = 0 ]; then
		# 清理开发机可能带入的字节码缓存
		find "$PREFIX_LIB/$PKG_NAME" -type d -name '__pycache__' -prune -exec rm -rf {} \; 2>/dev/null || true
	fi
}

install_launcher() {
	info "安装启动器：$PREFIX_BIN ..."
	run cp "$SCRIPT_DIR/scripts/btspeaker-launcher.sh" "$PREFIX_BIN"
	run chmod 0755 "$PREFIX_BIN"
}

install_init_script() {
	info "安装 procd 启动脚本：$INIT_SCRIPT ..."
	run cp "$SCRIPT_DIR/scripts/init.d-btspeaker" "$INIT_SCRIPT"
	run chmod 0755 "$INIT_SCRIPT"
}

install_diag_script() {
	if [ -f "$SCRIPT_DIR/scripts/bt-diag.sh" ]; then
		info "安装蓝牙诊断脚本：$SCRIPTS_DIR/bt-diag.sh ..."
		run cp "$SCRIPT_DIR/scripts/bt-diag.sh" "$SCRIPTS_DIR/bt-diag.sh"
		run chmod 0755 "$SCRIPTS_DIR/bt-diag.sh"
	fi
	if [ -f "$SCRIPT_DIR/scripts/install-deps.sh" ]; then
		info "安装依赖安装脚本：$SCRIPTS_DIR/install-deps.sh ..."
		run cp "$SCRIPT_DIR/scripts/install-deps.sh" "$SCRIPTS_DIR/install-deps.sh"
		run chmod 0755 "$SCRIPTS_DIR/install-deps.sh"
	fi
}

install_sounds() {
	if [ -d "$SCRIPT_DIR/assets/sounds" ] && [ -n "$(ls -A "$SCRIPT_DIR/assets/sounds" 2>/dev/null)" ]; then
		info "安装内置声音库到 $SOUND_DIR ..."
		run cp -R "$SCRIPT_DIR/assets/sounds/." "$SOUND_DIR/"
	else
		warn "未找到内置声音（assets/sounds 为空），声音库暂时为空"
		warn "可自行放入 mp3/wav 文件：$SOUND_DIR 或 $DATA_DIR/sounds"
	fi
}

seed_data_dir() {
	info "初始化数据目录 $DATA_DIR（config.json + 5 条示例规则，规则默认停用）..."
	if [ "$DRY_RUN" = 1 ]; then
		printf '  [dry-run] %s seed --data-dir %s\n' "$PREFIX_BIN" "$DATA_DIR"
		return 0
	fi
	if [ ! -x "$PREFIX_BIN" ]; then
		warn "启动器 $PREFIX_BIN 不存在，跳过数据目录初始化（服务首次启动时会自动初始化）"
		return 0
	fi
	# btspeaker seed 幂等：配置/规则已存在时保持不动，重复安装不会覆盖用户数据
	if "$PREFIX_BIN" seed --data-dir "$DATA_DIR"; then
		ok "数据目录初始化完成"
	else
		warn "btspeaker seed 执行失败；服务首次启动时会自动初始化数据目录"
	fi
}

add_firewall_rule() {
	if ! command -v uci >/dev/null 2>&1; then
		warn "未找到 uci，跳过防火墙配置；请手动放行 TCP $PORT（lan 区域）"
		return 0
	fi
	if [ "$DRY_RUN" = 1 ]; then
		printf '  [dry-run] uci set firewall.btspeaker=rule （src=lan, proto=tcp, dest_port=%s, target=ACCEPT）\n' "$PORT"
		printf '  [dry-run] uci commit firewall && /etc/init.d/firewall reload\n'
		return 0
	fi
	if ! uci -q get firewall.btspeaker >/dev/null 2>&1; then
		uci set firewall.btspeaker=rule 2>/dev/null || true
	fi
	uci set firewall.btspeaker.name='btspeaker' 2>/dev/null || true
	uci set firewall.btspeaker.src='lan' 2>/dev/null || true
	uci set firewall.btspeaker.dest_port="$PORT" 2>/dev/null || true
	uci set firewall.btspeaker.proto='tcp' 2>/dev/null || true
	uci set firewall.btspeaker.target='ACCEPT' 2>/dev/null || true
	if uci commit firewall 2>/dev/null; then
		/etc/init.d/firewall reload >/dev/null 2>&1 || true
		ok "防火墙已放行 TCP $PORT（lan 区域）"
	else
		warn "写入防火墙规则失败，请手动放行 TCP $PORT"
	fi
}

enable_and_start() {
	info "设置开机自启并启动服务 ..."
	if [ "$DRY_RUN" = 1 ]; then
		printf '  [dry-run] %s enable\n' "$INIT_SCRIPT"
		printf '  [dry-run] %s start\n' "$INIT_SCRIPT"
		return 0
	fi
	if [ ! -x "$INIT_SCRIPT" ]; then
		warn "未找到可执行的 $INIT_SCRIPT，跳过服务启动"
		return 0
	fi
	"$INIT_SCRIPT" enable >/dev/null 2>&1 || warn "设置开机自启失败（可手动 /etc/init.d/btspeaker enable）"
	"$INIT_SCRIPT" start >/dev/null 2>&1 || true
	sleep 2
	if service_running; then
		ok "服务已启动"
	else
		warn "服务可能未启动成功，请执行 /etc/init.d/btspeaker status 并查看 $DATA_DIR/btspeaker.log"
	fi
}

check_health() {
	[ "$DRY_RUN" = 1 ] && return 0
	_health=""
	if command -v wget >/dev/null 2>&1; then
		_health="$(wget -q -O - "http://127.0.0.1:$PORT/api/health" 2>/dev/null || true)"
	elif command -v curl >/dev/null 2>&1; then
		_health="$(curl -fsS "http://127.0.0.1:$PORT/api/health" 2>/dev/null || true)"
	fi
	case "$_health" in
		*'"status"'*)
			ok "HTTP 健康检查通过：$_health"
			;;
		*)
			warn "HTTP 健康检查暂未通过（服务可能仍在启动）"
			warn "稍后可手动验证：wget -qO- http://127.0.0.1:$PORT/api/health"
			;;
	esac
}

print_summary() {
	_lan="$(lan_ip)"
	cat <<EOF

============================================================
 $APP_NAME 安装完成（v$VERSION）
============================================================
 访问地址 : http://$_lan:$PORT/
 数据目录 : $DATA_DIR
 程序目录 : $PREFIX_LIB
 日志文件 : $DATA_DIR/btspeaker.log
 诊断脚本 : sh $SCRIPTS_DIR/bt-diag.sh
 服务管理 : $INIT_SCRIPT {start|stop|restart|reload|status|enable|disable}

 后续步骤：
 1. 浏览器打开访问地址，进入「音箱」页扫描并配对蓝牙音箱；
 2. 在「设置」页配置 TTS（在线 HTTP 接口音质最好；也可安装 espeak 离线兜底）；
 3. 5 条示例规则默认全部停用，确认音箱工作正常后，到「自动化」页逐条启用；
 4. 若局域网不可信，请在「设置 → 安全」中设置 api_token（命令执行默认关闭）。

 卸载：
   sh uninstall.sh           保留 $DATA_DIR 配置与数据
   sh uninstall.sh --purge   同时删除配置与数据
============================================================
EOF
}

# --------------------------------------------------------------------------- #
info "开始安装（目标：iStoreOS / OpenWrt x86_64）"

install_dependencies
check_optional_tools
create_dirs
install_package
install_launcher
install_init_script
install_diag_script
install_sounds
seed_data_dir
add_firewall_rule
enable_and_start
check_health
print_summary

if [ "$DRY_RUN" = 1 ]; then
	info "预览结束：以上为将要执行的操作，未改动系统"
fi
exit 0
