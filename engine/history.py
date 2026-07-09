"""Local dictation history, stored in SQLite.

Thread-safety: sqlite3 Connection objects are bound to the thread that created
them. Transcription runs on daemon threads while the UI reads on the main
thread, so we open a fresh connection per call (cheap for this volume) rather
than sharing one — avoids the 'objects created in a thread' error entirely.
"""
import os
import time
import sqlite3

import config


def _db_path():
    return os.path.join(config._CONFIG_DIR, 'history.db')


def _connect():
    os.makedirs(config._CONFIG_DIR, exist_ok=True)
    conn = sqlite3.connect(_db_path())
    conn.execute(
        """CREATE TABLE IF NOT EXISTS history (
               id       INTEGER PRIMARY KEY AUTOINCREMENT,
               ts       REAL    NOT NULL,   -- epoch seconds
               text     TEXT    NOT NULL,
               language TEXT,
               mode     TEXT,               -- ptt | toggle | test
               duration REAL                -- recording seconds
           )"""
    )
    return conn


def add(text, language=None, mode=None, duration=None):
    if not text:
        return
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO history (ts, text, language, mode, duration) "
            "VALUES (?, ?, ?, ?, ?)",
            (time.time(), text, language, mode, duration),
        )
        conn.commit()
    finally:
        conn.close()


def list_entries(query=None, limit=300):
    conn = _connect()
    try:
        if query:
            rows = conn.execute(
                "SELECT id, ts, text, language, mode, duration FROM history "
                "WHERE text LIKE ? ORDER BY id DESC LIMIT ?",
                ('%' + query + '%', limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, ts, text, language, mode, duration FROM history "
                "ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
    finally:
        conn.close()
    return [
        {'id': r[0], 'ts': r[1], 'text': r[2],
         'language': r[3], 'mode': r[4], 'duration': r[5]}
        for r in rows
    ]


def clear():
    conn = _connect()
    try:
        conn.execute("DELETE FROM history")
        conn.commit()
    finally:
        conn.close()
