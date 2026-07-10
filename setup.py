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

OPTIONS = {
    'argv_emulation': False,
    'iconfile': 'freeflo.icns',
    'plist': {
        'LSUIElement': True,
        'LSMultipleInstancesProhibited': True,
        'CFBundleName': 'freeflo',
        'CFBundleDisplayName': 'freeflo',
        'CFBundleIdentifier': 'com.freeflo.app',
        'CFBundleVersion': '1.0.0',
        'CFBundleShortVersionString': '1.0.0',
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
        'google',
        'google.auth',
        'google.oauth2',
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
        'keyring',
    ],
    'includes': [
        'config',
        'hotkey',
        'engine.recorder',
        'engine.transcriber',
        'engine.injector',
        'engine.gauth',
        'engine.backup',
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


setup(
    name='freeflo',
    app=APP,
    data_files=DATA_FILES,
    options={'py2app': OPTIONS},
    setup_requires=['py2app'],
)

# Run permission fix after setup() completes
if __name__ == '__main__':
    import sys
    if 'py2app' in sys.argv:
        post_build_fix_permissions()
