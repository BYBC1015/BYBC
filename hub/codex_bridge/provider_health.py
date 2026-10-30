"""Passive, model-specific CLI evidence; never a network gate or paid probe."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
import threading
import time
import uuid

from codex_hub.model_catalog import MODEL_ID_RE, normalize_provider_health

_lock = threading.Lock()


def health_path(config):
    options = getattr(config, 'backend_options', {}) or {}
    value = options.get('provider_health_path')
    return Path(value).with_suffix('.d') if value else None


def _snapshots(config):
    path = health_path(config)
    return sorted(path.glob('*.json'), reverse=True)[:256] if path else []


def health_revision(config):
    try:
        # Immutable snapshot names are a cheap cross-process generation marker.
        return tuple(path.name for path in _snapshots(config)) or None
    except OSError:
        return None


def read_health(config):
    rows = []
    try:
        paths = _snapshots(config)
    except OSError:
        paths = []
    for path in paths:
        try:
            if path.stat().st_size <= 32768:
                rows.extend(normalize_provider_health(json.loads(path.read_text(encoding='utf-8')))['models'])
        except (OSError, ValueError):
            continue
    # The most recently observed outcome wins, even if another process publishes
    # an older observation later. Each writer owns a unique immutable snapshot.
    rows.sort(key=lambda row: datetime.fromisoformat(row['observed_at']), reverse=True)
    unique = {}
    for row in rows:
        unique.setdefault(row['model'], row)
    return {'source': 'cli-outcome', 'models': list(unique.values())[:32]}


def outcome(result):
    """Only terminal protocol events count; tool output is not provider evidence."""
    raw = result.stdout
    if isinstance(raw, bytes):
        raw = raw.decode('utf-8', errors='replace')
    for line in reversed(str(raw or '')[-1048576:].splitlines()):
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get('type') == 'turn.completed':
            return ('reachable', 'turn_completed') if result.ok else None
        if event.get('type') != 'turn.failed':
            continue
        error = event.get('error')
        if not isinstance(error, dict):
            return None
        message = str(error.get('message') or '')[:8192].lower()
        code = str(error.get('code') or '').lower()
        if code in {'unauthorized', 'invalid_api_key', 'authentication_error'} or re.search(
                r'\b(?:401|unauthorized)\b|authentication (?:failed|required)|invalid api key', message):
            return 'auth_required', 'authentication_error'
        if code in {'rate_limit_exceeded', 'insufficient_quota'} or re.search(
                r'\b429\b|rate limit|quota exceeded|usage limit', message):
            return 'rate_limited', 'rate_limit'
        if code in {'connection_error', 'network_error'} or re.search(
                r'stream disconnected|error sending request|failed to (?:connect|send request)|'
                r'connection (?:reset|refused|closed)|network is unreachable|dns error|'
                r'error trying to connect|request timed out', message):
            return 'unreachable', 'transport_error'
        return None
    return None


def record_outcome(config, result):
    path = health_path(config)
    if not path or not result.command or '--model' not in result.command:
        return
    index = result.command.index('--model') + 1
    if index >= len(result.command) or not MODEL_ID_RE.fullmatch(result.command[index]):
        return
    observation = outcome(result)
    if not observation:
        return
    model = result.command[index]
    state, reason = observation
    observed_ns = time.time_ns()
    row = dict(model=model, state=state, reason=reason,
               observed_at=datetime.fromtimestamp(observed_ns / 1e9, timezone.utc).isoformat())
    with _lock:
        payload = {'source': 'cli-outcome', 'models': [row]}
        path.mkdir(parents=True, exist_ok=True)
        destination = path / f'{observed_ns:020d}-{uuid.uuid4().hex}.json'
        temporary = destination.with_suffix('.tmp')
        try:
            temporary.write_text(json.dumps(payload), encoding='utf-8')
            temporary.replace(destination)
            # Only these generated snapshots are pruned. Never user configuration.
            for old in sorted(path.glob('*.json'), reverse=True)[128:]:
                if re.fullmatch(r'[0-9]{20}-[a-f0-9]{32}\.json', old.name):
                    old.unlink(missing_ok=True)
        finally:
            temporary.unlink(missing_ok=True)
