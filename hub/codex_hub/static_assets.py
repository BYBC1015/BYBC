"""Content versions shared by server templates and desktop asset bundles."""
from functools import lru_cache
import hashlib
from pathlib import Path


@lru_cache(maxsize=512)
def _digest(path: str, modified: int, changed: int, size: int) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:16]


def asset_digest(root: Path, relative: str) -> str:
    root = Path(root).resolve()
    if not relative or "\\" in relative or ":" in relative or ".." in relative.split("/"):
        raise ValueError("invalid static resource")
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError("static resource is unavailable")
    stat = path.stat()
    return _digest(str(path), stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size)


def asset_url(root: Path, path: str) -> str:
    if not path.startswith('/static/') or '?' in path or '#' in path:
        raise ValueError("expected a static resource path")
    return path + '?v=' + asset_digest(root, path[len('/static/'):])
