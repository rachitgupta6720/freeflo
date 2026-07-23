"""Download, verify, and delete Turbo-mode models. Runs on a background thread —
report progress via the on_progress callback. Never raises into the UI thread;
callers show the returned error string."""
import hashlib
import os

import requests

import config
from engine import models


def _free_bytes(path):
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize


def download(tier, on_progress=None, should_cancel=None):
    """Download one model tier. Returns (ok: bool, error: str|None).
    on_progress(pct_float_0_100, bytes_done, bytes_total) is called as it streams.
    should_cancel() -> bool lets the UI abort."""
    m = models.get(tier)
    dest = config.get_turbo_model_path(tier)
    part = dest + '.part'
    total = m['size_bytes']

    # 1. disk-space pre-check (need the file + 500 MB headroom)
    if _free_bytes(config.get_models_dir()) < total + 500 * 1024 * 1024:
        return False, 'Not enough free disk space for this model.'

    # 2. stream to a .part temp file
    try:
        with requests.get(m['url'], stream=True, timeout=30) as r:
            r.raise_for_status()
            got = 0
            with open(part, 'wb') as f:
                for chunk in r.iter_content(chunk_size=1 << 20):   # 1 MB
                    if should_cancel and should_cancel():
                        f.close()
                        _safe_remove(part)
                        return False, 'Cancelled.'
                    if chunk:
                        f.write(chunk)
                        got += len(chunk)
                        if on_progress:
                            on_progress(min(100.0, got * 100.0 / total), got, total)
    except Exception as e:
        _safe_remove(part)
        return False, f'Download failed: {e}'

    # 3. verify checksum if we pinned one
    expected = m.get('sha256')
    if expected:
        if _sha256(part) != expected:
            _safe_remove(part)
            return False, 'Downloaded file failed integrity check.'

    # 4. atomically move into place + record it
    os.replace(part, dest)
    s = config.load()
    if tier not in s['turbo_models_installed']:
        s['turbo_models_installed'].append(tier)
        config.save(s)
    return True, None


def delete(tier):
    _safe_remove(config.get_turbo_model_path(tier))
    s = config.load()
    s['turbo_models_installed'] = [t for t in s['turbo_models_installed'] if t != tier]
    config.save(s)


def _sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def _safe_remove(path):
    try:
        os.remove(path)
    except OSError:
        pass
