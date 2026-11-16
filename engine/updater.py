"""Self-update via GitHub Releases — no backend required.

Finds the newest published release this Mac can run, compares its version to
the running one, and (on request) downloads the ``.zip`` asset into ~/Downloads
and reveals it in Finder for a drag-to-Applications install. The release body
doubles as the "What's new" shown to the user.

Two release lines share the repository:

* up to 1.4.x — the Whisper build. Runs on any macOS and on Intel; its updater
  takes whatever ``releases/latest`` carries as ``freeflo.zip``, with no check
  of what that build needs.
* this one — needs macOS 26+ on Apple silicon. It is published under its OWN
  asset name (``_ASSET_NAME``), so a 1.4.x client can never download it over a
  working install on a Mac that cannot run it, and it only ever offers
  releases that carry that asset (never the old Whisper build as an "update").
  It reads the release *list* rather than ``releases/latest``, so it works
  whether or not these releases are flagged as the repository's latest.

A release may also declare what it needs in its body::

    <!-- freeflo-requires: macos=26.0 arch=arm64 -->

(written by scripts/release.sh). A release whose requirements this Mac does
not meet is skipped instead of being offered.

Everything here fails silently to ``None`` — a missing network, a rate-limit, or
a malformed response must never disrupt dictation.
"""
import os
import platform
import re
import subprocess

import requests

# Owner/repo the app ships from (see scripts/release.sh).
GITHUB_REPO = 'rachitgupta6720/freeflo'
_RELEASES = f'https://api.github.com/repos/{GITHUB_REPO}/releases?per_page=30'
# Kept in sync with ASSET in scripts/release.sh.
_ASSET_NAME = 'freeflo-macos26.zip'

_REQUIRES = re.compile(r'<!--\s*freeflo-requires:(.*?)-->', re.S)
_AUTO = object()


def _parse(v):
    """'v1.2.3' | '1.2.3' -> (1, 2, 3). Missing/odd parts degrade to (0,)."""
    nums = re.findall(r'\d+', v or '')
    return tuple(int(n) for n in nums[:3]) or (0,)


def parse_requirements(body):
    """What a release says it needs: ``{'macos': (26, 0), 'arch': 'arm64'}``
    (either key may be missing; ``{}`` when the body declares nothing)."""
    out = {}
    m = _REQUIRES.search(body or '')
    if not m:
        return out
    macos = re.search(r'macos\s*=\s*(\d+(?:\.\d+)*)', m.group(1))
    if macos:
        out['macos'] = tuple(int(n) for n in macos.group(1).split('.')[:2])
    arch = re.search(r'arch\s*=\s*([A-Za-z0-9_]+)', m.group(1))
    if arch:
        out['arch'] = arch.group(1).lower()
    return out


def _running_macos():
    # The same trusted-or-None answer the speech engine gates on: None means
    # "cannot tell", and an unknown version never blocks an update.
    try:
        from engine import speech
        return speech.macos_version()
    except Exception:
        return None


def is_compatible(requirements, macos, arch):
    """Can a Mac running ``macos`` ((major, minor) or None = unknown) on
    ``arch`` ('arm64' / 'x86_64' / None) run a release with these
    requirements?"""
    need = requirements.get('macos')
    if need and macos and tuple(macos) < tuple(need):
        return False
    need_arch = requirements.get('arch')
    if need_arch and arch and arch.lower() != need_arch:
        return False
    return True


def check_for_update(current_version, timeout=8, macos=_AUTO, arch=_AUTO):
    """Return an update-info dict for the newest release that is newer than
    ``current_version``, carries this line's download, and can run on this
    Mac — else ``None``. Never raises.

    Dict shape::

        {version, tag, notes, url, download_url, published_at}
    """
    try:
        resp = requests.get(
            _RELEASES, timeout=timeout,
            headers={'Accept': 'application/vnd.github+json'},
        )
        resp.raise_for_status()
        releases = resp.json()
    except Exception:
        return None
    if not isinstance(releases, list):
        return None

    if macos is _AUTO:
        macos = _running_macos()
    if arch is _AUTO:
        arch = platform.machine() or None

    current = _parse(current_version)
    best = None
    for data in releases:
        try:
            if not isinstance(data, dict) or data.get('draft') or data.get('prerelease'):
                continue
            tag = data.get('tag_name') or data.get('name') or ''
            version = _parse(tag)
            if version <= current or (best and version <= best[0]):
                continue
            download_url = None
            for asset in data.get('assets') or []:
                if asset.get('name') == _ASSET_NAME:
                    download_url = asset.get('browser_download_url')
                    break
            if not download_url:
                continue        # not a build of this line (or not uploaded yet)
            body = data.get('body') or ''
            if not is_compatible(parse_requirements(body), macos, arch):
                continue        # needs a newer macOS / another CPU: not offered
            best = (version, {
                'version': tag.lstrip('v'),
                'tag': tag,
                'notes': _REQUIRES.sub('', body).strip(),
                'url': data.get('html_url'),
                'download_url': download_url,
                'published_at': data.get('published_at'),
            })
        except Exception:
            continue
    return best[1] if best else None


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
