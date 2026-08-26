# Delta Chat 客户端跨平台安装指南

本文只介绍在 Android、iOS/iPadOS、Linux、macOS 和 Windows 上安装 **Delta Chat 客户端**。Chatmail Relay 服务端的正式部署、Mock/IP-only 部署、证书配置、验证和故障排查已经在 [`README.md`](../README.md) 与 [`MOCK_INSTALL.md`](../MOCK_INSTALL.md) 中说明，不在本文重复。

优先使用 [Delta Chat 官方下载页](https://delta.chat/download)、系统应用商店或受支持的软件包管理器。这些渠道通常有更清晰的升级路径。手动安装 APK、DEB、RPM、AppImage、DMG、EXE 或便携包时，可能不会自动更新；应定期返回官方下载页检查新版本。

官方下载页会随发布更新版本和最低系统要求，因此本文不固定具体版本号。安装前请在下载页确认操作系统最低版本、CPU 架构和当前可用格式。

| 客户端平台 | 首选渠道 | 官方直接下载格式 | 更新说明 |
|---|---|---|---|
| Android | Google Play、F-Droid 或官方下载页列出的商店 | APK | 手动 APK 通常需要手动更新 |
| iOS/iPadOS | Apple App Store | 不提供镜像 IPA | 由 App Store 更新 |
| GNU/Linux | Flatpak 或发行版软件包管理器 | DEB、RPM、AppImage、tar.gz | 直接包和 AppImage 需要自行检查更新 |
| macOS | App Store 或 Homebrew | Universal/Apple Silicon DMG | DMG 手动安装时需自行检查更新 |
| Windows | Microsoft Store 或 Winget | Setup EXE、便携 EXE | 便携版为实验选项，需自行检查更新 |

### Android 客户端

从官方下载页选择应用商店可获得正常更新。无法使用商店时，可以下载官方 APK 或经过验证的镜像副本：

1. 确认 Android 最低版本要求。
2. 确认文件是稳定版 APK，不是未明确标注的测试版。
3. 按[手动下载校验](#手动下载与-sha-256-校验)检查文件。
4. 仅对用于安装的浏览器或文件管理器临时授权“安装未知应用”，安装后撤销授权。

不要从聊天附件、网盘分享或来源不明的网站安装 APK。安装前备份重要资料；不同签名或不同发行渠道的 APK 可能无法直接覆盖安装。

### iOS/iPadOS 客户端

通过 Apple App Store 搜索并安装 Delta Chat。本文不提供 IPA 镜像、越狱、侧载或绕过平台安全机制的说明。

### Linux 客户端

跨发行版首选 Flatpak：

```sh
flatpak install flathub chat.delta.desktop
```

Arch Linux 可使用：

```sh
sudo pacman -S deltachat-desktop
```

如果使用直接下载包，必须匹配发行版和 CPU 架构：

- Debian/Ubuntu 使用 `.deb`，例如 `sudo apt install ./<package>.deb`。
- Fedora/RHEL 系使用 `.rpm`，例如 `sudo dnf install ./<package>.rpm`。
- AppImage 下载后先校验，再执行 `chmod +x <package>.AppImage` 并运行。
- `tar.gz` 是归档包，不等同于由发行版管理的软件包。

不要在 RPM 系发行版上安装 DEB，也不要仅凭文件名猜测 `x86_64/amd64` 与 `arm64/aarch64` 架构。

### macOS 客户端

可从 App Store 安装，或使用 Homebrew：

```sh
brew install --cask deltachat
```

手动下载 DMG 时，在官方下载页选择 Universal 或与设备匹配的 Apple Silicon 构建，完成完整性校验后再打开。不要绕过 Gatekeeper 或删除隔离属性来运行来源不明的文件。

### Windows 客户端

首选 Microsoft Store，或在 PowerShell 中使用 Winget：

```powershell
winget install 9PJTXX7HN3PK
```

官方下载页也提供 Setup EXE 和实验性的便携 EXE。下载后核对架构和 SHA-256；便携版不会像商店版本一样自然地获得更新。不要关闭 SmartScreen 或其他系统保护来运行无法验证来源的文件。

### 手动下载与 SHA-256 校验

先阅读 Delta Chat 官方下载页链接的 **Verify Downloads** 说明并优先采用上游发布的校验方法。若上游没有发布摘要，镜像可以提供独立计算的 SHA-256，但必须明确标注“镜像计算”，不能称为上游认证。

Linux、macOS 或 Termux：

```sh
sha256sum <downloaded-file>
```

macOS 未安装 `sha256sum` 时：

```sh
shasum -a 256 <downloaded-file>
```

Windows PowerShell：

```powershell
Get-FileHash -Algorithm SHA256 "C:\Users\<user>\Downloads\<downloaded-file>"
```

计算值必须与同一版本、平台、格式和架构所列的 SHA-256 完全一致。不一致时删除文件并改用官方下载渠道；不要继续安装。

### `www.talk.cmsv.space` 镜像规则

`https://www.talk.cmsv.space/` 是计划用于保存官方 APK、DEB、RPM 等安装包的可选镜像入口，不是 Delta Chat 官方站点。只有在浏览器和命令行都能通过正常 HTTPS 证书校验，并且下载页展示下列完整信息时才能使用：

- 产品名、上游版本、稳定版或预览版渠道、操作系统、包格式和 CPU 架构。
- 原始文件名、文件大小、发布日期和 SHA-256。
- Delta Chat 官方下载或发布 URL、上游项目名称。
- “未经修改的上游镜像，不是 MamreTek 自行维护的构建”声明。
- 手动更新说明、许可证和必要的上游通知。

每个文件应使用不可变的版本化路径。人类可读的“最新版”页面可以指向版本化文件，但不能用一个会被覆盖的 `latest` 文件作为唯一下载链接。稳定版区域不得混入未明确标识的 beta/preview 包。

镜像发布者必须先从官方来源取回文件，按上游校验方式验证，确认再分发许可，并让文件、元数据和摘要原子地同时可见。已知受损版本应醒目标记并撤下；旧版本按公开保留策略处理。

如果镜像证书报错、不可访问、缺少元数据、摘要不一致或版本落后，请改用 [Delta Chat 官方下载页](https://delta.chat/download)。不得使用 `curl -k`、`--insecure`、忽略浏览器证书警告或关闭系统安全功能来下载客户端安装包。
