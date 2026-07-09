import os
import sys
import json

_APP_SUPPORT = os.path.expanduser('~/Library/Application Support')
_CONFIG_DIR = os.path.join(_APP_SUPPORT, 'freeflo')
_CONFIG_FILE = os.path.join(_CONFIG_DIR, 'config.json')
# Settings used to live here under the app's old name — migrate on first run.
_LEGACY_CONFIG_FILE = os.path.join(_APP_SUPPORT, 'WhisperDictate', 'config.json')

_DEFAULTS = {
    'enabled': True,
    'language': 'en',      # ISO 639-1 code, or 'auto' for auto-detect
    'ptt_key': 'left_option',    # push-to-talk (hold) key
    'toggle_key': 'right_option',  # toggle (tap on/off) key
    'save_history': True,        # log transcriptions to the local history DB
}

# Selectable hotkey keys. Each carries the virtual keycode of the physical key
# and the device-independent modifier mask that key sets (Alt=0x80000,
# Command=0x100000, Control=0x40000). The mask MUST match the key — checking a
# global Option mask breaks Command/Control keys.
HOTKEY_KEYS = {
    'left_option':   {'keycode': 0x3A, 'mask': 0x80000,  'label': 'Left Option (⌥)'},
    'right_option':  {'keycode': 0x3D, 'mask': 0x80000,  'label': 'Right Option (⌥)'},
    'right_command': {'keycode': 0x36, 'mask': 0x100000, 'label': 'Right Command (⌘)'},
    'right_control': {'keycode': 0x3E, 'mask': 0x40000,  'label': 'Right Control (⌃)'},
}


def resolve_key(name):
    """Return the {keycode, mask, label} for a key name, falling back safely."""
    return HOTKEY_KEYS.get(name) or HOTKEY_KEYS['left_option']


def _migrate_legacy():
    """Copy settings from the old WhisperDictate dir if we have none yet."""
    if os.path.exists(_CONFIG_FILE) or not os.path.exists(_LEGACY_CONFIG_FILE):
        return
    try:
        os.makedirs(_CONFIG_DIR, exist_ok=True)
        with open(_LEGACY_CONFIG_FILE) as f:
            data = json.load(f)
        with open(_CONFIG_FILE, 'w') as f:
            json.dump(data, f, indent=2)
    except (OSError, ValueError):
        pass


def _resources_dir():
    """Returns the Resources path when running as a packaged .app, else None."""
    if getattr(sys, 'frozen', False):
        return os.path.normpath(
            os.path.join(os.path.dirname(sys.executable), '..', 'Resources')
        )
    return None


def get_whisper_cli():
    r = _resources_dir()
    if r:
        return os.path.join(r, 'whisper-cli')
    return os.path.expanduser('~/whisper.cpp/build-static/bin/whisper-cli')


def get_ui_dir():
    """Directory holding the window's HTML assets. When frozen they live in
    Contents/Resources/ui (shipped via setup.py DATA_FILES); from source they
    sit next to this file in ./ui."""
    r = _resources_dir()
    if r:
        return os.path.join(r, 'ui')
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), 'ui')


def get_model_path(language='en'):
    """Return the appropriate model path for the given language.
    English uses the fast .en base model (high accuracy, low latency).
    Every other language uses the multilingual `small` model — `base` is too
    weak for non-Latin scripts like Hindi (it romanises / mixes scripts),
    while `small` produces clean Devanagari."""
    model_file = 'ggml-base.en.bin' if language == 'en' else 'ggml-small.bin'
    r = _resources_dir()
    if r:
        return os.path.join(r, model_file)
    return os.path.expanduser(f'~/whisper.cpp/models/{model_file}')


def load():
    os.makedirs(_CONFIG_DIR, exist_ok=True)
    _migrate_legacy()
    if not os.path.exists(_CONFIG_FILE):
        save(_DEFAULTS.copy())
        return _DEFAULTS.copy()
    with open(_CONFIG_FILE) as f:
        data = json.load(f)
    return {**_DEFAULTS, **data}


def save(data):
    os.makedirs(_CONFIG_DIR, exist_ok=True)
    with open(_CONFIG_FILE, 'w') as f:
        json.dump(data, f, indent=2)
