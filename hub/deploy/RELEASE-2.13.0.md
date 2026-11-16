# Hub 2.13.0 / Windows 1.3.0 / Android 0.3.0

设置在当前工作区内打开，旧设置链接自动迁移；保留聊天草稿、滚动位置和浏览器返回行为。Android 设置、模型管理、返回栈与减少动效使用本地原生控件，离线时不会跳回旧网页。

模型服务支持 Ollama、LM Studio 与 OpenAI 兼容 API。服务绑定实际执行电脑，手机只同步设置；连接检查读取模型目录，不提交生成请求。可在身份模型设置中分配，每次执行固定连接版本。Codex CLI 保持默认，本地工程和文件修改继续由 CLI 处理；兼容 API 可进行聊天、受支持的图片分析以及有界 Hub 工具调用。服务实际可用性取决于执行电脑在线、模型已启动及提供方协议支持。

本次稳定性修复包括损坏任务记录隔离、完成与附件回执校验、未确认结果保留恢复记录、取消状态等待真实停止确认。外部推理或工具写入结果不明时，不自动重放。API 调用限制工具轮数和上下文长度，避免无界消耗；不会将连接检查成功当作推理、视觉质量或工具能力已经验证。

模型密钥为只写配置，存放在服务器数据目录的 `.private/model-services`，普通模型目录、数据库记录、浏览器缓存和便携导出不包含密钥。管理员需使用受保护的服务器备份保留该目录；跨主机恢复后重新提供凭据，缺失时停止而不降级为空密钥。编辑地址时必须重新指定或明确清除凭据。删除服务不改变已经冻结的在途执行。

Windows 安装包采用目录运行环境和原生安装启动器，面向 Windows 10/11 x64，使用系统 API-set 与通用 C 运行库。安装前会只读检查固定系统目录中的 VC/MFC 版本、架构和必需接口；缺少依赖时在提取前停止，提供[微软官方 v14 运行库页面](https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist)，由用户安装或修复后重试。Hub 不自动修改系统运行库。本轮完整包验收使用 x64 VC/MFC 14.50 环境，结果以当次记录为准；前置检查通过不代表所有 Windows 10/11 版本或功能均已验证。具体依赖边界和可重现构建入口见 [Windows 1.3.0 发布说明](BRIDGE-1.3.0.md)。

发布安装包前须完成服务、界面、执行恢复、隔离安装和 Android 构建检查；实际安装包哈希、测试结果、下载入口和发布状态以当次交付记录为准。模拟器验证不等同于物理手机验收。

交互参考（2026-11-17 查阅）：

- [Linear 的一致导航与层级](https://linear.app/changelog/2026-03-12-ui-refresh)
- [Raycast 的模型服务与聊天修复记录](https://www.raycast.com/changelog)
- [Fluent 2 的动效原则](https://fluent2.microsoft.design/motion/)
- [Cherry Studio](https://github.com/CherryHQ/cherry-studio) 与 [Open WebUI](https://github.com/open-webui/open-webui) 的服务管理方式
- [Ollama](https://docs.ollama.com/api/openai-compatibility) 与 [LM Studio](https://lmstudio.ai/docs/developer/openai-compat) 官方兼容接口

仅参考交互和兼容协议，本轮未复制上述项目源码。
