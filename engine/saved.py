"""Saved prompts — bookmarked dictations and manually typed text that users
want to reuse without re-dictating.

Shares the same SQLite database as history (history.db) but uses its own
table (saved_prompts) and its own local snapshot file (saved-snapshot.json).

Thread-safety: same one-connection-per-call pattern as engine.history — see
its module docstring for rationale.

Sync model: mirrors history's uuid / updated_at / deleted_at / synced_at
columns and the same last-write-wins upsert, but lives in a *separate*
Drive file (freeflo-saved.json) so old app versions that don't know about
saved prompts can never clobber them during a backup upload.
"""
import os
import json
import time
import uuid as _uuidlib
import sqlite3

import config


def _db_path():
    return os.path.join(config._CONFIG_DIR, 'history.db')


def _connect():
    os.makedirs(config._CONFIG_DIR, exist_ok=True)
    conn = sqlite3.connect(_db_path())
    conn.execute(
        """CREATE TABLE IF NOT EXISTS saved_prompts (
               id           INTEGER PRIMARY KEY AUTOINCREMENT,
               uuid         TEXT    NOT NULL UNIQUE,
               ts           REAL    NOT NULL,
               text         TEXT    NOT NULL,
               history_uuid TEXT    UNIQUE,
               updated_at   REAL,
               deleted_at   REAL,
               synced_at    REAL
           )"""
    )
    conn.commit()
    return conn


# ------------------------------------------------------------------
# CRUD
# ------------------------------------------------------------------

def add_from_history(text, history_uuid):
    """Save a history entry. INSERT OR IGNORE makes rapid heart clicks
    harmless — the UNIQUE(history_uuid) constraint silently drops the
    duplicate."""
    if not text or not history_uuid:
        return
    conn = _connect()
    try:
        now = time.time()
        conn.execute(
            "INSERT OR IGNORE INTO saved_prompts "
            "(uuid, ts, text, history_uuid, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (str(_uuidlib.uuid4()), now, text, history_uuid, now),
        )
        conn.commit()
    finally:
        conn.close()


def add_manual(text):
    if not text:
        return
    conn = _connect()
    try:
        now = time.time()
        conn.execute(
            "INSERT INTO saved_prompts "
            "(uuid, ts, text, history_uuid, updated_at) "
            "VALUES (?, ?, ?, NULL, ?)",
            (str(_uuidlib.uuid4()), now, text, now),
        )
        conn.commit()
    finally:
        conn.close()


def list_entries(query=None, limit=300):
    conn = _connect()
    try:
        if query:
            rows = conn.execute(
                "SELECT uuid, ts, text, history_uuid FROM saved_prompts "
                "WHERE deleted_at IS NULL AND text LIKE ? ORDER BY id DESC LIMIT ?",
                ('%' + query + '%', limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT uuid, ts, text, history_uuid FROM saved_prompts "
                "WHERE deleted_at IS NULL ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
    finally:
        conn.close()
    return [
        {'uuid': r[0], 'ts': r[1], 'text': r[2], 'history_uuid': r[3]}
        for r in rows
    ]


def remove(uuid):
    if not uuid:
        return
    conn = _connect()
    try:
        now = time.time()
        conn.execute(
            "UPDATE saved_prompts SET deleted_at = ?, updated_at = ? "
            "WHERE uuid = ? AND deleted_at IS NULL",
            (now, now, uuid),
        )
        conn.commit()
    finally:
        conn.close()


def remove_by_history_uuid(history_uuid):
    if not history_uuid:
        return
    conn = _connect()
    try:
        now = time.time()
        conn.execute(
            "UPDATE saved_prompts SET deleted_at = ?, updated_at = ? "
            "WHERE history_uuid = ? AND deleted_at IS NULL",
            (now, now, history_uuid),
        )
        conn.commit()
    finally:
        conn.close()


def edit(uuid, text):
    if not uuid or not text:
        return
    conn = _connect()
    try:
        now = time.time()
        conn.execute(
            "UPDATE saved_prompts SET text = ?, updated_at = ? "
            "WHERE uuid = ? AND deleted_at IS NULL",
            (text, now, uuid),
        )
        conn.commit()
    finally:
        conn.close()


def saved_history_uuids():
    """Set of history_uuid values that are currently saved — used to render
    heart icon state in the History view without per-entry lookups."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT history_uuid FROM saved_prompts "
            "WHERE deleted_at IS NULL AND history_uuid IS NOT NULL"
        ).fetchall()
    finally:
        conn.close()
    return {r[0] for r in rows}


# ------------------------------------------------------------------
# Sync helpers (mirror engine.history)
# ------------------------------------------------------------------

def _row_to_sync_dict(r):
    return {
        'uuid': r[0], 'ts': r[1], 'text': r[2], 'history_uuid': r[3],
        'updated_at': r[4], 'deleted_at': r[5],
    }


_SYNC_COLUMNS = "uuid, ts, text, history_uuid, updated_at, deleted_at"


def dirty_entries():
    conn = _connect()
    try:
        rows = conn.execute(
            f"SELECT {_SYNC_COLUMNS} FROM saved_prompts "
            "WHERE synced_at IS NULL OR synced_at < updated_at"
        ).fetchall()
    finally:
        conn.close()
    return [_row_to_sync_dict(r) for r in rows]


def all_entries_for_sync():
    conn = _connect()
    try:
        rows = conn.execute(
            f"SELECT {_SYNC_COLUMNS} FROM saved_prompts"
        ).fetchall()
    finally:
        conn.close()
    return [_row_to_sync_dict(r) for r in rows]


def mark_synced(uuids, synced_at):
    if not uuids:
        return
    conn = _connect()
    try:
        conn.executemany(
            "UPDATE saved_prompts SET synced_at = ? WHERE uuid = ?",
            [(synced_at, u) for u in uuids],
        )
        conn.commit()
    finally:
        conn.close()


def upsert_from_remote(entry):
    """Merge one remote entry into the local DB, last-write-wins on
    updated_at. Same logic as history.upsert_from_remote."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT updated_at FROM saved_prompts WHERE uuid = ?",
            (entry['uuid'],),
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT OR IGNORE INTO saved_prompts "
                "(uuid, ts, text, history_uuid, updated_at, deleted_at, synced_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (entry['uuid'], entry['ts'], entry['text'],
                 entry.get('history_uuid'), entry['updated_at'],
                 entry.get('deleted_at'), entry['updated_at']),
            )
        elif entry['updated_at'] > (row[0] or 0):
            conn.execute(
                "UPDATE saved_prompts SET text = ?, history_uuid = ?, "
                "updated_at = ?, deleted_at = ?, synced_at = ? WHERE uuid = ?",
                (entry['text'], entry.get('history_uuid'),
                 entry['updated_at'], entry.get('deleted_at'),
                 entry['updated_at'], entry['uuid']),
            )
        conn.commit()
    finally:
        conn.close()


# ------------------------------------------------------------------
# Local snapshot safety net
# ------------------------------------------------------------------

_SNAPSHOT_SCHEMA = 1


def snapshot_path():
    return os.path.join(config._CONFIG_DIR, 'saved-snapshot.json')


def write_snapshot():
    entries = all_entries_for_sync()
    path = snapshot_path()
    tmp = path + '.tmp'
    os.makedirs(config._CONFIG_DIR, exist_ok=True)
    with open(tmp, 'w') as f:
        json.dump({'schema': _SNAPSHOT_SCHEMA, 'written': time.time(),
                   'entries': entries}, f)
    os.replace(tmp, path)
    return len(entries)


def restore_from_snapshot_if_empty():
    """If the saved_prompts table holds no rows but a local snapshot exists,
    merge the snapshot back in. Checks saved_prompts count independently
    from history (Bug 3 fix)."""
    conn = _connect()
    try:
        n = conn.execute("SELECT COUNT(*) FROM saved_prompts").fetchone()[0]
    finally:
        conn.close()
    if n > 0:
        return 0
    path = snapshot_path()
    if not os.path.exists(path):
        return 0
    try:
        with open(path) as f:
            entries = json.load(f).get('entries', [])
    except (OSError, ValueError):
        return 0
    for e in entries:
        try:
            upsert_from_remote(e)
        except Exception:
            pass
    return len(entries)
