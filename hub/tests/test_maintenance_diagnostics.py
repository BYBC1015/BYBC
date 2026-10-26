import json

from codex_hub.maintenance_diagnostics import collect_diagnostics, summarize_journal


def test_diagnostics_aggregate_without_leaking_url_or_exception_details():
    lines = [json.dumps({"MESSAGE": '127.0.0.1 - "POST /api/token?secret=TOPSECRET HTTP/1.1" 500 Internal Server Error'}),
             json.dumps({"MESSAGE": 'ValueError: TOPSECRET user@example.com'}),
             json.dumps({"MESSAGE": 'Traceback (most recent call last):'}), 'not json']
    summary = summarize_journal(lines)
    assert summary['http_status_classes'] == {'5xx': 1}
    assert summary['error_categories'] == {'ValueError': 1, 'tracebacks': 1}
    assert summary['unreadable_records'] == 1
    assert 'TOPSECRET' not in json.dumps(summary)
    assert 'user@' not in json.dumps(summary)


def test_diagnostics_bound_and_tolerate_malformed_records():
    lines = ['[]', json.dumps({'MESSAGE': ['private']}), 'x' * 65537,
             json.dumps({'MESSAGE': 'TimeoutError'})]
    summary = summarize_journal(lines, limit=3)
    assert summary['sampled_records'] == 0
    assert summary['unreadable_records'] == 3
    assert summary['error_categories'] == {}


def test_diagnostics_do_not_report_missing_evidence_as_healthy(monkeypatch):
    def unavailable(*args, **kwargs):
        raise OSError('not available')
    monkeypatch.setattr('codex_hub.maintenance_diagnostics.subprocess.run', unavailable)
    monkeypatch.setattr('codex_hub.maintenance_diagnostics.urlopen', unavailable)
    result = collect_diagnostics()
    assert result['health']['ok'] is False
    assert all(s['logs_available'] is False and s['state'] == 'unavailable'
               for s in result['services'].values())
