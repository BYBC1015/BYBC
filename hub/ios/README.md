# BYBC Hub iOS 0.1.0

独立 SwiftUI + WKWebView App 工程，最低 iOS / iPadOS 17。它复用 Hub 的真实网页工作台和服务器会话，不是添加到主屏幕的 PWA，也不在手机运行 Codex CLI。构建目标名 `Hub`，默认 Bundle ID `xyz.baicang.codexhub.ios`；该 ID 仍须由实际 Apple 开发团队确认注册。

## 已实现

- 原生外壳保留一个 WKWebView，持久化登录 Cookie；回到 App 时触发网页的数据更新，冷启动返回最后一个有效项目/对话。
- 原生小猫头鹰顶栏、系统黑体、44 pt 操作区域、系统安全区与键盘布局。正文、手机抽屉、输入框由共享 `mobile-workspace` 统一适配。
- 聊天输入框的图片/文件选择使用 WebKit 系统选择器；不申请整个文件库读取权限，不建立第二套草稿数据库。
- 文件下载通过保留当前登录态的 WKDownload；完成后显示原生分享面板，可选“存储到文件”。原对话不跳到外部浏览器。只接受同一 Hub 的文件路由；登录跳转、外站跳转不会被保存为成果。
- 返回先关闭图片预览、对话框、导航抽屉或工作详情，再返回网页历史。外部用户链接才交给 Safari。
- 连接故障显示清楚的恢复信息，最多 3 次自动 GET 重试；旧导航回调不能覆盖新导航状态；不自动重复发送 POST 消息。
- URL、恢复目标、文件路径边界有 XCTest；包含版本化隐私清单的 UserDefaults 使用理由。

手机是控制和查看成果的入口。执行任务仍由服务器调度 BYBC-YOLO / BYBC-PRO 等在线执行端；手机锁屏不会接管电脑任务。

## 构建与验证

在安装 Xcode 和 iPhone Simulator runtime 的 macOS 上运行：

```bash
bash hub/ios/scripts/verify-ios.sh
```

本地项目根为 `codex-hub` 时，改用 `bash ios/scripts/verify-ios.sh`。可设置 `HUB_IOS_OUTPUT` 指定构建证据目录。脚本依次验证 plist / Xcode 工程、编译 **unsigned simulator app**、选择已安装的 iPhone 模拟器运行 XCTest。`HUB_IOS_SKIP_TESTS=1` 仅用于编译诊断，不能算完整回归。

公开 BYBC 仓库可把 `ci/ios-simulator.yml` 放到仓库根 `.github/workflows/hub-ios.yml`，通过 `workflow_dispatch` 运行。它使用 macOS runner、只读仓库权限、25 分钟超时，不需要签名密钥，不创建 Apple 账号，不生产 IPA，不推送 TestFlight。产物名称明确包含 `UNSIGNED-SIMULATOR-ONLY`。

## 交付状态与后续条件

2026-10-30 在 BYBC-YOLO（Windows）完成源工程和 Windows 可用的文件/结构检查；本机没有 Xcode / Swift SDK，因此本记录不宣称已通过原生编译或真机测试。macOS CI 的实际 build / test 结论以对应运行记录为准。

当前没有已验证的 Apple 开发团队、签名证书、profile、已签名 IPA 或 TestFlight 分发。模拟器 `.app` 不能安装到真实 iPhone。签名安装需要可用的 Mac/Xcode + 开发团队与设备/分发配置；正式 TestFlight 还需注册 Bundle ID / App Store Connect 应用及账号权限。不要把密码或验证码写入仓库。

签名前必须用真实 iPhone/iPad 验证：登录恢复、截图和 PDF 选择、键盘与旋转、快速连续消息、后台返回刷新、断网/重连、文件下载和保存、VoiceOver。现有 Owl 图片用于 App 顶栏；App Store 专用 1024px 不透明 AppIcon、商店隐私表及隐私政策尚待发行配置，不把未具备这些条件的构建称为商店成品。

首版不包含 APNs 推送、系统分享扩展、后台文件传输或离线执行器。这些能力须另行接入服务器与 Apple entitlement，并验证真实设备；不依靠模拟器测试冒充已支持。

## 官方依据

- [WKDownload](https://developer.apple.com/documentation/webkit/wkdownload) 和 [WKDownloadDelegate](https://developer.apple.com/documentation/webkit/wkdownloaddelegate) 提供沿当前 WebKit 会话的下载及重定向处理。
- [WebKit 文件选择回调](https://developer.apple.com/documentation/webkit/wkuidelegate/webview(_:runopenpanelwith:initiatedbyframe:completionhandler:)) 说明未替换回调时保留 iOS 默认文件上传行为。本项目不自行禁用/覆盖它。
- [Apple 测试与发行](https://developer.apple.com/documentation/xcode/distributing-your-app-for-beta-testing-and-releases) 区分模拟器构建、签名设备安装、TestFlight 和商店发行。
