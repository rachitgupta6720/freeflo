"""Sync dictation history with the user's own Google Drive, in the hidden
`appDataFolder` — invisible in the regular Drive UI, and scoped so freeflo
can only ever see files it created itself.

sync() handles both backup and restore with one code path: it downloads
whatever is already backed up, merges it into the local DB (this merge *is*
"restore on a new Mac" — a fresh device just has nothing local to keep), then
uploads a merged snapshot back. Local data never wins by default; `updated_at`
decides, so a delete or edit on one device reaches every other device.
"""
import json
import time

import requests

from engine import gauth, history

_FILE_NAME = 'freeflo-backup.json'
_SCHEMA_VERSION = 1
_DRIVE_FILES = 'https://www.googleapis.com/drive/v3/files'
_DRIVE_UPLOAD = 'https://www.googleapis.com/upload/drive/v3/files'


class NotConnected(Exception):
    pass


def _session():
    creds = gauth.get_credentials()
    if creds is None:
        raise NotConnected('Google Backup is not connected.')
    session = requests.Session()
    session.headers['Authorization'] = f'Bearer {creds.token}'
    return session


def _find_file(session):
    resp = session.get(_DRIVE_FILES, params={
        'spaces': 'appDataFolder',
        'q': f"name = '{_FILE_NAME}'",
        'fields': 'files(id)',
    }, timeout=15)
    resp.raise_for_status()
    files = resp.json().get('files', [])
    return files[0]['id'] if files else None


def _download(session, file_id):
    resp = session.get(f'{_DRIVE_FILES}/{file_id}', params={'alt': 'media'}, timeout=30)
    resp.raise_for_status()
    return resp.json().get('entries', [])


def _upload(session, file_id, entries):
    body = json.dumps({'schema': _SCHEMA_VERSION, 'entries': entries}).encode('utf-8')
    if file_id is None:
        metadata = json.dumps({'name': _FILE_NAME, 'parents': ['appDataFolder']}).encode('utf-8')
        resp = session.post(
            _DRIVE_UPLOAD, params={'uploadType': 'multipart'},
            files={
                'metadata': ('metadata', metadata, 'application/json'),
                'file': ('file', body, 'application/json'),
            },
            timeout=30,
        )
    else:
        resp = session.patch(
            f'{_DRIVE_UPLOAD}/{file_id}', params={'uploadType': 'media'},
            data=body, headers={'Content-Type': 'application/json'},
            timeout=30,
        )
    resp.raise_for_status()


def sync():
    """Pull the remote backup, merge it locally, push the merged result back.
    Returns a small summary dict for the UI."""
    session = _session()
    file_id = _find_file(session)
    remote_entries = _download(session, file_id) if file_id else []

    for entry in remote_entries:
        history.upsert_from_remote(entry)

    merged = history.all_entries_for_sync()
    _upload(session, file_id, merged)

    now = time.time()
    history.mark_synced([e['uuid'] for e in merged], now)

    return {'pulled': len(remote_entries), 'total': len(merged), 'synced_at': now}


def delete_remote():
    """Remove the backup file from Drive entirely (used when the user asks to
    disconnect and forget, not on a plain disconnect)."""
    session = _session()
    file_id = _find_file(session)
    if file_id:
        resp = session.delete(f'{_DRIVE_FILES}/{file_id}', timeout=15)
        resp.raise_for_status()
