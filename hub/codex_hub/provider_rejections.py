"""Small, non-sensitive rejection attestations shared by server and workers."""
from __future__ import annotations

import re

CLI_QUOTA_CODES = frozenset({'rate_limit_exceeded', 'insufficient_quota', 'usage_limit_reached',
                            'quota_exceeded', 'usage_limit_exceeded'})
HTTP_QUOTA_CODES = frozenset({'insufficient_quota', 'quota_exhausted', 'billing_hard_limit_reached',
                             'insufficient_balance', 'balance_not_enough', 'payment_required'})
HTTP_429_QUOTA_CODES = frozenset({'insufficient_quota', 'quota_exhausted', 'billing_hard_limit_reached'})


def rejection_sequence(kinds):
    if (not isinstance(kinds, list) or not 1 <= len(kinds) <= 8 or kinds[-1] != 'turn.failed'
            or any(not isinstance(kind, str) or kind not in {'thread.started', 'turn.started', 'error', 'turn.failed'} for kind in kinds)
            or any(kinds.count(kind) > 1 for kind in ('thread.started', 'turn.started', 'turn.failed'))):
        return False
    rank = {'thread.started': 0, 'turn.started': 1, 'error': 2, 'turn.failed': 3}
    return [rank[kind] for kind in kinds] == sorted(rank[kind] for kind in kinds)


def normalize_rejection(value):
    """Validate bounded evidence; never infer no-work from user/model prose."""
    if (not isinstance(value, dict) or type(value.get('schema_version')) is not int
            or value.get('reason') != 'quota_exhausted' or value.get('no_work_observed') is not True
            or not isinstance(value.get('code'), str)):
        return None
    if value['schema_version'] == 1:
        if (value['code'] not in CLI_QUOTA_CODES
                or type(value.get('returncode')) is not int or value['returncode'] <= 0
                or not isinstance(value.get('stream_sha256'), str)
                or not re.fullmatch('[0-9a-f]{64}', value['stream_sha256'])
                or not rejection_sequence(value.get('event_types'))):
            return None
        fields = ('schema_version', 'reason', 'code', 'event_types', 'stream_sha256', 'returncode', 'no_work_observed')
    elif value['schema_version'] == 2:
        if (type(value.get('http_status')) is not int or value['http_status'] not in {402, 429}
                or type(value.get('request_index')) is not int or value['request_index'] != 1
                or value['code'] not in HTTP_QUOTA_CODES
                or (value['http_status'] == 429 and value['code'] not in HTTP_429_QUOTA_CODES)):
            return None
        fields = ('schema_version', 'reason', 'http_status', 'code', 'request_index', 'no_work_observed')
    else:
        return None
    return {key: value[key] for key in fields}
