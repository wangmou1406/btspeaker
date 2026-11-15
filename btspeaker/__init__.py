"""蓝音管家 BTSpeaker —— iStoreOS 蓝牙音箱连接与自动化服务。

纯标准库实现，可运行于 iStoreOS / OpenWrt x86_64（Python 3.9+）。
"""

from __future__ import annotations

__version__ = "1.0.0"
APP_NAME = "btspeaker"
APP_TITLE = "蓝音管家"
APP_SUBTITLE = "蓝牙音箱连接 · 自动化播控"
DEFAULT_PORT = 8722
DEFAULT_DATA_DIR = "/etc/btspeaker"
# 项目主页（本仓库为本地交付形态，暂不指向外部托管地址）
PROJECT_URL = "本地部署 · iStoreOS x86_64"

__all__ = [
    "__version__",
    "APP_NAME",
    "APP_TITLE",
    "APP_SUBTITLE",
    "DEFAULT_PORT",
    "DEFAULT_DATA_DIR",
    "PROJECT_URL",
]
