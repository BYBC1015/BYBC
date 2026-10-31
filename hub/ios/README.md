# BYBC Hub iOS 0.2.0 · build 3

最低 iOS / iPadOS 17。应用使用安装包内的 SwiftUI 界面、TabView 和 NavigationStack，聊天、项目、智能体、工作详情及工具箱均为本机页面。URLSession 只同步 Hub API 1 的 JSON 数据与私有附件；要求服务器提供 `api/mobile`（Hub 2.12.0）。应用没有 WKWebView、网页历史、JavaScript 注入或失败后打开旧网页的路径。WebKit 仅用于首次迁移旧版登录 Cookie，不创建浏览器页面。

## 本轮实现

- 原生中文系统字体、猫头鹰图标、圆角聊天与输入区、系统键盘和安全区、横竖屏、动态字体及系统辅助功能。看智能体详情不改变收件人；点击“交给他”才选择长期执行者。
- 聊天输入区可选择图片、视频和文件。附件落入应用私有目录后再发送，保持原始字节；照片选择的迟到回调校验账号、项目、对话与代次，不能误加到另一场对话。图片显示经过登录校验的本机缩略预览，原文件通过 QuickLook 打开并经系统分享面板保存。
- 原生项目与对话切换、任务结果/真实进度、历史任务、较早消息/对话、项目文件库、设备状态、反馈和安装包下载。下载不导航到网页，校验大小，安装包额外核对 SHA-256。未缓存的离线页面明确提示，不能伪装在线。
- 已同步内容按服务器、用户、登录摘要和项目/对话隔离，并显示离线状态与保存时间。草稿和待确认发送单独按服务器 + 稳定用户 ID 保存；登录过期、退出、重新登录不默默删除未发送的原始附件，只有重新验证同一用户才能恢复。展示缓存不能跨登录会话读取。
- 每次发送先保存固定请求键及不可变内容，再上传。超时/后台恢复只查收据，明确点击重试才复用原请求键重新发送；不自动重复派单。上一条待确认消息独立显示，用户仍可编辑下一条；新会话的首条收据未明确前，下一条暂不发送，以免误建会话。收到收据不清空用户已开始写的下一条。
- 前台增量轮询按 revision 更新，后台停止轮询。切换、暂停与退出取消迟到请求并再次核对账号/代次。URLSession 自动 Cookie 和 HTTP 缓存关闭；当前请求代次才能提交 Set-Cookie。所有 API 与附件请求拒绝重定向；JSON 接收上限 16 MB，文件上限 2 GB，在接收过程中限额并核对实际字节数。
- 文件选择限制 12 个附件、单个 50 MB、总计 100 MB。手机用于安排任务和查看成果，模型与制作仍由在线执行端完成。

## 验证

在带 Xcode 和 iPhone Simulator 的 macOS 上运行：

```bash
bash hub/ios/scripts/verify-ios.sh
```

当前本地目录为 `codex-hub` 时使用 `bash ios/scripts/verify-ios.sh`。脚本检查工程/plist，编译 unsigned simulator app，再运行 XCTest 和 XCUITest。`HUB_IOS_SKIP_TESTS=1` 只用于编译诊断，不代表完整回归。CI 模板位于 `ci/ios-simulator.yml`，复制至 BYBC 仓库 `.github/workflows/hub-ios.yml`；测试日志、xcresult 和 unsigned `.app` 留在 `Hub-iOS-0.2.0-UNSIGNED-SIMULATOR-ONLY` 产物中。

本轮新增真实隔离 API 导出的 7 份 JSON 契约样本，测试 Codable 解码、私有附件字节、跨账号/重新登录草稿隔离、待确认发送与下一条草稿、传输错误/重定向/大小/取消。原生 UI 测试实际启动应用，操作 Tab、详情、返回、对话切换、离线缓存、重启恢复和横屏，保留聊天/项目/智能体/任务/工具箱/键盘截图。UI 测试启动环境 `HUB_TESTING=1` + `HUB_UI_TESTING=1`，地址强制为 `example.invalid` 且 URLProtocol 阻断所有外部网络；本地 fixture 仅编译在 Debug 中。

保留的 9 项旧 Policy 测试只验证 URL 信任/兼容规则，不能用来证明新版加载 HTML，也不能替代原生交互测试。本机为 Windows，没有 Xcode；**源码静态检查不等于本轮 macOS 编译或 UI 测试已通过**。应以本轮 CI 日志和截图为验收证据，不沿用 0.1.x 的测试结果。

## 分发边界

工程 Bundle ID 为 `xyz.baicang.codexhub.ios`，实际发行需 Apple 开发团队注册和签名。unsigned 模拟器 `.app` 不能安装到真实 iPhone；当前源码不等于已签名 IPA 或 TestFlight 版本。真机签名前需验证 Cookie 恢复、照片/PDF、系统文件保存、键盘与旋转、VoiceOver、后台返回和弱网。没有 APNs 推送、系统分享扩展、后台上传或手机本地模型执行器；不将未验证能力标成已经可用。
