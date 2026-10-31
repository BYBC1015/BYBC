# BYBC Hub iOS 0.1.1

独立 SwiftUI + WKWebView App 工程，最低 iOS / iPadOS 17，要求 Hub 网页 2.11.0 或更新版本。它复用 Hub 的真实网页工作台和服务器会话，不是添加到主屏幕的 PWA，也不在手机运行 Codex CLI。构建目标名 `Hub`，默认 Bundle ID `xyz.baicang.codexhub.ios`；该 ID 仍须由实际 Apple 开发团队确认注册。

## 已实现

- 本机记录页可离线阅读当前对话末 30 条已发送的用户与智能体文字。仅存有界纯文本，标出保存时间和非实时状态，不运行旧 HTML，不包含 Cookie 明文、草稿和附件文件；按服务器/登录摘要/用户/项目/对话隔离，7 天过期，登录/登出清除。异步捕获、返回和后台切换均有代次检查。

- 原生外壳保留一个 WKWebView，持久化登录 Cookie；回到 App 时触发网页的数据更新，冷启动仅恢复与当前登录会话一致的项目/对话地址。
- 恢复页面时使用绕过本地 HTTP 缓存的 GET；Wi-Fi、蜂窝网络等路径切换即使一直可用也会唤醒网页版本校验。保留 Cookie、IndexedDB 草稿与附件，不将网络重连当成重新发送任务。2026-10-31 新增请求策略 XCTest，需本轮 macOS CI 验证。
- 原生小猫头鹰顶栏、系统黑体、44 pt 操作区域、系统安全区与键盘布局。正文、手机抽屉、输入框由共享 `mobile-workspace` 统一适配。
- 聊天输入框的图片/文件选择使用 WebKit 系统选择器；不申请整个文件库读取权限，不建立第二套草稿数据库。
- 文件下载通过保留当前登录态的 WKDownload；完成后显示原生分享面板，可选“存储到文件”。原对话不跳到外部浏览器。只接受同一 Hub 的文件路由；登录跳转、外站跳转不会被保存为成果。
- 返回先关闭图片预览、对话框、导航抽屉或工作详情，再以 fresh GET 打开原生历史地址，禁用会恢复旧页面快照的 WebKit 滑动历史。外部用户链接才交给 Safari。
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

2026-10-31 的 0.1.1 / build 2 源码新增本地记录、最低网页发行版检查、账号恢复隔离及异步生命周期保护，XCTest 从 5 项扩充为 9 项。本轮仍需新的 macOS CI 构建/测试，不沿用以下历史版本结果宣称本版验证完成。

2026-10-30 已在 GitHub macOS runner 使用 **Xcode 26.6 / build 17F113** 完成 unsigned simulator 原生编译及 **4 项 XCTest，0 失败**：[验证运行 40981287956](https://github.com/BYBC1015/BYBC/actions/runs/40981287956)，源码提交 `8fa835f0f94a84fee1457567972bf2360d99fda8`。测试覆盖对话恢复、下载与恢复边界、可信地址与路径限制、文件名处理；不把这些策略测试说成完整真机交互验收。

该次证据包 `Hub-iOS-0.1.0-UNSIGNED-SIMULATOR-ONLY` 包含 `.app`、`.xcresult` 与构建日志，6569519 bytes，SHA-256 `62632b371b8e42b56fa608fb62559d527713b7e79324770675320b40d7bf971f`；GitHub artifact 保留至 2026-11-06 00:48 UTC。它用于模拟器与审计，不是 iPhone 安装包。BYBC-YOLO 本机仍为 Windows，没有本地 Xcode / Swift SDK。

当前没有已验证的 Apple 开发团队、签名证书、profile、已签名 IPA 或 TestFlight 分发。模拟器 `.app` 不能安装到真实 iPhone。签名安装需要可用的 Mac/Xcode + 开发团队与设备/分发配置；正式 TestFlight 还需注册 Bundle ID / App Store Connect 应用及账号权限。不要把密码或验证码写入仓库。

签名前必须用真实 iPhone/iPad 验证：登录恢复、截图和 PDF 选择、键盘与旋转、快速连续消息、后台返回刷新、断网/重连、文件下载和保存、VoiceOver。App 顶栏保留现有 Owl 图片；主屏幕图标使用内置图像生成工具制作、经目视确认的猫头鹰方形母版，经确定性尺寸导出为 1024×1024 RGB PNG，配置在 `AppIcon.appiconset`，Debug / Release 均指定该图标。此图标补丁还须随下一次 CI 验证其编译产物。商店隐私表及隐私政策仍需发行配置，不把未具备签名与发行条件的构建称为商店成品。

首版不包含 APNs 推送、系统分享扩展、后台文件传输或离线执行器。这些能力须另行接入服务器与 Apple entitlement，并验证真实设备；不依靠模拟器测试冒充已支持。

## 官方依据

- [WKDownload](https://developer.apple.com/documentation/webkit/wkdownload) 和 [WKDownloadDelegate](https://developer.apple.com/documentation/webkit/wkdownloaddelegate) 提供沿当前 WebKit 会话的下载及重定向处理。
- [WebKit 文件选择回调](https://developer.apple.com/documentation/webkit/wkuidelegate/webview(_:runopenpanelwith:initiatedbyframe:completionhandler:)) 说明未替换回调时保留 iOS 默认文件上传行为。本项目不自行禁用/覆盖它。
- [Apple 测试与发行](https://developer.apple.com/documentation/xcode/distributing-your-app-for-beta-testing-and-releases) 区分模拟器构建、签名设备安装、TestFlight 和商店发行。
