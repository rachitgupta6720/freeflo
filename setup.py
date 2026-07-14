import os
import stat
import shutil
from setuptools import setup

APP = ['app.py']

DATA_FILES = [
    ('', [
        os.path.expanduser('~/whisper.cpp/build-static/bin/whisper-cli'),
        os.path.expanduser('~/whisper.cpp/models/ggml-base.en.bin'),   # English (fast)
        os.path.expanduser('~/whisper.cpp/models/ggml-small.bin'),      # Multilingual (Hindi etc.)
    ]),
    # Window HTML assets -> Contents/Resources/ui (see config.get_ui_dir).
    ('ui', ['ui/index.html']),
]

# Bundle the Google OAuth client into Contents/Resources when it exists at
# build time, so the packaged .app can offer backup without a shell env var
# (a double-clicked app has none). scripts/release.sh writes this from the
# FREEFLO_GOOGLE_* env vars before building; it's .gitignored. Without it, the
# build still succeeds and the Backup tab simply shows "not available".
if os.path.exists('google_client.json'):
    DATA_FILES.append(('', ['google_client.json']))

OPTIONS = {
    'argv_emulation': False,
    'iconfile': 'freeflo.icns',
    'plist': {
        'LSUIElement': True,
        'LSMultipleInstancesProhibited': True,
        'CFBundleName': 'freeflo',
        'CFBundleDisplayName': 'freeflo',
        'CFBundleIdentifier': 'com.freeflo.app',
        'CFBundleVersion': '1.1.0',
        'CFBundleShortVersionString': '1.1.0',
        'NSMicrophoneUsageDescription': (
            'freeflo uses the microphone to transcribe your speech.'
        ),
        'NSPrincipalClass': 'NSApplication',
    },
    'packages': [
        # App modules
        'engine',
        'ui',
        # UI
        'rumps',
        'WebKit',
        # Audio
        'sounddevice',
        '_sounddevice_data',        # portaudio dylib lives here
        # Numerics
        'numpy',
        'scipy',
        # Clipboard
        'pyperclip',
        # cffi — C extension backend for sounddevice
        'cffi',
        # PyObjC frameworks (rumps + hotkey tap dependencies)
        'objc',
        'Cocoa',
        'AppKit',
        'Foundation',
        'Quartz',
        'ApplicationServices',
        'CoreText',
        # Google Drive backup — OAuth + REST, hand-rolled to skip the much
        # heavier google-api-python-client (httplib2, discovery docs, etc).
        # NOTE: `google` is a PEP 420 namespace package (no __init__.py), which
        # py2app cannot bundle via `packages` — doing so fails the build with
        # "No module named 'google'". The google.* modules are pulled in via
        # `includes` below instead; only real packages are listed here.
        'google_auth_oauthlib',
        'requests_oauthlib',
        'oauthlib',
        'requests',
        'urllib3',
        'certifi',
        'idna',
        'charset_normalizer',
        'cachetools',
        'pyasn1',
        'pyasn1_modules',
        'rsa',
        'cryptography',       # pulled in by google.auth.crypt (compiled ext)
        'keyring',
        'keyring.backends',   # ensure the macOS Keychain backend is bundled
    ],
    'includes': [
        'config',
        'hotkey',
        'engine.recorder',
        'engine.transcriber',
        'engine.injector',
        'engine.gauth',
        'engine.backup',
        # Keychain backend — imported explicitly by engine.gauth since py2app
        # can't rely on keyring's entry-point backend discovery when frozen.
        'keyring.backends.macOS',
        # google.* namespace-package modules the backup code uses (see the note
        # in `packages`). Listing the leaf modules lets modulegraph follow their
        # imports without py2app choking on the bare `google` namespace.
        'google.auth.transport.requests',
        'google.oauth2.credentials',
        # cffi C extension
        '_cffi_backend',
    ],
    'excludes': [
        'tkinter', 'matplotlib', 'PIL', 'wx',
        'IPython', 'jupyter',
    ],
    'strip': False,
    'optimize': 0,
}


def post_build_fix_permissions():
    """Ensure whisper-cli retains its executable bit after py2app copies it."""
    bundle = os.path.join('dist', 'freeflo.app')
    binary = os.path.join(bundle, 'Contents', 'Resources', 'whisper-cli')
    if os.path.exists(binary):
        current = os.stat(binary).st_mode
        os.chmod(binary, current | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        print(f'post-build: chmod +x {binary}')
    else:
        print(f'post-build WARNING: whisper-cli not found at {binary}')


# PEP 420 namespace packages (no __init__.py) cannot be imported from py2app's
# zipped site-packages, and py2app refuses to take them in `packages` (the build
# fails resolving the bare namespace). The backup feature pulls several in —
# `google` (google-auth) plus `jaraco` and `backports` (keyring's deps) — so we
# copy them into the bundle as loose directories after the build, where normal
# directory-based namespace resolution works.
_NAMESPACE_PACKAGES = ['google', 'jaraco', 'backports']


def post_build_bundle_namespace_pkgs():
    import glob
    import importlib
    libs = glob.glob(os.path.join('dist', 'freeflo.app', 'Contents',
                                   'Resources', 'lib', 'python3.*'))
    if not libs:
        print('post-build WARNING: bundle lib dir not found; skipping namespace copy')
        return
    dest_root = libs[0]
    site_packages = set()
    for name in _NAMESPACE_PACKAGES:
        try:
            mod = importlib.import_module(name)
            src = list(mod.__path__)[0]
        except Exception as e:
            print(f'post-build WARNING: cannot locate namespace package {name!r}: {e}')
            continue
        site_packages.add(os.path.dirname(src))
        dest = os.path.join(dest_root, name)
        if os.path.isdir(dest):
            shutil.rmtree(dest)
        shutil.copytree(src, dest,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        print(f'post-build: bundled namespace package {name} -> {dest}')

    # mypyc-compiled packages (e.g. charset_normalizer) import a separate
    # top-level *__mypyc*.so that nothing references statically, so py2app
    # doesn't bundle it and the package fails to import. Copy any such shared
    # objects in from site-packages. Their names are build-specific hashes,
    # hence the glob rather than a fixed module name.
    for sp in site_packages:
        for so in glob.glob(os.path.join(sp, '*__mypyc*.so')):
            shutil.copy2(so, os.path.join(dest_root, os.path.basename(so)))
            print(f'post-build: bundled compiled module {os.path.basename(so)}')


setup(
    name='freeflo',
    app=APP,
    data_files=DATA_FILES,
    options={'py2app': OPTIONS},
    setup_requires=['py2app'],
)

# Run post-build fixups after setup() completes
if __name__ == '__main__':
    import sys
    if 'py2app' in sys.argv:
        post_build_fix_permissions()
        post_build_bundle_namespace_pkgs()
