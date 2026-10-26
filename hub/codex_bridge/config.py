from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit


DEFAULT_VPN_NAME = "cmynetwork"
DEFAULT_VPN_HEALTH_URL = "https://chatgpt.com/"
DEFAULT_CMYNETWORK_EXE = Path(r"C:\Program Files\CMYNetwork\cmynetwork.exe")
DEFAULT_CODEX_EXEC_SANDBOX = "workspace-write"


def default_app_dir() -> Path:
    base = os.environ.get("APPDATA")
    if base:
        return Path(base) / "CodexBridge"
    return Path.home() / ".codex-bridge"


def default_config_path() -> Path:
    return default_app_dir() / "config.json"


def default_codex_cli_path() -> Path | None:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    candidate = Path(appdata) / "npm" / "codex.cmd"
    return candidate if candidate.exists() else None


def default_hermes_cli_path() -> Path | None:
    local_app_data = os.environ.get("LOCALAPPDATA")
    candidates: list[Path] = []
    if local_app_data:
        candidates.append(
            Path(local_app_data)
            / "hermes"
            / "hermes-agent"
            / "venv"
            / "Scripts"
            / "hermes.exe"
        )
        candidates.append(Path(local_app_data) / "hermes" / "bin" / "hermes.exe")
    found = shutil.which("hermes") or shutil.which("hermes.exe")
    if found:
        candidates.append(Path(found))
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def default_project_root() -> Path:
    return Path.home() / "Documents" / "CodexProjects"


@dataclass
class BridgeBinding:
    """One Hub device token bound to one local AI execution backend."""

    id: str
    token: str
    execution_backend: str = "codex"
    name: str = ""
    enabled: bool = True
    interval_seconds: int | None = None
    open_codex: bool | None = None
    auto_execute_codex: bool | None = None
    ack_mentions: bool | None = None
    ignore_existing_on_first_run: bool | None = None
    prompt_dir: Path | None = None
    state_path: Path | None = None
    codex_exec_output_dir: Path | None = None
    hermes_model: str | None = None
    hermes_provider: str | None = None
    hermes_toolsets: str | None = None
    hermes_timeout_seconds: int | None = None
    hermes_output_dir: Path | None = None
    backend_options: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.id = normalize_binding_id(self.id)
        self.token = str(self.token or "").strip()
        self.execution_backend = (
            str(self.execution_backend or "codex").strip().lower()
        )
        self.name = str(self.name or "").strip()
        self.enabled = bool(self.enabled)
        if self.interval_seconds is not None:
            self.interval_seconds = max(1, int(self.interval_seconds))
        self.prompt_dir = optional_path(self.prompt_dir)
        self.state_path = optional_path(self.state_path)
        self.codex_exec_output_dir = optional_path(self.codex_exec_output_dir)
        self.hermes_output_dir = optional_path(self.hermes_output_dir)
        if self.hermes_timeout_seconds is not None:
            self.hermes_timeout_seconds = max(30, int(self.hermes_timeout_seconds))
        self.backend_options = dict(self.backend_options or {})

    @classmethod
    def from_mapping(cls, data: dict[str, Any], index: int = 0) -> "BridgeBinding":
        backend = str(
            data.get("execution_backend") or data.get("backend") or "codex"
        )
        binding_id = str(data.get("id") or f"{backend}-{index + 1}")
        return cls(
            id=binding_id,
            name=str(data.get("name") or ""),
            token=str(data.get("token") or ""),
            execution_backend=backend,
            enabled=to_bool(data.get("enabled", True)),
            interval_seconds=optional_int(data, "interval_seconds"),
            open_codex=optional_bool(data, "open_codex"),
            auto_execute_codex=optional_bool(data, "auto_execute_codex"),
            ack_mentions=optional_bool(data, "ack_mentions"),
            ignore_existing_on_first_run=optional_bool(
                data, "ignore_existing_on_first_run"
            ),
            prompt_dir=optional_path(data.get("prompt_dir")),
            state_path=optional_path(data.get("state_path")),
            codex_exec_output_dir=optional_path(data.get("codex_exec_output_dir")),
            hermes_model=optional_text(data, "hermes_model"),
            hermes_provider=optional_text(data, "hermes_provider"),
            hermes_toolsets=optional_text(data, "hermes_toolsets"),
            hermes_timeout_seconds=optional_int(data, "hermes_timeout_seconds"),
            hermes_output_dir=optional_path(data.get("hermes_output_dir")),
            backend_options=dict(data.get("backend_options") or {}),
        )

    def to_mapping(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "name": self.name or self.execution_backend.title(),
            "enabled": self.enabled,
            "token": self.token,
            "execution_backend": self.execution_backend,
        }
        optional_values = {
            "interval_seconds": self.interval_seconds,
            "open_codex": self.open_codex,
            "auto_execute_codex": self.auto_execute_codex,
            "ack_mentions": self.ack_mentions,
            "ignore_existing_on_first_run": self.ignore_existing_on_first_run,
            "prompt_dir": self.prompt_dir,
            "state_path": self.state_path,
            "codex_exec_output_dir": self.codex_exec_output_dir,
            "hermes_model": self.hermes_model,
            "hermes_provider": self.hermes_provider,
            "hermes_toolsets": self.hermes_toolsets,
            "hermes_timeout_seconds": self.hermes_timeout_seconds,
            "hermes_output_dir": self.hermes_output_dir,
        }
        for key, value in optional_values.items():
            if value is not None:
                data[key] = str(value) if isinstance(value, Path) else value
        if self.backend_options:
            data["backend_options"] = self.backend_options
        return data


@dataclass
class BridgeConfig:
    hub: str
    token: str
    prompt_dir: Path
    state_path: Path
    inbox_url: str = ""
    projects: dict[str, Path] = field(default_factory=dict)
    project_root: Path | None = None
    auto_create_missing_projects: bool = True
    interval_seconds: int = 20
    hub_request_timeout_seconds: int = 12
    hub_get_retry_attempts: int = 3
    hub_retry_backoff_seconds: float = 1.0
    hub_source_ip: str = ""
    open_codex: bool = True
    ack_mentions: bool = True
    ignore_existing_on_first_run: bool = True
    codex_exe_path: Path | None = None
    auto_submit_codex_prompt: bool = False
    codex_submit_delay_seconds: float = 3.0
    codex_submit_hotkey: str = "enter"
    auto_execute_codex: bool = False
    codex_cli_path: Path | None = None
    codex_exec_sandbox: str = DEFAULT_CODEX_EXEC_SANDBOX
    codex_exec_timeout_seconds: int = 1800
    codex_exec_output_dir: Path | None = None
    codex_exec_ignore_user_config: bool = True
    execution_backend: str = "codex"
    hermes_cli_path: Path | None = None
    hermes_model: str = ""
    hermes_provider: str = ""
    hermes_toolsets: str = ""
    hermes_timeout_seconds: int = 1800
    hermes_output_dir: Path | None = None
    vpn_name: str = DEFAULT_VPN_NAME
    vpn_exe_path: Path | None = DEFAULT_CMYNETWORK_EXE
    vpn_health_url: str = DEFAULT_VPN_HEALTH_URL
    vpn_check_timeout_seconds: int = 8
    vpn_failure_threshold: int = 3
    vpn_recovery_cooldown_seconds: int = 300
    vpn_auto_recover: bool = False
    vpn_switch_command: str = ""
    bindings: list[BridgeBinding] = field(default_factory=list)
    binding_id: str = "default"
    binding_name: str = ""
    backend_options: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.hub = self.hub.rstrip("/")
        self.inbox_url = (self.inbox_url or default_inbox_url(self.hub)).rstrip("/")
        self.prompt_dir = Path(self.prompt_dir)
        self.state_path = Path(self.state_path)
        self.projects = {
            str(slug): Path(path) for slug, path in self.projects.items()
        }
        self.project_root = optional_path(self.project_root) or default_project_root()
        self.auto_create_missing_projects = bool(self.auto_create_missing_projects)
        self.hub_request_timeout_seconds = max(
            3, int(self.hub_request_timeout_seconds)
        )
        self.hub_get_retry_attempts = min(
            5, max(1, int(self.hub_get_retry_attempts))
        )
        self.hub_retry_backoff_seconds = max(
            0.0, float(self.hub_retry_backoff_seconds)
        )
        self.hub_source_ip = str(self.hub_source_ip or "").strip()
        self.codex_exe_path = optional_path(self.codex_exe_path)
        self.codex_submit_delay_seconds = max(
            0.0, float(self.codex_submit_delay_seconds)
        )
        self.codex_submit_hotkey = (self.codex_submit_hotkey or "enter").strip()
        self.codex_cli_path = optional_path(self.codex_cli_path)
        self.codex_exec_sandbox = (
            self.codex_exec_sandbox or DEFAULT_CODEX_EXEC_SANDBOX
        ).strip()
        self.codex_exec_output_dir = Path(
            self.codex_exec_output_dir or default_app_dir() / "exec-results"
        )
        self.execution_backend = (self.execution_backend or "codex").strip().lower()
        self.hermes_cli_path = optional_path(self.hermes_cli_path) or default_hermes_cli_path()
        self.hermes_model = (self.hermes_model or "").strip()
        self.hermes_provider = (self.hermes_provider or "").strip()
        self.hermes_toolsets = (self.hermes_toolsets or "").strip()
        self.hermes_timeout_seconds = max(30, int(self.hermes_timeout_seconds))
        self.hermes_output_dir = Path(
            self.hermes_output_dir or default_app_dir() / "hermes-results"
        )
        self.vpn_exe_path = optional_path(self.vpn_exe_path)
        self.vpn_name = (self.vpn_name or DEFAULT_VPN_NAME).strip()
        self.vpn_health_url = (
            self.vpn_health_url or DEFAULT_VPN_HEALTH_URL
        ).strip()
        normalized_bindings: list[BridgeBinding] = []
        used_ids: set[str] = set()
        for index, binding in enumerate(self.bindings):
            item = (
                binding
                if isinstance(binding, BridgeBinding)
                else BridgeBinding.from_mapping(dict(binding), index)
            )
            candidate = item.id
            suffix = 2
            while candidate in used_ids:
                candidate = f"{item.id}-{suffix}"
                suffix += 1
            item.id = candidate
            used_ids.add(candidate)
            normalized_bindings.append(item)
        self.bindings = normalized_bindings
        self.binding_id = normalize_binding_id(self.binding_id)
        self.binding_name = (self.binding_name or "").strip()
        self.backend_options = dict(self.backend_options or {})

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "BridgeConfig":
        app_dir = default_app_dir()
        binding_data = data.get("bindings") or []
        token = str(data.get("token", ""))
        if not binding_data:
            token = os.environ.get("CODEX_HUB_TOKEN") or token
        projects = data.get("projects") or {}
        return cls(
            hub=str(data.get("hub", "https://203.0.113.10/hub")),
            token=token,
            prompt_dir=Path(data.get("prompt_dir") or app_dir / "prompts"),
            state_path=Path(data.get("state_path") or app_dir / "state.json"),
            inbox_url=str(data.get("inbox_url") or ""),
            projects={str(k): Path(v) for k, v in projects.items()},
            project_root=optional_path(data.get("project_root"))
            or default_project_root(),
            auto_create_missing_projects=to_bool(
                data.get("auto_create_missing_projects", True)
            ),
            interval_seconds=int(data.get("interval_seconds", 20)),
            hub_request_timeout_seconds=int(
                data.get("hub_request_timeout_seconds", 12)
            ),
            hub_get_retry_attempts=int(data.get("hub_get_retry_attempts", 3)),
            hub_retry_backoff_seconds=float(
                data.get("hub_retry_backoff_seconds", 1.0)
            ),
            hub_source_ip=str(data.get("hub_source_ip") or ""),
            open_codex=bool(data.get("open_codex", True)),
            ack_mentions=bool(data.get("ack_mentions", True)),
            ignore_existing_on_first_run=bool(
                data.get("ignore_existing_on_first_run", True)
            ),
            codex_exe_path=optional_path(data.get("codex_exe_path")),
            auto_submit_codex_prompt=to_bool(
                data.get("auto_submit_codex_prompt", False)
            ),
            codex_submit_delay_seconds=float(
                data.get("codex_submit_delay_seconds", 3.0)
            ),
            codex_submit_hotkey=str(data.get("codex_submit_hotkey") or "enter"),
            auto_execute_codex=to_bool(data.get("auto_execute_codex", False)),
            codex_cli_path=optional_path(
                data.get("codex_cli_path") or default_codex_cli_path()
            ),
            codex_exec_sandbox=str(
                data.get("codex_exec_sandbox") or DEFAULT_CODEX_EXEC_SANDBOX
            ),
            codex_exec_timeout_seconds=int(
                data.get("codex_exec_timeout_seconds", 1800)
            ),
            codex_exec_output_dir=Path(
                data.get("codex_exec_output_dir")
                or app_dir / "exec-results"
            ),
            codex_exec_ignore_user_config=to_bool(
                data.get("codex_exec_ignore_user_config", True)
            ),
            execution_backend=str(data.get("execution_backend") or "codex"),
            hermes_cli_path=optional_path(
                data.get("hermes_cli_path") or default_hermes_cli_path()
            ),
            hermes_model=str(data.get("hermes_model") or ""),
            hermes_provider=str(data.get("hermes_provider") or ""),
            hermes_toolsets=str(data.get("hermes_toolsets") or ""),
            hermes_timeout_seconds=int(data.get("hermes_timeout_seconds", 1800)),
            hermes_output_dir=Path(
                data.get("hermes_output_dir") or app_dir / "hermes-results"
            ),
            vpn_name=str(data.get("vpn_name") or DEFAULT_VPN_NAME),
            vpn_exe_path=optional_path(
                data.get("vpn_exe_path") or DEFAULT_CMYNETWORK_EXE
            ),
            vpn_health_url=str(
                data.get("vpn_health_url") or DEFAULT_VPN_HEALTH_URL
            ),
            vpn_check_timeout_seconds=int(
                data.get("vpn_check_timeout_seconds", 8)
            ),
            vpn_failure_threshold=int(data.get("vpn_failure_threshold", 3)),
            vpn_recovery_cooldown_seconds=int(
                data.get("vpn_recovery_cooldown_seconds", 300)
            ),
            vpn_auto_recover=to_bool(data.get("vpn_auto_recover", False)),
            vpn_switch_command=str(data.get("vpn_switch_command") or ""),
            backend_options=dict(data.get("backend_options") or {}),
            bindings=[
                BridgeBinding.from_mapping(item, index)
                for index, item in enumerate(binding_data)
                if isinstance(item, dict)
            ],
        )

    def runtime_configs(self) -> list["BridgeConfig"]:
        """Expand the app config into isolated, single-binding runner configs."""
        if not self.bindings:
            return [self] if token_is_configured(self.token) else []

        configs: list[BridgeConfig] = []
        for binding in self.bindings:
            if not binding.enabled or not token_is_configured(binding.token):
                continue
            binding_root = default_app_dir() / "bindings" / binding.id
            configs.append(
                replace(
                    self,
                    token=binding.token,
                    execution_backend=binding.execution_backend,
                    interval_seconds=(
                        binding.interval_seconds
                        if binding.interval_seconds is not None
                        else self.interval_seconds
                    ),
                    open_codex=(
                        binding.open_codex
                        if binding.open_codex is not None
                        else self.open_codex
                    ),
                    auto_execute_codex=(
                        binding.auto_execute_codex
                        if binding.auto_execute_codex is not None
                        else self.auto_execute_codex
                    ),
                    ack_mentions=(
                        binding.ack_mentions
                        if binding.ack_mentions is not None
                        else self.ack_mentions
                    ),
                    ignore_existing_on_first_run=(
                        binding.ignore_existing_on_first_run
                        if binding.ignore_existing_on_first_run is not None
                        else self.ignore_existing_on_first_run
                    ),
                    prompt_dir=binding.prompt_dir or binding_root / "prompts",
                    state_path=binding.state_path or binding_root / "state.json",
                    codex_exec_output_dir=(
                        binding.codex_exec_output_dir
                        or binding_root / "codex-results"
                    ),
                    hermes_model=(
                        binding.hermes_model
                        if binding.hermes_model is not None
                        else self.hermes_model
                    ),
                    hermes_provider=(
                        binding.hermes_provider
                        if binding.hermes_provider is not None
                        else self.hermes_provider
                    ),
                    hermes_toolsets=(
                        binding.hermes_toolsets
                        if binding.hermes_toolsets is not None
                        else self.hermes_toolsets
                    ),
                    hermes_timeout_seconds=(
                        binding.hermes_timeout_seconds
                        if binding.hermes_timeout_seconds is not None
                        else self.hermes_timeout_seconds
                    ),
                    hermes_output_dir=(
                        binding.hermes_output_dir
                        or binding_root / "hermes-results"
                    ),
                    projects=dict(self.projects),
                    bindings=[],
                    binding_id=binding.id,
                    binding_name=binding.name or binding.execution_backend.title(),
                    backend_options=dict(binding.backend_options),
                )
            )
        return configs

    def to_mapping(self) -> dict[str, Any]:
        data = {
            "hub": self.hub,
            "inbox_url": self.inbox_url,
            "token": self.token,
            "interval_seconds": self.interval_seconds,
            "hub_request_timeout_seconds": self.hub_request_timeout_seconds,
            "hub_get_retry_attempts": self.hub_get_retry_attempts,
            "hub_retry_backoff_seconds": self.hub_retry_backoff_seconds,
            "hub_source_ip": self.hub_source_ip,
            "open_codex": self.open_codex,
            "ack_mentions": self.ack_mentions,
            "ignore_existing_on_first_run": self.ignore_existing_on_first_run,
            "prompt_dir": str(self.prompt_dir),
            "state_path": str(self.state_path),
            "projects": {slug: str(path) for slug, path in self.projects.items()},
            "project_root": str(self.project_root or ""),
            "auto_create_missing_projects": self.auto_create_missing_projects,
            "codex_exe_path": str(self.codex_exe_path or ""),
            "auto_submit_codex_prompt": self.auto_submit_codex_prompt,
            "codex_submit_delay_seconds": self.codex_submit_delay_seconds,
            "codex_submit_hotkey": self.codex_submit_hotkey,
            "auto_execute_codex": self.auto_execute_codex,
            "codex_cli_path": str(self.codex_cli_path or ""),
            "codex_exec_sandbox": self.codex_exec_sandbox,
            "codex_exec_timeout_seconds": self.codex_exec_timeout_seconds,
            "codex_exec_output_dir": str(self.codex_exec_output_dir or ""),
            "codex_exec_ignore_user_config": self.codex_exec_ignore_user_config,
            "execution_backend": self.execution_backend,
            "hermes_cli_path": str(self.hermes_cli_path or ""),
            "hermes_model": self.hermes_model,
            "hermes_provider": self.hermes_provider,
            "hermes_toolsets": self.hermes_toolsets,
            "hermes_timeout_seconds": self.hermes_timeout_seconds,
            "hermes_output_dir": str(self.hermes_output_dir or ""),
            "vpn_name": self.vpn_name,
            "vpn_exe_path": str(self.vpn_exe_path or ""),
            "vpn_health_url": self.vpn_health_url,
            "vpn_check_timeout_seconds": self.vpn_check_timeout_seconds,
            "vpn_failure_threshold": self.vpn_failure_threshold,
            "vpn_recovery_cooldown_seconds": self.vpn_recovery_cooldown_seconds,
            "vpn_auto_recover": self.vpn_auto_recover,
            "vpn_switch_command": self.vpn_switch_command,
        }
        if self.bindings:
            data["bindings"] = [binding.to_mapping() for binding in self.bindings]
        if self.backend_options:
            data["backend_options"] = dict(self.backend_options)
        return data


def load_config(path: Path | None = None) -> BridgeConfig:
    config_path = path or default_config_path()
    raw = config_path.read_text(encoding="utf-8-sig")
    return BridgeConfig.from_mapping(json.loads(raw))


def sample_config() -> dict[str, Any]:
    app_dir = default_app_dir()
    return {
        "hub": "https://203.0.113.10/hub",
        "inbox_url": "https://203.0.113.10/inbox/messages",
        "token": "PASTE_DEVICE_TOKEN_HERE",
        "interval_seconds": 20,
        "hub_request_timeout_seconds": 12,
        "hub_get_retry_attempts": 3,
        "hub_retry_backoff_seconds": 1.0,
        "hub_source_ip": "",
        "open_codex": True,
        "ack_mentions": True,
        "ignore_existing_on_first_run": True,
        "prompt_dir": str(app_dir / "prompts"),
        "state_path": str(app_dir / "state.json"),
        "projects": {
            "private-server-buildout": str(
                Path.home() / "Documents" / "PrivateServer"
            )
        },
        "project_root": str(default_project_root()),
        "auto_create_missing_projects": True,
        "codex_exe_path": "",
        "auto_submit_codex_prompt": False,
        "codex_submit_delay_seconds": 3.0,
        "codex_submit_hotkey": "enter",
        "auto_execute_codex": False,
        "codex_cli_path": str(default_codex_cli_path() or ""),
        "codex_exec_sandbox": DEFAULT_CODEX_EXEC_SANDBOX,
        "codex_exec_timeout_seconds": 1800,
        "codex_exec_output_dir": str(app_dir / "exec-results"),
        "codex_exec_ignore_user_config": True,
        "execution_backend": "codex",
        "hermes_cli_path": str(default_hermes_cli_path() or ""),
        "hermes_model": "",
        "hermes_provider": "",
        "hermes_toolsets": "",
        "hermes_timeout_seconds": 1800,
        "hermes_output_dir": str(app_dir / "hermes-results"),
        "vpn_name": DEFAULT_VPN_NAME,
        "vpn_exe_path": str(DEFAULT_CMYNETWORK_EXE),
        "vpn_health_url": DEFAULT_VPN_HEALTH_URL,
        "vpn_check_timeout_seconds": 8,
        "vpn_failure_threshold": 3,
        "vpn_recovery_cooldown_seconds": 300,
        "vpn_auto_recover": False,
        "vpn_switch_command": "",
    }


def write_sample_config(path: Path | None = None, overwrite: bool = False) -> Path:
    config_path = path or default_config_path()
    if config_path.exists() and not overwrite:
        return config_path
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps(sample_config(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return config_path


def save_config(config: BridgeConfig, path: Path | None = None) -> Path:
    config_path = path or default_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps(config.to_mapping(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return config_path


def update_local_tool_settings(
    config: BridgeConfig,
    *,
    codex_exe_path: Any,
    auto_submit_codex_prompt: bool,
    codex_submit_delay_seconds: float,
    codex_submit_hotkey: str,
    auto_execute_codex: bool,
    codex_cli_path: Any,
    codex_exec_sandbox: str,
    codex_exec_timeout_seconds: int,
    vpn_exe_path: Any,
    vpn_health_url: str,
    vpn_auto_recover: bool,
    vpn_switch_command: str,
) -> BridgeConfig:
    config.codex_exe_path = optional_path(codex_exe_path)
    config.auto_submit_codex_prompt = bool(auto_submit_codex_prompt)
    config.codex_submit_delay_seconds = max(0.0, float(codex_submit_delay_seconds))
    config.codex_submit_hotkey = (codex_submit_hotkey or "enter").strip()
    config.auto_execute_codex = bool(auto_execute_codex)
    config.codex_cli_path = optional_path(codex_cli_path)
    config.codex_exec_sandbox = (
        codex_exec_sandbox or DEFAULT_CODEX_EXEC_SANDBOX
    ).strip()
    config.codex_exec_timeout_seconds = max(30, int(codex_exec_timeout_seconds))
    config.vpn_name = DEFAULT_VPN_NAME
    config.vpn_exe_path = optional_path(vpn_exe_path)
    config.vpn_health_url = (vpn_health_url or DEFAULT_VPN_HEALTH_URL).strip()
    config.vpn_auto_recover = bool(vpn_auto_recover)
    config.vpn_switch_command = (vpn_switch_command or "").strip()
    return config


def find_binding(config: BridgeConfig, backend: str) -> BridgeBinding | None:
    target = str(backend or "").strip().lower()
    return next(
        (
            binding
            for binding in config.bindings
            if binding.execution_backend == target
        ),
        None,
    )


def update_binding_runtime_settings(
    config: BridgeConfig,
    backend: str,
    *,
    enabled: bool,
    interval_seconds: int,
    open_codex: bool,
    auto_execute_codex: bool,
) -> BridgeConfig:
    binding = find_binding(config, backend)
    if binding is not None:
        binding.enabled = bool(enabled)
        binding.interval_seconds = max(1, int(interval_seconds))
        binding.open_codex = bool(open_codex)
        binding.auto_execute_codex = bool(auto_execute_codex)
    elif config.execution_backend == str(backend).strip().lower():
        config.interval_seconds = max(1, int(interval_seconds))
        config.open_codex = bool(open_codex)
        config.auto_execute_codex = bool(auto_execute_codex)
    return config


def update_hermes_tool_settings(
    config: BridgeConfig,
    *,
    hermes_cli_path: Any,
    hermes_model: str,
    hermes_provider: str,
    hermes_toolsets: str,
    hermes_timeout_seconds: int,
) -> BridgeConfig:
    config.hermes_cli_path = optional_path(hermes_cli_path)
    config.hermes_model = str(hermes_model or "").strip()
    config.hermes_provider = str(hermes_provider or "").strip()
    config.hermes_toolsets = str(hermes_toolsets or "").strip()
    config.hermes_timeout_seconds = max(30, int(hermes_timeout_seconds))
    binding = find_binding(config, "hermes")
    if binding is not None:
        binding.hermes_model = config.hermes_model
        binding.hermes_provider = config.hermes_provider
        binding.hermes_toolsets = config.hermes_toolsets
        binding.hermes_timeout_seconds = config.hermes_timeout_seconds
    return config


def update_hub_client_settings(
    config: BridgeConfig,
    *,
    hub: str,
    timeout_seconds: int,
    get_retry_attempts: int,
    retry_backoff_seconds: float,
    inbox_url: str | None = None,
) -> BridgeConfig:
    value = str(hub or "").strip().rstrip("/")
    if not value.startswith(("http://", "https://")):
        raise ValueError("Hub 地址必须以 http:// 或 https:// 开头。")
    config.hub = value
    if inbox_url is not None:
        inbox_value = str(inbox_url or "").strip().rstrip("/")
        if not inbox_value.startswith(("http://", "https://")):
            raise ValueError("邮箱地址必须以 http:// 或 https:// 开头。")
        config.inbox_url = inbox_value
    config.hub_request_timeout_seconds = max(3, int(timeout_seconds))
    config.hub_get_retry_attempts = min(5, max(1, int(get_retry_attempts)))
    config.hub_retry_backoff_seconds = max(0.0, float(retry_backoff_seconds))
    return config


def hub_workspace_url(hub: str) -> str:
    parts = urlsplit(str(hub or "").strip())
    path = parts.path.rstrip("/")
    if not path.endswith("/projects"):
        path = f"{path}/projects"
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def default_inbox_url(hub: str) -> str:
    parts = urlsplit(str(hub or "").strip())
    return urlunsplit((parts.scheme, parts.netloc, "/inbox/messages", "", ""))


def optional_path(value: Any) -> Path | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return Path(text)


def optional_bool(data: dict[str, Any], key: str) -> bool | None:
    if key not in data or data[key] is None:
        return None
    return to_bool(data[key])


def optional_int(data: dict[str, Any], key: str) -> int | None:
    if key not in data or data[key] is None or str(data[key]).strip() == "":
        return None
    return int(data[key])


def optional_text(data: dict[str, Any], key: str) -> str | None:
    if key not in data or data[key] is None:
        return None
    return str(data[key]).strip()


def normalize_binding_id(value: Any) -> str:
    normalized = re.sub(r"[^a-z0-9_-]+", "-", str(value or "binding").lower())
    return normalized.strip("-_") or "binding"


def token_is_configured(token: str) -> bool:
    value = str(token or "").strip()
    return bool(value and value != "PASTE_DEVICE_TOKEN_HERE")


def to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)
