# dist-openwrt/

由 GitHub Actions（`.github/workflows/build-openwrt.yml`）自动构建的 **OpenWrt x86_64 预编译包**：

- `bluez-alsa_<版本>_x86_64.ipk`：BlueALSA 守护进程 + ALSA PCM 插件（A2DP source/sink、AAC、mpg123）
- `bluez-alsa-utils_<版本>_x86_64.ipk`：`bluealsa-aplay` / `a2dpconf` / `bluealsactl`
- `btspeaker_<版本>_all.ipk`：蓝音管家本体（OpenWrt ipk，`Architecture: all`）
- `SHA256SUMS`：校验和

这些文件由 CI 自动提交更新，**不要手动改**。安装步骤见 [`docs/OPENWRT-FLASH.md`](../docs/OPENWRT-FLASH.md)。

下载（国内可用 raw 直连，或加镜像前缀 `https://ghfast.top/`）：

```text
https://raw.githubusercontent.com/wangmou1406/btspeaker/main/dist-openwrt/
```
