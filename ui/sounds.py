"""Optional audio cues for dictation start / stop.

Uses the sounds that ship with macOS (/System/Library/Sounds) through NSSound,
so nothing is bundled. Off by default: the caller opts in with
set_enabled(True), typically from a user setting.

    from ui import sounds
    sounds.set_enabled(True)     # loads the sounds so the first cue is instant
    sounds.play_start()          # hotkey down
    sounds.play_stop()           # hotkey up

Every function is safe to call from any thread, never raises, and returns at
once - playback is handed to the main run loop and NSSound plays
asynchronously from there.

Note for the caller: a start cue played through the speakers is picked up by
the microphone at the very beginning of the recording. It is short and quiet
enough not to be transcribed, but if that ever shows up, play the start cue
just before asking the helper to start rather than after.
"""
import logging
import threading

log = logging.getLogger('freeflo.sounds')

try:
    from AppKit import NSSound
    _HAVE_APPKIT = True
except Exception:                       # PyObjC missing / not on macOS
    _HAVE_APPKIT = False

# Names of system sounds (see /System/Library/Sounds). Both are brief.
START_SOUND = 'Tink'
STOP_SOUND = 'Pop'
ERROR_SOUND = 'Funk'

_lock = threading.Lock()
_enabled = False
_volume = 0.35                          # 0..1, relative to the system volume
_names = {'start': START_SOUND, 'stop': STOP_SOUND, 'error': ERROR_SOUND}
_cache = {}                             # sound name -> NSSound (or None)


def set_enabled(enabled):
    """Turn cues on or off. Turning them on preloads the sounds."""
    global _enabled
    try:
        _enabled = bool(enabled)
        if _enabled:
            preload()
    except Exception:
        log.debug('set_enabled failed', exc_info=True)


def is_enabled():
    return _enabled


def set_volume(volume):
    """Cue loudness, 0.0-1.0 (scaled by the system output volume)."""
    global _volume
    try:
        v = float(volume)
        if v != v:
            return
        _volume = min(1.0, max(0.0, v))
        with _lock:
            cached = [s for s in _cache.values() if s is not None]
        for snd in cached:
            snd.setVolume_(_volume)
    except Exception:
        log.debug('set_volume failed', exc_info=True)


def set_sounds(start=None, stop=None, error=None):
    """Choose different system sounds by name (e.g. 'Morse', 'Bottle')."""
    try:
        for key, name in (('start', start), ('stop', stop), ('error', error)):
            if name:
                _names[key] = str(name)
        if _enabled:
            preload()
    except Exception:
        log.debug('set_sounds failed', exc_info=True)


def preload():
    """Load the cue sounds now so the first play has no disk latency."""
    try:
        for name in list(_names.values()):
            _get(name)
    except Exception:
        log.debug('preload failed', exc_info=True)


def play_start():
    _play('start')


def play_stop():
    _play('stop')


def play_error():
    _play('error')


def _get(name):
    with _lock:
        if name in _cache:
            return _cache[name]
    snd = None
    if _HAVE_APPKIT:
        shared = NSSound.soundNamed_(name)
        if shared is not None:
            # Our own copy: volume changes must not leak into the shared
            # named instance other code in the process may be using.
            snd = shared.copy()
            snd.setVolume_(_volume)
        else:
            log.info('system sound %r not found', name)
    with _lock:
        _cache.setdefault(name, snd)
        return _cache[name]


def _play(key):
    try:
        if not _enabled or not _HAVE_APPKIT:
            return
        snd = _get(_names[key])
        if snd is None:
            return
        # Always asynchronous, always on the main thread: the caller (often
        # the hotkey path) never waits on the audio hardware. stop first so a
        # cue that is still ringing restarts instead of being ignored.
        snd.performSelectorOnMainThread_withObject_waitUntilDone_(
            'stop', None, False)
        snd.performSelectorOnMainThread_withObject_waitUntilDone_(
            'play', None, False)
    except Exception:
        log.debug('play %s failed', key, exc_info=True)
