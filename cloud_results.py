"""Save complete calculation reports into the selected DB.
Full results are also cached independently in browser IndexedDB by browser_vault.
"""
import json
import sqlite3
from storage import connect, cloud_enabled

DDL = '''CREATE TABLE IF NOT EXISTS saved_results(
 id TEXT PRIMARY KEY, name TEXT NOT NULL, created TEXT NOT NULL,
 payload TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1)'''


def save_result(db_file, report):
    db = connect(db_file)
    try:
        db.execute(DDL)
        payload = json.dumps(report, ensure_ascii=False, separators=(',', ':'))
        db.execute('''INSERT INTO saved_results(id,name,created,payload,revision)
            VALUES(?,?,?,?,1) ON CONFLICT(id) DO NOTHING''',
            (report['id'], report['name'], report['created'], payload))
        db.commit()
    finally:
        db.close()


def recent_results(db_file, limit=25):
    db=connect(db_file)
    try:
        db.execute(DDL)
        db.commit()
        return db.execute('SELECT id,name,created FROM saved_results ORDER BY created DESC LIMIT ?', (limit,)).fetchall()
    finally:
        db.close()


def get_result(db_file, result_id):
    db=connect(db_file)
    try:
        row=db.execute('SELECT payload FROM saved_results WHERE id=?',(result_id,)).fetchone()
        return json.loads(row[0]) if row else None
    finally:
        db.close()
