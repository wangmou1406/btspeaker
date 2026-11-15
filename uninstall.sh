#!/bin/sh
# 蓝音管家 BTSpeaker —— 卸载脚本（iStoreOS / OpenWrt x86_64）
#
# 用法（以 root 执行）：
#   sh uninstall.sh            停止并卸载程序，保留 /etc/btspeaker 配置与数据
#   sh uninstall.sh --purge    同时删除 /etc/btspeaker（配置、规则、日志、声音库）
#   sh uninstall.sh --dry-run  仅预览，不改动系统
#   sh uninstall.sh --yes      跳过交互确认（脚本化卸载）
#
# 卸载内容：
#   /etc/init.d/btspeaker、/usr/bin/btspeaker、/usr/lib/btspeaker
#   /usr/share/btspeaker（内置声音库与诊断脚本）
#   防火墙 btspeaker 规则（TCP 8722）
#   默认保留：/etc/btspeaker（config.json、rules.json、日志、用户声音）

set -u

APP_NAME="蓝音管家 BTSpeaker"
PREFIX_LIB="/usr/lib/btspeaker"
PREFIX_BIN="/usr/bin/btspeaker"
SHARE_DIR="/usr/share/btspeaker"
DATA_DIR="/etc/btspeaker"
INIT_SCRIPT="/etc/init.d/btspeaker"

PURGE=0
DRY_RUN=0
ASSUME_YES=0

info() { printf '[信息] %s\n' "$*"; }
ok()   { printf '[完成] %s\n' "$*"; }
warn() { printf '[警告] %s\n' "$*" >&2; }
die()  { printf '[错误] %s\n' "$*" >&2; exit 1; }

usage() {
	cat <<'EOF'
蓝音管家 BTSpeaker 卸载脚本

用法：
  sh uninstall.sh [选项]

选项：
  --purge         同时删除 /etc/btspeaker（配置、规则、日志、用户声音）
  --dry-run, -n   只预览将要执行的操作，不修改系统
  --yes, -y       跳过交互确认
  --help, -h      显示本帮助

说明：
  默认卸载只移除程序文件并停止服务，保留 /etc/btspeaker 数据目录，
  以便重新安装后继续使用原有配置与规则。
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
		warn "当前不是 root，--dry-run 仅预览，不会修改系统"
	else
		die "请以 root 身份运行：sudo sh uninstall.sh"
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
	read -r _answer
	case "$_answer" in
		y|Y|yes|YES)
			return 0
			;;
		*)
			return 1
			;;
	esac
}

info "蓝音管家 BTSpeaker 卸载程序"
if [ "$DRY_RUN" = 1 ]; then
	info "当前为 --dry-run 预览模式，不会改动系统"
fi
if [ "$PURGE" = 1 ]; then
	warn "已启用 --purge：将同时删除 $DATA_DIR（配置、规则、日志、用户声音）"
fi

confirm || { info "已取消卸载"; exit 0; }

# 1. 停止并禁用服务
info "停止并禁用服务 ..."
if [ -x "$INIT_SCRIPT" ]; then
	run "$INIT_SCRIPT" stop
	run "$INIT_SCRIPT" disable
else
	warn "未找到 $INIT_SCRIPT，跳过服务停止"
fi
# 兜底：确认进程已退出
if [ "$DRY_RUN" = 0 ] && command -v pgrep >/dev/null 2>&1; then
	if pgrep -f 'btspeaker serve' >/dev/null 2>&1; then
		warn "检测到残留进程，尝试结束 ..."
		pkill -f 'btspeaker serve' 2>/dev/null || true
		sleep 1
	fi
fi

# 2. 移除程序文件
info "移除程序文件 ..."
run rm -f "$PREFIX_BIN"
run rm -rf "$PREFIX_LIB"
run rm -f "$INIT_SCRIPT"
run rm -rf "$SHARE_DIR"

# 3. 移除防火墙规则
info "移除防火墙规则 ..."
if command -v uci >/dev/null 2>&1; then
	if [ "$DRY_RUN" = 1 ]; then
		printf '  [dry-run] uci delete firewall.btspeaker && uci commit firewall && /etc/init.d/firewall reload\n'
	else
		if uci -q get firewall.btspeaker >/dev/null 2>&1; then
			uci -q delete firewall.btspeaker 2>/dev/null || true
			uci commit firewall 2>/dev/null || true
			/etc/init.d/firewall reload >/dev/null 2>&1 || true
			ok "防火墙规则已移除"
		else
			info "未发现 btspeaker 防火墙规则，跳过"
		fi
	fi
else
	warn "未找到 uci，跳过防火墙清理；如曾手动放行 TCP 8722，请自行删除规则"
fi

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
		info "如需彻底删除，请重新执行：sh uninstall.sh --purge"
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
	echo "  重新安装：上传 btspeaker_<版本>_all.ipk，或在项目目录执行 sh install.sh"
fi
exit 0
