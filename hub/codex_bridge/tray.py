from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import threading
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
        open_toolbox: Callable[[], None],
        exit_app: Callable[[], None],
    ):
        self.icon_path = icon_path
        self.title = title
        self.show_window = show_window
        self.start_bridge = start_bridge
        self.stop_bridge = stop_bridge
        self.open_hub = open_hub
        self.open_toolbox = open_toolbox
        self.exit_app = exit_app
        self.icon: Any | None = None
        self.error: Exception | None = None
        self.is_running = False

    def start(self, *, wait_until_ready: float = 0) -> bool:
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
                pystray.MenuItem("工具箱", self._call(self.open_toolbox)),
                pystray.MenuItem("连接与设置", self._call(self.show_window)),
                pystray.MenuItem("启动后台工作", self._call(self.start_bridge)),
                pystray.MenuItem("暂停接收工作", self._call(self.stop_bridge)),
                pystray.MenuItem("安全退出", self._call(self.exit_app)),
            )
            self.icon = pystray.Icon("codex_bridge", image, self.title, menu)
            if wait_until_ready > 0:
                ready = threading.Event()
                errors = []
                pending_icon = self.icon
                def setup(icon):
                    # A late setup after a failed/closed tray cannot resurrect
                    # an inaccessible hidden application.
                    if self.icon is not icon:
                        icon.stop()
                        return
                    try:
                        icon.visible = True
                        if self.icon is not icon:
                            icon.visible = False
                            return
                    except Exception as exc:
                        errors.append(exc)
                    finally:
                        ready.set()
                def launch():
                    # pystray creates its setup/native threads here. Let them
                    # inherit daemon mode so failed native initialization cannot
                    # keep a background request alive after its UI has exited.
                    if self.icon is not pending_icon:
                        return
                    try:
                        pending_icon.run_detached(setup=setup)
                    except Exception as exc:
                        errors.append(exc)
                        ready.set()
                threading.Thread(target=launch, name='hub-background-tray', daemon=True).start()
                if not ready.wait(wait_until_ready):
                    raise RuntimeError('系统托盘未能及时就绪。')
                if errors:
                    raise errors[0]
            else:
                self.icon.run_detached()
            self.is_running = True
            return True
        except Exception as exc:
            self.error = exc
            icon = self.icon
            self.icon = None
            self.is_running = False
            if icon is not None:
                try:
                    icon.stop()
                except Exception:
                    pass
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
