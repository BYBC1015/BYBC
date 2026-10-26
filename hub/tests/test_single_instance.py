import sys
import uuid

import pytest

from codex_bridge.single_instance import SingleInstance


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="Windows mutex test")
def test_windows_mutex_allows_only_one_bridge_instance():
    name = rf"Local\CodexBridge.Test.{uuid.uuid4()}"
    first = SingleInstance(name)
    second = SingleInstance(name)

    try:
        assert first.acquire() is True
        assert second.acquire() is False
    finally:
        second.close()
        first.close()

    replacement = SingleInstance(name)
    try:
        assert replacement.acquire() is True
    finally:
        replacement.close()
