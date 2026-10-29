"""Allowlisted CLI progress, separate from authoritative completion receipts."""
from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Callable


ProgressCallback = Callable[[str, str], None]
_INTERNAL_TAGS = ('hub-reflection', 'hub-artifacts', 'think', 'analysis')
_INTERNAL_BLOCK = re.compile(
    r'<(hub-reflection|hub-artifacts|think|analysis)\b[^>]*>.*?(?:</\1\s*>|\Z)', re.I | re.S)
_INTERNAL_CLOSE = re.compile(r'</(?:hub-reflection|hub-artifacts|think|analysis)\s*>', re.I)


def public_message(text: str) -> str:
    """Withhold unfinished internal tags as well as their completed blocks."""
    text = _INTERNAL_BLOCK.sub('', text)
    text = _INTERNAL_CLOSE.sub('', text)
    # A snapshot can stop at any character of an opening/closing marker. Hold
    # that suffix until the next snapshot instead of briefly exposing it.
    start = text.rfind('<')
    if start >= 0:
        tail = text[start:].lower()
        markers = tuple(prefix + tag + '>' for tag in _INTERNAL_TAGS for prefix in ('<', '</'))
        if any(marker.startswith(tail) or tail.startswith(marker[:-1]) for marker in markers):
            text = text[:start]
    return text.strip()


class ProgressDispatcher:
    """Coalesce updates on a daemon sender; a slow Hub never stalls CLI pipes."""
    def __init__(self, callback: ProgressCallback, interval: float = .35):
        self.callback = callback
        self.interval = max(0., interval)
        self._condition = threading.Condition()
        self._pending: dict[str, str] = {}
        self._latest: dict[str, str] = {}
        self._retry_at: dict[str, float] = {}
        self._failures: dict[str, int] = {}
        self._closing = False
        self._stopped = False
        self._thread = threading.Thread(target=self._send, name='hub-visible-progress', daemon=True)
        self._thread.start()

    def publish(self, kind: str, text: str) -> None:
        if kind not in ('status', 'assistant') or not text:
            return
        with self._condition:
            if self._closing or self._stopped or self._latest.get(kind) == text:
                return
            self._latest[kind] = text
            self._pending[kind] = text
            self._retry_at.pop(kind, None)
            self._failures[kind] = 0
            self._condition.notify()

    def _send(self) -> None:
        next_send = 0.
        while True:
            with self._condition:
                while not self._stopped:
                    if self._closing and not self._pending:
                        return
                    now = time.monotonic()
                    available = [kind for kind in ('assistant', 'status') if kind in self._pending
                                 and (self._closing or self._retry_at.get(kind, 0) <= now)]
                    delay = next_send - now
                    if available and (self._closing or delay <= 0):
                        kind = available[0]
                        text = self._pending.pop(kind)
                        break
                    if self._pending and not available:
                        delay = max(delay, min(self._retry_at.get(kind, 0) for kind in self._pending) - now)
                    self._condition.wait(max(.001, delay) if self._pending else None)
                else:
                    return
            try:
                self.callback(kind, text)
            except Exception:
                # Retry only the latest actual public answer, at most twice.
                # A transient failed POST must not hide that answer until a
                # refresh or model completion. Never restart model work.
                with self._condition:
                    if (kind == 'assistant' and not self._closing and not self._stopped
                            and self._latest.get(kind) == text and self._failures.get(kind, 0) < 2):
                        count = self._failures.get(kind, 0) + 1
                        self._failures[kind] = count
                        self._pending[kind] = text
                        self._retry_at[kind] = time.monotonic() + max(.1, self.interval) * 2 ** (count - 1)
                        self._condition.notify_all()
            next_send = time.monotonic() + self.interval

    def close(self, timeout: float = .4) -> None:
        with self._condition:
            self._closing = True
            self._condition.notify_all()
        self._thread.join(timeout=max(0., timeout))
        with self._condition:
            # An in-flight HTTP call has its own timeout. Do not queue further
            # callbacks after completion; the server fences any late in-flight
            # update against the authoritative completion state.
            self._stopped = True
            self._pending.clear()
            self._condition.notify_all()


class VisibleCodexProgress:
    """Read exec JSONL, never raw stdout/stderr, reasoning or tool arguments."""
    _TOOL_STATES = {
        'command_execution': '正在执行工具',
        'mcp_tool_call': '正在调用工具',
        'web_search': '正在查找资料',
        'file_change': '正在更新项目文件',
    }

    def __init__(self, publish: ProgressCallback, redact: Callable[[str], str] = lambda value: value):
        self.publish = publish
        self.redact = redact
        self._messages: dict[str, str] = {}
        self._active_tools: dict[str, str] = {}
        self._visible = ''

    def started(self) -> None:
        self.publish('status', '已开始处理')

    def feed(self, line: str) -> None:
        if len(line) > 1_048_576:
            return
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            return
        if not isinstance(event, dict):
            return
        event_type = event.get('type')
        if event_type == 'turn.started':
            self.started()
            return
        if event_type not in ('item.started', 'item.updated', 'item.completed'):
            return
        item = event.get('item')
        if not isinstance(item, dict):
            return
        item_type = item.get('type')
        if not isinstance(item_type, str):
            return
        if item_type in self._TOOL_STATES:
            item_id = item.get('id')
            if event_type in ('item.started', 'item.updated'):
                if isinstance(item_id, str) and len(item_id) <= 200:
                    self._active_tools[item_id] = item_type
                self.publish('status', self._TOOL_STATES[item_type])
            elif event_type == 'item.completed':
                if isinstance(item_id, str):self._active_tools.pop(item_id, None)
                remaining = next(reversed(self._active_tools.values()), None)
                self.publish('status', self._TOOL_STATES[remaining] if remaining else '工具调用已结束，正在继续处理')
            return
        if item_type != 'agent_message' or not isinstance(item.get('text'), str):
            return
        item_id = item.get('id')
        if not isinstance(item_id, str) or not item_id or len(item_id) > 200:
            return
        text = public_message(self.redact(item['text']))
        previous = self._messages.get(item_id, '')
        # Codex exec item.updated contains a full snapshot. App-server token
        # delta events are a different protocol and are intentionally ignored.
        # Do not replace a prefix users already saw if an item is corrected.
        if not text.startswith(previous):
            return
        self._messages[item_id] = text
        visible = '\n\n'.join(value for value in self._messages.values() if value)[:24000]
        if visible and visible != self._visible and visible.startswith(self._visible):
            self._visible = visible
            self.publish('assistant', visible)
