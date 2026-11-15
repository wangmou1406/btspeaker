#!/bin/sh
# 蓝音管家 BTSpeaker 启动器（安装为 /usr/bin/btspeaker）
#
# 用法示例：
#   btspeaker serve                          # 启动 Web 服务（procd 使用）
#   btspeaker serve --data-dir /etc/btspeaker --port 8722
#   btspeaker --help                         # 查看命令行帮助
#
# 包安装位置：/usr/lib/btspeaker/btspeaker（PYTHONPATH 指向 /usr/lib/btspeaker）

set -u

PYTHON="${BTSPEAKER_PYTHON:-/usr/bin/python3}"
LIB_DIR="${BTSPEAKER_LIB:-/usr/lib/btspeaker}"

if [ ! -x "$PYTHON" ]; then
	PYTHON="$(command -v python3 2>/dev/null || true)"
fi
if [ -z "$PYTHON" ] || [ ! -x "$PYTHON" ]; then
	echo "错误：未找到 python3，请先安装 python3-light（opkg install python3-light）" >&2
	exit 127
fi

if [ ! -d "$LIB_DIR/btspeaker" ]; then
	echo "错误：未找到程序目录 $LIB_DIR/btspeaker，请重新安装蓝音管家" >&2
	exit 1
fi

# 保证能 import btspeaker 包，同时保留用户已有的 PYTHONPATH
if [ -n "${PYTHONPATH:-}" ]; then
	PYTHONPATH="$LIB_DIR:$PYTHONPATH"
else
	PYTHONPATH="$LIB_DIR"
fi
export PYTHONPATH
# 中文日志在串口/管道下不乱码
export PYTHONIOENCODING="${PYTHONIOENCODING:-utf-8}"
export PYTHONUNBUFFERED=1

exec "$PYTHON" -m btspeaker "$@"
