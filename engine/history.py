"""Local dictation history, stored in SQLite.

Thread-safety: sqlite3 Connection objects are bound to the thread that created
them. Transcription runs on daemon threads while the UI reads on the main
thread, so we open a fresh connection per call (cheap for this volume) rather
than sharing one — avoids the 'objects created in a thread' error entirely.

Sync model (used by engine.backup): every row carries a stable `uuid` that
survives across devices, unlike the local AUTOINCREMENT `id`, plus an
`updated_at` watermark. Deletes are soft (`deleted_at` tombstones) so a delete
on one device can propagate to another instead of being silently resurrected
by the next merge. `synced_at` tracks whether a row's current state has
already reached the backup.
"""
import os
import time
import uuid as _uuidlib
import sqlite3

import config

_NEW_COLUMNS = {
    'uuid':       'ALTER TABLE history ADD COLUMN uuid TEXT',
    'updated_at': 'ALTER TABLE history ADD COLUMN updated_at REAL',
    'deleted_at': 'ALTER TABLE history ADD COLUMN deleted_at REAL',
    'synced_at':  'ALTER TABLE history ADD COLUMN synced_at REAL',
}


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
    _migrate_schema(conn)
    return conn


def _migrate_schema(conn):
    """Add sync columns to DBs created before backup support existed, and
    backfill them for any rows that predate the columns."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(history)")}
    added_uuid_col = 'uuid' not in existing
    for name, ddl in _NEW_COLUMNS.items():
        if name not in existing:
            conn.execute(ddl)
    conn.execute("UPDATE history SET updated_at = ts WHERE updated_at IS NULL")
    if added_uuid_col:
        rows = conn.execute("SELECT id FROM history WHERE uuid IS NULL").fetchall()
        conn.executemany(
            "UPDATE history SET uuid = ? WHERE id = ?",
            [(str(_uuidlib.uuid4()), row[0]) for row in rows],
        )
    conn.commit()


def add(text, language=None, mode=None, duration=None):
    if not text:
        return
    conn = _connect()
    try:
        now = time.time()
        conn.execute(
            "INSERT INTO history (uuid, ts, text, language, mode, duration, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (str(_uuidlib.uuid4()), now, text, language, mode, duration, now),
        )
        conn.commit()
    finally:
        conn.close()


def list_entries(query=None, limit=300):
    conn = _connect()
    try:
        if query:
            rows = conn.execute(
                "SELECT id, uuid, ts, text, language, mode, duration FROM history "
                "WHERE deleted_at IS NULL AND text LIKE ? ORDER BY id DESC LIMIT ?",
                ('%' + query + '%', limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, uuid, ts, text, language, mode, duration FROM history "
                "WHERE deleted_at IS NULL ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
    finally:
        conn.close()
    return [
        {'id': r[0], 'uuid': r[1], 'ts': r[2], 'text': r[3],
         'language': r[4], 'mode': r[5], 'duration': r[6]}
        for r in rows
    ]


def clear():
    """Soft-delete every entry, so the deletion can sync to other devices as a
    tombstone instead of a hard DELETE a later merge would resurrect."""
    conn = _connect()
    try:
        now = time.time()
        conn.execute(
            "UPDATE history SET deleted_at = ?, updated_at = ? WHERE deleted_at IS NULL",
            (now, now),
        )
        conn.commit()
    finally:
        conn.close()


def _row_to_sync_dict(r):
    return {'uuid': r[0], 'ts': r[1], 'text': r[2], 'language': r[3],
            'mode': r[4], 'duration': r[5], 'updated_at': r[6], 'deleted_at': r[7]}


_SYNC_COLUMNS = "uuid, ts, text, language, mode, duration, updated_at, deleted_at"


def dirty_entries():
    """Rows whose current state hasn't reached the backup yet."""
    conn = _connect()
    try:
        rows = conn.execute(
            f"SELECT {_SYNC_COLUMNS} FROM history "
            "WHERE synced_at IS NULL OR synced_at < updated_at"
        ).fetchall()
    finally:
        conn.close()
    return [_row_to_sync_dict(r) for r in rows]


def all_entries_for_sync():
    """Every row, including soft-deleted ones — the outgoing backup snapshot."""
    conn = _connect()
    try:
        rows = conn.execute(f"SELECT {_SYNC_COLUMNS} FROM history").fetchall()
    finally:
        conn.close()
    return [_row_to_sync_dict(r) for r in rows]


def mark_synced(uuids, synced_at):
    if not uuids:
        return
    conn = _connect()
    try:
        conn.executemany(
            "UPDATE history SET synced_at = ? WHERE uuid = ?",
            [(synced_at, u) for u in uuids],
        )
        conn.commit()
    finally:
        conn.close()


def upsert_from_remote(entry):
    """Merge one remote entry into the local DB, last-write-wins on
    updated_at. The merged row is marked synced immediately since it now
    matches the backup."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT updated_at FROM history WHERE uuid = ?", (entry['uuid'],)
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO history (uuid, ts, text, language, mode, duration, "
                "updated_at, deleted_at, synced_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (entry['uuid'], entry['ts'], entry['text'], entry.get('language'),
                 entry.get('mode'), entry.get('duration'), entry['updated_at'],
                 entry.get('deleted_at'), entry['updated_at']),
            )
        elif entry['updated_at'] > (row[0] or 0):
            conn.execute(
                "UPDATE history SET text = ?, language = ?, mode = ?, duration = ?, "
                "updated_at = ?, deleted_at = ?, synced_at = ? WHERE uuid = ?",
                (entry['text'], entry.get('language'), entry.get('mode'),
                 entry.get('duration'), entry['updated_at'], entry.get('deleted_at'),
                 entry['updated_at'], entry['uuid']),
            )
        conn.commit()
    finally:
        conn.close()
