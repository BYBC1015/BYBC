from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any


class BridgeTray:
    def __init__(
        self,
        *,
        icon_path: Path,
        title: str,
        show_window: Callable[[], None],
        start_bridge: Callable[[], None],
        stop_bridge: Callable[[], None],
        open_hub: Callable[[], None],
        exit_app: Callable[[], None],
    ):
        self.icon_path = icon_path
        self.title = title
        self.show_window = show_window
        self.start_bridge = start_bridge
        self.stop_bridge = stop_bridge
        self.open_hub = open_hub
        self.exit_app = exit_app
        self.icon: Any | None = None
        self.error: Exception | None = None
        self.is_running = False

    def start(self) -> bool:
        try:
            import pystray
            from PIL import Image
        except Exception as exc:
            self.error = exc
            return False

        try:
            image = Image.open(self.icon_path).convert("RGBA")
            menu = pystray.Menu(
                pystray.MenuItem("打开 Hub", self._call(self.open_hub), default=True),
                pystray.MenuItem("连接与设置", self._call(self.show_window)),
                pystray.MenuItem("启动后台工作", self._call(self.start_bridge)),
                pystray.MenuItem("暂停接收工作", self._call(self.stop_bridge)),
                pystray.MenuItem("安全退出", self._call(self.exit_app)),
            )
            self.icon = pystray.Icon("codex_bridge", image, self.title, menu)
            self.icon.run_detached()
            self.is_running = True
            return True
        except Exception as exc:
            self.error = exc
            self.icon = None
            self.is_running = False
            return False

    def stop(self) -> None:
        icon = self.icon
        self.icon = None
        self.is_running = False
        if icon is not None:
            icon.stop()

    @staticmethod
    def _call(callback: Callable[[], None]) -> Callable[[Any, Any], None]:
        def wrapped(_icon: Any, _item: Any) -> None:
            callback()

        return wrapped
