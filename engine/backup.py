"""Sync dictation history with the user's own Google Drive, in the hidden
`appDataFolder` — invisible in the regular Drive UI, and scoped so freeflo
can only ever see files it created itself.

sync() handles both backup and restore with one code path: it downloads
whatever is already backed up, merges it into the local DB (this merge *is*
"restore on a new Mac" — a fresh device just has nothing local to keep), then
uploads a merged snapshot back. Local data never wins by default; `updated_at`
decides, so a delete or edit on one device reaches every other device.

Saved prompts live in a separate Drive file (freeflo-saved.json) so that old
app versions — which hardcode the upload payload for freeflo-backup.json —
can never clobber saved data during a backup cycle.
"""
import json
import time

import requests

from engine import gauth, history, saved

_FILE_NAME = 'freeflo-backup.json'
_SAVED_FILE_NAME = 'freeflo-saved.json'
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


def _find_file_by_name(session, name):
    resp = session.get(_DRIVE_FILES, params={
        'spaces': 'appDataFolder',
        'q': f"name = '{name}'",
        'fields': 'files(id)',
    }, timeout=15)
    resp.raise_for_status()
    files = resp.json().get('files', [])
    return files[0]['id'] if files else None


def _find_file(session):
    return _find_file_by_name(session, _FILE_NAME)


def _download(session, file_id):
    resp = session.get(f'{_DRIVE_FILES}/{file_id}', params={'alt': 'media'}, timeout=30)
    resp.raise_for_status()
    return resp.json()


def _upload(session, file_id, name, blob):
    """Upload a JSON blob to Drive. When updating an existing file, unknown
    keys from a previous download are preserved — this future-proofs the
    format so newer app versions can add keys without older versions
    clobbering them on re-upload."""
    body = json.dumps(blob).encode('utf-8')
    if file_id is None:
        metadata = json.dumps({'name': name, 'parents': ['appDataFolder']}).encode('utf-8')
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
    remote_blob = _download(session, file_id) if file_id else {}
    remote_entries = remote_blob.get('entries', [])

    for entry in remote_entries:
        history.upsert_from_remote(entry)

    now = time.time()
    pushed = 0
    if history.dirty_entries() or file_id is None:
        merged = history.all_entries_for_sync()
        blob = dict(remote_blob)
        blob['schema'] = _SCHEMA_VERSION
        blob['entries'] = merged
        _upload(session, file_id, _FILE_NAME, blob)
        history.mark_synced([e['uuid'] for e in merged], now)
        pushed = len(merged)

    return {'pulled': len(remote_entries), 'pushed': pushed, 'synced_at': now}


def sync_saved():
    """Pull/merge/push saved prompts via a separate Drive file so old app
    versions can never clobber them."""
    session = _session()
    file_id = _find_file_by_name(session, _SAVED_FILE_NAME)
    remote_blob = _download(session, file_id) if file_id else {}
    remote_entries = remote_blob.get('entries', [])

    for entry in remote_entries:
        saved.upsert_from_remote(entry)

    now = time.time()
    pushed = 0
    if saved.dirty_entries() or file_id is None:
        merged = saved.all_entries_for_sync()
        blob = dict(remote_blob)
        blob['schema'] = _SCHEMA_VERSION
        blob['entries'] = merged
        _upload(session, file_id, _SAVED_FILE_NAME, blob)
        saved.mark_synced([e['uuid'] for e in merged], now)
        pushed = len(merged)

    return {'pulled': len(remote_entries), 'pushed': pushed, 'synced_at': now}


def delete_remote():
    """Remove the backup file from Drive entirely (used when the user asks to
    disconnect and forget, not on a plain disconnect)."""
    session = _session()
    file_id = _find_file(session)
    if file_id:
        resp = session.delete(f'{_DRIVE_FILES}/{file_id}', timeout=15)
        resp.raise_for_status()


def delete_remote_saved():
    """Remove the saved prompts backup from Drive."""
    session = _session()
    file_id = _find_file_by_name(session, _SAVED_FILE_NAME)
    if file_id:
        resp = session.delete(f'{_DRIVE_FILES}/{file_id}', timeout=15)
        resp.raise_for_status()
