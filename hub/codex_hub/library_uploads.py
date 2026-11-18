"""Atomic, user-scoped receipts for explicitly retried library uploads."""
import hashlib
import json
import re

from .repository import utc_now


def save_library_upload(repo, project, user, stored, request_key):
    if not re.fullmatch(r'[A-Za-z0-9_.-]{8,100}', request_key):
        raise ValueError('上传编号无效，请重新选择文件。')
    db = repo.connection
    db.execute('''CREATE TABLE IF NOT EXISTS hub_library_uploads (
        project_id INTEGER NOT NULL REFERENCES projects(id),
        user_id INTEGER NOT NULL REFERENCES users(id),
        request_key TEXT NOT NULL, payload_hash TEXT NOT NULL,
        attachment_id INTEGER NOT NULL REFERENCES attachments(id),
        created_at TEXT NOT NULL, PRIMARY KEY(project_id,user_id,request_key))''')
    db.commit()
    payload_hash = hashlib.sha256(json.dumps({key:stored[key] for key in
        ('original_filename','content_type','byte_size','sha256')}, sort_keys=True).encode()).hexdigest()
    try:
        db.execute('BEGIN IMMEDIATE')
        previous = db.execute('SELECT * FROM hub_library_uploads WHERE project_id=? AND user_id=? AND request_key=?',
            (project['id'], user['id'], request_key)).fetchone()
        if previous:
            if previous['payload_hash'] != payload_hash:
                raise ValueError('同一上传编号的文件内容不一致，请核对原文件。')
            attachment = repo.get_attachment(previous['attachment_id'])
            db.commit()
            return attachment, True
        now = utc_now()
        entry = db.execute('''INSERT INTO entries(project_id,author_type,author_user_id,source_label,
            kind,body_markdown,metadata_json,created_at,updated_at) VALUES (?,'user',?,?,'file',?,'{}',?,?)''',
            (project['id'], user['id'], user['username'], 'Uploaded attachment: '+stored['original_filename'], now, now))
        attachment = db.execute('''INSERT INTO attachments(project_id,entry_id,original_filename,
            stored_relpath,content_type,byte_size,sha256,created_by_user_id,created_at) VALUES (?,?,?,?,?,?,?,?,?)''',
            (project['id'], entry.lastrowid, stored['original_filename'], stored['stored_relpath'],
             stored['content_type'], stored['byte_size'], stored['sha256'], user['id'], now))
        db.execute('INSERT INTO hub_library_uploads VALUES (?,?,?,?,?,?)',
            (project['id'], user['id'], request_key, payload_hash, attachment.lastrowid, now))
        db.execute('UPDATE projects SET updated_at=? WHERE id=?', (now, project['id']))
        db.commit()
        return repo.get_attachment(attachment.lastrowid), False
    except Exception:
        db.rollback()
        raise
