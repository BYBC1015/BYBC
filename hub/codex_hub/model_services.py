"""Project-owned model connections, immutable task revisions and read-only probes.

Credentials live outside the database/export tree. Only the assigned device,
holding the exact execution fence, can fetch a task's frozen connection.
"""
from __future__ import annotations

import json
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

KINDS = {'ollama', 'lmstudio', 'openai_compatible'}
PROBE_MESSAGES = {
    'ready': '已连通并读取模型目录；尚未验证实际生成。',
    'auth': '服务拒绝凭据，请检查 API 密钥。',
    'network': '执行电脑暂时无法连接模型服务。',
    'timeout': '模型服务响应超时，请检查服务是否已启动。',
    'invalid': '服务未返回兼容的模型目录。',
    'http': '服务返回错误，请核对 API 地址。',
    'missing_secret': '凭据不可用，请重新保存 API 密钥。',
    'expired': '执行电脑未完成连接检查，可重新检查。',
}


def now():
    return datetime.now(timezone.utc).isoformat()


def initialize_model_services(db):
    db.executescript('''
      CREATE TABLE IF NOT EXISTS hub_model_services (
        id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        revision INTEGER NOT NULL, config_json TEXT NOT NULL, deleted INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'unverified', status_message TEXT NOT NULL DEFAULT '',
        models_json TEXT NOT NULL DEFAULT '[]', updated_at TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS hub_model_service_versions (
        service_id INTEGER NOT NULL REFERENCES hub_model_services(id) ON DELETE CASCADE,
        revision INTEGER NOT NULL, config_json TEXT NOT NULL, secret_ref TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL, PRIMARY KEY(service_id,revision));
      CREATE TABLE IF NOT EXISTS hub_model_service_probes (
        id INTEGER PRIMARY KEY, service_id INTEGER NOT NULL REFERENCES hub_model_services(id) ON DELETE CASCADE,
        revision INTEGER NOT NULL, device_id INTEGER NOT NULL REFERENCES devices(id),
        state TEXT NOT NULL, claim_token TEXT NOT NULL DEFAULT '', attempts INTEGER NOT NULL DEFAULT 0,
        expires_at TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
      CREATE INDEX IF NOT EXISTS hub_model_probe_device ON hub_model_service_probes(device_id,state);
    ''')
    db.commit()


def clean_config(data, previous=None):
    old = previous or {}
    def text(key, limit, default=''):
        value = data.get(key, old.get(key, default))
        if not isinstance(value, str) or not value.strip() or len(value) > limit or any(ord(c) < 32 for c in value):
            raise ValueError('请填写有效的名称、模型和服务地址')
        return value.strip()
    kind = text('kind', 32, 'openai_compatible')
    if kind not in KINDS:
        raise ValueError('不支持的模型服务类型')
    base_url = text('base_url', 2048).rstrip('/')
    try:
        url = urlsplit(base_url)
        if (url.scheme not in {'https', 'http'} or not url.hostname or url.username or url.password
                or url.query or url.fragment or '\\' in base_url or not url.port and url.netloc.endswith(':')):
            raise ValueError()
        # Access the property to reject malformed ports.
        _ = url.port
    except ValueError:
        raise ValueError('服务地址需为 HTTP(S) API 根地址，不能含密钥、查询参数或片段') from None
    raw_id = data.get('device_id', old.get('device_id'))
    if isinstance(raw_id, bool) or not re.fullmatch(r'[1-9][0-9]{0,14}', str(raw_id)):
        raise ValueError('请选择实际运行服务连接的电脑')
    result = dict(name=text('name', 100), kind=kind, base_url=base_url,
                  model=text('model', 100), device_id=int(raw_id))
    for flag, default in [('enabled', True), ('supports_images', False), ('supports_tools', False)]:
        value = data.get(flag, old.get(flag, default))
        if not isinstance(value, bool):
            raise ValueError('能力选项必须为开或关')
        result[flag] = value
    return result


class ModelServices:
    def __init__(self, db, data_dir):
        self.db = db
        self.private_dir = Path(data_dir) / '.private' / 'model-services'

    def _row(self, project_id, service_id):
        row = self.db.execute('SELECT * FROM hub_model_services WHERE project_id=? AND id=? AND deleted=0',
                              (project_id, service_id)).fetchone()
        if not row:
            raise ValueError('模型服务不存在')
        return dict(row)

    def list(self, project_id):
        rows = self.db.execute('SELECT * FROM hub_model_services WHERE project_id=? AND deleted=0 ORDER BY id', (project_id,))
        return [dict(id=r['id'], revision=r['revision'], **json.loads(r['config_json']), status=r['status'],
                     status_message=r['status_message'], models=json.loads(r['models_json'])) for r in rows]

    def _read_secret(self, ref):
        if not ref:
            return ''
        if not re.fullmatch(r'[a-f0-9]{48}', ref):
            raise ValueError('模型服务凭据不可用，请重新保存')
        try:
            return (self.private_dir / ref).read_text(encoding='utf-8')
        except OSError:
            raise ValueError('模型服务凭据不可用，请重新保存') from None

    def _write_secret(self, key):
        if not key:
            return ''
        self.private_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.private_dir.chmod(0o700)
        ref = secrets.token_hex(24)
        fd = os.open(self.private_dir / ref, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(key)
        return ref

    def save(self, project_id, data, service_id=None):
        try:
            self.db.execute('BEGIN IMMEDIATE')
            row = self._row(project_id, service_id) if service_id else None
            old = json.loads(row['config_json']) if row else {}
            config = clean_config(data, old)
            membership = self.db.execute('''SELECT 1 FROM project_memberships m JOIN devices d ON d.id=m.device_id
                WHERE m.project_id=? AND m.device_id=? AND m.permission IN ('write','admin') AND d.is_active=1''',
                (project_id, config['device_id'])).fetchone()
            if not membership:
                raise ValueError('执行电脑必须是本项目可写的有效连接')
            key = data.get('api_key', '')
            if not isinstance(key, str) or len(key) > 4096 or any(ord(c) < 32 or ord(c) > 126 for c in key):
                raise ValueError('API 密钥格式无效')
            if not isinstance(data.get('clear_api_key', False), bool):
                raise ValueError('清除密钥选项无效')
            old_ref = ''
            if row:
                old_ref = self.db.execute('SELECT secret_ref FROM hub_model_service_versions WHERE service_id=? AND revision=?',
                    (service_id, row['revision'])).fetchone()['secret_ref']
            # Never carry a credential to a different host merely by editing an URL.
            if old_ref and config['base_url'] != old.get('base_url') and not key and not data.get('clear_api_key'):
                raise ValueError('更改服务地址时请重新填写密钥，或明确清除原密钥')
            ref = '' if data.get('clear_api_key') else self._write_secret(key.strip()) if key.strip() else old_ref
            config['has_api_key'] = bool(ref)
            revision = row['revision'] + 1 if row else 1
            encoded = json.dumps(config, ensure_ascii=False)
            if row:
                self.db.execute("UPDATE hub_model_services SET revision=?,config_json=?,status='unverified',status_message='',models_json='[]',updated_at=? WHERE id=?",
                    (revision, encoded, now(), service_id))
            else:
                service_id = self.db.execute('INSERT INTO hub_model_services(project_id,revision,config_json,updated_at) VALUES (?,?,?,?)',
                    (project_id, revision, encoded, now())).lastrowid
            self.db.execute('INSERT INTO hub_model_service_versions VALUES (?,?,?,?,?)', (service_id, revision, encoded, ref, now()))
            self.db.execute("UPDATE hub_model_service_probes SET state='obsolete',updated_at=? WHERE service_id=? AND state IN ('queued','running')",
                            (now(), service_id))
            self.db.commit()
            return service_id
        except Exception:
            self.db.rollback()
            raise

    def delete(self, project_id, service_id):
        self._row(project_id, service_id)
        # Frozen, already-started executions retain their immutable revision.
        with self.db:
            self.db.execute('UPDATE hub_model_services SET deleted=1,updated_at=? WHERE id=?', (now(), service_id))
            self.db.execute("UPDATE hub_model_service_probes SET state='obsolete',updated_at=? WHERE service_id=? AND state IN ('queued','running')", (now(), service_id))

    def version(self, project_id, service_id, revision, device_id):
        row = self.db.execute('''SELECT v.* FROM hub_model_service_versions v JOIN hub_model_services s ON s.id=v.service_id
            WHERE s.project_id=? AND s.id=? AND v.revision=?''', (project_id, service_id, revision)).fetchone()
        if not row:
            raise ValueError('任务的模型服务版本不存在')
        config = json.loads(row['config_json'])
        if config['device_id'] != device_id:
            raise ValueError('模型服务不属于此执行电脑')
        return dict(config, id=service_id, revision=revision, api_key=self._read_secret(row['secret_ref']),
                    timeout_seconds=90, max_tokens=8192)

    def queue_probe(self, project_id, service_id):
        try:
            self.db.execute('BEGIN IMMEDIATE')
            row = self._row(project_id, service_id)
            config = json.loads(row['config_json'])
            pending = self.db.execute("SELECT id FROM hub_model_service_probes WHERE service_id=? AND revision=? AND state IN ('queued','running')",
                                      (service_id, row['revision'])).fetchone()
            if pending:
                self.db.commit()
                return pending['id']
            job_id = self.db.execute('''INSERT INTO hub_model_service_probes(service_id,revision,device_id,state,created_at,updated_at)
                VALUES (?,?,?,'queued',?,?)''', (service_id, row['revision'], config['device_id'], now(), now())).lastrowid
            self.db.execute("UPDATE hub_model_services SET status='queued',status_message='等待执行电脑检查连接',updated_at=? WHERE id=?", (now(), service_id))
            self.db.commit()
            return job_id
        except Exception:
            self.db.rollback()
            raise

    def claim_probe(self, device_id):
        try:
            self.db.execute('BEGIN IMMEDIATE')
            stamp = now()
            self.db.execute("UPDATE hub_model_service_probes SET state='queued' WHERE device_id=? AND state='running' AND expires_at<? AND attempts<3", (device_id, stamp))
            expired = self.db.execute("SELECT service_id,revision FROM hub_model_service_probes WHERE device_id=? AND state='running' AND expires_at<? AND attempts>=3", (device_id, stamp)).fetchall()
            for row in expired:
                self.db.execute("UPDATE hub_model_services SET status='error',status_message=? WHERE id=? AND revision=?", (PROBE_MESSAGES['expired'], row['service_id'], row['revision']))
            self.db.execute("UPDATE hub_model_service_probes SET state='error' WHERE device_id=? AND state='running' AND expires_at<? AND attempts>=3", (device_id, stamp))
            row = self.db.execute('''SELECT j.*,s.project_id FROM hub_model_service_probes j JOIN hub_model_services s ON s.id=j.service_id
                JOIN project_memberships m ON m.project_id=s.project_id AND m.device_id=j.device_id
                WHERE j.device_id=? AND j.state='queued' AND s.deleted=0 AND s.revision=j.revision
                AND m.permission IN ('write','admin') ORDER BY j.id LIMIT 1''', (device_id,)).fetchone()
            if not row:
                self.db.commit()
                return None
            token = secrets.token_hex(24)
            expires = (datetime.now(timezone.utc) + timedelta(seconds=120)).isoformat()
            try:
                service = self.version(row['project_id'], row['service_id'], row['revision'], device_id)
            except ValueError:
                self.db.execute("UPDATE hub_model_service_probes SET state='error' WHERE id=?", (row['id'],))
                self.db.execute("UPDATE hub_model_services SET status='error',status_message=? WHERE id=? AND revision=?", (PROBE_MESSAGES['missing_secret'], row['service_id'], row['revision']))
                self.db.commit()
                return None
            self.db.execute("UPDATE hub_model_service_probes SET state='running',claim_token=?,attempts=attempts+1,expires_at=?,updated_at=? WHERE id=?", (token, expires, stamp, row['id']))
            self.db.commit()
            return {'id': row['id'], 'claim_token': token, 'service': service}
        except Exception:
            self.db.rollback()
            raise

    def complete_probe(self, device_id, job_id, data):
        code = data.get('code')
        if not isinstance(code, str) or code not in PROBE_MESSAGES:
            raise ValueError('检查结果无效')
        claim_token = data.get('claim_token')
        if not isinstance(claim_token, str) or not re.fullmatch(r'[a-f0-9]{48}', claim_token):
            raise ValueError('连接检查凭据已失效')
        # Only known codes and model IDs are persisted, never upstream bodies/URLs.
        models = data.get('models', [])
        if not isinstance(models, list) or len(models) > 1000:
            raise ValueError('模型目录无效')
        models = list(dict.fromkeys(m for m in models if isinstance(m, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}', m)))[:500]
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            row = self.db.execute('SELECT * FROM hub_model_service_probes WHERE id=? AND device_id=?', (job_id, device_id)).fetchone()
            if not row or not secrets.compare_digest(row['claim_token'], claim_token):
                raise ValueError('连接检查凭据已失效')
            if row['state'] in {'ready', 'error', 'obsolete'}:
                return {'accepted': True}
            if row['state'] != 'running' or row['expires_at'] <= now():
                raise ValueError('连接检查未在执行')
            allowed = self.db.execute('''SELECT 1 FROM hub_model_services s
                JOIN project_memberships m ON m.project_id=s.project_id AND m.device_id=?
                JOIN devices d ON d.id=m.device_id
                WHERE s.id=? AND s.revision=? AND s.deleted=0
                AND m.permission IN ('write','admin') AND d.is_active=1''',
                (device_id, row['service_id'], row['revision'])).fetchone()
            if not allowed:
                raise ValueError('连接检查权限已失效')
            status = 'ready' if code == 'ready' else 'error'
            self.db.execute('UPDATE hub_model_service_probes SET state=?,updated_at=? WHERE id=?', (status, now(), job_id))
            self.db.execute('UPDATE hub_model_services SET status=?,status_message=?,models_json=?,updated_at=? WHERE id=? AND revision=? AND deleted=0',
                (status, PROBE_MESSAGES[code], json.dumps(models), now(), row['service_id'], row['revision']))
        return {'accepted': True}


def service_models(db, project_id, device_id):
    if not db or not project_id:
        return []
    rows = db.execute('SELECT * FROM hub_model_services WHERE project_id=? AND deleted=0', (project_id,))
    result = []
    for row in rows:
        item = json.loads(row['config_json'])
        if not item['enabled'] or item['device_id'] != device_id:
            continue
        result.append({'model': f'hub-service/{row["id"]}', 'displayName': item['name'] + ' · ' + item['model'],
            'defaultReasoningEffort': 'none', 'supportedReasoningEfforts': [{'reasoningEffort': 'none', 'description': '由服务模型配置决定'}],
            'isDefault': False, 'service_revision': row['revision'], 'supports_tools': item['supports_tools'],
            'supports_images': item['supports_images'], 'execution_backend': 'openai_compatible'})
    return result
