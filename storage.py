"""Database backend. SQLite for local development, PostgreSQL for deployed app.

Database URL is read from CUTPLANNER_DATABASE_URL or Streamlit [database] url.
Never expose this credential to browser code.
"""
import os
import re
import sqlite3


def database_url():
    url = os.environ.get('CUTPLANNER_DATABASE_URL', '').strip()
    if url:
        return url
    try:
        import streamlit as st
        return str(st.secrets['database']['url']).strip()
    except (FileNotFoundError, KeyError, AttributeError, ImportError):
        return ''


class PgCursor:
    def __init__(self, cursor):
        self.cursor = cursor

    def __iter__(self):
        return iter(self.cursor)

    def fetchall(self):
        return self.cursor.fetchall()

    def fetchone(self):
        return self.cursor.fetchone()


class PgConnection:
    """Subset of sqlite's execute/commit API used by the legacy planner."""
    def __init__(self, url):
        import psycopg
        self.db = psycopg.connect(url, connect_timeout=8, autocommit=False)

    def execute(self, sql, values=()):
        sql = sql.replace('INSERT OR IGNORE INTO patterns', 'INSERT INTO patterns')
        sql = sql.replace('VALUES(?,?,?,?,?,?)', 'VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT (key) DO NOTHING') if 'INSERT INTO patterns' in sql else sql
        sql = sql.replace('?', '%s')
        # SQLite's INTEGER PRIMARY KEY autoincrement is not PostgreSQL syntax.
        sql = sql.replace('id INTEGER PRIMARY KEY', 'id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY')
        cur = self.db.execute(sql, values)
        return PgCursor(cur)

    def commit(self):
        self.db.commit()

    def close(self):
        self.db.close()


def connect(path):
    url = database_url()
    return PgConnection(url) if url else sqlite3.connect(path)


def cloud_enabled():
    return bool(database_url())
