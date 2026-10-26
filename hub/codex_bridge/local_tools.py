"""Launch the applications shipped in the Hub Windows installer."""
from __future__ import annotations

import os
import json
import subprocess
import sys
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import messagebox, ttk


@dataclass(frozen=True)
class LocalTool:
    key: str
    name: str
    version: str
    relative_exe: str
    description: str


TOOLS = (
    LocalTool("video-freeze", "视频末帧延长", "1.1.1",
              "tools/video-freeze/BYBC视频末帧延长工具.exe",
              "拖入视频或文件夹预览名称、时长，点击开始后延长末帧。默认保存原目录，原名后加 _bybc，超时再加 _X。"),
    LocalTool("kling-batch", "可灵批处理", "1.2.1",
              "tools/kling-batch/可灵批处理.exe",
              "场景替换、净背景与全链路。抽帧检查、逐镜头返修、版本交付和达芬奇入轨；关闭后驻留托盘。"),
    LocalTool("beat-workshop", "音乐制作 · 节拍工坊", "0.4.0",
              "tools/beat-workshop/main.js",
              "鼓机、乐器伴奏与自然环境声。音频保存到下载目录。需安装达芬奇 Studio 21.1。"),
    LocalTool("color-workshop", "达芬奇调色 · 节点预设", "1.0.1",
              "tools/color-workshop/main.js",
              "六种风格与八节点模板，一键应用并保留原版本。支持强度调整、配方迁移与设备检测。"),
)


def install_root() -> Path:
    # A one-file PyInstaller application's _MEIPASS is temporary. Companions
    # deliberately live next to the installed EXE, never in that temp folder.
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1] / "build" / "hub-suite" / "payload"


def tool_path(key: str, root: Path | None = None) -> Path:
    tool = next((item for item in TOOLS if item.key == key), None)
    if tool is None:
        raise ValueError(f"未知工具：{key}")
    return (root or install_root()) / tool.relative_exe


def launch_tool(key: str, root: Path | None = None) -> subprocess.Popen:
    path = tool_path(key, root)
    if not path.is_file():
        raise FileNotFoundError("工具文件缺失，请重新安装 Codex Bridge。\n" + str(path))
    env = os.environ.copy()
    # Independent frozen applications must initialize their own unpacked runtime.
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    if key == "color-workshop":
        resolve = find_resolve_root()
        env["HUB_RESOLVE_ROOT"] = str(resolve)
        env.pop("HUB_RESOLVE_OVERRIDE", None)
        env.pop("ELECTRON_RUN_AS_NODE", None)
        return subprocess.Popen([str(resolve / "Electron/electron.exe"), str(path.parent)],
                                cwd=str(path.parent), env=env, shell=False)
    if key == "beat-workshop":
        resolve = find_resolve_root()
        runtime = path.parent / "runtime.json"
        content = json.dumps({"resolveRoot": str(resolve)}, ensure_ascii=False)
        if not runtime.exists() or runtime.read_text(encoding="utf-8") != content:
            runtime.write_text(content, encoding="utf-8")
        env.pop("ELECTRON_RUN_AS_NODE", None)
        return subprocess.Popen([str(resolve / "Electron/electron.exe"), str(path.parent)],
                                cwd=str(path.parent), env=env, shell=False)
    return subprocess.Popen([str(path)], cwd=str(path.parent), env=env, shell=False)


def find_resolve_root() -> Path:
    import winreg
    candidates = []
    device = Path(os.environ.get("APPDATA", str(Path.home()))) / "HubColorWorkshop/device.json"
    if device.is_file():
        try:
            saved = json.loads(device.read_text(encoding="utf-8"))
            if isinstance(saved,dict) and isinstance(saved.get("resolveRoot"), str):
                candidates.append(Path(saved["resolveRoot"]))
        except (OSError, ValueError):
            pass
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            with winreg.OpenKey(hive, r"SOFTWARE\Blackmagic Design\DaVinci Resolve") as key:
                candidates.append(Path(winreg.QueryValueEx(key, "InstallLocation")[0]))
        except OSError:
            pass
    for drive in (os.environ.get("ProgramFiles", "C:/Program Files"), "D:/Program Files"):
        candidates.append(Path(drive) / "Blackmagic Design/DaVinci Resolve")
    for root in candidates:
        if all((root / file).is_file() for file in ("Resolve.exe", "Electron/electron.exe", "ResolvePython/ResolvePython.exe")):
            return root
    raise FileNotFoundError("此工具需要达芬奇 Studio 21.1 的本地运行环境。请先安装达芬奇；调色工具可通过 launch.ps1 -ResolveRoot 指定自定义安装目录。")


def launch_toolbox(config_path: Path) -> subprocess.Popen:
    command = ([sys.executable] if getattr(sys, "frozen", False) else
               [sys.executable, "-m", "codex_bridge.cli"])
    env = os.environ.copy()
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    return subprocess.Popen(command + ["--tools", "--config", str(config_path)], env=env, shell=False)


def populate_tools(parent: ttk.Frame) -> dict[str, ttk.Button]:
    parent.columnconfigure(0, weight=1)
    parent.columnconfigure(1, weight=1)
    ttk.Label(parent, text="本地工具", font=("Segoe UI", 17, "bold")).grid(
        row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))
    ttk.Label(parent, text="安装一次，在这里打开。每个工具使用独立窗口。",
              wraplength=650).grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 16))
    buttons = {}
    for index, tool in enumerate(TOOLS):
        card = ttk.LabelFrame(parent, text=f"{tool.name}  ·  {tool.version}", padding=12)
        card.grid(row=2+index//2, column=index%2, sticky="nsew", padx=(0, 10), pady=(0, 12))
        card.columnconfigure(0, weight=1)
        ttk.Label(card, text=tool.description, wraplength=275).grid(row=0, column=0, sticky="nw")
        card.rowconfigure(0, weight=1)

        def launch(key=tool.key):
            try:
                launch_tool(key)
            except OSError as exc:
                messagebox.showerror("无法打开工具", str(exc), parent=parent)

        buttons[tool.key] = ttk.Button(card, text="打开工具", command=launch)
        buttons[tool.key].grid(row=1, column=0, sticky="w", pady=(10, 0))
    ttk.Label(parent, text="可灵首次使用需配置 API 凭据。安装和打开窗口不会自动提交生成。",
              wraplength=650).grid(row=2+(len(TOOLS)+1)//2, column=0, columnspan=2, sticky="w", pady=(6, 0))
    return buttons


def main(config_path: Path | None = None) -> int:
    from . import __version__
    from .gui import set_window_icon

    root = tk.Tk()
    root.title(f"Codex Bridge 附属工具箱 v{__version__}")
    root.geometry("840x540")
    root.minsize(720, 490)
    set_window_icon(root)
    content = ttk.Frame(root, padding=20)
    content.pack(fill="both", expand=True)
    populate_tools(content)
    root.mainloop()
    return 0
