"""Self-update via GitHub Releases — no backend required.

Checks the latest published release, compares its version to the running one,
and (on request) downloads the ``.zip`` asset into ~/Downloads and reveals it in
Finder for a drag-to-Applications install. The release body doubles as the
"What's new" shown to the user.

Everything here fails silently to ``None`` — a missing network, a rate-limit, or
a malformed response must never disrupt dictation.
"""
import os
import re
import subprocess

import requests

# Owner/repo the app ships from (see scripts/release.sh).
GITHUB_REPO = 'rachitgupta6720/freeflo'
_LATEST = f'https://api.github.com/repos/{GITHUB_REPO}/releases/latest'
_ASSET_NAME = 'freeflo.zip'


def _parse(v):
    """'v1.2.3' | '1.2.3' -> (1, 2, 3). Missing/odd parts degrade to (0,)."""
    nums = re.findall(r'\d+', v or '')
    return tuple(int(n) for n in nums[:3]) or (0,)


def check_for_update(current_version, timeout=8):
    """Return an update-info dict when the latest release is newer than
    ``current_version``, else ``None``. Never raises.

    Dict shape::

        {version, tag, notes, url, download_url, published_at}
    """
    try:
        resp = requests.get(
            _LATEST, timeout=timeout,
            headers={'Accept': 'application/vnd.github+json'},
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return None

    if data.get('draft') or data.get('prerelease'):
        return None

    tag = data.get('tag_name') or data.get('name') or ''
    if _parse(tag) <= _parse(current_version):
        return None

    download_url = None
    for asset in data.get('assets', []):
        if asset.get('name') == _ASSET_NAME:
            download_url = asset.get('browser_download_url')
            break

    return {
        'version': tag.lstrip('v'),
        'tag': tag,
        'notes': (data.get('body') or '').strip(),
        'url': data.get('html_url'),
        'download_url': download_url,
        'published_at': data.get('published_at'),
    }


def download_update(download_url, version, timeout=180):
    """Stream the release zip into ~/Downloads and return its path. Raises on
    failure so the caller can fall back to opening the release page."""
    if not download_url:
        raise ValueError('This release has no downloadable asset.')
    dest_dir = os.path.expanduser('~/Downloads')
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, f'freeflo-{version}.zip')
    with requests.get(download_url, stream=True, timeout=timeout) as r:
        r.raise_for_status()
        with open(dest, 'wb') as f:
            for chunk in r.iter_content(chunk_size=65536):
                if chunk:
                    f.write(chunk)
    return dest


def reveal(path):
    """Reveal a file in Finder."""
    subprocess.run(['open', '-R', path], capture_output=True)


def open_url(url):
    if url:
        subprocess.run(['open', url], capture_output=True)
