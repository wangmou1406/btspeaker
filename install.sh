#!/usr/bin/env bash
# 蓝音管家 BTSpeaker —— Linux 一键安装脚本（systemd）
#
# 单行安装（推荐）：
#   curl -fsSL https://raw.githubusercontent.com/wangmou1406/btspeaker/main/install.sh | sudo bash
#
# 源码方式（在仓库目录内）：
#   sudo bash install.sh                 # 安装 / 重复执行即升级覆盖
#   sudo bash install.sh --no-deps       # 依赖已自备时跳过系统包安装
#   sudo bash install.sh --optional      # 额外安装 espeak-ng 与 ffmpeg
#   bash install.sh --dry-run            # 只打印计划，不改动系统（非 root 也可）
#
# 安装内容：
#   /opt/btspeaker/btspeaker      Python 包（含 web/ 控制台与 seed_rules.json）
#   /opt/btspeaker/scripts        bt-diag.sh、install-deps.sh
#   /usr/local/bin/btspeaker      启动器（PYTHONPATH=/opt/btspeaker）
#   /etc/systemd/system/btspeaker.service   systemd 单元（开机自启）
#   /usr/share/btspeaker/sounds   内置声音库
#   /etc/btspeaker                数据目录（config.json、rules.json、日志、用户声音）
#
# 依赖：python3、bluez、bluez-alsa（多包名自动尝试）、alsa-utils、mpg123；
#       可选：espeak-ng（离线 TTS）、ffmpeg（多格式解码）。

set -u

APP_NAME="蓝音管家 BTSpeaker"
GITHUB_OWNER="wangmou1406"
GITHUB_REPO="btspeaker"
GITHUB_BRANCH="main"
TARBALL_PATH="${GITHUB_OWNER}/${GITHUB_REPO}/archive/refs/heads/${GITHUB_BRANCH}.tar.gz"
TARBALL_URL="https://github.com/${TARBALL_PATH}"
# 国内网络直连 github.com 经常超时：依次回退到公共加速镜像。
# 可用 BTSPEAKER_TARBALL_URL 完全覆盖（支持 http(s)/file:// 或内网镜像），
# 或用 BTSPEAKER_MIRROR=https://your-mirror/ 指定前缀（脚本会拼成 <前缀>/https://github.com/...）。
TARBALL_MIRRORS=(
	"https://ghfast.top/https://github.com/${TARBALL_PATH}"
	"https://ghproxy.net/https://github.com/${TARBALL_PATH}"
	"https://gh-proxy.com/https://github.com/${TARBALL_PATH}"
	"https://hub.gitmirror.com/https://github.com/${TARBALL_PATH}"
)

INSTALL_DIR="/opt/btspeaker"
LAUNCHER="/usr/local/bin/btspeaker"
UNIT_FILE="/etc/systemd/system/btspeaker.service"
DATA_DIR="/etc/btspeaker"
SOUND_DIR="/usr/share/btspeaker/sounds"
PORT="8722"

DRY_RUN=0
NO_DEPS=0
OPTIONAL=0
SRC_DIR=""
SRC_MODE=""
CLEANUP_DIR=""

# --------------------------------------------------------------------------- #
info() { printf '[信息] %s\n' "$*"; }
ok()   { printf '[完成] %s\n' "$*"; }
warn() { printf '[警告] %s\n' "$*" >&2; }
err()  { printf '[错误] %s\n' "$*" >&2; }
die()  { err "$*"; exit 1; }

usage() {
	cat <<'EOF'
蓝音管家 BTSpeaker —— Linux 一键安装脚本

用法：
  sudo bash install.sh [选项]

选项：
  --dry-run, -n   只打印将要执行的操作，不修改系统（非 root 也可运行）
  --no-deps       跳过系统依赖安装（python3/bluez/bluez-alsa 等已自备）
  --optional      额外安装 espeak-ng（离线 TTS）与 ffmpeg（多格式解码）
  --help, -h      显示本帮助

单行安装：
  curl -fsSL https://raw.githubusercontent.com/wangmou1406/btspeaker/main/install.sh | sudo bash

说明：
  脚本会自动识别发行版（apt / dnf / pacman / zypper）安装依赖，
  部署到 /opt/btspeaker，注册 systemd 服务 btspeaker 并设置开机自启。
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

cleanup() {
	if [ -n "${CLEANUP_DIR:-}" ] && [ -d "$CLEANUP_DIR" ]; then
		rm -rf "$CLEANUP_DIR"
	fi
}
trap cleanup EXIT INT TERM

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
		--optional)
			OPTIONAL=1
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

# --------------------------------------------------------------------------- #
# 逐个尝试 GitHub 直连与镜像下载源码包；成功返回 0
download_archive() {
	local archive="$1"
	local -a urls=()
	local url
	if [ -n "${BTSPEAKER_TARBALL_URL:-}" ]; then
		urls=("${BTSPEAKER_TARBALL_URL}")
	else
		urls=("${TARBALL_URL}")
		if [ -n "${BTSPEAKER_MIRROR:-}" ]; then
			urls=("${BTSPEAKER_MIRROR%/}/https://github.com/${TARBALL_PATH}" "${urls[@]}")
		fi
		urls+=("${TARBALL_MIRRORS[@]}")
	fi
	for url in "${urls[@]}"; do
		info "尝试下载：${url}"
		rm -f "$archive"
		if command -v curl >/dev/null 2>&1; then
			if curl -fL --retry 1 --connect-timeout 6 --max-time 180 -o "$archive" "$url" 2>/dev/null \
				&& [ -s "$archive" ]; then
				return 0
			fi
		elif wget -q -T 20 -O "$archive" "$url" 2>/dev/null && [ -s "$archive" ]; then
			return 0
		fi
		warn "该地址不可用，尝试下一个..."
	done
	return 1
}

# tarball 全部失败时，若本机有 git，尝试通过镜像 clone
try_git_clone() {
	local target="$1"
	local prefix
	command -v git >/dev/null 2>&1 || return 1
	for prefix in "" "https://ghfast.top/" "https://ghproxy.net/" "https://hub.gitmirror.com/"; do
		info "尝试 git clone：${prefix}https://github.com/${GITHUB_OWNER}/${GITHUB_REPO}.git"
		rm -rf "$target"
		if git clone --depth 1 --quiet "${prefix}https://github.com/${GITHUB_OWNER}/${GITHUB_REPO}.git" "$target" 2>/dev/null; then
			return 0
		fi
		warn "clone 失败，尝试下一个..."
	done
	return 1
}

# 定位源码目录：仓库目录内直接使用；curl | bash 场景自动下载源码包
detect_source() {
	local script_path="${BASH_SOURCE[0]:-$0}"
	local dir=""

	if [ -n "$script_path" ] && [ -f "$script_path" ] && [ "$(basename -- "$script_path")" != "bash" ]; then
		dir="$(cd -- "$(dirname -- "$script_path")" >/dev/null 2>&1 && pwd || true)"
	fi
	if [ -n "$dir" ] && [ -f "$dir/btspeaker/__init__.py" ] && [ -f "$dir/packaging/systemd/btspeaker.service" ]; then
		SRC_DIR="$dir"
		SRC_MODE="local"
		return 0
	fi
	if [ -n "${BTSPEAKER_SRC_DIR:-}" ] && [ -f "${BTSPEAKER_SRC_DIR}/btspeaker/__init__.py" ]; then
		SRC_DIR="$(cd -- "${BTSPEAKER_SRC_DIR}" >/dev/null 2>&1 && pwd)"
		SRC_MODE="env"
		return 0
	fi
	SRC_DIR=""
	SRC_MODE="download"
}

fetch_source() {
	if [ "$SRC_MODE" = "local" ]; then
		info "使用本地源码目录：$SRC_DIR"
		return 0
	fi
	if [ "$SRC_MODE" = "env" ]; then
		info "使用 BTSPEAKER_SRC_DIR 指定目录：$SRC_DIR"
		return 0
	fi

	info "未检测到本地仓库文件（curl | bash 模式），将从 GitHub 下载源码包"
	if [ "$DRY_RUN" = 1 ]; then
		printf '  [dry-run] 下载 %s\n' "$TARBALL_URL"
		printf '  [dry-run] 直连超时会依次尝试 %d 个加速镜像（ghfast/ghproxy/gh-proxy/gitmirror）\n' "${#TARBALL_MIRRORS[@]}"
		printf '  [dry-run] 解压到临时目录并进入 btspeaker-main/\n'
		SRC_DIR="<自动下载的源码目录>"
		return 0
	fi

	if ! command -v curl >/dev/null 2>&1 && ! command -v wget >/dev/null 2>&1 && ! command -v git >/dev/null 2>&1; then
		die "需要 curl、wget 或 git 之一来下载源码包（如：apt install curl 或 apt install git）"
	fi
	CLEANUP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/btspeaker-install.XXXXXX")" || die "创建临时目录失败"
	local archive="$CLEANUP_DIR/btspeaker.tar.gz"

	info "下载源码包（GitHub 直连失败会自动尝试镜像）..."
	if ! download_archive "$archive"; then
		warn "所有源码包地址均失败，尝试通过镜像 git clone..."
		if try_git_clone "$CLEANUP_DIR/src"; then
			SRC_DIR="$CLEANUP_DIR/src"
			[ -f "$SRC_DIR/btspeaker/__init__.py" ] || die "clone 的源码结构异常（缺少 btspeaker/__init__.py）"
			info "源码已就绪：$SRC_DIR"
			return 0
		fi
		die "下载失败：GitHub 直连与加速镜像均不可达（国内网络常见）。三种解决方式：
  1) 换网络 / 挂代理后重试本命令；
  2) 指定可用镜像重试：
       sudo BTSPEAKER_TARBALL_URL='https://<你的镜像>/btspeaker-main.tar.gz' bash install.sh
     或 sudo BTSPEAKER_MIRROR='https://ghfast.top/' bash install.sh
  3) 在能访问 GitHub 的机器下载源码包，scp 到本机解压后进入目录执行：sudo bash install.sh"
	fi
	tar -xzf "$archive" -C "$CLEANUP_DIR" || die "解压源码包失败"
	SRC_DIR=""
	local dir
	for dir in "$CLEANUP_DIR"/*/; do
		if [ -f "$dir/install.sh" ] && [ -f "$dir/btspeaker/__init__.py" ]; then
			SRC_DIR="${dir%/}"
			break
		fi
	done
	[ -n "$SRC_DIR" ] || die "源码包结构异常（未找到 btspeaker/ 与 install.sh）"
	info "源码已解压：$SRC_DIR"
}

# --------------------------------------------------------------------------- #
# 发行版检测
PM=""
DISTRO="未知发行版"
detect_distro() {
	if command -v apt-get >/dev/null 2>&1; then
		PM="apt"
	elif command -v dnf >/dev/null 2>&1; then
		PM="dnf"
	elif command -v pacman >/dev/null 2>&1; then
		PM="pacman"
	elif command -v zypper >/dev/null 2>&1; then
		PM="zypper"
	fi
	if [ -r /etc/os-release ]; then
		# shellcheck disable=SC1091
		. /etc/os-release
		DISTRO="${PRETTY_NAME:-${ID:-未知发行版}}"
	fi
}

install_dependencies() {
	if [ "$NO_DEPS" = 1 ]; then
		info "已指定 --no-deps，跳过系统依赖安装"
		info "如后续缺依赖，可手动执行：sudo bash $INSTALL_DIR/scripts/install-deps.sh"
		return 0
	fi

	local extra=""
	[ "$OPTIONAL" = 1 ] && extra="、espeak-ng、ffmpeg（可选）"
	info "安装运行时依赖：python3、bluez、bluez-alsa、alsa-utils、mpg123${extra}"

	if [ "$DRY_RUN" = 1 ]; then
		if [ -z "$PM" ]; then
			printf '  [dry-run] 未检测到 apt/dnf/pacman/zypper，将提示手动安装依赖\n'
		else
			printf '  [dry-run] %s（若检测到下载的源码目录，则执行 scripts/install-deps.sh）\n' \
				"bash $SRC_DIR/scripts/install-deps.sh$([ "$OPTIONAL" = 1 ] && echo ' --optional')"
		fi
		return 0
	fi

	if [ -f "$SRC_DIR/scripts/install-deps.sh" ]; then
		if [ "$OPTIONAL" = 1 ]; then
			bash "$SRC_DIR/scripts/install-deps.sh" --optional || warn "依赖安装未全部成功，可稍后重试：sudo bash $INSTALL_DIR/scripts/install-deps.sh --optional"
		else
			bash "$SRC_DIR/scripts/install-deps.sh" || warn "依赖安装未全部成功，可稍后重试：sudo bash $INSTALL_DIR/scripts/install-deps.sh"
		fi
	else
		warn "安装包中缺少 scripts/install-deps.sh，跳过依赖安装"
		warn "请手动安装：python3 bluez bluez-alsa alsa-utils mpg123"
	fi
}

# --------------------------------------------------------------------------- #
create_dirs() {
	info "创建目录 ..."
	run mkdir -p "$INSTALL_DIR" "$DATA_DIR" "$DATA_DIR/sounds" "$DATA_DIR/tmp" "$SOUND_DIR"
}

install_program() {
	info "部署程序到 $INSTALL_DIR/btspeaker ..."
	if [ "$DRY_RUN" = 1 ]; then
		printf '  [dry-run] rm -rf %s/btspeaker\n' "$INSTALL_DIR"
		printf '  [dry-run] cp -R %s/btspeaker %s/\n' "$SRC_DIR" "$INSTALL_DIR"
		printf '  [dry-run] cp -R %s/scripts %s/scripts\n' "$SRC_DIR" "$INSTALL_DIR"
		printf '  [dry-run] cp -R %s/assets/sounds/. %s/\n' "$SRC_DIR" "$SOUND_DIR"
		printf '  [dry-run] 清理 __pycache__ 并修正文件权限\n'
		return 0
	fi
	run rm -rf "$INSTALL_DIR/btspeaker"
	run cp -R "$SRC_DIR/btspeaker" "$INSTALL_DIR/"
	if [ -d "$SRC_DIR/scripts" ]; then
		run rm -rf "$INSTALL_DIR/scripts"
		run cp -R "$SRC_DIR/scripts" "$INSTALL_DIR/scripts"
	fi
	if [ -d "$SRC_DIR/assets/sounds" ]; then
		run cp -R "$SRC_DIR/assets/sounds/." "$SOUND_DIR/"
	fi
	find "$INSTALL_DIR/btspeaker" -type d -name '__pycache__' -prune -exec rm -rf {} \; 2>/dev/null || true
	chmod -R a+rX "$INSTALL_DIR/btspeaker" 2>/dev/null || true
}

install_launcher() {
	info "安装启动器：$LAUNCHER ..."
	if [ "$DRY_RUN" = 1 ]; then
		printf '  [dry-run] cp %s/packaging/bin/btspeaker %s\n' "$SRC_DIR" "$LAUNCHER"
		printf '  [dry-run] chmod 0755 %s\n' "$LAUNCHER"
		return 0
	fi
	if [ -f "$SRC_DIR/packaging/bin/btspeaker" ]; then
		run cp "$SRC_DIR/packaging/bin/btspeaker" "$LAUNCHER"
	else
		warn "缺少 packaging/bin/btspeaker，生成等价启动器"
		cat > "$LAUNCHER" <<'LAUNCHER_EOF'
#!/bin/sh
set -u
PYTHON="${BTSPEAKER_PYTHON:-/usr/bin/python3}"
LIB_DIR="${BTSPEAKER_LIB:-/opt/btspeaker}"
[ -x "$PYTHON" ] || PYTHON="$(command -v python3 2>/dev/null || true)"
[ -n "$PYTHON" ] && [ -x "$PYTHON" ] || { echo "错误：未找到 python3" >&2; exit 127; }
if [ -n "${PYTHONPATH:-}" ]; then PYTHONPATH="$LIB_DIR:$PYTHONPATH"; else PYTHONPATH="$LIB_DIR"; fi
export PYTHONPATH PYTHONIOENCODING="${PYTHONIOENCODING:-utf-8}" PYTHONUNBUFFERED=1
exec "$PYTHON" -m btspeaker "$@"
LAUNCHER_EOF
	fi
	run chmod 0755 "$LAUNCHER"
}

install_unit() {
	info "安装 systemd 单元：$UNIT_FILE ..."
	if [ "$DRY_RUN" = 1 ]; then
		printf '  [dry-run] cp %s/packaging/systemd/btspeaker.service %s\n' "$SRC_DIR" "$UNIT_FILE"
		printf '  [dry-run] chmod 0644 %s\n' "$UNIT_FILE"
		return 0
	fi
	if [ -f "$SRC_DIR/packaging/systemd/btspeaker.service" ]; then
		run cp "$SRC_DIR/packaging/systemd/btspeaker.service" "$UNIT_FILE"
		run chmod 0644 "$UNIT_FILE"
	else
		warn "缺少 packaging/systemd/btspeaker.service，跳过 systemd 单元安装"
	fi
}

seed_data() {
	info "初始化数据目录 $DATA_DIR（config.json + 5 条示例规则，规则默认停用）..."
	if [ "$DRY_RUN" = 1 ]; then
		printf '  [dry-run] PYTHONPATH=%s python3 -m btspeaker seed --data-dir %s\n' "$INSTALL_DIR" "$DATA_DIR"
		return 0
	fi
	local python_bin
	python_bin="$(command -v python3 2>/dev/null || true)"
	if [ -z "$python_bin" ]; then
		warn "未找到 python3，跳过数据目录初始化（安装 python3 后执行：sudo PYTHONPATH=$INSTALL_DIR python3 -m btspeaker seed --data-dir $DATA_DIR）"
		return 0
	fi
	if PYTHONPATH="$INSTALL_DIR" "$python_bin" -m btspeaker seed --data-dir "$DATA_DIR"; then
		ok "数据目录初始化完成"
	else
		warn "数据目录初始化失败；服务首次启动时会自动初始化"
	fi
}

enable_service() {
	info "注册并启动 systemd 服务 btspeaker ..."
	if [ "$DRY_RUN" = 1 ]; then
		printf '  [dry-run] systemctl daemon-reload\n'
		printf '  [dry-run] systemctl enable --now btspeaker\n'
		return 0
	fi
	if ! command -v systemctl >/dev/null 2>&1; then
		warn "未检测到 systemd（systemctl）：跳过服务注册"
		warn "可手动前台运行：sudo $LAUNCHER serve --data-dir $DATA_DIR"
		return 0
	fi
	systemctl daemon-reload || warn "systemctl daemon-reload 失败"
	systemctl enable --now btspeaker || warn "systemctl enable --now btspeaker 失败"
	# 重复安装（升级）时确保加载新代码
	systemctl restart btspeaker || warn "systemctl restart btspeaker 失败"
	sleep 1
	if systemctl is-active --quiet btspeaker; then
		ok "服务已启动（开机自启已启用）"
	else
		warn "服务可能未启动成功，请查看：journalctl -u btspeaker -n 80 --no-pager"
	fi
}

check_health() {
	[ "$DRY_RUN" = 1 ] && return 0
	local health=""
	if command -v curl >/dev/null 2>&1; then
		health="$(curl -fsS --max-time 5 "http://127.0.0.1:${PORT}/api/health" 2>/dev/null || true)"
	elif command -v wget >/dev/null 2>&1; then
		health="$(wget -q -O - "http://127.0.0.1:${PORT}/api/health" 2>/dev/null || true)"
	fi
	case "$health" in
		*'"status"'*)
			ok "HTTP 健康检查通过：$health"
			;;
		*)
			warn "HTTP 健康检查暂未通过（服务可能仍在启动）"
			warn "稍后可验证：curl -s http://127.0.0.1:${PORT}/api/health"
			;;
	esac
}

lan_ip() {
	local ip=""
	ip="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
	if [ -z "$ip" ] && command -v ip >/dev/null 2>&1; then
		ip="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i = 1; i < NF; i++) if ($i == "src") { print $(i + 1); exit }}' || true)"
	fi
	[ -n "$ip" ] || ip="<本机 LAN IP>"
	printf '%s' "$ip"
}

print_summary() {
	local ip
	ip="$(lan_ip)"
	cat <<EOF

============================================================
 $APP_NAME 安装完成（Linux / systemd）
============================================================
 访问地址 : http://$ip:$PORT/
 程序目录 : $INSTALL_DIR
 数据目录 : $DATA_DIR
 内置声音 : $SOUND_DIR
 服务单元 : $UNIT_FILE
 诊断脚本 : sudo sh $INSTALL_DIR/scripts/bt-diag.sh
 服务管理 : systemctl {start|stop|restart|status} btspeaker
             journalctl -u btspeaker -f      # 实时日志
             sudo systemctl disable btspeaker  # 取消开机自启

 后续 3 步：
 1. 连音箱：浏览器打开上面的访问地址 →「音箱」页扫描并配对；
           或命令行：btspeaker devices --scan / btspeaker pair <MAC> / btspeaker connect <MAC>
 2. 配 TTS：在「设置」页填写 TTS 服务地址；也可安装 espeak-ng 离线兜底
           （sudo sh $INSTALL_DIR/scripts/install-deps.sh --optional）
 3. 开规则：5 条示例规则默认全部停用，到「自动化」页确认后逐条启用。

 卸载：sudo bash uninstall.sh            # 保留 $DATA_DIR 配置与数据
       sudo bash uninstall.sh --purge    # 同时删除配置与数据
============================================================
EOF
}

# --------------------------------------------------------------------------- #
# 主流程
printf '蓝音管家 BTSpeaker —— Linux 一键安装\n'
if [ "$DRY_RUN" = 1 ]; then
	info "当前为 --dry-run 预览模式，只打印计划，不会改动系统"
fi

detect_source
fetch_source

if [ "$(id -u 2>/dev/null || echo 1)" -ne 0 ]; then
	if [ "$DRY_RUN" = 1 ]; then
		warn "当前不是 root：--dry-run 仅预览，正式安装需要 root"
	else
		die "请以 root 身份运行：sudo bash install.sh（或 curl ... | sudo bash）"
	fi
fi

detect_distro
info "系统：$DISTRO${PM:+（包管理器：$PM）}"

install_dependencies
create_dirs
install_program
install_launcher
install_unit
seed_data
enable_service
check_health
print_summary

if [ "$DRY_RUN" = 1 ]; then
	info "预览结束：以上为将要执行的操作，未改动系统"
fi
exit 0
