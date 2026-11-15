#!/bin/sh
# 蓝音管家 BTSpeaker —— Linux 运行时依赖安装脚本（幂等，可重复执行）
#
# 用途：install.sh 会调用本脚本安装依赖；如果安装时网络异常导致依赖没装上，
#       或想单独补装（例如之后才想用 TTS / 播放 ogg），可手动执行：
#
#   sudo sh scripts/install-deps.sh              # 必需依赖
#   sudo sh scripts/install-deps.sh --optional   # 额外安装 espeak-ng / ffmpeg
#
# 支持 apt(Debian/Ubuntu)、dnf(Fedora)、pacman(Arch)、zypper(openSUSE)。

set -u

OPTIONAL=0
while [ $# -gt 0 ]; do
	case "$1" in
		--optional)
			OPTIONAL=1
			;;
		-h|--help)
			sed -n '2,12p' "$0"
			exit 0
			;;
		*)
			printf '[错误] 未知参数：%s（可用 --optional / --help）\n' "$1" >&2
			exit 2
			;;
	esac
	shift
done

info() { printf '[信息] %s\n' "$*"; }
ok()   { printf '[完成] %s\n' "$*"; }
warn() { printf '[警告] %s\n' "$*" >&2; }
die()  { printf '[错误] %s\n' "$*" >&2; exit 1; }

if [ "$(id -u 2>/dev/null || echo 1)" -ne 0 ]; then
	die "请以 root 运行：sudo sh $0"
fi

# --------------------------------------------------------------------------- #
# 发行版与包管理器检测
# --------------------------------------------------------------------------- #
PM=""
if command -v apt-get >/dev/null 2>&1; then
	PM="apt"
elif command -v dnf >/dev/null 2>&1; then
	PM="dnf"
elif command -v pacman >/dev/null 2>&1; then
	PM="pacman"
elif command -v zypper >/dev/null 2>&1; then
	PM="zypper"
fi

DISTRO="未知发行版"
if [ -r /etc/os-release ]; then
	# shellcheck disable=SC1091
	. /etc/os-release
	DISTRO="${PRETTY_NAME:-${ID:-未知发行版}}"
fi

if [ -z "$PM" ]; then
	warn "未检测到 apt / dnf / pacman / zypper，无法自动安装依赖。"
	warn "请手动安装：python3、bluez、bluez-alsa、alsa-utils、mpg123"
	warn "Debian/Ubuntu: apt install python3 bluez bluez-alsa-utils alsa-utils mpg123"
	warn "Fedora:        dnf install python3 bluez bluez-alsa alsa-utils mpg123"
	warn "Arch:          pacman -S python bluez bluez-alsa alsa-utils mpg123"
	warn "openSUSE:      zypper install python3 bluez bluez-alsa alsa-utils mpg123"
	exit 1
fi

info "系统：$DISTRO（包管理器：$PM）"

# 安装一个或多个包；返回 0=成功
pkg_install() {
	case "$PM" in
		apt)
			DEBIAN_FRONTEND=noninteractive apt-get install -y "$@"
			;;
		dnf)
			dnf install -y "$@"
			;;
		pacman)
			pacman -S --needed --noconfirm "$@"
			;;
		zypper)
			zypper --non-interactive install "$@"
			;;
	esac
}

pkg_update() {
	case "$PM" in
		apt)
			apt-get update
			;;
		dnf)
			dnf makecache -q
			;;
		pacman)
			pacman -Sy
			;;
		zypper)
			zypper --non-interactive refresh
			;;
	esac
}

has_cmd() {
	command -v "$1" >/dev/null 2>&1
}

# BlueALSA 不同发行版包名不同；依次尝试，成功即止
install_bluealsa() {
	for pkg in bluez-alsa-utils bluez-alsa bluealsa; do
		info "尝试安装 bluez-alsa 包：$pkg"
		if pkg_install "$pkg" >/dev/null 2>&1; then
			ok "已安装 $pkg"
			return 0
		fi
	done
	warn "bluez-alsa 自动安装失败（包名可能因发行版/版本而异）"
	warn "请按发行版手动安装："
	warn "  Debian/Ubuntu: apt install bluez-alsa-utils"
	warn "  Fedora:        dnf install bluez-alsa"
	warn "  Arch:          pacman -S bluez-alsa（或 AUR: bluez-alsa-git）"
	warn "  openSUSE:      zypper install bluez-alsa"
	return 1
}

# --------------------------------------------------------------------------- #
info "更新软件包索引（失败不致命）..."
pkg_update >/dev/null 2>&1 || warn "更新软件包索引失败，继续尝试安装"

info "检查必需依赖..."
MISSING=""
# python3
if has_cmd python3; then
	ok "python3 已安装：$(python3 --version 2>&1)"
else
	MISSING="$MISSING python3"
fi
# bluez（提供 bluetoothctl / bluetoothd）
if has_cmd bluetoothctl; then
	ok "bluez 已安装：$(bluetoothctl --version 2>/dev/null | head -n 1)"
else
	MISSING="$MISSING bluez"
fi
# alsa-utils（提供 aplay）
if has_cmd aplay; then
	ok "alsa-utils 已安装"
else
	MISSING="$MISSING alsa-utils"
fi
# mpg123
if has_cmd mpg123; then
	ok "mpg123 已安装"
else
	MISSING="$MISSING mpg123"
fi

FAILED=""
if [ -n "$MISSING" ]; then
	info "安装缺失的必需依赖：$MISSING"
	for pkg in $MISSING; do
		if pkg_install "$pkg" >/dev/null 2>&1; then
			ok "已安装 $pkg"
		else
			warn "安装失败：$pkg"
			FAILED="$FAILED $pkg"
		fi
	done
else
	ok "必需依赖已全部安装"
fi

# bluez-alsa：单独处理（多包名、且不强制成功）
if has_cmd bluealsa || [ -f /usr/lib/systemd/system/bluealsa.service ] \
	|| [ -f /lib/systemd/system/bluealsa.service ] \
	|| [ -f /usr/lib/systemd/system/bluez-alsa.service ]; then
	ok "bluez-alsa 已安装（bluealsa 可用）"
else
	install_bluealsa || true
fi

# --------------------------------------------------------------------------- #
if [ "$OPTIONAL" = 1 ]; then
	info "检查可选组件（TTS 离线兜底 / 多格式解码）..."
	if has_cmd espeak-ng || has_cmd espeak; then
		ok "espeak-ng 已安装"
	else
		if pkg_install espeak-ng >/dev/null 2>&1 || pkg_install espeak >/dev/null 2>&1; then
			ok "espeak-ng 已安装"
		else
			warn "espeak-ng 安装失败（可选，不影响核心功能）"
		fi
	fi
	if has_cmd ffmpeg; then
		ok "ffmpeg 已安装"
	else
		if pkg_install ffmpeg >/dev/null 2>&1; then
			ok "ffmpeg 已安装"
		else
			warn "ffmpeg 安装失败（可选项，仅非 mp3/wav 格式需要）"
		fi
	fi
else
	info "可选组件（espeak-ng / ffmpeg）未检查；需要时执行：sh $0 --optional"
fi

# --------------------------------------------------------------------------- #
echo ""
if [ -n "$FAILED" ]; then
	warn "以下必需依赖安装失败：$FAILED"
	warn "请检查网络/软件源后重试：sh $0；或按发行版手动安装。"
	exit 1
fi
if ! has_cmd python3; then
	die "python3 仍未安装，无法运行蓝音管家"
fi
ok "依赖检查完成。"
echo "提示：bluez-alsa 负责把蓝牙 A2DP 音频桥接给 ALSA；若自动安装失败，"
echo "      可用发行版包名安装后重启服务："
echo "      Debian/Ubuntu: apt install bluez-alsa-utils"
echo "      Fedora: dnf install bluez-alsa    Arch: pacman -S bluez-alsa"
echo "      openSUSE: zypper install bluez-alsa"
exit 0
