# Windows / worker 1.3.0

与 Web 2.13.0 配套：打包当前设置面板、短动效与旧缓存迁移；更新任务池异常隔离、严格完成/附件回执、API 模型执行器与独立只读连接探测。

升级保留原用户配置、未完成回执和本地项目绑定。运行中的任务须收尾后切换后台，不因更新重放结果未知的任务。可见窗口正在使用时不强制关闭，安装包发布与安装实例完成更新应分别记录。

新增模型服务只在上报 `model_services: true` 的执行连接中可选；旧 worker 继续原 Codex 路径。API 模型没有本地 shell、桌面或 MCP 工程权限，项目落地工作继续使用 Codex CLI。

软件包验证需检查三个冻结核心、全部打包界面资源、新装/重复安装/旧版升级/卸载与用户数据保留，并保留证据。发布与平台下载哈希另行核验。

## Windows 运行库要求

本版面向 Windows 10 1803（内部版本 17134）及以上，或 Windows 11 x64，使用系统提供的 API-set 与通用 C 运行库。外层通过 RtlGetVersion 检查实际系统构建号，MSI 使用系统登记的构建号，避免兼容版本号造成误判。构建时仅从指定程序运行环境中排除经过审计的系统依赖；其他第三方 DLL 继续保留。MFC 是原本已存在的外部系统依赖，本轮补充其前置检查，并非从原包新增移除。

安装器在提取前读取真实 Windows 版本、原生架构，以及固定 System32 目录中的 `VCRUNTIME140.dll`、`VCRUNTIME140_1.dll`、`mfc140u.dll`，检查文件版本、x64 PE 架构和本版实际引用的接口。检查不搜索 PATH、不加载候选 DLL，也不安装或修改系统组件。缺少或不满足要求时会停止并提供[微软官方运行库页面](https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist)和[微软 v14 x64 下载入口](https://aka.ms/vc14/vc_redist.x64.exe)，由用户安装或修复后重试。

文件版本下限 `14.42.34438.0` 来自本轮收集的 VC 文件，并结合实际接口清单使用；它不是对所有组件工具链兼容性的充分保证。微软建议使用最新受支持的 v14 运行库，并要求运行库不早于构建组件所用的工具链，见[官方兼容说明](https://learn.microsoft.com/en-us/cpp/porting/binary-compat-2015-2017?view=msvc-170)。本版接受满足检查条件的更新运行库，不固定要求 14.50。本轮完整包验收使用 x64 VC/MFC 14.50 环境，各项结果以发布验收记录为准；不能据此声称所有 Windows 10/11 版本均经过实机验证。前置检查通过不代替安装、升级、卸载和功能验收。

## Windows 发布构建入口

1.3.0 的默认发布格式是 **标准 per-user MSI + 单 EXE 校验启动器**。三个程序仍使用目录运行环境；程序文件的安装、修复、升级和卸载由 Windows Installer 执行，不调用旧 Python 安装器或清理逻辑。旧 `onefile` 和 custom 原生外壳仅保留历史源码，不是当前发布入口。

默认安装目录为 `%LOCALAPPDATA%/Programs/CodexHub-MSI/1.3.0`，与旧 custom 安装并存。唯一版本化开始菜单入口运行本版 `CodexBridge.exe`，不自动启动、不修改旧快捷方式或自启、不强制关闭旧程序。旧后台持有单例时，新入口只打开前台；若同一 Hub 的旧前台窗口仍存在，先关闭旧窗口再点新入口，后台任务不会被强制停止。用户配置和可变 `runtime.json` 不属于 MSI 文件清单。

从仓库根目录执行以下流程，所有暂存和工作目录使用新的独立子目录。`D:/HubBuild` 是构建示例位置。公开工具 ZIP 必须来自固定哈希的发行文件，不能从现用安装目录或用户配置复制。前两步只编译三个程序，`--prepare-only` 只准备并校验公开组件；正式打包入口是后续的 `prepare_msi_payload → build_msi_release → build_msi_bootstrapper`。

```powershell
uv run --frozen --with pyinstaller --with pillow --with pystray --with 'pywinauto>=0.6.9' --with 'pywebview==6.2.1' python tools/hub-suite/build_directory_release.py --stage workers --stage-root D:/HubBuild/products-v1.3.0

uv run --frozen --with pyinstaller --with pillow --with pystray --with 'pywinauto>=0.6.9' --with 'pywebview==6.2.1' python tools/hub-suite/build_directory_release.py --stage desktop --stage-root D:/HubBuild/products-v1.3.0

uv run --frozen python tools/hub-suite/build_desktop.py --prepare-only --staging-parent D:/HubBuild --stage-root D:/HubBuild/public-v1.3.0 --video-archive <公开视频工具ZIP> --kling-archive <公开可灵工具ZIP>

uv run --frozen python tools/hub-suite/prepare_msi_payload.py --public-payload D:/HubBuild/public-v1.3.0/payload --public-manifest D:/HubBuild/public-v1.3.0/prepared-public-files.json --binaries-dir D:/HubBuild/products-v1.3.0/directory-dist --output D:/HubBuild/msi-payload-v1.3.0 --readme tools/hub-suite/README-MSI.md

uv run --frozen python tools/hub-suite/build_msi_release.py --payload D:/HubBuild/msi-payload-v1.3.0 --output D:/HubBuild/release/CodexHub-v1.3.0.msi --work-directory D:/HubBuild/msi-work-v1.3.0

uv run --frozen python tools/hub-suite/build_msi_bootstrapper.py --msi D:/HubBuild/release/CodexHub-v1.3.0.msi --proof-json D:/HubBuild/release/CodexHub-v1.3.0.build.json --output D:/HubBuild/release/CodexHub-Setup-v1.3.0.exe --version 1.3.0 --icon codex_bridge/assets/codex-owl.ico
```

MSI 构建报告固定 ProductCode、PackageCode、安装标记组件、全部文件清单和哈希。`NativeMsiBootstrapper.cs`、`build_msi_bootstrapper.py`、`WindowsRuntimePrerequisites.cs`、`windows-runtime-prerequisites.json`、`system-runtime-profile.json` 以及上述 MSI 准备/构建脚本都是发布必需源码或数据。编译报告和中间文件放在安装载荷外；只有最终单 EXE 安装器作为用户交付入口，原 MSI 与构建证明作为内部可重现资料保存。

外层验证完整 MSI 哈希，再从持久安装源缓存调用固定 System32 的 `msiexec`。缓存位于 `%LOCALAPPDATA%/BYBC/CodexHubInstallerCache/<ProductCode>/<PackageCode>.msi`，不依赖 Downloads、临时目录或原 EXE；已有缓存必须再次匹配完整哈希。该缓存不由 MSI 提前删除，卸载后仍保留供修复和回退使用，不泛删其他版本或系统 Installer 缓存。

标准返回码 0 必须再通过 MSI 产品登记、已登记标记路径、标记哈希和全部 owned 文件校验，才报告完成；3010 明确为待重启生效，不宣称文件全部就绪；1602 是用户取消。安装始终禁止自动重启和 Restart Manager 自动关闭应用，不自动启动 Bridge。错误日志只保留有界尾部，包含结果与源缓存路径。

新外壳回归入口为 `python tools/hub-suite/test_msi_bootstrapper.py`。该测试只用合成子进程及只读 MSI API，不执行真实安装。真实发布验收还必须覆盖完整安装、重复安装、移除原 EXE/临时源后的标准修复、标准卸载、用户文件与配置保留、三程序启动和新旧实例共存。隔离验证可使用 `--quiet --install-dir <独立目录> --cache-dir <独立持久缓存根> --no-shortcuts --log <新日志文件>`；缓存隔离必须明确指定，不能假定修改 APPDATA 环境变量会覆盖 Windows KnownFolder。所有验收完成后才发布安装器和校验文件。
