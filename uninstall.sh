#!/usr/bin/env bash
# 蓝音管家 BTSpeaker —— Linux 卸载脚本（systemd）
#
# 用法（以 root 执行）：
#   sudo bash uninstall.sh            停止并卸载程序，保留 /etc/btspeaker 配置与数据
#   sudo bash uninstall.sh --purge    同时删除 /etc/btspeaker（配置、规则、日志、用户声音）
#   sudo bash uninstall.sh --dry-run  仅预览，不改动系统
#   sudo bash uninstall.sh --yes      跳过交互确认（脚本化卸载）
#
# 卸载内容：
#   systemd 服务 btspeaker（停止 + 禁用开机自启）
#   /etc/systemd/system/btspeaker.service
#   /opt/btspeaker（程序、内置脚本）
#   /usr/local/bin/btspeaker（启动器）
#   /usr/share/btspeaker（内置声音库）
#   默认保留：/etc/btspeaker（config.json、rules.json、日志、用户声音）

set -u

APP_NAME="蓝音管家 BTSpeaker"
INSTALL_DIR="/opt/btspeaker"
LAUNCHER="/usr/local/bin/btspeaker"
UNIT_FILE="/etc/systemd/system/btspeaker.service"
DATA_DIR="/etc/btspeaker"
SHARE_DIR="/usr/share/btspeaker"

PURGE=0
DRY_RUN=0
ASSUME_YES=0

info() { printf '[信息] %s\n' "$*"; }
ok()   { printf '[完成] %s\n' "$*"; }
warn() { printf '[警告] %s\n' "$*" >&2; }
die()  { printf '[错误] %s\n' "$*" >&2; exit 1; }

usage() {
	cat <<'EOF'
蓝音管家 BTSpeaker —— Linux 卸载脚本

用法：
  sudo bash uninstall.sh [选项]

选项：
  --purge         同时删除 /etc/btspeaker（配置、规则、日志、用户声音）
  --dry-run, -n   只打印将要执行的操作，不修改系统
  --yes, -y       跳过交互确认
  --help, -h      显示本帮助

说明：
  默认卸载只停止/禁用 systemd 服务并移除程序文件，保留 /etc/btspeaker，
  重新安装后可直接沿用原有配置与规则。
EOF
}

run() {
	if [ "$DRY_RUN" = 1 ]; then
		printf '  [dry-run] %s\n' "$*"
		return 0
	fi
	"$@"
}

# --------------------------------------------------------------------------- #
while [ $# -gt 0 ]; do
	case "$1" in
		--purge)
			PURGE=1
			;;
		--dry-run|-n)
			DRY_RUN=1
			;;
		--yes|-y)
			ASSUME_YES=1
			;;
		--help|-h)
			usage
			exit 0
			;;
		*)
			printf '[错误] 未知参数：%s\n' "$1" >&2
			usage >&2
			exit 2
			;;
	esac
	shift
done

if [ "$(id -u 2>/dev/null || echo 1)" -ne 0 ]; then
	if [ "$DRY_RUN" = 1 ]; then
		warn "当前不是 root：--dry-run 仅预览，正式卸载需要 root"
	else
		die "请以 root 身份运行：sudo bash uninstall.sh"
	fi
fi

confirm() {
	[ "$ASSUME_YES" = 1 ] && return 0
	[ "$DRY_RUN" = 1 ] && return 0
	if [ ! -t 0 ]; then
		warn "当前不是交互终端，已取消卸载（如确认卸载请加 --yes）"
		return 1
	fi
	printf '确认卸载 %s ？[y/N] ' "$APP_NAME"
	read -r answer
	case "$answer" in
		y|Y|yes|YES)
			return 0
			;;
		*)
			return 1
			;;
	esac
}

printf '蓝音管家 BTSpeaker —— Linux 卸载程序\n'
if [ "$DRY_RUN" = 1 ]; then
	info "当前为 --dry-run 预览模式，只打印计划，不会改动系统"
fi
if [ "$PURGE" = 1 ]; then
	warn "已启用 --purge：将同时删除 $DATA_DIR（配置、规则、日志、用户声音）"
fi

confirm || { info "已取消卸载"; exit 0; }

# 1. 停止并禁用 systemd 服务
info "停止并禁用 systemd 服务 btspeaker ..."
if command -v systemctl >/dev/null 2>&1; then
	run systemctl stop btspeaker
	run systemctl disable btspeaker
else
	warn "未检测到 systemd，跳过服务停止/禁用"
fi
# 兜底：结束可能残留的前台进程
if [ "$DRY_RUN" = 0 ] && command -v pgrep >/dev/null 2>&1; then
	if pgrep -f 'btspeaker serve' >/dev/null 2>&1; then
		warn "检测到残留进程，正在结束 ..."
		pkill -f 'btspeaker serve' 2>/dev/null || true
		sleep 1
	fi
fi

# 2. 删除 systemd 单元
info "删除 systemd 单元：$UNIT_FILE ..."
run rm -f "$UNIT_FILE"
if command -v systemctl >/dev/null 2>&1; then
	run systemctl daemon-reload
	run systemctl reset-failed btspeaker
fi

# 3. 删除程序文件
info "删除程序文件 ..."
run rm -f "$LAUNCHER"
run rm -rf "$INSTALL_DIR"
run rm -rf "$SHARE_DIR"

# 4. 处理数据目录
if [ "$PURGE" = 1 ]; then
	info "删除数据目录 $DATA_DIR ..."
	run rm -rf "$DATA_DIR"
else
	if [ -d "$DATA_DIR" ]; then
		info "保留数据目录：$DATA_DIR"
		info "  配置文件：$DATA_DIR/config.json"
		info "  规则文件：$DATA_DIR/rules.json"
		info "  日志文件：$DATA_DIR/btspeaker.log"
		info "如需彻底删除，请重新执行：sudo bash uninstall.sh --purge"
	fi
fi

echo ""
if [ "$DRY_RUN" = 1 ]; then
	info "预览结束：以上为将要执行的操作，未改动系统"
else
	ok "$APP_NAME 已卸载"
	if [ "$PURGE" = 0 ]; then
		echo "  配置与数据保留在 $DATA_DIR，重新安装后可直接沿用。"
	fi
	echo "  重新安装：curl -fsSL https://raw.githubusercontent.com/wangmou1406/btspeaker/main/install.sh | sudo bash"
	echo ""
	echo "  残留检查（应无输出 / 无匹配）："
	echo "    pgrep -af 'btspeaker serve'"
	echo "    ls /opt/btspeaker /etc/systemd/system/btspeaker.service 2>&1"
	echo "    systemctl status btspeaker 2>&1 | head -3"
fi
exit 0
