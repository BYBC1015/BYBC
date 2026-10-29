"""Actual CLI usage counters only; no estimates, reasoning text or prices."""
import json

FIELDS = ('input_tokens', 'cached_input_tokens', 'output_tokens')
MAX_COUNT = (1 << 53) - 1


def normalize_token_usage(value):
    if not isinstance(value, dict):return None
    if any(type(value.get(key)) is not int or not 0 <= value[key] <= MAX_COUNT for key in FIELDS):
        return None
    if value['cached_input_tokens'] > value['input_tokens']:return None
    return {key: value[key] for key in FIELDS}


def usage_from_jsonl(output):
    if isinstance(output, bytes):output = output.decode('utf-8', errors='replace')
    if not isinstance(output, str):return None
    completed = []
    for line in output.splitlines():
        if len(line) > 1_048_576:continue
        try:event = json.loads(line)
        except (TypeError, ValueError):continue
        if not isinstance(event, dict) or event.get('type') != 'turn.completed':continue
        usage = normalize_token_usage(event.get('usage'))
        if usage is None:return None
        completed.append((event.get('turn_id'), usage))
    if not completed:return None
    if len(completed) == 1:return completed[0][1]
    # Exec normally emits exactly one completed turn. Multiple events without
    # stable identities cannot distinguish replay from another billable turn.
    turns = {}
    for turn_id, usage in completed:
        if not isinstance(turn_id, str) or not turn_id or len(turn_id) > 200:return None
        if turn_id in turns and turns[turn_id] != usage:return None
        turns[turn_id] = usage
    return normalize_token_usage({key: sum(usage[key] for usage in turns.values()) for key in FIELDS})
