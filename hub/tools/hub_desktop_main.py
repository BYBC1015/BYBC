from multiprocessing import freeze_support
from codex_bridge.desktop_window import main


if __name__ == '__main__':
    freeze_support()
    raise SystemExit(main())
