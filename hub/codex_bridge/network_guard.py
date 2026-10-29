from __future__ import annotations

import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from .subprocess_utils import hidden_subprocess_kwargs


@dataclass
class ProbeResult:
    ok: bool
    detail: str = ""


@dataclass
class NetworkGuardResult:
    ok: bool
    action: str
    message: str
    failure_count: int = 0


class HealthProbe(Protocol):
    def check(self, url: str, timeout_seconds: int) -> ProbeResult: ...


class ProcessProbe(Protocol):
    def is_running(self, process_name: str) -> bool: ...


class ActionRunner(Protocol):
    def start_exe(self, path: Path) -> None: ...

    def run_command(self, command: str) -> None: ...


class UrlHealthProbe:
    def check(self, url: str, timeout_seconds: int) -> ProbeResult:
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 CodexBridge/1.0",
                "Accept": "text/html,application/xhtml+xml",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                return ProbeResult(ok=True, detail=f"HTTP {response.status}")
        except urllib.error.HTTPError as exc:
            return ProbeResult(ok=True, detail=f"HTTP {exc.code}")
        except Exception as exc:
            return ProbeResult(ok=False, detail=str(exc))


class WindowsProcessProbe:
    def is_running(self, process_name: str) -> bool:
        if not sys.platform.startswith("win"):
            return False
        image_name = process_name
        if not image_name.lower().endswith(".exe"):
            image_name = f"{image_name}.exe"
        try:
            completed = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq {image_name}"],
                capture_output=True,
                text=True,
                encoding="utf-8", errors="replace",
                timeout=5,
                check=False,
                **hidden_subprocess_kwargs(),
            )
        except Exception:
            return False
        return image_name.lower() in completed.stdout.lower()


class SubprocessActionRunner:
    def start_exe(self, path: Path) -> None:
        subprocess.Popen(
            [str(path)], cwd=str(path.parent), **hidden_subprocess_kwargs()
        )

    def run_command(self, command: str) -> None:
        subprocess.Popen(command, shell=True, **hidden_subprocess_kwargs())


class NetworkGuard:
    def __init__(
        self,
        config: Any,
        health_probe: HealthProbe | None = None,
        process_probe: ProcessProbe | None = None,
        action_runner: ActionRunner | None = None,
        now: Callable[[], float] | None = None,
    ):
        self.config = config
        self.health_probe = health_probe or UrlHealthProbe()
        self.process_probe = process_probe or WindowsProcessProbe()
        self.action_runner = action_runner or SubprocessActionRunner()
        self.now = now or time.time
        self.failure_count = 0
        self.last_action_at = 0.0

    def check(self) -> NetworkGuardResult:
        health_url = str(getattr(self.config, "vpn_health_url", "https://chatgpt.com/"))
        timeout_seconds = int(getattr(self.config, "vpn_check_timeout_seconds", 8))
        vpn_name = str(getattr(self.config, "vpn_name", "cmynetwork") or "cmynetwork")
        process_running = self.process_probe.is_running(vpn_name)
        probe = self.health_probe.check(health_url, timeout_seconds)

        if probe.ok:
            self.failure_count = 0
            state = "running" if process_running else "not running"
            return NetworkGuardResult(
                ok=True,
                action="none",
                message=f"{vpn_name} 连通正常（{probe.detail}；进程 {state}）。",
                failure_count=0,
            )

        self.failure_count += 1
        threshold = max(1, int(getattr(self.config, "vpn_failure_threshold", 3)))
        if self.failure_count < threshold:
            return NetworkGuardResult(
                ok=False,
                action="none",
                message=(
                    f"{vpn_name} ChatGPT 连通测试失败 "
                    f"{self.failure_count}/{threshold}：{probe.detail}"
                ),
                failure_count=self.failure_count,
            )

        auto_recover = bool(getattr(self.config, "vpn_auto_recover", False))
        if not auto_recover:
            return NetworkGuardResult(
                ok=False,
                action="notify",
                message=(
                    f"{vpn_name} 可能已断开。ChatGPT 无法访问；"
                    "自动恢复已关闭。"
                ),
                failure_count=self.failure_count,
            )

        cooldown = max(
            1, int(getattr(self.config, "vpn_recovery_cooldown_seconds", 300))
        )
        current = self.now()
        if self.last_action_at and current - self.last_action_at < cooldown:
            return NetworkGuardResult(
                ok=False,
                action="cooldown",
                message=f"{vpn_name} 自动恢复冷却中。",
                failure_count=self.failure_count,
            )

        switch_command = str(getattr(self.config, "vpn_switch_command", "") or "").strip()
        if switch_command:
            self.action_runner.run_command(switch_command)
            self.last_action_at = current
            return NetworkGuardResult(
                ok=False,
                action="switch_command",
                message=f"{vpn_name} 切换命令已启动。",
                failure_count=self.failure_count,
            )

        vpn_exe_path = getattr(self.config, "vpn_exe_path", None)
        if vpn_exe_path and Path(vpn_exe_path).exists():
            self.action_runner.start_exe(Path(vpn_exe_path))
            self.last_action_at = current
            return NetworkGuardResult(
                ok=False,
                action="start_vpn",
                message=f"{vpn_name} 启动命令已发出。",
                failure_count=self.failure_count,
            )

        return NetworkGuardResult(
            ok=False,
            action="missing_vpn_path",
            message=f"{vpn_name} 程序路径缺失。",
            failure_count=self.failure_count,
        )


def find_default_cmynetwork_path() -> Path | None:
    candidates = [
        Path(r"C:\Program Files\CMYNetwork\cmynetwork.exe"),
        Path(r"C:\Program Files (x86)\CMYNetwork\cmynetwork.exe"),
    ]
    for env_name in ("LOCALAPPDATA", "APPDATA"):
        base = Path.home()
        try:
            import os

            if os.environ.get(env_name):
                base = Path(os.environ[env_name])
        except Exception:
            pass
        candidates.append(base / "Programs" / "CMYNetwork" / "cmynetwork.exe")
        candidates.append(base / "CMYNetwork" / "cmynetwork.exe")

    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None
