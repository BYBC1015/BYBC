"""Onedir installer entry; preserve complete outer-launcher self-copy on uninstall."""
from __future__ import annotations
import os
from pathlib import Path
import sys
import tempfile
import time
import traceback
import re
import installer

def validate_receipt_contract():
    receipt = Path(os.environ.get('CODEX_HUB_INSTALLER_RECEIPT_PATH', ''))
    nonce = os.environ.get('CODEX_HUB_INSTALLER_NONCE', '')
    operation = os.environ.get('CODEX_HUB_INSTALLER_OPERATION', '')
    target = os.environ.get('CODEX_HUB_INSTALLER_TARGET', '')
    root = Path(getattr(sys, '_MEIPASS', '')).parent
    if (not re.fullmatch(r'[a-f0-9]{32}', nonce) or not receipt.is_absolute()
            or receipt.parent != root or receipt.name != '.completion-' + nonce + '.json'
            or receipt.exists() or receipt.is_symlink()
            or operation not in {'install', 'remove', 'uninstall_launch', 'scan', 'help'}
            or (target and not Path(target).is_absolute())
            or (operation in {'remove', 'uninstall_launch', 'scan'} and not target)):
        raise RuntimeError('The installer completion contract is invalid. Restart the packaged installer.')

def main():
    outer = os.environ.pop('CODEX_HUB_INSTALLER_OUTER_EXE', '')
    if not outer:
        raise RuntimeError('Start this installer through its packaged launcher.')
    path = Path(outer)
    if not path.is_absolute() or not path.is_file() or path.suffix.lower() != '.exe':
        raise RuntimeError('The packaged installer launcher is unavailable.')
    with path.open('rb') as stream:
        if stream.read(2) != b'MZ':
            raise RuntimeError('Invalid packaged installer launcher.')
    validate_receipt_contract()
    # installer.main copies sys.executable to an external temporary directory
    # for --uninstall. Copy the complete SFX, never the onedir core by itself.
    sys.executable = str(path)
    return installer.main()

if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        log = Path(tempfile.gettempdir()) / 'CodexHubInstaller.log'
        with log.open('a', encoding='utf-8') as stream:
            stream.write(time.strftime('%Y-%m-%d %H:%M:%S') + '\n' + traceback.format_exc() + '\n')
        if '--quiet' not in sys.argv:
            installer.messagebox.showerror(installer.NAME, str(exc))
        raise SystemExit(1)
