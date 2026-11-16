# Windows / worker 1.3.0

与 Web 2.13.0 配套：打包当前设置面板、短动效与旧缓存迁移；更新任务池异常隔离、严格完成/附件回执、API 模型执行器与独立只读连接探测。

升级保留原用户配置、未完成回执和本地项目绑定。运行中的任务须收尾后切换后台，不因更新重放结果未知的任务。可见窗口正在使用时不强制关闭，安装包发布与安装实例完成更新应分别记录。

新增模型服务只在上报 `model_services: true` 的执行连接中可选；旧 worker 继续原 Codex 路径。API 模型没有本地 shell、桌面或 MCP 工程权限，项目落地工作继续使用 Codex CLI。

软件包验证需检查三个冻结核心、全部打包界面资源、新装/重复安装/旧版升级/卸载与用户数据保留，并保留证据。发布与平台下载哈希另行核验。

## Windows 运行库要求

本版面向 Windows 10/11 x64，使用系统提供的 API-set、通用 C 运行库和系统归档工具。构建时仅从指定的程序运行环境中排除经过审计的系统依赖；其他第三方 DLL 继续保留。MFC 是原本已存在的外部系统依赖，本轮补充其前置检查，并非从原包新增移除。

安装器在提取前读取真实 Windows 版本、原生架构，以及固定 System32 目录中的 `VCRUNTIME140.dll`、`VCRUNTIME140_1.dll`、`mfc140u.dll`，检查文件版本、x64 PE 架构和本版实际引用的接口。检查不搜索 PATH、不加载候选 DLL，也不安装或修改系统组件。缺少或不满足要求时会停止并提供[微软官方运行库页面](https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist)和[微软 v14 x64 下载入口](https://aka.ms/vc14/vc_redist.x64.exe)，由用户安装或修复后重试。

文件版本下限 `14.42.34438.0` 来自本轮收集的 VC 文件，并结合实际接口清单使用；它不是对所有组件工具链兼容性的充分保证。微软建议使用最新受支持的 v14 运行库，并要求运行库不早于构建组件所用的工具链，见[官方兼容说明](https://learn.microsoft.com/en-us/cpp/porting/binary-compat-2015-2017?view=msvc-170)。本版接受满足检查条件的更新运行库，不固定要求 14.50。本轮完整包验收使用 x64 VC/MFC 14.50 环境，各项结果以发布验收记录为准；不能据此声称所有 Windows 10/11 版本均经过实机验证。前置检查通过不代替安装、升级、卸载和功能验收。

## Windows 发布构建入口

1.3.0 的默认 Windows 发布格式是 **directory runtime + native installer**。三个程序使用 PyInstaller `onedir`，安装与卸载外层使用系统 .NET Framework 编译的启动器。旧 `onefile` 提取路径在本轮机器上出现启动前失败，不再作为本版 Windows 发布入口。

从仓库根目录执行以下命令。`D:/HubBuild` 仅为示例构建位置；使用新的独立子目录。公开工具压缩包须来自受固定校验值约束的发行文件，不能从现用安装目录或用户配置复制。

```powershell
uv run --frozen --with pyinstaller --with pillow --with pystray --with 'pywinauto>=0.6.9' --with 'pywebview==6.2.1' python tools/hub-suite/build_directory_release.py --stage all --stage-root D:/HubBuild/package-v1.3.0

uv run --frozen --with pyinstaller --with pillow --with pystray --with 'pywinauto>=0.6.9' --with 'pywebview==6.2.1' python tools/hub-suite/build_desktop.py --prepare-only --staging-parent D:/HubBuild --stage-root D:/HubBuild/package-v1.3.0 --video-archive <公开视频工具ZIP> --kling-archive <公开可灵工具ZIP>

uv run --frozen --with pyinstaller --with pillow --with pystray --with 'pywinauto>=0.6.9' --with 'pywebview==6.2.1' python tools/hub-suite/build_desktop.py --package-prepared --staging-parent D:/HubBuild --stage-root D:/HubBuild/package-v1.3.0 --binaries-dir D:/HubBuild/package-v1.3.0/directory-dist --installer-core D:/HubBuild/package-v1.3.0/directory-dist/InstallerCore
```

`NativeInstallerWrapper.cs`、`WindowsRuntimePrerequisites.cs`、`windows-runtime-prerequisites.json`、`build_native_wrapper.py` 与 `native_installer_main.py` 都是发布必需源码或数据。启动器先检查系统依赖，再按内嵌清单校验归档，用固定系统路径的归档工具解出目录运行环境，重新核对完整文件集及哈希，转发参数、传递退出码并清理自己的临时目录；实际安装、升级、回退、配置保留和卸载继续调用原 `installer.py`。卸载时复制完整外层启动器，不能单独复制目录运行环境里的 `InstallerCore.exe`。编译中间 ZIP、清单、C# 元数据和构建报告保存在安装载荷之外，只将最终 `Uninstall.exe` 放入产品清单。

安装器完成文件校验后，先原子保存安装提交记录和本次回执，再清理旧程序。外层必须同时核对本次随机编号、操作、目标与安装记录，不能仅因子进程退出码为 0 就报告成功。新版已提交但旧程序清理中断时，明确提示清理未完成并保留残余；卸载进程已启动与文件已移除也分别记录。

外壳集成回归入口为 `python tools/hub-suite/test_native_wrapper.py`（Windows）。这组测试使用合成子程序，不能替代真实安装、旧版升级、卸载及三个目录程序的启动验收。发布前必须另外核对全部安装文件的字节数与 SHA-256、用户文件和本机偏好保留、静默模式及隐藏窗口验证；这些验收完成后再发布 EXE 和校验文件。
