#!/usr/bin/env python3
"""蓝音管家 BTSpeaker —— .ipk 打包工具（纯标准库，无需 opkg/ipkg-build）。

手工构造现代 OpenWrt 的 ipk（外层 tar.gz 容器；旧式 ar 容器已被新版 opkg 拒绝，
真机上会报 `pkg_init_from_file: Malformed package file`）：

    ./debian-binary    内容为 "2.0\\n"
    ./control.tar.gz   control / postinst / prerm 等控制文件
    ./data.tar.gz      程序文件树（usr/、etc/）

成员名带 "./" 前缀、宽度/权限/顺序对齐 OpenWrt 官方构建产物（21.02+ / 23.05 实测格式）。

同时产出可直接解压后执行 install.sh 的离线安装包：
    dist/btspeaker-<版本>-x86_64.tar.gz

用法：
    python scripts/build_ipk.py                 # 构建到 dist/
    python scripts/build_ipk.py --output-dir x  # 自定义输出目录

构建过程完全在内存中完成（不写系统临时目录），构建后自动重新解析产物做结构自校验。
只打包仓库中真实存在的内容：btspeaker/ 包（含 web/ 与 seed_rules.json）、
启动器、procd 启动脚本、诊断脚本、assets/sounds 声音库。
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import os
import re
import sys
import tarfile
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

# --------------------------------------------------------------------------- #
# 包元数据
# --------------------------------------------------------------------------- #
ROOT = Path(__file__).resolve().parents[2]
ARCHIVE_DIR = Path(__file__).resolve().parent  # OpenWrt 归档脚本所在目录（packaging/openwrt）

PKG_NAME = "btspeaker"
# 纯 Python 标准库 + shell 脚本，与 CPU 架构无关，用 all 兼容所有 iStoreOS/OpenWrt 设备
ARCH = "all"
MAINTAINER = "BTSpeaker Project <btspeaker@example.com>"
SECTION = "net"
PRIORITY = "optional"
# 运行时依赖：不再写入 control 的 Depends（iStore 本地安装沙箱无法解析 feeds 依赖，
# 硬依赖会导致安装失败），改由 postinst 在真机上尽力安装，并随包提供
# /usr/share/btspeaker/scripts/install-deps.sh 供手动重试。
# 注意：只列 OpenWrt 官方 feed 真实存在的包名；
#   - x86_64 内核的 btusb 为内建，没有 kmod-btusb 包；
#   - bluealsa 不在官方 feed，单列为可选（固件可能已内置，或用第三方源）。
RUNTIME_DEPENDS = (
    "python3-light",
    "python3-urllib",
    "python3-openssl",
    "python3-email",
    "python3-logging",
    "python3-uuid",
    "bluez-daemon",
    "bluez-utils",
    "alsa-utils",
    "mpg123",
    "kmod-bluetooth",
)
RUNTIME_OPTIONAL = (
    "bluealsa",  # A2DP 放音桥接；官方 feed 无此包，装在固件/第三方源里时自动使用
)
DESCRIPTION_LINES = [
    "蓝音管家 BTSpeaker —— 蓝牙音箱连接与自动化播控服务",
    "纯 Python 标准库实现的独立 Web 服务：蓝牙音箱扫描/配对/连接、",
    "声音库播放、TTS 语音播报、定时任务与蓝牙事件自动化。",
    "适用于 iStoreOS / OpenWrt x86_64 软路由（opkg / procd）。",
]

# 固定时间戳，保证产物可复现
EPOCH = 1700000000
GZIP_LEVEL = 9

# 运行时必需（缺失只警告，不中断构建；便于并行开发期间先验证打包流程）
EXPECTED_RUNTIME = [
    "usr/lib/btspeaker/btspeaker/__init__.py",
    "usr/lib/btspeaker/btspeaker/__main__.py",
    "usr/lib/btspeaker/btspeaker/cli.py",
    "usr/lib/btspeaker/btspeaker/service.py",
    "usr/lib/btspeaker/btspeaker/api.py",
    "usr/lib/btspeaker/btspeaker/seed_rules.json",
    "usr/lib/btspeaker/btspeaker/web/index.html",
    "usr/lib/btspeaker/btspeaker/web/app.js",
    "usr/lib/btspeaker/btspeaker/web/style.css",
]

EXCLUDE_DIR_NAMES = {"__pycache__", ".git", ".pytest_cache", ".mypy_cache"}
EXCLUDE_SUFFIXES = (".pyc", ".pyo")
EXCLUDE_ARC_PARTS = ("__pycache__", ".pyc", ".pyo", ".btsdata", ".git/", "/tests/", "/dist/")


class BuildError(Exception):
    """打包失败（结构/内容不合法）。"""


# --------------------------------------------------------------------------- #
# 通用工具
# --------------------------------------------------------------------------- #
def read_version() -> str:
    path = ROOT / "VERSION"
    if not path.is_file():
        raise BuildError("仓库根目录缺少 VERSION 文件：%s" % path)
    version = path.read_text(encoding="utf-8").strip()
    if not re.match(r"^[0-9A-Za-z][0-9A-Za-z.+~_-]*$", version):
        raise BuildError("VERSION 内容不合法（只允许字母/数字/._+~-）：%r" % version)
    return version


def gzip_bytes(data: bytes) -> bytes:
    """gzip 压缩（固定 mtime，保证可复现）。"""
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0, compresslevel=GZIP_LEVEL) as gz:
        gz.write(data)
    return buf.getvalue()


def walk_files(base: Path) -> List[Path]:
    """递归列出普通文件，跳过缓存/隐藏目录/字节码。"""
    result: List[Path] = []
    for dirpath, dirnames, filenames in os.walk(str(base)):
        dirnames[:] = sorted(
            name for name in dirnames if name not in EXCLUDE_DIR_NAMES and not name.startswith(".")
        )
        for name in sorted(filenames):
            if name.endswith(EXCLUDE_SUFFIXES) or name.startswith("."):
                continue
            result.append(Path(dirpath) / name)
    return result


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------- #
# tar / ar 构造
# --------------------------------------------------------------------------- #
def make_tar(
    entries: Sequence[Tuple[str, bytes, int]],
    gz: bool,
    sort: bool = True,
    root_entry: bool = False,
) -> bytes:
    """构造 tar；entries 为 (归档名, 内容, 模式)，目录会自动补目录项。

    sort=False 时保持传入顺序（ipk 外层容器需要对齐官方成员顺序）；
    root_entry=True 时额外写入 "." 根目录项（官方 ipk 内层 tar 都有）。
    """
    files = sorted(entries, key=lambda item: item[0]) if sort else list(entries)
    dirs = set()
    if root_entry and files and all(name.startswith("./") for name, _d, _m in files):
        dirs.add(".")
    for name, _data, _mode in files:
        clean = name[2:] if name.startswith("./") else name
        parts = clean.strip("/").split("/")[:-1]
        for idx in range(1, len(parts) + 1):
            prefix = "./" if name.startswith("./") else ""
            dirs.add(prefix + "/".join(parts[:idx]) + "/")

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        for dirname in sorted(dirs):
            info = tarfile.TarInfo(dirname)
            info.type = tarfile.DIRTYPE
            info.mode = 0o755
            info.mtime = EPOCH
            info.uid = 0
            info.gid = 0
            info.uname = "root"
            info.gname = "root"
            tar.addfile(info)
        for name, data, mode in files:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = mode
            info.mtime = EPOCH
            info.uid = 0
            info.gid = 0
            info.uname = "root"
            info.gname = "root"
            tar.addfile(info, io.BytesIO(data))
    raw = buf.getvalue()
    return gzip_bytes(raw) if gz else raw


def make_ipk_container(members: Sequence[Tuple[str, bytes]]) -> bytes:
    """构造现代 OpenWrt ipk 外层容器（tar.gz，成员带 "./" 前缀）。

    自 OpenWrt 21.02 起 ipk 外层改为 tar.gz；opkg 不再接受旧式 ar 容器。
    成员顺序对齐官方构建：debian-binary → data.tar.gz → control.tar.gz。
    """
    entries = [("./" + name, data, 0o644) for name, data in members]
    return make_tar(entries, gz=True, sort=False, root_entry=False)


def parse_ipk_container(data: bytes) -> List[Tuple[str, bytes]]:
    """解析 ipk（tar.gz 外层），返回 [(name, body)]（name 已去掉 "./" 前缀）。"""
    if not data.startswith(b"\x1f\x8b"):
        raise BuildError(
            "ipk 外层不是 gzip/tar.gz（新版 opkg 要求 tar.gz 容器，收到魔数 %r）"
            % data[:8].hex()
        )
    members: List[Tuple[str, bytes]] = []
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            for info in tar.getmembers():
                if not info.isfile():
                    continue
                handle = tar.extractfile(info)
                body = handle.read() if handle is not None else b""
                name = info.name[2:] if info.name.startswith("./") else info.name
                members.append((name, body))
    except tarfile.TarError as exc:
        raise BuildError("ipk 外层 tar.gz 解析失败：%s" % exc)
    return members


def tar_members(data: bytes) -> Dict[str, tarfile.TarInfo]:
    """读取 tar.gz 成员信息（名称 -> TarInfo）；同时校验能否完整解析。"""
    result: Dict[str, tarfile.TarInfo] = {}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for info in tar.getmembers():
            result[info.name.lstrip("./")] = info
    return result


# --------------------------------------------------------------------------- #
# 控制脚本（安装/卸载时由 opkg 执行）
# --------------------------------------------------------------------------- #
POSTINST = """#!/bin/sh
# 蓝音管家 BTSpeaker：opkg 安装后脚本（幂等）
# 职责：建目录 -> 初始化数据目录（btspeaker seed，已存在不改动）-> 放行端口 -> 启用服务
DATA_DIR=/etc/btspeaker
PORT=8722

mkdir -p "$DATA_DIR" "$DATA_DIR/sounds" "$DATA_DIR/tmp" 2>/dev/null
mkdir -p /usr/share/btspeaker/sounds 2>/dev/null

# 依赖安装：本包 control 中不写 Depends（iStore 本地安装沙箱无法解析 feeds 依赖，
# 硬依赖会直接导致安装失败），改为安装后在真机上尽力安装缺失依赖；失败不阻断安装。
DEPS="__RUNTIME_DEPS__"
if command -v opkg >/dev/null 2>&1; then
	missing=""
	for dep in $DEPS; do
		opkg list-installed 2>/dev/null | grep -q "^$dep " || missing="$missing $dep"
	done
	if [ -n "$missing" ]; then
		echo "btspeaker: 正在安装依赖:$missing"
		opkg update >/dev/null 2>&1 || true
		for dep in $missing; do
			opkg install "$dep" >/dev/null 2>&1 || echo "btspeaker: 依赖安装失败: $dep（可执行 install-deps.sh 重试）"
		done
	fi
fi

# 可选依赖（官方 feed 通常没有 bluealsa，固件内置或第三方源提供时才可用）
for dep in __RUNTIME_OPTIONAL__; do
	if command -v opkg >/dev/null 2>&1 && ! opkg list-installed 2>/dev/null | grep -q "^$dep "; then
		echo "btspeaker: 可选依赖未安装: $dep（A2DP 放音需要；官方源无此包，可查看 bt-diag.sh 的建议）"
	fi
done

# 初始化 config.json 与 5 条示例规则（规则默认停用；已存在则保持不变）
if [ -x /usr/bin/btspeaker ]; then
	/usr/bin/btspeaker seed --data-dir "$DATA_DIR" >/dev/null 2>&1 || true
fi

# 防火墙放行 TCP 8722（lan 区域，幂等）
if command -v uci >/dev/null 2>&1; then
	if ! uci -q get firewall.btspeaker >/dev/null 2>&1; then
		uci set firewall.btspeaker=rule 2>/dev/null || true
	fi
	uci set firewall.btspeaker.name='btspeaker' 2>/dev/null || true
	uci set firewall.btspeaker.src='lan' 2>/dev/null || true
	uci set firewall.btspeaker.dest_port="$PORT" 2>/dev/null || true
	uci set firewall.btspeaker.proto='tcp' 2>/dev/null || true
	uci set firewall.btspeaker.target='ACCEPT' 2>/dev/null || true
	uci commit firewall 2>/dev/null || true
	/etc/init.d/firewall reload >/dev/null 2>&1 || true
fi

# 开机自启并立即启动（重复执行安全）
if [ -x /etc/init.d/btspeaker ]; then
	/etc/init.d/btspeaker enable >/dev/null 2>&1 || true
	/etc/init.d/btspeaker start >/dev/null 2>&1 || true
fi

exit 0
"""

PRERM = """#!/bin/sh
# 蓝音管家 BTSpeaker：opkg 卸载/升级前脚本（停止并禁用服务）
if [ -x /etc/init.d/btspeaker ]; then
	/etc/init.d/btspeaker stop >/dev/null 2>&1 || true
	/etc/init.d/btspeaker disable >/dev/null 2>&1 || true
fi
exit 0
"""


def build_control(version: str) -> str:
    lines = [
        "Package: %s" % PKG_NAME,
        "Version: %s" % version,
        "Architecture: %s" % ARCH,
        "Maintainer: %s" % MAINTAINER,
        "Section: %s" % SECTION,
        "Priority: %s" % PRIORITY,
        "Description: %s" % DESCRIPTION_LINES[0],
    ]
    for extra in DESCRIPTION_LINES[1:]:
        lines.append(" %s" % extra)
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# payload 收集
# --------------------------------------------------------------------------- #
def add_file(entries: List[Tuple[str, bytes, int]], source: Path, arcname: str, mode: int) -> bool:
    if not source.is_file():
        return False
    entries.append((arcname, source.read_bytes(), mode))
    return True


def collect_payload() -> Tuple[List[Tuple[str, bytes, int]], List[str]]:
    """收集 ipk data.tar.gz 的内容；返回 (entries, warnings)。"""
    entries: List[Tuple[str, bytes, int]] = []
    warnings: List[str] = []

    pkg_dir = ROOT / PKG_NAME
    if not pkg_dir.is_dir():
        raise BuildError("找不到程序包目录：%s" % pkg_dir)
    package_files = walk_files(pkg_dir)
    if not package_files:
        raise BuildError("程序包目录为空：%s" % pkg_dir)
    for path in package_files:
        rel = path.relative_to(pkg_dir).as_posix()
        entries.append(("usr/lib/btspeaker/%s/%s" % (PKG_NAME, rel), path.read_bytes(), 0o644))

    # 启动器与 procd 启动脚本（必需，位于归档目录）
    if not add_file(entries, ARCHIVE_DIR / "btspeaker-launcher.sh", "usr/bin/btspeaker", 0o755):
        raise BuildError("缺少 packaging/openwrt/btspeaker-launcher.sh")
    if not add_file(entries, ARCHIVE_DIR / "init.d-btspeaker", "etc/init.d/btspeaker", 0o755):
        raise BuildError("缺少 packaging/openwrt/init.d-btspeaker")

    # 诊断脚本（可选但应存在）
    if not add_file(entries, ARCHIVE_DIR / "bt-diag.sh", "usr/share/btspeaker/scripts/bt-diag.sh", 0o755):
        warnings.append("缺少 packaging/openwrt/bt-diag.sh（OpenWrt 版），安装包将不含蓝牙诊断脚本")
    if not add_file(entries, ARCHIVE_DIR / "install-deps.sh", "usr/share/btspeaker/scripts/install-deps.sh", 0o755):
        warnings.append("缺少 packaging/openwrt/install-deps.sh（OpenWrt 版），安装包将不含依赖安装脚本")

    # 内置声音库
    sounds_dir = ROOT / "assets" / "sounds"
    sound_count = 0
    if sounds_dir.is_dir():
        for path in walk_files(sounds_dir):
            rel = path.relative_to(sounds_dir).as_posix()
            entries.append(("usr/share/btspeaker/sounds/%s" % rel, path.read_bytes(), 0o644))
            sound_count += 1
    if sound_count == 0:
        warnings.append("assets/sounds 为空或不存在，安装包将不含内置声音库")

    # 安全兜底：确保没有任何开发/测试产物混入
    for name, _data, _mode in entries:
        for bad in EXCLUDE_ARC_PARTS:
            if bad in ("/" + name):
                raise BuildError("payload 中混入了应排除的内容：%s" % name)
    return entries, warnings


def collect_bundle(version: str) -> Tuple[List[Tuple[str, bytes, int]], List[str]]:
    """收集离线安装包（解压后可在项目目录执行 install.sh）。"""
    entries: List[Tuple[str, bytes, int]] = []
    warnings: List[str] = []
    prefix = "%s-%s" % (PKG_NAME, version)

    def add(rel: str, mode: int) -> None:
        """从仓库根目录添加（程序包、声音库、版本文件等）。"""
        path = ROOT / rel
        if path.is_file():
            entries.append(("%s/%s" % (prefix, rel), path.read_bytes(), mode))
        elif path.is_dir():
            for child in walk_files(path):
                child_rel = child.relative_to(ROOT).as_posix()
                entries.append(("%s/%s" % (prefix, child_rel), child.read_bytes(), 0o644))
        else:
            warnings.append("离线包跳过不存在的文件：%s" % rel)

    def add_archive(rel: str, mode: int) -> None:
        """从 OpenWrt 归档目录添加（脚本按旧仓库的 scripts/ 布局打包）。"""
        src_rel = rel[len("scripts/"):] if rel.startswith("scripts/") else rel
        path = ARCHIVE_DIR / src_rel
        if path.is_file():
            entries.append(("%s/%s" % (prefix, rel), path.read_bytes(), mode))
        else:
            warnings.append("离线包跳过不存在的归档文件：%s" % rel)

    add_archive("install.sh", 0o755)
    add_archive("uninstall.sh", 0o755)
    add_archive("scripts/init.d-btspeaker", 0o755)
    add_archive("scripts/btspeaker-launcher.sh", 0o755)
    add_archive("scripts/bt-diag.sh", 0o755)
    add_archive("scripts/install-deps.sh", 0o755)
    add_archive("README.OpenWrt.md", 0o644)
    add("VERSION", 0o644)
    add("LICENSE", 0o644)
    add(PKG_NAME, 0o644)
    add("assets/sounds", 0o644)

    if not any(name.endswith("/install.sh") for name, _d, _m in entries):
        raise BuildError("离线安装包缺少 install.sh")

    # 安全兜底：排除规则同 payload
    for name, _data, _mode in entries:
        for bad in EXCLUDE_ARC_PARTS:
            if bad in ("/" + name):
                raise BuildError("离线包中混入了应排除的内容：%s" % name)
    return entries, warnings


# --------------------------------------------------------------------------- #
# 构建
# --------------------------------------------------------------------------- #
def build_ipk(version: str, output_dir: Path, verbose: bool = True) -> Dict[str, object]:
    entries, warnings = collect_payload()
    control_text = build_control(version)
    postinst_text = POSTINST.replace("__RUNTIME_DEPS__", " ".join(RUNTIME_DEPENDS)).replace(
        "__RUNTIME_OPTIONAL__", " ".join(RUNTIME_OPTIONAL)
    )
    postinst_bytes = postinst_text.encode("utf-8")

    control_entries = [
        ("./control", control_text.encode("utf-8"), 0o644),
        ("./postinst", postinst_bytes, 0o755),
        ("./prerm", PRERM.encode("utf-8"), 0o755),
    ]
    # data.tar.gz 内路径统一加 "./" 前缀（ipk 惯例）
    data_entries = [("./" + name, data, mode) for name, data, mode in entries]

    control_tar = make_tar(control_entries, gz=True, root_entry=True)
    data_tar = make_tar(data_entries, gz=True, root_entry=True)
    ipk_bytes = make_ipk_container(
        [
            ("debian-binary", b"2.0\n"),
            ("data.tar.gz", data_tar),
            ("control.tar.gz", control_tar),
        ]
    )

    ipk_name = "%s_%s_%s.ipk" % (PKG_NAME, version, ARCH)
    if verbose:
        print("  程序文件：%d 个（含声音库 %s）" % (
            len(entries), "已包含" if any(n.startswith("usr/share/btspeaker/sounds/") for n, _d, _m in entries) else "未包含"))
    return {
        "version": version,
        "ipk_name": ipk_name,
        "ipk_bytes": ipk_bytes,
        "control_tar": control_tar,
        "data_tar": data_tar,
        "entry_count": len(entries),
        "warnings": warnings,
    }


def build_tarball(version: str) -> Dict[str, object]:
    entries, warnings = collect_bundle(version)
    tar_bytes = make_tar(entries, gz=True)
    return {
        "tar_name": "%s-%s-%s.tar.gz" % (PKG_NAME, version, ARCH),
        "tar_bytes": tar_bytes,
        "entry_count": len(entries),
        "warnings": warnings,
    }


# --------------------------------------------------------------------------- #
# 自校验
# --------------------------------------------------------------------------- #
def verify_ipk(ipk_bytes: bytes, version: str, control_tar: bytes, data_tar: bytes) -> List[str]:
    """重新解析 ipk 产物，校验结构/字段/文件清单；返回检查报告，失败抛 BuildError。"""
    report: List[str] = []

    members = parse_ipk_container(ipk_bytes)
    names = [name for name, _body in members]
    expected_members = ["debian-binary", "data.tar.gz", "control.tar.gz"]
    if names != expected_members:
        raise BuildError("ipk 成员不正确（期望 %r）：%r" % (expected_members, names))
    report.append("ipk 容器（tar.gz）成员正确：%s" % " / ".join(names))
    member_map = {name: body for name, body in members}

    if member_map["debian-binary"] != b"2.0\n":
        raise BuildError("debian-binary 内容应为 '2.0\\n'，实际为 %r" % member_map["debian-binary"])
    report.append("debian-binary 内容为 2.0")

    if member_map["control.tar.gz"] != control_tar or member_map["data.tar.gz"] != data_tar:
        raise BuildError("ipk 中内嵌的 control.tar.gz/data.tar.gz 与构建输入不一致")

    control_files = tar_members(member_map["control.tar.gz"])
    expected_control = {"control", "postinst", "prerm"}
    missing = expected_control - set(control_files)
    if missing:
        raise BuildError("control.tar.gz 缺少文件：%s" % ", ".join(sorted(missing)))
    if control_files["postinst"].mode & 0o111 == 0:
        raise BuildError("control.tar.gz: postinst 缺少可执行权限")
    if control_files["prerm"].mode & 0o111 == 0:
        raise BuildError("control.tar.gz: prerm 缺少可执行权限")
    report.append("control.tar.gz 成员正确：%s" % " / ".join(sorted(name for name in control_files if name != ".")))

    # 解析 control 字段
    control_info = control_files["control"]
    control_raw = _tar_extract(member_map["control.tar.gz"], "control")
    fields: Dict[str, str] = {}
    current = ""
    for line in control_raw.decode("utf-8").splitlines():
        if line.startswith(" ") and current:
            fields[current] += "\n" + line.strip()
        elif ":" in line:
            key, _sep, value = line.partition(":")
            current = key.strip()
            fields[current] = value.strip()
    required = ["Package", "Version", "Architecture", "Maintainer", "Section", "Priority", "Description"]
    for key in required:
        if not fields.get(key):
            raise BuildError("control 缺少字段：%s" % key)
    if fields["Package"] != PKG_NAME:
        raise BuildError("control Package 应为 %s，实际 %r" % (PKG_NAME, fields["Package"]))
    if fields["Version"] != version:
        raise BuildError("control Version 应为 %s，实际 %r" % (version, fields["Version"]))
    if fields["Architecture"] != ARCH:
        raise BuildError("control Architecture 应为 %s，实际 %r" % (ARCH, fields["Architecture"]))
    # iStore 本地安装沙箱无法解析 feeds 依赖，硬依赖会导致 cannot find dependency 安装失败；
    # 因此刻意不在 control 里写 Depends，改由 postinst / install-deps.sh 处理。
    if fields.get("Depends"):
        raise BuildError("control 不应声明 Depends（会让 iStore 安装沙箱解析依赖失败）：%r" % fields.get("Depends"))
    if control_info.mode & 0o111:
        raise BuildError("control 文件不应带可执行权限")
    report.append("control 字段正确：Package=%s Version=%s Architecture=%s（%s）" % (
        fields["Package"], fields["Version"], fields["Architecture"],
        "纯脚本包，跨架构" if ARCH == "all" else "架构专用"))
    report.append("无硬依赖（%d 个运行时依赖由 postinst / install-deps.sh 安装）" % len(RUNTIME_DEPENDS))

    # 校验 data.tar.gz 文件清单
    data_files = tar_members(member_map["data.tar.gz"])
    data_names = set(data_files)
    for required_path in (
        "usr/lib/btspeaker/btspeaker/__init__.py",
        "usr/bin/btspeaker",
        "etc/init.d/btspeaker",
    ):
        if required_path not in data_names:
            raise BuildError("data.tar.gz 缺少必需文件：%s" % required_path)
    for path in ("usr/bin/btspeaker", "etc/init.d/btspeaker"):
        if data_files[path].mode & 0o111 == 0:
            raise BuildError("%s 缺少可执行权限" % path)
    if "usr/lib/btspeaker/btspeaker/seed_rules.json" in data_names:
        report.append("已包含服务端种子规则 btspeaker/seed_rules.json（5 条示例，默认停用）")
    else:
        report.append("提示：尚未包含 btspeaker/seed_rules.json（服务端 seed 需要该文件）")

    forbidden_hits = [
        name for name in data_names
        if "__pycache__" in name or name.endswith((".pyc", ".pyo"))
        or name.startswith(("tests/", "test/", "docs/", "dist/", ".btsdata/"))
    ]
    if forbidden_hits:
        raise BuildError("data.tar.gz 混入了应排除的文件：%s" % ", ".join(sorted(forbidden_hits)[:10]))
    report.append("data.tar.gz 清单干净：%d 个文件（无 __pycache__/tests/docs/dist/.btsdata）" % len(data_names))

    # 并行开发期间允许某些模块暂缺，但明确提示
    missing_runtime = [path for path in EXPECTED_RUNTIME if path not in data_names]
    if missing_runtime:
        report.append("提示：以下运行时文件当前仓库中尚不存在（集成完成后重新构建即可）：%s" % ", ".join(missing_runtime))
    return report


def _tar_extract(tar_bytes: bytes, name: str) -> bytes:
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:gz") as tar:
        for info in tar.getmembers():
            if info.name.lstrip("./") == name:
                handle = tar.extractfile(info)
                if handle is None:
                    raise BuildError("无法读取 %s" % name)
                return handle.read()
    raise BuildError("tar 中找不到文件：%s" % name)


def verify_tarball(tar_bytes: bytes, version: str) -> List[str]:
    report: List[str] = []
    prefix = "%s-%s/" % (PKG_NAME, version)
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:gz") as tar:
        names = [info.name for info in tar.getmembers()]
    if not names:
        raise BuildError("离线安装包为空")
    # tar 目录项会以去掉结尾 "/" 的名字出现，单独放行顶层目录本身
    top_dir = prefix.rstrip("/")
    for name in names:
        if name.rstrip("/") == top_dir:
            continue
        if not name.startswith(prefix):
            raise BuildError("离线包存在非预期顶层路径：%s" % name)
    required = {
        prefix + "install.sh",
        prefix + "uninstall.sh",
        prefix + "btspeaker/__init__.py",
        prefix + "scripts/init.d-btspeaker",
        prefix + "scripts/btspeaker-launcher.sh",
    }
    missing = required - set(names)
    if missing:
        raise BuildError("离线安装包缺少：%s" % ", ".join(sorted(missing)))
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:gz") as tar:
        install_info = tar.getmember(prefix + "install.sh")
    if install_info.mode & 0o111 == 0:
        raise BuildError("离线包 install.sh 缺少可执行权限")
    report.append("离线安装包结构正确：顶层 %s，%d 个条目（解压后进入目录执行 install.sh）" % (
        prefix, len(names)))
    return report


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def write_output(output_dir: Path, name: str, data: bytes) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / name
    tmp = output_dir / (".%s.tmp" % name)
    tmp.write_bytes(data)
    os.replace(str(tmp), str(target))
    return target


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="蓝音管家 BTSpeaker .ipk 打包工具（纯标准库）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--output-dir", default="dist", help="产物输出目录（默认 dist/，相对仓库根目录）")
    parser.add_argument("--quiet", action="store_true", help="只输出关键信息")
    args = parser.parse_args(argv)

    try:
        version = read_version()
        output_dir = Path(args.output_dir)
        if not output_dir.is_absolute():
            output_dir = ROOT / output_dir

        print("蓝音管家 BTSpeaker 打包工具")
        print("  仓库：%s" % ROOT)
        print("  版本：%s  架构：%s" % (version, ARCH))
        print("")

        print("[1/4] 收集程序文件并生成 control 元数据 ...")
        ipk = build_ipk(version, output_dir, verbose=not args.quiet)

        print("[2/4] 构建 ipk（tar.gz 外层: ./debian-binary + ./data.tar.gz + ./control.tar.gz）...")
        ipk_path = write_output(output_dir, ipk["ipk_name"], ipk["ipk_bytes"])

        print("[3/4] 构建离线安装包 tar.gz ...")
        bundle = build_tarball(version)
        tar_path = write_output(output_dir, bundle["tar_name"], bundle["tar_bytes"])

        print("[4/4] 自校验产物 ...")
        warnings: List[str] = list(ipk["warnings"]) + list(bundle["warnings"])
        report = verify_ipk(ipk["ipk_bytes"], version, ipk["control_tar"], ipk["data_tar"])
        report += verify_tarball(bundle["tar_bytes"], version)

        print("")
        for line in report:
            print("  [校验] %s" % line)
        for warning in warnings:
            print("  [警告] %s" % warning)

        ipk_hash = sha256(ipk["ipk_bytes"])
        tar_hash = sha256(bundle["tar_bytes"])
        print("")
        print("产物：")
        print("  %s" % ipk_path)
        print("      %d 字节  sha256=%s" % (len(ipk["ipk_bytes"]), ipk_hash))
        print("  %s" % tar_path)
        print("      %d 字节  sha256=%s" % (len(bundle["tar_bytes"]), tar_hash))
        print("")
        print("校验方式：")
        print("  tar -tzf %s   # 现代 OpenWrt ipk 外层是 tar.gz" % ipk_path.name)
        print("  tar -xzOf %s ./data.tar.gz | tar -tzf -" % ipk_path.name)
        print("")
        print("构建成功。")
        return 0
    except BuildError as exc:
        print("构建失败：%s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
