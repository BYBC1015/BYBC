"""Content-free execution timings retained after transient progress is cleared."""
from __future__ import annotations

from datetime import datetime
import math
import re


def initialize_execution_telemetry(db):
    db.executescript('''
      CREATE TABLE IF NOT EXISTS hub_execution_timings (
        execution_id INTEGER PRIMARY KEY REFERENCES hub_task_executions(id) ON DELETE CASCADE,
        from_start INTEGER NOT NULL DEFAULT 0 CHECK(from_start IN (0,1)),
        first_activity_at REAL, last_activity_at REAL,
        first_public_at REAL, last_public_at REAL
      );
      CREATE TRIGGER IF NOT EXISTS hub_execution_timings_created
        AFTER INSERT ON hub_task_executions BEGIN
          INSERT OR IGNORE INTO hub_execution_timings(execution_id,from_start)
            VALUES (NEW.id,1);
        END;
    ''')
    for operation in ('INSERT', 'UPDATE'):
        db.execute(f'''
          CREATE TRIGGER IF NOT EXISTS hub_execution_timings_{operation.lower()}
          AFTER {operation} ON hub_execution_progress BEGIN
            INSERT INTO hub_execution_timings
              (execution_id,from_start,first_activity_at,last_activity_at,first_public_at,last_public_at)
            VALUES (NEW.execution_id,0,NEW.updated_at,NEW.updated_at,
              CASE WHEN NEW.last_kind='assistant' AND length(trim(NEW.assistant_text,char(9)||char(10)||char(13)||' '))>0 THEN NEW.updated_at END,
              CASE WHEN NEW.last_kind='assistant' AND length(trim(NEW.assistant_text,char(9)||char(10)||char(13)||' '))>0 THEN NEW.updated_at END)
            ON CONFLICT(execution_id) DO UPDATE SET
              first_activity_at=COALESCE(first_activity_at,excluded.first_activity_at),
              last_activity_at=excluded.last_activity_at,
              first_public_at=COALESCE(first_public_at,excluded.first_public_at),
              last_public_at=COALESCE(excluded.last_public_at,last_public_at);
          END
        ''')


def _elapsed(start, end):
    if start is None or end is None:
        return None
    value = end-start
    # Clock corrections are missing evidence, never a fabricated zero delay.
    return round(value, 3) if math.isfinite(value) and value >= 0 else None


def _has_public_result(db, task_id, entry_id):
    entry=db.execute('SELECT body_markdown FROM entries WHERE id=? AND task_id=? AND deleted_at IS NULL',
                     (entry_id,task_id)).fetchone()
    if not entry:return False
    text=re.sub(r'<(hub-reflection|hub-artifacts)\b[^>]*>.*?(?:</\1\s*>|$)',
                '',str(entry['body_markdown'] or ''),flags=re.I|re.S).strip()
    # Removing an internal block can leave an otherwise empty Markdown fence.
    text=re.sub(r'```[^\n`]*\n\s*```','',text).strip()
    return bool(text)


def execution_latency(db, project_id, *, limit=100):
    """Read bounded diagnostic metadata; never return prompts, tokens or output."""
    rows=db.execute('''SELECT r.id AS execution_id,r.task_id,r.generation,r.state,
        r.created_at AS acquired_at,r.started_at,r.completed_at,r.result_entry_id,
        t.created_at AS submitted_at,t.assigned_agent_id,t.assigned_device_id,t.controller_read_only,
        r.device_id AS execution_device_id,
        m.from_start,m.first_activity_at,m.last_activity_at,m.first_public_at,m.last_public_at
        FROM hub_task_executions r JOIN tasks t ON t.id=r.task_id
        LEFT JOIN hub_execution_timings m ON m.execution_id=r.id
        WHERE t.project_id=? ORDER BY r.id DESC LIMIT ?''',
        (project_id,max(1,min(int(limit),500))))
    result=[]
    for row in rows:
        item=dict(row)
        try:
            submitted=datetime.fromisoformat(item['submitted_at'].replace('Z','+00:00')).timestamp()
        except (ValueError,TypeError,AttributeError):
            submitted=None
        first_public=item['first_public_at'] if item['from_start'] else None
        source='progress' if first_public is not None else None
        if (item['from_start'] and first_public is None and item['result_entry_id']
                and _has_public_result(db,item['task_id'],item['result_entry_id'])):
            first_public=item['completed_at']
            source='completed_result' if first_public is not None else None
        item.update(
            timing_coverage='complete' if item.pop('from_start') else 'partial_or_unavailable',
            submit_to_acquire_seconds=_elapsed(submitted,item['acquired_at']),
            acquire_to_start_seconds=_elapsed(item['acquired_at'],item['started_at']),
            submit_to_first_public_seconds=_elapsed(submitted,first_public),
            start_to_first_public_seconds=_elapsed(item['started_at'],first_public),
            execution_seconds=_elapsed(item['started_at'],item['completed_at']),
            first_public_source=source)
        result.append(item)
    return result
