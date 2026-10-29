"""Bounded verified bytes reused across tasks, with task-local copies for tools."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import shutil
import tempfile
import threading


MAX_CACHE_BYTES = 256 * 1024 * 1024
MAX_CACHE_FILES = 128
_LOCK = threading.RLock()


def digest(path):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


class AttachmentCache:
    """A cache hit is only considered after a fresh authorized task manifest."""
    def __init__(self, root, hub, slug):
        self.root = Path(root).expanduser().absolute()
        if self.root.resolve() != self.root or self.root.is_symlink():
            raise OSError('Attachment cache must not redirect to another directory')
        if not re.fullmatch(r'[a-z0-9][a-z0-9-]{1,62}[a-z0-9]', slug):
            raise ValueError('Invalid cache project')
        self.directory = self.root / hashlib.sha256(str(hub).encode()).hexdigest()[:32] / slug

    def _path(self, sha256):
        if not re.fullmatch(r'[0-9a-fA-F]{64}', sha256):
            raise ValueError('Invalid cache digest')
        path = self.directory / (sha256.lower() + '.blob')
        if path.resolve() != path or path.is_symlink():
            raise OSError('Attachment cache entry redirected')
        return path

    @staticmethod
    def _verified(path, sha256, size):
        return path.is_file() and path.stat().st_size == size and digest(path) == sha256.lower()

    @staticmethod
    def _copy_verified(source, target, sha256, size):
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=target.parent, prefix=target.name + '.', suffix='.part', delete=False) as stream:
                temporary = Path(stream.name)
            shutil.copyfile(source, temporary)
            if not AttachmentCache._verified(temporary, sha256, size):
                raise OSError('Cached attachment integrity changed')
            temporary.replace(target)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def restore(self, target, sha256, size):
        with _LOCK:
            source = self._path(sha256)
            if not self._verified(source, sha256, size):
                # Corruption is a cache miss; never let it replace task inputs.
                if source.exists():source.unlink()
                return False
            self._copy_verified(source, target, sha256, size)
            os.utime(source, None)
            return True

    def store(self, source, sha256, size):
        if size > MAX_CACHE_BYTES:return
        with _LOCK:
            target = self._path(sha256)
            target.parent.mkdir(parents=True, exist_ok=True)
            self._path(sha256)  # Recheck after directory creation.
            if not self._verified(target, sha256, size):
                self._copy_verified(source, target, sha256, size)
            else:
                os.utime(target, None)
            self._trim()

    def _trim(self):
        # Only this dedicated cache's own digest-named regular files are owned.
        # Original project inputs and task-local working copies are never pruned.
        entries = []
        for path in self.root.glob('*/*/*.blob'):
            if (path.resolve() != path or path.is_symlink()
                    or not re.fullmatch(r'[0-9a-f]{64}\.blob', path.name)):
                continue
            stat = path.stat()
            entries.append((stat.st_mtime_ns, path, stat.st_size))
        total = sum(size for _, _, size in entries)
        count = len(entries)
        for _, path, size in sorted(entries):
            if total <= MAX_CACHE_BYTES and count <= MAX_CACHE_FILES:break
            path.unlink(missing_ok=True)
            total -= size
            count -= 1
