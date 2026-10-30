from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from .config import default_config_path, load_config, write_sample_config
from .controller import BridgeController
from .gui import main as gui_main
from .runner import BridgeRunner
from .single_instance import SingleInstance, notify_already_running


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="CodexBridge",
        description="Local Codex Hub bridge for opening assigned Hub work in Codex.",
    )
    parser.add_argument("--config", type=Path, default=default_config_path())
    parser.add_argument("--init", action="store_true", help="Create a sample config.")
    parser.add_argument("--once", action="store_true", help="Poll once and exit.")
    parser.add_argument("--ui", action="store_true", help="Open the local UI.")
    parser.add_argument("--tools", action="store_true", help="Open the bundled Hub toolbox.")
    parser.add_argument("--cli", action="store_true", help="Run the console poller.")
    parser.add_argument("--background", action="store_true", help="Start in the system tray without opening windows.")
    parser.add_argument(
        "--start",
        action="store_true",
        help="Open the local UI and start polling immediately.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite config when used with --init.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.background and (args.init or args.once or args.cli or args.ui or args.tools):
        parser.error('--background cannot be combined with --init, --once, --cli, --ui or --tools')
    if args.tools:
        from .local_tools import main as tools_main
        return tools_main(args.config)
    if args.init:
        path = write_sample_config(args.config, overwrite=args.overwrite)
        print(f"Config ready: {path}")
        return 0

    instance = SingleInstance()
    if not instance.acquire():
        if not (args.start or args.once or args.cli or args.ui or args.background):
            from .desktop_window import launch_desktop
            try:
                if launch_desktop(args.config):
                    return 0
            except (OSError, RuntimeError):
                pass
        if not (args.start or args.background):
            notify_already_running()
        return 0
    try:
        return _run(args)
    finally:
        instance.close()


def _run(args: argparse.Namespace) -> int:

    if args.ui or args.start or not (args.once or args.cli):
        if args.background:
            return gui_main(args.config, auto_start=args.start, background=True)
        return gui_main(args.config, auto_start=args.start)

    if not args.config.exists():
        path = write_sample_config(args.config, overwrite=False)
        print(f"Config created: {path}")
        print("Edit token and project paths, then start CodexBridge again.")
        return 2

    config = load_config(args.config)
    runtime_configs = config.runtime_configs()
    if not runtime_configs:
        print("No enabled AI binding has a valid device token.")
        return 2

    try:
        controller = BridgeController(
            config,
            runner_factory=BridgeRunner,
            log_callback=print,
        )
    except Exception as exc:
        print(f"Unable to initialize AI bindings: {exc}", file=sys.stderr)
        return 2

    print("CodexBridge multi-AI service started.")
    print(f"Hub: {config.hub}")
    print(f"Config: {args.config}")
    print(f"AI bindings: {len(runtime_configs)}")
    print("Press Ctrl+C to stop.")

    if args.once:
        controller.run_all_once_safely()
        return 1 if controller.latest_errors else 0

    controller.start()
    try:
        while controller.is_running:
            time.sleep(0.5)
    except KeyboardInterrupt:
        controller.stop()
        print("Stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
