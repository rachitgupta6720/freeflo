import os
import sys
import stat
import shutil
from setuptools import setup

APP = ['app.py']

ROOT = os.path.dirname(os.path.abspath(__file__))

# The native speech helper (microphone + on-device transcription). Built by
# scripts/build_helper.sh into build/ — which is also py2app's scratch
# directory, so never `rm -rf build` between building the helper and the app
# (scripts/release.sh cleans first, then builds the helper).
HELPER = os.path.join(ROOT, 'build', 'freeflo-helper')


def find_llama_server():
    """Turbo mode's llama.cpp server binary, built STATIC/self-contained so it
    carries no @rpath dylibs or Homebrew deps. Looked for, in order:
    $FREEFLO_LLAMA_SERVER, ./vendor/llama-server (gitignored), then a local
    build at ~/llama.cpp/build-static. Returns None if there is none — the app
    still builds, with Turbo unavailable. Models are NOT bundled; they are
    downloaded on demand."""
    candidates = [
        os.environ.get('FREEFLO_LLAMA_SERVER') or '',
        os.path.join(ROOT, 'vendor', 'llama-server'),
        os.path.expanduser('~/llama.cpp/build-static/bin/llama-server'),
    ]
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return None


BUILDING = 'py2app' in sys.argv
BINARIES = []
if BUILDING:
    if not os.path.isfile(HELPER):
        sys.exit('setup.py: build/freeflo-helper is missing - run '
                 'scripts/build_helper.sh first (dictation cannot work without it).')
    BINARIES.append(HELPER)
    LLAMA_SERVER = find_llama_server()
    if LLAMA_SERVER:
        print(f'setup.py: bundling llama-server from {LLAMA_SERVER}')
        BINARIES.append(LLAMA_SERVER)
    else:
        print('setup.py: WARNING - no llama-server found (set FREEFLO_LLAMA_SERVER '
              'or put one at vendor/llama-server). Building WITHOUT Turbo mode.')

DATA_FILES = [
    ('', BINARIES),
    # Window HTML assets -> Contents/Resources/ui (see config.get_ui_dir).
    ('ui', ['ui/index.html', 'ui/onboarding.html']),
]

# Bundle the Google OAuth client into Contents/Resources when it exists at
# build time, so the packaged .app can offer backup without a shell env var
# (a double-clicked app has none). scripts/release.sh writes this from the
# FREEFLO_GOOGLE_* env vars before building; it's .gitignored. Without it, the
# build still succeeds and the Backup tab simply shows "not available".
if os.path.exists('google_client.json'):
    DATA_FILES.append(('', ['google_client.json']))

# Telemetry keys (PostHog + Sentry), same pattern as google_client.json: bundled
# when present, .gitignored, and absent → telemetry stays a no-op.
if os.path.exists('telemetry.json'):
    DATA_FILES.append(('', ['telemetry.json']))

OPTIONS = {
    'argv_emulation': False,
    'iconfile': 'freeflo.icns',
    'plist': {
        'LSUIElement': True,
        'LSMultipleInstancesProhibited': True,
        'CFBundleName': 'freeflo',
        'CFBundleDisplayName': 'freeflo',
        'CFBundleIdentifier': 'com.freeflo.app',
        'CFBundleVersion': '1.5.0',
        'CFBundleShortVersionString': '1.5.0',
        # The on-device transcriber (Speech framework: SpeechAnalyzer) only
        # exists on macOS 26+. Older systems refuse to launch the app with the
        # standard "requires a newer macOS" message instead of a broken one.
        'LSMinimumSystemVersion': '26.0',
        # Asked for by the bundled freeflo-helper (a child of this app, so the
        # prompt and the Privacy list entry are freeflo's). Speech Recognition
        # permission is NOT needed: SpeechAnalyzer transcribes on device
        # without it, so there is no NSSpeechRecognitionUsageDescription.
        'NSMicrophoneUsageDescription': (
            'freeflo listens while you hold your dictation key and turns '
            'what you say into text, entirely on this Mac.'
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
        # (No audio / numerics packages: the microphone and the transcriber
        # live in the bundled native helper, not in Python.)
        # PyObjC frameworks (rumps, hotkey tap, recording pill, pasteboard)
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
        # Opt-in telemetry (engine.telemetry) via PostHog — analytics, identity,
        # AND crash/error tracking. Bundled regardless of keys; inert without a
        # telemetry.json / env vars, and gated on user consent. (Sentry is an
        # optional extra the code supports but we don't ship it.)
        'posthog',
    ],
    'includes': [
        'config',
        'hotkey',
        'engine.speech',
        'engine.locales',
        'engine.vocabulary',
        'engine.injector',
        'engine.gauth',
        'engine.backup',
        'engine.logs',
        'engine.updater',
        'engine.telemetry',
        'engine.setup_state',
        'ui.hud',
        'ui.sounds',
        'ui.window',      # imported lazily (WebKit) when a window first opens
        # engine.injector reads the keyboard layout through Carbon (to find
        # the key that types "v" for Cmd+V); it imports ctypes lazily.
        'ctypes',
        # Keychain backend — imported explicitly by engine.gauth since py2app
        # can't rely on keyring's entry-point backend discovery when frozen.
        'keyring.backends.macOS',
        # google.* namespace-package modules the backup code uses (see the note
        # in `packages`). Listing the leaf modules lets modulegraph follow their
        # imports without py2app choking on the bare `google` namespace.
        'google.auth.transport.requests',
        'google.auth.exceptions',   # RefreshError — engine.gauth.is_auth_expired
        'google.oauth2.credentials',
        # cffi backend: not ours any more (it was sounddevice's), but
        # `cryptography` (google-auth) loads it at import time and nothing
        # references it statically.
        '_cffi_backend',
    ],
    'excludes': [
        'tkinter', 'matplotlib', 'PIL', 'wx',
        'IPython', 'jupyter',
        # Retired with the Whisper/PortAudio engine. Excluded explicitly so a
        # stray copy in the build environment cannot ride along.
        'numpy', 'scipy', 'sounddevice', '_sounddevice_data', 'pyperclip',
        # backports.tarfile ships a dist-info that py2app's collector duplicates
        # ("[Errno 17] File exists"). It's a keyring/jaraco build-time dep, not
        # needed at runtime, and the whole `backports` namespace is copied loose
        # in post_build_bundle_namespace_pkgs anyway.
        'backports.tarfile',
    ],
    'strip': False,
    'optimize': 0,
}


def strip_conflicting_dist_info():
    """py2app 0.28 aborts while collecting ``backports.tarfile``'s dist-info with
    "[Errno 17] File exists" — a bug in its namespace-package dist-info collector.
    The package is still needed at runtime (jaraco.context imports it) and IS
    bundled as a loose namespace dir by post_build_bundle_namespace_pkgs, so we
    remove just its dist-info *metadata* from the build environment to sidestep
    the clash without affecting imports."""
    import glob
    import shutil
    import sysconfig
    try:
        site_dir = sysconfig.get_paths()['purelib']
    except Exception:
        return
    for d in glob.glob(os.path.join(site_dir, 'backports.tarfile-*.dist-info')):
        shutil.rmtree(d, ignore_errors=True)
        print(f'pre-build: removed {os.path.basename(d)} (py2app dist-info collision)')


def post_build_fix_permissions():
    """Ensure bundled binaries retain their executable bit after py2app copies
    them (freeflo-helper for speech->text, llama-server for Turbo mode)."""
    bundle = os.path.join('dist', 'freeflo.app')
    for name, required in (('freeflo-helper', True), ('llama-server', False)):
        binary = os.path.join(bundle, 'Contents', 'Resources', name)
        if os.path.exists(binary):
            current = os.stat(binary).st_mode
            os.chmod(binary, current | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            print(f'post-build: chmod +x {binary}')
        elif required:
            sys.exit(f'post-build ERROR: {name} is missing from the bundle ({binary})')
        else:
            print(f'post-build WARNING: {name} not in the bundle - Turbo mode unavailable')


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


if BUILDING:
    strip_conflicting_dist_info()

setup(
    name='freeflo',
    app=APP,
    data_files=DATA_FILES,
    options={'py2app': OPTIONS},
    setup_requires=['py2app'],
)

# Run post-build fixups after setup() completes
if __name__ == '__main__':
    if BUILDING:
        post_build_fix_permissions()
        post_build_bundle_namespace_pkgs()
        # The copies above break the seal py2app made, and macOS keys the
        # Microphone / Accessibility grants to the signature: signing is a
        # separate step (scripts/dev_sign.sh), never done here.
        print('post-build: now run scripts/dev_sign.sh '
              '(the bundle is not sealed until it is signed)')
