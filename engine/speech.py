"""Supervisor/client for the native speech helper (``freeflo-helper serve``).

Why a helper process at all
---------------------------
The previous engine captured audio in-process through PortAudio. A PortAudio
stream teardown can deadlock inside CoreAudio, and a thread stuck in a native
call cannot be cancelled or killed -- so the app could only say "Audio stalled,
restart freeflo". The microphone and the transcriber now live in a small child
process instead. A *process* that hangs can always be killed (SIGKILL) and
replaced, which is what this module does. The user never has to restart the
app: the worst case is one dictation that reports an error.

What this module guarantees
---------------------------
* ``start()`` / ``stop()`` / ``cancel()`` and every other public method return
  immediately. They only take a short lock, append to a queue and poke a pipe.
  They are safe to call from the CGEventTap callback thread and from the main
  thread, where blocking would kill the hotkey or freeze the app.
* Every session id returned by ``start()`` ends with EXACTLY ONE terminal
  callback: ``on_final``, ``on_cancelled`` or ``on_error``. Never zero (so the
  app cannot be left on "processing" forever), never two.
* Events belonging to a session that was cancelled, superseded or already
  finished are dropped; callbacks only ever see the live session.
* If the helper hangs, crashes, stops reading, or talks nonsense it is
  SIGKILLed and respawned. Sessions in flight get their one error; the next
  dictation works with no user action. Respawns back off if it crash-loops.
* A missing binary or an OS older than macOS 26 puts the engine in a clear
  ``unavailable`` state with a message, instead of retrying forever.

Threads
-------
``speech-io``  owns the helper process: spawning, all reads and writes,
               deadlines, kill/respawn. All supervisor state is confined to
               this thread, so there are no lock-ordering problems.
``speech-cb``  runs EVERY callback, one at a time, in the order the events
               happened. Callbacks never run on the caller's thread and never
               on the main thread: marshal to the main thread yourself before
               touching AppKit/rumps. A slow callback only delays later
               callbacks -- it cannot stall supervision -- but keep them short
               and hand heavy work (refine, paste) to your own thread, or the
               level meter of the next dictation will lag.

Standard library only: no numpy / scipy / sounddevice / PyObjC.
"""
import atexit
import collections
import errno
import itertools
import json
import logging
import os
import platform
import queue
import selectors
import subprocess
import threading
import time
import weakref

log = logging.getLogger('freeflo.speech')

MIN_MACOS = (26, 0)

# Error codes the *helper* reports (passed through untouched).
HELPER_ERROR_CODES = (
    'mic_denied', 'speech_denied', 'no_input_device', 'locale_unsupported',
    'locale_not_installed', 'engine_failed', 'bad_command', 'internal',
    # language downloads (install)
    'network', 'no_space', 'cancelled', 'install_failed',
)

# Error codes this supervisor adds.
E_HELPER_MISSING = 'helper_missing'          # binary absent / not launchable
E_WRONG_ARCH = 'wrong_arch'                  # helper is arm64, this Mac is not
E_OS_TOO_OLD = 'os_too_old'                  # macOS < 26
E_HELPER_CRASHED = 'helper_crashed'          # process exited or pipe broke
E_HELPER_TIMEOUT = 'helper_timeout'          # an operation blew its deadline
E_HELPER_PROTOCOL = 'helper_protocol'        # unparseable output
E_HELPER_UNAVAILABLE = 'helper_unavailable'  # could not get a helper up in time

# Short, human status lines for each code (menu bar / pill). The ``message``
# passed to ``on_error`` is the technical detail for the log; show these.
USER_MESSAGES = {
    'mic_denied': 'Microphone access is off - allow it in System Settings',
    'speech_denied': 'Speech Recognition access is off - allow it in System Settings',
    'no_input_device': 'No microphone found',
    'locale_unsupported': 'That language is not supported for dictation',
    'locale_not_installed': 'That language needs a one-time download',
    'engine_failed': 'Microphone hiccup - try again',
    'bad_command': 'Something went wrong - try again',
    'internal': 'Something went wrong - try again',
    'network': 'No internet - the language could not be downloaded',
    'no_space': 'Not enough free space to download the language',
    'install_failed': 'The language download did not finish - try again',
    'cancelled': 'Download cancelled',
    E_HELPER_MISSING: 'freeflo is incomplete - please reinstall it',
    E_WRONG_ARCH: 'freeflo needs a Mac with Apple silicon',
    E_OS_TOO_OLD: 'freeflo needs macOS 26 or later',
    E_HELPER_CRASHED: 'Dictation restarted - try again',
    E_HELPER_TIMEOUT: 'Dictation restarted - try again',
    E_HELPER_PROTOCOL: 'Dictation restarted - try again',
    E_HELPER_UNAVAILABLE: 'Dictation is unavailable right now - try again',
}


def user_message(code):
    """A short status line for an error code (never the raw technical text)."""
    return USER_MESSAGES.get(code, 'Something went wrong - try again')


# Session states (speech-io thread only).
_PENDING = 'pending'        # start not yet written to the helper
_STARTING = 'starting'      # start written, waiting for started/error
_RECORDING = 'recording'    # mic live
_STOPPING = 'stopping'      # stop written, waiting for final/error

# Engine states, reported through on_state and the ``state`` property.
STATE_IDLE = 'idle'                  # helper never needed yet
STATE_STARTING = 'starting'          # helper spawned, waiting for "ready"
STATE_READY = 'ready'
STATE_RESTARTING = 'restarting'      # helper failed; a respawn is scheduled
STATE_UNAVAILABLE = 'unavailable'    # binary missing or OS too old
STATE_STOPPED = 'stopped'            # shutdown() was called

_AUTO = object()

# Errno values that mean "this binary will never launch as it stands".
_EBADARCH = getattr(errno, 'EBADARCH', 86)
_EXEC_FATAL = (errno.ENOENT, errno.EACCES, errno.EPERM, errno.ENOEXEC,
               _EBADARCH, errno.ENOTDIR, errno.EISDIR)

_live_engines = weakref.WeakSet()


def _kill_live_engines():
    # atexit backstop. The helper also exits by itself when our end of its
    # stdin closes (which the kernel does even if we are SIGKILLed), so this
    # only matters for a helper that is already hung at the moment we exit.
    for eng in list(_live_engines):
        try:
            eng._kill_now()
        except Exception:
            pass


atexit.register(_kill_live_engines)


def parse_macos_version(raw):
    """'26.1.0' -> (26, 1), or None if the answer cannot be trusted.

    macOS tells a process the version its *main executable* was built to
    expect, not always the real one. Two such compatibility answers exist and
    neither says anything about the real system, so both map to None:

    * 10.16 - every macOS 11+ as seen by a binary linked against a pre-11 SDK;
    * 16-25 - macOS 26+ as seen by a binary linked against a pre-26 SDK (the
      year-based numbering skipped those majors, so no real macOS has them).
      The packaged app's launcher is py2app's prebuilt stub, linked against
      an old SDK, so this is the answer the shipped app can get on macOS 26.
    """
    try:
        parts = [int(p) for p in str(raw).split('.')[:2] if p.isdigit()]
    except Exception:
        return None
    if not parts:
        return None
    if len(parts) == 1:
        parts.append(0)
    if parts[0] == 10 and parts[1] >= 16:
        return None
    if 16 <= parts[0] <= 25:
        return None
    return (parts[0], parts[1])


def macos_version():
    """(major, minor) of the running macOS, or None if it cannot be trusted
    (see parse_macos_version). The caller must not treat None as "too old":
    LSMinimumSystemVersion already keeps the app off older systems."""
    try:
        return parse_macos_version(platform.mac_ver()[0])
    except Exception:
        return None


def _num(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clean_context(context, max_items, max_chars):
    """Custom-vocabulary hints for the helper: strings only, trimmed, deduped,
    and bounded so a huge list can never produce a multi-megabyte command."""
    out, seen, total = [], set(), 0
    for item in context or ():
        if not isinstance(item, str):
            continue
        word = ' '.join(item.split())
        if not word:
            continue
        key = word.casefold()
        if key in seen:
            continue
        if len(out) >= max_items or total + len(word) > max_chars:
            break
        seen.add(key)
        total += len(word)
        out.append(word)
    return out


# How many languages one start/prewarm may name (the helper uses at most
# ``status['max_languages']`` of them, today 2).
MAX_LANGUAGES = 4


def _clean_languages(languages):
    """The ``languages`` of a start/prewarm: strings only, trimmed, without
    duplicates ('en_IN' == 'en-in'), at most MAX_LANGUAGES. Fewer than two
    means an ordinary one-language dictation: returns ()."""
    if isinstance(languages, str) or not languages:
        return ()
    out, seen = [], set()
    for item in languages:
        if not isinstance(item, str):
            continue
        tag = item.strip()
        key = tag.replace('_', '-').lower()
        if not tag or key in seen:
            continue
        seen.add(key)
        out.append(tag)
        if len(out) >= MAX_LANGUAGES:
            break
    return tuple(out) if len(out) >= 2 else ()


def _str_list(value):
    """A JSON list of strings, or [] for anything else."""
    if not isinstance(value, list):
        return []
    return [v for v in value if isinstance(v, str)]


class _Session(object):
    __slots__ = ('id', 'locale', 'context', 'partials', 'state', 't_request',
                 't_started', 'deadline', 'deferred', 'last_level',
                 'auto_stop_at', 'auto_info', 'languages')

    def __init__(self, sid, locale, context, partials, t_request,
                 languages=()):
        self.id = sid
        self.locale = locale
        self.languages = languages     # () or 2+ tags, locale == languages[0]
        self.context = context
        self.partials = partials
        self.state = _PENDING
        self.t_request = t_request
        self.t_started = None
        self.deadline = None
        self.deferred = False
        self.last_level = None
        self.auto_stop_at = None
        self.auto_info = None    # why the ENGINE stopped it, for on_final_info


class SpeechEngine(object):
    """Client + watchdog for one ``freeflo-helper serve`` child process.

    Callbacks (all optional, all invoked on the ``speech-cb`` thread)::

        on_started(session_id)                       mic is live
        on_level(session_id, rms)                    0.0-1.0, ~15-20 per second
        on_partial(session_id, text)                 full running transcript
        on_final(session_id, text, audio_sec, elapsed_ms)      terminal
        on_cancelled(session_id)                               terminal
        on_error(session_id_or_None, code, message)  terminal when id is set
        on_final_info(session_id, info)  only when the final is flagged:
                               {'silent': True} (the input was pure digital
                               silence - muted mic / lid closed),
                               {'mic_lost': True} (the mic vanished or stopped
                               delivering audio, and the session was ended
                               for the caller with the text so far) and/or
                               {'capped': True} (the session ran into
                               max_session_sec and was stopped for the
                               caller). Delivered just BEFORE that session's
                               on_final. A two-language dictation always
                               gets one, with 'languages' (both, primary
                               first), 'used' (those whose words are in the
                               text) and 'dominant' (the one most of the
                               text came from).
        on_started_info(session_id, info)   just BEFORE on_started, when
                               the helper reported 'languages' (two in use)
                               or 'missing' (requested ones left out because
                               they are unsupported or not installed):
                               {'locale', 'languages', 'missing'}
        on_status(dict)        {'mic','speech','supported','installed',
                                'default_locale','input_device',
                                'groups','max_languages'}
        on_prewarmed(locale)
        on_install_progress(locale, fraction)   the first one (fraction 0)
                               arrives at once, then at least every 2 s
        on_installed(locale)   just AFTER an on_status whose 'installed'
                               already lists it
        on_install_failed(locale, code, message)
        on_state(state, detail)   engine state changes (STATE_* constants)

    ``on_final`` can arrive without ``stop()`` having been called: a session
    that runs past ``max_session_sec`` (a missed key-release), or whose
    microphone stops delivering audio, is stopped automatically and the text
    recognised so far is delivered normally (flagged through
    ``on_final_info``).

    They may be passed to the constructor or assigned later as attributes.

    Typical wiring::

        engine = SpeechEngine(config.get_helper_path(),
                              on_final=..., on_error=..., on_level=...)
        engine.prewarm('en-IN')            # at launch: spawns + loads model
        sid = engine.start('en-IN', context=vocabulary.context())  # key down
        engine.stop(sid)                   # key up   -> on_final / on_error
        engine.cancel(sid)                 # or abort -> on_cancelled
        engine.shutdown()                  # app quit

    Show ``user_message(code)`` for errors; the ``message`` argument is
    technical detail for the log. After an error nothing needs resetting:
    the next ``start()`` just works (or reports why it cannot).
    """

    # Every tunable is a plain attribute; pass any of them as a keyword
    # argument to override (the tests shrink them to milliseconds).
    DEFAULTS = dict(
        # Helper must print {"event":"ready"} this soon after exec. Generous:
        # the first launch of a new binary pays for Gatekeeper/dyld checks.
        ready_timeout=8.0,
        # start() -> started/error. Counted from the start() call, so it also
        # covers a lazy spawn.
        start_timeout=5.0,
        # Same, but when the last status said the mic permission has never
        # been asked: the helper may be sitting behind the system prompt, and
        # killing it would not make the user decide any faster.
        permission_start_timeout=120.0,
        # stop() -> final/error: base + per-second-of-audio, capped. The
        # recogniser streams while recording, so finalising is normally a few
        # hundred ms; this only has to beat "never".
        stop_timeout_base=6.0,
        stop_timeout_per_audio_sec=0.2,
        stop_timeout_max=90.0,
        # cancel -> cancelled. Past this the mic may still be held, so the
        # helper is killed, which releases it for certain.
        cancel_timeout=3.0,
        # Idle liveness probe. Only sent when nothing else is in flight;
        # active operations are covered by their own deadlines.
        ping_interval=10.0,
        ping_timeout=4.0,
        prewarm_timeout=45.0,
        # An asset download with no progress event for this long is
        # considered dead. The helper reports progress at least every 2 s
        # (a heartbeat) and gives up on a download itself after 180 s
        # without movement, so silence this long means the helper is stuck.
        install_stall_timeout=60.0,
        # While waiting on a permission prompt the helper may not answer
        # pings; give the user this long before pings resume.
        permission_prompt_timeout=180.0,
        # Recording but the level meter went silent: the audio tap died
        # (device vanished, CoreAudio wedge). The session is stopped so the
        # text recognised so far is still delivered; the helper is only
        # killed if it then fails to answer that stop. Only armed once the
        # helper has been seen to emit level events at all. None disables.
        level_stall_timeout=5.0,
        # Bytes we could not hand to the helper's stdin for this long mean it
        # stopped reading commands.
        write_timeout=3.0,
        # A missed key-release must not keep the mic open forever. The
        # session is stopped normally (its text is delivered). None disables.
        max_session_sec=300.0,
        # Respawn delay by consecutive-failure count. First failure respawns
        # at once (one-off crash); a crash loop settles at 30 s.
        backoff=(0.0, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0),
        # A user-triggered start() skips the backoff wait, but never spawns
        # more often than this.
        min_respawn_interval=0.25,
        # A helper that stayed up this long is healthy: forget past failures.
        stable_after=20.0,
        # One stray non-JSON line is tolerated (and logged); this many in a
        # row, or a single line longer than max_line_bytes, means the stream
        # is garbage and the helper is replaced.
        max_garbage_lines=5,
        max_line_bytes=1 << 20,
        # shutdown(): time allowed between "quit" and SIGKILL.
        quit_grace=1.0,
        # Helper errors after which a fresh process is cheap insurance.
        recycle_on=('engine_failed', 'internal'),
        # False (default): a start() issued while the previous session is
        # still finalising is held back until that session's final arrives,
        # so no helper implementation can mistake it for "cancel the old
        # one". True: send it immediately (only if the helper is known to
        # finalise and record concurrently).
        overlap_finalize=False,
        max_context_items=200,
        max_context_chars=8000,
        # Helper stderr is forwarded to the log (it must never contain
        # transcript text). Turn off if in doubt.
        log_helper_stderr=True,
    )

    def __init__(self, helper_path, helper_args=('serve',), env=None,
                 macos=_AUTO, min_macos=MIN_MACOS,
                 on_started=None, on_level=None, on_partial=None,
                 on_final=None, on_cancelled=None, on_error=None,
                 on_status=None, on_prewarmed=None, on_install_progress=None,
                 on_installed=None, on_state=None, on_final_info=None,
                 on_install_failed=None, on_started_info=None, **tunables):
        for name, default in self.DEFAULTS.items():
            setattr(self, name, tunables.pop(name, default))
        if tunables:
            raise TypeError('unknown SpeechEngine option(s): %s'
                            % ', '.join(sorted(tunables)))

        # A list/tuple is an argv prefix (used by the tests to run a Python
        # fake); the app passes a plain path.
        if isinstance(helper_path, (list, tuple)):
            self._argv = [str(a) for a in helper_path] + list(helper_args)
        else:
            self._argv = [str(helper_path)] + list(helper_args)
        self._env = env

        self.on_started = on_started
        self.on_level = on_level
        self.on_partial = on_partial
        self.on_final = on_final
        self.on_cancelled = on_cancelled
        self.on_error = on_error
        self.on_status = on_status
        self.on_prewarmed = on_prewarmed
        self.on_install_progress = on_install_progress
        self.on_installed = on_installed
        self.on_install_failed = on_install_failed
        self.on_final_info = on_final_info
        self.on_started_info = on_started_info
        self.on_state = on_state

        # --- shared with caller threads (guarded by _lock) ---
        self._lock = threading.Lock()
        self._ids = itertools.count(1)
        self._id_state = {}          # id -> 'open' | 'stopping'
        self._latest_id = None
        self._closed = False
        self._threads_started = False
        self._ops = collections.deque()      # append/popleft are atomic
        self._wake_r = self._wake_w = None
        self._io_thread = None
        self._cb_thread = None
        self._cbq = queue.Queue()

        # --- read-mostly snapshots, safe to read from any thread ---
        self.state = STATE_IDLE
        self.state_detail = ''
        self.unavailable = None      # (code, message) while unavailable
        self.last_status = None      # last "status" event, as a dict
        self.last_prewarm = None     # last "prewarmed": locale/resolved/languages/missing
        self._install_state = {}     # locale -> dict, replaced (never mutated)
        self.helper_version = None
        self.helper_os = None
        self.helper_pid = None
        self.spawn_count = 0
        self.restart_count = 0
        self.last_failure = None     # (code, message, wall_time)

        # --- speech-io thread only ---
        self._sel = None
        self._proc = None
        self._gen = 0
        self._ready = False
        self._ready_at = None
        self._ready_deadline = None
        self._want_proc = False
        self._spawn_at = 0.0
        self._last_spawn_at = -1e9
        self._failures = 0
        self._permanent = None       # (code, message): never retried
        self._rx = bytearray()
        self._tx = bytearray()
        self._tx_since = None
        self._tx_registered = False
        self._err_buf = bytearray()
        self._err_tail = collections.deque(maxlen=20)
        self._garbage = 0
        self._active = None          # _Session: pending/starting/recording
        self._finalizing = {}        # id -> _Session awaiting final
        self._cancel_wait = {}       # id -> deadline for "cancelled"
        self._queued_cmds = []       # non-session commands awaiting "ready"
        self._prewarm_locale = None  # re-issued after every respawn
        self._prewarm_languages = ()
        self._prewarm_partials = True
        self._prewarm_deadline = None
        self._installs = {}          # locale -> time of last progress
        self._perm_pending_until = None
        self._ping_seq = 0
        self._ping_deadline = None
        self._next_ping_at = 0.0
        self._levels_seen = False
        self._recycle_reason = None
        self._zombies = []
        self._shutdown_requested = False

        if macos is _AUTO:
            macos = macos_version()
        if macos is not None and min_macos and tuple(macos) < tuple(min_macos):
            self._permanent = (
                E_OS_TOO_OLD,
                'freeflo needs macOS %d or later (this Mac runs %s)'
                % (min_macos[0], '.'.join(str(p) for p in macos)))
            self.unavailable = self._permanent
            self.state = STATE_UNAVAILABLE
            self.state_detail = self._permanent[1]

        _live_engines.add(self)

    # ------------------------------------------------------------------
    # Public API -- any thread, never blocks
    # ------------------------------------------------------------------

    def ensure_running(self):
        """Spawn the helper now (it is otherwise spawned on first use)."""
        return self._post(('ensure',))

    def start(self, locale, context=None, partials=True, languages=None):
        """Begin a dictation. Returns the session id (an int), or None if the
        engine has been shut down. The outcome arrives via callbacks; a start
        that cannot be honoured still returns an id and reports ``on_error``.

        ``languages`` (optional, primary first, e.g. ['en-IN', 'hi-IN'])
        asks the helper to listen in two languages at once and pick the
        better transcript sentence by sentence. With fewer than two entries
        it is an ordinary dictation in ``locale``; otherwise ``locale`` is
        ignored in favour of ``languages[0]``. Extra languages that are not
        installed are left out (reported through ``on_started_info``) rather
        than failing the dictation.

        Starting while another session is recording cancels that one (it gets
        ``on_cancelled``)."""
        ctx = tuple(_clean_context(context, self.max_context_items,
                                   self.max_context_chars))
        langs = _clean_languages(languages)
        if langs:
            locale = langs[0]
        with self._lock:
            if self._closed:
                return None
            sid = next(self._ids)
            self._id_state[sid] = 'open'
            self._latest_id = sid
            self._post_locked(('start', sid, str(locale), ctx, bool(partials),
                               time.monotonic(), langs))
        return sid

    def stop(self, session_id=None):
        """Stop the mic and ask for the final transcript. ``session_id``
        defaults to the most recent start(). Returns False (and does nothing)
        if that session is unknown, already stopped, or already finished."""
        with self._lock:
            sid = self._latest_id if session_id is None else session_id
            if self._closed or self._id_state.get(sid) != 'open':
                return False
            self._id_state[sid] = 'stopping'
            self._post_locked(('stop', sid, time.monotonic()))
        return True

    def cancel(self, session_id=None):
        """Stop the mic and throw the recording away. Also valid after stop()
        to abandon a transcript that has not arrived yet. Returns False if
        the session is unknown or already finished."""
        with self._lock:
            sid = self._latest_id if session_id is None else session_id
            if self._closed or sid not in self._id_state:
                return False
            del self._id_state[sid]
            self._post_locked(('cancel', sid))
        return True

    def prewarm(self, locale, partials=True, languages=None):
        """Load the model for ``locale`` so the first start is instant. The
        engine remembers the locale and prewarms again after any respawn.

        ``partials`` and ``languages`` must match what ``start()`` will be
        called with: the helper keeps a separate recogniser configuration for
        each, and prewarming another one would leave the first dictation
        cold."""
        langs = _clean_languages(languages)
        if langs:
            locale = langs[0]
        return self._post(('prewarm', str(locale), bool(partials), langs))

    def request_status(self):
        """Ask for permissions + locales; answered through ``on_status`` (a
        status is also fetched automatically every time the helper starts)."""
        return self._post(('simple', {'cmd': 'status'}))

    def request_permissions(self):
        """Trigger the system microphone / speech prompts."""
        return self._post(('simple', {'cmd': 'request_permissions'}))

    def install(self, locale):
        """Download the system speech asset for ``locale``. Progress, the
        result and failures arrive through on_install_progress /
        on_installed / on_install_failed; ``installs()`` has the latest
        state of every download asked for."""
        return self._post(('simple', {'cmd': 'install', 'locale': str(locale)}))

    def installs(self):
        """{locale: {'state': 'downloading'|'installed'|'failed',
        'fraction', 'already', 'code', 'message'}} for every install asked
        for since the engine started (keyed by the locale as passed to
        install()). A snapshot copy, safe to read from any thread."""
        current = self._install_state
        return dict((k, dict(v)) for k, v in current.items())

    def restart(self, reason='requested'):
        """Kill and respawn the helper (a manual 'fix it' for a menu item)."""
        return self._post(('restart', str(reason)))

    def stop_timeout_for(self, audio_sec):
        """Seconds allowed between stop and final for ``audio_sec`` of audio."""
        audio = max(0.0, _num(audio_sec))
        return min(self.stop_timeout_max,
                   self.stop_timeout_base
                   + self.stop_timeout_per_audio_sec * audio)

    @property
    def is_available(self):
        """False once the engine knows it can never work as installed."""
        return self.unavailable is None

    @property
    def is_ready(self):
        return self.state == STATE_READY

    def diagnostics(self):
        """A small, text-free summary for bug reports."""
        return {
            'state': self.state,
            'detail': self.state_detail,
            'helper': self._argv[0],
            'helper_version': self.helper_version,
            'helper_os': self.helper_os,
            'helper_pid': self.helper_pid,
            'spawns': self.spawn_count,
            'restarts': self.restart_count,
            'last_failure': self.last_failure,
            'status': self.last_status,
        }

    def shutdown(self, timeout=2.5):
        """Stop the helper (quit, then SIGKILL) and the engine's threads.
        Blocks for at most ``timeout`` seconds; call it on the way out of the
        app. Idempotent. No callbacks are delivered for sessions still open."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._id_state.clear()
            started = self._threads_started
            if started:
                self._ops.append(('shutdown',))
                self._wake()
        if not started:
            self._set_state_snapshot(STATE_STOPPED, '')
            _live_engines.discard(self)
            return
        io = self._io_thread
        if io is not None and io is not threading.current_thread():
            io.join(timeout)
            if io.is_alive():
                # Should be impossible; never leave the helper behind anyway.
                log.error('speech-io did not stop in %.1fs - killing helper',
                          timeout)
                self._kill_now()
        self._cbq.put(None)
        cb = self._cb_thread
        if cb is not None and cb is not threading.current_thread():
            cb.join(0.5)
        _live_engines.discard(self)

    # ------------------------------------------------------------------
    # Caller-side plumbing
    # ------------------------------------------------------------------

    def _post(self, op):
        with self._lock:
            if self._closed:
                return False
            self._post_locked(op)
        return True

    def _post_locked(self, op):
        if not self._threads_started:
            self._start_threads()
        self._ops.append(op)
        self._wake()

    def _start_threads(self):
        self._sel = selectors.DefaultSelector()
        self._wake_r, self._wake_w = os.pipe()
        os.set_blocking(self._wake_r, False)
        os.set_blocking(self._wake_w, False)
        self._sel.register(self._wake_r, selectors.EVENT_READ, 'wake')
        self._cb_thread = threading.Thread(
            target=self._cb_main, name='speech-cb', daemon=True)
        self._io_thread = threading.Thread(
            target=self._io_main, name='speech-io', daemon=True)
        self._threads_started = True
        self._cb_thread.start()
        self._io_thread.start()

    def _wake(self):
        try:
            os.write(self._wake_w, b'x')
        except (BlockingIOError, OSError):
            pass    # pipe full (already woken) or closed (shutting down)

    def _kill_now(self):
        """Last-resort kill from a foreign thread (atexit / stuck shutdown)."""
        proc = self._proc
        if proc is not None:
            try:
                proc.kill()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # speech-cb thread
    # ------------------------------------------------------------------

    def _cb_main(self):
        while True:
            item = self._cbq.get()
            if item is None:
                return
            name, args = item
            fn = getattr(self, 'on_' + name, None)
            if fn is None:
                continue
            try:
                fn(*args)
            except Exception:
                log.exception('speech callback on_%s failed', name)

    def _emit(self, name, *args):
        if self._shutdown_requested:
            return
        # The level meter is disposable: if a slow callback has let a backlog
        # build up, drop meter ticks rather than replay them late.
        if name == 'level' and self._cbq.qsize() > 64:
            return
        self._cbq.put((name, args))

    def _finish(self, sid, name, *args):
        """Deliver a session's single terminal callback."""
        with self._lock:
            self._id_state.pop(sid, None)
        self._emit(name, sid, *args)

    def _set_state_snapshot(self, state, detail):
        self.state = state
        self.state_detail = detail

    def _set_state(self, state, detail=''):
        if state == self.state and detail == self.state_detail:
            return
        self._set_state_snapshot(state, detail)
        self._emit('state', state, detail)

    # ------------------------------------------------------------------
    # speech-io thread: main loop
    # ------------------------------------------------------------------

    def _io_main(self):
        try:
            while True:
                try:
                    self._drain_ops()
                    if self._shutdown_requested:
                        break
                    self._tick()
                    timeout = self._next_timeout()
                    for key, _mask in self._sel.select(timeout):
                        tag = key.data
                        if tag == 'wake':
                            self._drain_wake()
                        elif tag == 'out':
                            self._on_stdout()
                        elif tag == 'err':
                            self._on_stderr()
                        elif tag == 'in':
                            self._flush_tx()
                except Exception:
                    # The supervisor must outlive its own bugs: log, replace
                    # the helper (fails any session in flight exactly once)
                    # and keep going.
                    log.exception('speech supervisor error - resetting helper')
                    try:
                        self._fail_helper('internal', 'supervisor error')
                    except Exception:
                        log.exception('speech supervisor reset failed')
                    time.sleep(0.05)
        finally:
            self._io_cleanup()

    def _drain_wake(self):
        try:
            while os.read(self._wake_r, 4096):
                pass
        except (BlockingIOError, OSError):
            pass

    def _drain_ops(self):
        while True:
            try:
                op = self._ops.popleft()
            except IndexError:
                return
            getattr(self, '_op_' + op[0])(*op[1:])
            if self._shutdown_requested:
                return

    # ------------------------------------------------------------------
    # speech-io thread: operations requested by callers
    # ------------------------------------------------------------------

    def _wants_helper(self, bypass_backoff=False):
        self._want_proc = True
        # A missing binary is re-checked on every user action, so the engine
        # heals by itself if the file shows up. An old OS never changes.
        if self.unavailable is not None and self._permanent is None:
            self.unavailable = None
        if bypass_backoff and self._proc is None:
            soonest = max(time.monotonic(),
                          self._last_spawn_at + self.min_respawn_interval)
            if soonest < self._spawn_at:
                self._spawn_at = soonest

    def _op_ensure(self):
        self._wants_helper(bypass_backoff=True)

    def _op_shutdown(self):
        self._shutdown_requested = True

    def _op_restart(self, reason):
        self._wants_helper(bypass_backoff=True)
        if self._proc is not None:
            self._fail_helper(E_HELPER_CRASHED, 'restart %s' % reason,
                              count=False)

    def _op_start(self, sid, locale, context, partials, t_request,
                  languages=()):
        sess = _Session(sid, locale, context, partials, t_request, languages)
        old = self._active
        if old is not None:
            self._supersede(old)
        if self._permanent is not None:
            self._finish(sid, 'error', *self._permanent)
            return
        self._wants_helper(bypass_backoff=True)
        sess.deadline = t_request + self.start_timeout
        self._active = sess
        self._try_send_start()

    def _supersede(self, old):
        """A newer start() replaces ``old``. The app has moved on, so the old
        session is closed out right away; the helper's own confirmation is
        only tracked to prove it released the microphone."""
        self._active = None
        if old.state != _PENDING:
            self._cancel_wait[old.id] = time.monotonic() + self.cancel_timeout
            self._finish(old.id, 'cancelled')
            self._send({'cmd': 'cancel', 'id': old.id})
        else:
            self._finish(old.id, 'cancelled')

    def _try_send_start(self):
        s = self._active
        if s is None or s.state != _PENDING:
            return
        if self._finalizing and not self.overlap_finalize:
            # Held back until the previous final lands; its clock restarts
            # then, so a long finalisation cannot expire the queued start.
            s.deferred = True
            return
        now = time.monotonic()
        if s.deferred:
            s.deferred = False
            s.deadline = now + self.start_timeout
        if not self._ready:
            return
        status = self.last_status
        if status and status.get('mic') == 'undetermined':
            s.deadline = max(s.deadline, now + self.permission_start_timeout)
        # State flips only after the write: if the pipe turns out to be dead
        # (helper died while idle), nothing of this session was lost, so it
        # stays pending and is served by the respawn.
        cmd = {'cmd': 'start', 'id': s.id, 'locale': s.locale,
               'context': list(s.context), 'partials': s.partials}
        if s.languages:
            cmd['languages'] = list(s.languages)
        if self._send(cmd):
            s.state = _STARTING

    def _op_stop(self, sid, t_request):
        s = self._active
        if s is None or s.id != sid:
            return                              # unknown / stale id
        if s.state == _PENDING:
            # The mic never opened (helper still coming up, or held behind a
            # finalising session): there is nothing to transcribe.
            self._active = None
            self._finish(sid, 'final', '', 0.0, 0.0)
            return
        self._begin_stop(s, t_request)

    def _begin_stop(self, s, t_request):
        since = s.t_started if s.t_started is not None else s.t_request
        audio = max(0.0, t_request - since)
        deadline = time.monotonic() + self.stop_timeout_for(audio)
        if s.state == _STARTING and s.deadline is not None:
            # Released before the mic came up: the helper still gets whatever
            # is left of the start allowance (which is long while the system
            # permission prompt is open) before it is declared hung.
            deadline = max(deadline, s.deadline)
        s.state = _STOPPING
        s.deadline = deadline
        self._active = None
        self._finalizing[s.id] = s
        self._send({'cmd': 'stop', 'id': s.id})

    def _auto_stop(self, s, now, reason):
        """Stop a recording on the caller's behalf (it ran too long, or its
        microphone went quiet). The final is delivered as usual, flagged with
        ``reason`` through on_final_info."""
        s.auto_info = reason
        with self._lock:
            if self._id_state.get(s.id) == 'open':
                self._id_state[s.id] = 'stopping'
        self._begin_stop(s, now)

    def _op_cancel(self, sid):
        s = self._active
        if s is not None and s.id == sid:
            self._supersede(s)
            return
        s = self._finalizing.pop(sid, None)
        if s is not None:
            self._cancel_wait[sid] = time.monotonic() + self.cancel_timeout
            self._finish(sid, 'cancelled')
            if self._send({'cmd': 'cancel', 'id': sid}):
                self._try_send_start()

    def _op_prewarm(self, locale, partials=True, languages=()):
        self._wants_helper()
        self._prewarm_locale = locale
        self._prewarm_languages = languages
        self._prewarm_partials = partials
        if self._ready:
            self._send_cmd(self._prewarm_cmd())

    def _prewarm_cmd(self):
        cmd = {'cmd': 'prewarm', 'locale': self._prewarm_locale}
        if self._prewarm_languages:
            cmd['languages'] = list(self._prewarm_languages)
        if not self._prewarm_partials:
            cmd['partials'] = False     # the helper's default is True
        return cmd

    def _set_install(self, locale, **fields):
        """Copy-on-write update of one download's state (speech-io only)."""
        if not isinstance(locale, str):
            return
        state = dict(self._install_state)
        entry = dict(state.get(locale) or {
            'state': 'downloading', 'fraction': 0.0, 'already': False,
            'code': None, 'message': None})
        entry.update(fields)
        state[locale] = entry
        self._install_state = state

    def _install_failed(self, locale, code, message):
        self._set_install(locale, state='failed', code=code, message=message)
        self._emit('install_failed', locale, code, message)

    def _op_simple(self, cmd):
        if cmd.get('cmd') == 'install':
            self._set_install(cmd.get('locale'), state='downloading',
                              fraction=0.0, already=False, code=None,
                              message=None)
        self._wants_helper()
        if self._ready:
            self._send_cmd(cmd)
        elif cmd.get('cmd') != 'status' and cmd not in self._queued_cmds:
            # (a status is fetched automatically as soon as the helper is up)
            self._queued_cmds.append(cmd)

    # ------------------------------------------------------------------
    # speech-io thread: process lifecycle
    # ------------------------------------------------------------------

    def _spawn(self):
        now = time.monotonic()
        self._last_spawn_at = now
        exe = self._argv[0]
        if not (os.path.isfile(exe) and os.access(exe, os.X_OK)):
            self._set_unavailable(
                E_HELPER_MISSING, 'speech helper not found at %s' % exe)
            return
        try:
            proc = subprocess.Popen(
                self._argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, bufsize=0, close_fds=True,
                env=self._env)
        except OSError as e:
            if e.errno == _EBADARCH:
                # The helper is arm64-only (the recogniser is): an Intel Mac.
                self._set_unavailable(
                    E_WRONG_ARCH,
                    'speech helper cannot run on this Mac: %s' % e)
            elif e.errno in _EXEC_FATAL:
                self._set_unavailable(
                    E_HELPER_MISSING,
                    'speech helper could not be launched: %s' % e)
            else:
                # e.g. EAGAIN under memory pressure: transient, back off.
                self._note_failure(E_HELPER_CRASHED,
                                   'could not launch speech helper: %s' % e)
            return
        for f in (proc.stdin, proc.stdout, proc.stderr):
            os.set_blocking(f.fileno(), False)
        self._proc = proc
        self._gen += 1
        self._ready = False
        self._ready_deadline = now + self.ready_timeout
        self.spawn_count += 1
        self.helper_pid = proc.pid
        self._sel.register(proc.stdout.fileno(), selectors.EVENT_READ, 'out')
        self._sel.register(proc.stderr.fileno(), selectors.EVENT_READ, 'err')
        log.info('Speech helper spawned (pid %d, spawn #%d)',
                 proc.pid, self.spawn_count)
        self._set_state(STATE_STARTING)

    def _set_unavailable(self, code, message):
        log.error('Speech engine unavailable (%s): %s', code, message)
        self.unavailable = (code, message)
        self.last_failure = (code, message, time.time())
        s = self._active
        if s is not None:
            self._active = None
            self._finish(s.id, 'error', code, message)
        del self._queued_cmds[:]
        self._set_state(STATE_UNAVAILABLE, message)

    def _teardown_proc(self):
        """SIGKILL the helper (if any), release its pipes and reap it."""
        proc = self._proc
        if proc is None:
            return
        self._proc = None
        self._gen += 1
        self._ready = False
        self._ready_at = None
        self._ready_deadline = None
        self.helper_pid = None
        for f in (proc.stdin, proc.stdout, proc.stderr):
            try:
                self._sel.unregister(f.fileno())
            except (KeyError, ValueError, OSError):
                pass
        self._tx_registered = False
        try:
            proc.kill()
        except Exception:
            pass
        for f in (proc.stdin, proc.stdout, proc.stderr):
            try:
                f.close()
            except Exception:
                pass
        try:
            proc.wait(timeout=0.5)      # reap: SIGKILL is all but instant
        except Exception:
            self._zombies.append(proc)  # retried from _tick, never leaked
        del self._rx[:]
        del self._tx[:]
        del self._err_buf[:]
        self._tx_since = None
        self._garbage = 0
        self._ping_deadline = None
        self._prewarm_deadline = None
        self._perm_pending_until = None
        self._recycle_reason = None

    def _note_failure(self, code, message, count=True):
        self.restart_count += 1
        self.last_failure = (code, message, time.time())
        if count:
            self._failures += 1
        delay = 0.0
        if count and self.backoff:
            # 1st consecutive failure -> backoff[0], 2nd -> backoff[1], ...
            delay = self.backoff[min(self._failures, len(self.backoff)) - 1]
        self._spawn_at = time.monotonic() + delay
        self._set_state(STATE_RESTARTING, message)

    def _fail_helper(self, code, message, count=True):
        """The helper is unusable: kill it, give every session it was serving
        its one error, and schedule a replacement."""
        tail = ' | '.join(self._err_tail)
        log.warning('Speech helper failed (%s): %s%s', code, message,
                    (' -- last stderr: ' + tail) if tail else '')
        if self._prewarm_deadline is not None:
            # It died (or was killed) with a prewarm in flight. Do not hand
            # the same prewarm to the replacement: if the prewarm is what
            # kills it, that would be a crash loop. The next dictation loads
            # the model anyway and re-arms this.
            self._prewarm_locale = None
            self._prewarm_languages = ()
        self._teardown_proc()
        s = self._active
        if s is not None and s.state != _PENDING:
            self._active = None
            self._finish(s.id, 'error', code, message)
        for sid in list(self._finalizing):
            del self._finalizing[sid]
            self._finish(sid, 'error', code, message)
        self._cancel_wait.clear()
        for locale in list(self._installs):
            del self._installs[locale]
            self._install_failed(locale, code,
                                 'install interrupted: %s' % message)
            self._emit('error', None, code,
                       'install of %s interrupted: %s' % (locale, message))
        self._note_failure(code, message, count)
        # A start that was queued behind a (now failed) finalising session is
        # no longer blocked: restart its clock so it cannot wait forever.
        self._try_send_start()

    def _helper_died(self, why):
        proc = self._proc
        if proc is None:
            return
        rc = proc.poll()
        if rc is None:
            try:
                rc = proc.wait(timeout=0.25)
            except Exception:
                rc = None
        if rc is None:
            desc = why
        elif rc < 0:
            desc = '%s, signal %d' % (why, -rc)
        else:
            desc = '%s, exit code %d' % (why, rc)
        self._fail_helper(E_HELPER_CRASHED,
                          'speech helper stopped unexpectedly (%s)' % desc)

    def _io_cleanup(self):
        """Orderly exit: quit, wait briefly, then SIGKILL. Runs on speech-io."""
        proc = self._proc
        if proc is not None:
            try:
                os.write(proc.stdin.fileno(), b'{"cmd":"quit"}\n')
            except Exception:
                pass
            try:
                proc.stdin.close()      # EOF also tells the helper to leave
            except Exception:
                pass
            end = time.monotonic() + self.quit_grace
            while proc.poll() is None and time.monotonic() < end:
                time.sleep(0.01)
            self._teardown_proc()
        for z in self._zombies:
            try:
                z.wait(timeout=0.2)
            except Exception:
                pass
        try:
            self._sel.close()
        except Exception:
            pass
        for fd in (self._wake_r, self._wake_w):
            try:
                os.close(fd)
            except Exception:
                pass
        self._set_state_snapshot(STATE_STOPPED, '')

    # ------------------------------------------------------------------
    # speech-io thread: pipe I/O
    # ------------------------------------------------------------------

    def _send_cmd(self, cmd):
        """Send a non-session command to a ready helper, arming its watchdog."""
        now = time.monotonic()
        name = cmd.get('cmd')
        if name == 'prewarm':
            self._prewarm_deadline = now + self.prewarm_timeout
        elif name == 'install':
            self._installs[cmd.get('locale')] = now
        elif name == 'request_permissions':
            self._perm_pending_until = now + self.permission_prompt_timeout
        return self._send(cmd)

    def _send(self, obj):
        """Queue one JSON line for the helper. Returns False if the helper is
        gone (or died while we wrote)."""
        if self._proc is None:
            return False
        gen = self._gen
        # ensure_ascii keeps the line pure ASCII: valid UTF-8 JSON that no
        # locale setting on either side can mangle.
        self._tx += json.dumps(obj, separators=(',', ':')).encode('ascii')
        self._tx += b'\n'
        if self._tx_since is None:
            self._tx_since = time.monotonic()
        self._flush_tx()
        return self._proc is not None and self._gen == gen

    def _flush_tx(self):
        proc = self._proc
        if proc is None:
            return
        fd = proc.stdin.fileno()
        while self._tx:
            try:
                n = os.write(fd, self._tx)
            except BlockingIOError:
                break
            except OSError:
                self._helper_died('command pipe closed')
                return
            del self._tx[:n]
        if self._tx:
            if not self._tx_registered:
                self._sel.register(fd, selectors.EVENT_WRITE, 'in')
                self._tx_registered = True
        else:
            self._tx_since = None
            if self._tx_registered:
                try:
                    self._sel.unregister(fd)
                except (KeyError, ValueError, OSError):
                    pass
                self._tx_registered = False

    def _on_stdout(self):
        proc = self._proc
        if proc is None:
            return
        gen = self._gen
        try:
            data = os.read(proc.stdout.fileno(), 65536)
        except BlockingIOError:
            return
        except OSError:
            data = b''
        if not data:
            self._helper_died('output closed')
            return
        self._rx += data
        while self._gen == gen:
            nl = self._rx.find(b'\n')
            if nl < 0:
                break
            line = bytes(self._rx[:nl])
            del self._rx[:nl + 1]
            self._on_line(line)
        if self._gen == gen and len(self._rx) > self.max_line_bytes:
            self._fail_helper(E_HELPER_PROTOCOL,
                              'helper sent an oversized line')

    def _on_stderr(self):
        proc = self._proc
        if proc is None:
            return
        try:
            data = os.read(proc.stderr.fileno(), 65536)
        except (BlockingIOError, OSError):
            return
        if not data:
            # stderr closed; stdout EOF / poll() decides whether that is fatal.
            try:
                self._sel.unregister(proc.stderr.fileno())
            except (KeyError, ValueError, OSError):
                pass
            return
        self._err_buf += data
        while True:
            nl = self._err_buf.find(b'\n')
            if nl < 0:
                break
            line = bytes(self._err_buf[:nl]).decode('utf-8', 'replace').strip()
            del self._err_buf[:nl + 1]
            if line:
                self._err_tail.append(line[:300])
                if self.log_helper_stderr:
                    log.info('helper: %s', line[:500])
        if len(self._err_buf) > 65536:
            del self._err_buf[:]

    def _on_line(self, raw):
        raw = raw.strip()
        if not raw:
            return
        ev = None
        try:
            ev = json.loads(raw.decode('utf-8'))
        except (ValueError, UnicodeDecodeError):
            pass
        if not isinstance(ev, dict) or not isinstance(ev.get('event'), str):
            self._garbage += 1
            # Never log the content: it could be transcript text.
            log.warning('Ignoring a malformed line from the speech helper '
                        '(%d bytes, %d in a row)', len(raw), self._garbage)
            if self._garbage >= self.max_garbage_lines:
                self._fail_helper(E_HELPER_PROTOCOL,
                                  'helper output is not valid protocol')
            return
        self._garbage = 0
        handler = getattr(self, '_ev_' + ev['event'], None)
        if handler is None:
            log.debug('Ignoring unknown helper event %r', ev['event'])
            return
        handler(ev)

    # ------------------------------------------------------------------
    # speech-io thread: helper events
    # ------------------------------------------------------------------

    def _ev_ready(self, ev):
        if self._ready:
            return
        now = time.monotonic()
        self._ready = True
        self._ready_at = now
        self._ready_deadline = None
        self._next_ping_at = now + (self.ping_interval or 0.0)
        self.helper_version = ev.get('version')
        self.helper_os = ev.get('os')
        log.info('Speech helper ready (version %s, os %s)',
                 self.helper_version, self.helper_os)
        self._set_state(STATE_READY)
        gen = self._gen
        # Session first: nothing may sit between a key press and the mic.
        self._try_send_start()
        if self._gen == gen:
            self._send({'cmd': 'status'})
        if self._gen == gen and self._prewarm_locale:
            self._send_cmd(self._prewarm_cmd())
        queued, self._queued_cmds = self._queued_cmds, []
        for cmd in queued:
            if self._gen != gen:
                break
            self._send_cmd(cmd)

    def _ev_pong(self, ev):
        if self._ping_deadline is not None and ev.get('seq') == self._ping_seq:
            self._ping_deadline = None
            self._next_ping_at = time.monotonic() + (self.ping_interval or 0.0)

    def _ev_status(self, ev):
        self._perm_pending_until = None
        status = {
            'mic': ev.get('mic'),
            'speech': ev.get('speech'),
            'supported': list(ev.get('supported') or ()),
            'installed': list(ev.get('installed') or ()),
            # Optional extras (None when the helper does not send them): the
            # best locale for this Mac, and the current input device's name
            # (None also means "no microphone").
            'default_locale': (ev.get('default_locale')
                               if isinstance(ev.get('default_locale'), str)
                               else None),
            'input_device': (ev.get('input_device')
                             if isinstance(ev.get('input_device'), str)
                             else None),
            # Locales that share one speech model (on macOS 27 every
            # non-English locale of India), and how many languages one
            # dictation can listen for (1 for a helper older than 1.1).
            'groups': [_str_list(g) for g in (ev.get('groups') or ())
                       if isinstance(g, list)]
                      if isinstance(ev.get('groups'), list) else [],
            'max_languages': _max_languages(ev.get('max_languages')),
        }
        self.last_status = status
        self._emit('status', status)
        if status['mic'] == 'undetermined':
            # A start already in flight is (or is about to be) sitting behind
            # the system permission prompt: give the user time to answer it.
            until = time.monotonic() + self.permission_start_timeout
            s = self._active
            if s is not None and s.state == _STARTING:
                s.deadline = max(s.deadline, until)
            for fs in self._finalizing.values():
                if fs.t_started is None:
                    fs.deadline = max(fs.deadline, until)

    def _ev_prewarmed(self, ev):
        self._prewarm_deadline = None
        resolved = ev.get('resolved')
        self.last_prewarm = {
            'locale': ev.get('locale'),
            'resolved': resolved if isinstance(resolved, str) else None,
            'languages': _str_list(ev.get('languages')),
            'missing': _str_list(ev.get('missing')),
        }
        self._emit('prewarmed', ev.get('locale'))

    def _ev_install_progress(self, ev):
        locale = ev.get('locale')
        fraction = min(1.0, max(0.0, _num(ev.get('fraction'))))
        if isinstance(locale, str):
            # (Re-)arms the stall watchdog: progress proves a download is
            # running even if we had stopped tracking it.
            self._installs[locale] = time.monotonic()
            self._set_install(locale, state='downloading', fraction=fraction)
        self._emit('install_progress', locale, fraction)

    def _ev_installed(self, ev):
        locale = ev.get('locale')
        self._installs.pop(locale, None)
        old = self.last_status
        if old is not None:
            # The app learns that the language is usable from the status, so
            # a fresh one goes out first (built from the helper's own list
            # when it sends one); a real status is fetched right after.
            fresh = ev.get('installed')
            if isinstance(fresh, list):
                installed = _str_list(fresh)
            else:
                installed = list(old.get('installed') or ())
                for tag in (ev.get('resolved'), locale):
                    if isinstance(tag, str) and tag not in installed:
                        installed.append(tag)
            status = dict(old)
            status['installed'] = installed
            self.last_status = status
            self._emit('status', status)
        self._set_install(locale, state='installed', fraction=1.0,
                          already=ev.get('already') is True, code=None,
                          message=None)
        self._emit('installed', locale)
        self._send({'cmd': 'status'})

    def _ev_started(self, ev):
        sid = ev.get('id')
        now = time.monotonic()
        s = self._active
        if s is not None and s.id == sid and s.state == _STARTING:
            s.state = _RECORDING
            s.t_started = now
            s.last_level = now
            s.deadline = None
            if self.max_session_sec:
                s.auto_stop_at = now + self.max_session_sec
            # This is the model now in memory: reload it after a respawn.
            self._prewarm_locale = s.locale
            self._prewarm_languages = s.languages
            self._prewarm_partials = s.partials
            languages, missing = ev.get('languages'), ev.get('missing')
            if isinstance(languages, list) or isinstance(missing, list):
                locale = ev.get('locale')
                self._emit('started_info', sid, {
                    'locale': locale if isinstance(locale, str) else s.locale,
                    'languages': _str_list(languages),
                    'missing': _str_list(missing),
                })
            self._emit('started', sid)
            status = self.last_status
            if status and status.get('mic') != 'granted':
                # The mic is live, so the cached permission state is stale
                # (the user just answered the prompt). Refresh it, or every
                # later start would keep the long permission deadline.
                self._send({'cmd': 'status'})
            return
        s = self._finalizing.get(sid)
        if s is not None:
            if s.t_started is None:
                # Released before the mic came up. Our stop may have reached
                # the helper before the session existed on its side and been
                # dropped as an "unknown id"; now that the session certainly
                # exists, say it again. (If the first one did register, this
                # one is the unknown id and is ignored.)
                s.t_started = now
                self._send({'cmd': 'stop', 'id': sid})
            return
        if sid in self._cancel_wait:
            # Same race for a cancel: without this the microphone would stay
            # open until the cancel deadline kills the helper.
            self._send({'cmd': 'cancel', 'id': sid})

    def _ev_level(self, ev):
        s = self._active
        if s is None or s.id != ev.get('id') or s.state != _RECORDING:
            return
        s.last_level = time.monotonic()
        self._levels_seen = True
        self._emit('level', s.id, min(1.0, max(0.0, _num(ev.get('rms')))))

    def _ev_partial(self, ev):
        sid = ev.get('id')
        s = self._active
        if not (s is not None and s.id == sid) and sid not in self._finalizing:
            return
        text = ev.get('text')
        self._emit('partial', sid, text if isinstance(text, str) else '')

    def _ev_final(self, ev):
        sid = ev.get('id')
        s = self._finalizing.pop(sid, None)
        if s is None:
            s = self._active
            if s is None or s.id != sid or s.state == _PENDING:
                return                          # stale: dropped
            self._active = None
        text = ev.get('text')
        self._failures = 0                      # a full round trip: healthy
        info = {}
        for flag in ('silent', 'mic_lost'):
            if ev.get(flag) is True:
                info[flag] = True
        if s.auto_info:
            info[s.auto_info] = True
        languages = _str_list(ev.get('languages'))
        if len(languages) >= 2:
            info['languages'] = languages
            info['used'] = _str_list(ev.get('used'))
            dominant = ev.get('dominant')
            info['dominant'] = (dominant if isinstance(dominant, str)
                                else languages[0])
        if info:
            # Not terminal: delivered just before this session's on_final.
            self._emit('final_info', sid, info)
        self._finish(sid, 'final', text if isinstance(text, str) else '',
                     _num(ev.get('audio_sec')), _num(ev.get('elapsed_ms')))
        self._try_send_start()

    def _ev_cancelled(self, ev):
        sid = ev.get('id')
        if self._cancel_wait.pop(sid, None) is not None:
            return                              # confirmation of our cancel
        s = self._finalizing.pop(sid, None)
        if s is None:
            s = self._active
            if s is None or s.id != sid or s.state == _PENDING:
                return
            self._active = None
        self._finish(sid, 'cancelled')          # helper cancelled it itself
        self._try_send_start()

    def _ev_error(self, ev):
        sid = ev.get('id')
        code = ev.get('code') if isinstance(ev.get('code'), str) else 'internal'
        message = ev.get('message')
        message = message if isinstance(message, str) else ''
        if sid is None:
            locale = ev.get('locale') if isinstance(ev.get('locale'), str) else None
            cmd = ev.get('cmd') if isinstance(ev.get('cmd'), str) else None
            if cmd == 'install' and locale is not None:
                # A download failed and the helper said which one: only that
                # locale's stall watchdog is stood down.
                self._installs.pop(locale, None)
                log.warning('Speech asset install failed for %s (%s): %s',
                            locale, code, message)
                self._install_failed(locale, code, message)
                return
            if cmd == 'prewarm' or (cmd is None
                                    and self._prewarm_deadline is not None):
                # The prewarm's answer (unsupported / not installed): do not
                # keep re-issuing it after respawns.
                self._prewarm_deadline = None
                self._prewarm_locale = None
                self._prewarm_languages = ()
            if cmd is None:
                # An older helper names neither the request nor the locale.
                # It may have been a download's error, so stop the stall
                # watchdog for all of them rather than kill a healthy helper
                # minutes later over an install that already failed; any
                # download still running re-arms it with its next progress
                # event.
                if self._installs:
                    for failed in list(self._installs):
                        self._install_failed(failed, code, message)
                self._installs.clear()
            log.warning('Speech helper error (%s): %s', code, message)
            self._emit('error', None, code, message)
            return
        if self._cancel_wait.pop(sid, None) is not None:
            return                              # it is gone either way
        s = self._finalizing.pop(sid, None)
        if s is None:
            s = self._active
            if s is None or s.id != sid or s.state == _PENDING:
                return
            self._active = None
        log.warning('Dictation %s failed in the helper (%s): %s',
                    sid, code, message)
        self._finish(sid, 'error', code, message)
        if code in ('mic_denied', 'speech_denied'):
            self._send({'cmd': 'status'})   # keep last_status truthful
        if code in (self.recycle_on or ()):
            self._recycle_reason = code
        self._try_send_start()

    # ------------------------------------------------------------------
    # speech-io thread: watchdog
    # ------------------------------------------------------------------

    def _helper_busy(self):
        s = self._active
        return bool((s is not None and s.state != _PENDING)
                    or self._finalizing or self._cancel_wait
                    or self._prewarm_deadline is not None or self._installs
                    or self._perm_pending_until is not None or self._tx)

    def _tick(self):
        now = time.monotonic()

        if self._zombies:
            self._zombies = [z for z in self._zombies if z.poll() is None]

        # A start still waiting for a helper (spawning, crash-looping) gets
        # its answer on time whatever state the process is in.
        s = self._active
        if (s is not None and s.state == _PENDING and not s.deferred
                and now > s.deadline):
            detail = self.last_failure[1] if self.last_failure else 'not ready'
            self._active = None
            self._finish(s.id, 'error', E_HELPER_UNAVAILABLE,
                         'speech helper did not come up in time (%s)' % detail)

        proc = self._proc
        if proc is None:
            if (self._want_proc and self.unavailable is None
                    and now >= self._spawn_at):
                self._spawn()
            return

        if proc.poll() is not None:
            # Exited. Pick up anything it managed to say first (a final
            # followed by a crash still counts), then treat it as a crash.
            for _ in range(64):
                if self._proc is not proc:
                    break
                self._on_stdout()
            if self._proc is proc:
                self._helper_died('process exited')
            return

        if not self._ready:
            if now > self._ready_deadline:
                self._fail_helper(E_HELPER_TIMEOUT,
                                  'speech helper did not become ready')
            return

        if self._tx and now - self._tx_since > self.write_timeout:
            self._fail_helper(E_HELPER_TIMEOUT,
                              'speech helper stopped reading commands')
            return

        s = self._active
        if s is not None:
            if s.state == _STARTING and now > s.deadline:
                self._fail_helper(E_HELPER_TIMEOUT,
                                  'no response to start within %.1fs'
                                  % (now - s.t_request))
                return
            if s.state == _RECORDING:
                stall = self.level_stall_timeout
                if (stall and self._levels_seen
                        and now - s.last_level > stall):
                    # Salvage first: the helper has already recognised what
                    # was said and can hand it over in a moment. Killing it
                    # here would throw a possibly minute-long dictation away.
                    # If it really is hung, the stop deadline kills it.
                    log.warning('Dictation %s: microphone stopped delivering '
                                'audio - stopping it to keep the text so far',
                                s.id)
                    self._auto_stop(s, now, 'mic_lost')
                    return
                if s.auto_stop_at is not None and now > s.auto_stop_at:
                    log.warning('Dictation %s hit the %.0fs cap - stopping it',
                                s.id, self.max_session_sec)
                    self._auto_stop(s, now, 'capped')
                    return

        for sid, fs in self._finalizing.items():
            if now > fs.deadline:
                self._fail_helper(E_HELPER_TIMEOUT,
                                  'no transcript after stop')
                return

        for sid, deadline in self._cancel_wait.items():
            if now > deadline:
                self._fail_helper(E_HELPER_TIMEOUT,
                                  'helper did not confirm cancel')
                return

        if self._prewarm_deadline is not None and now > self._prewarm_deadline:
            # (_fail_helper also drops the prewarm, so one slow model load
            # cannot turn into a kill loop.)
            self._fail_helper(E_HELPER_TIMEOUT, 'prewarm did not finish')
            return

        for locale, last in self._installs.items():
            if now - last > self.install_stall_timeout:
                self._fail_helper(E_HELPER_TIMEOUT,
                                  'download of %s stalled' % locale)
                return

        if (self._perm_pending_until is not None
                and now > self._perm_pending_until):
            self._perm_pending_until = None

        if self._ping_deadline is not None:
            if now > self._ping_deadline:
                self._fail_helper(E_HELPER_TIMEOUT,
                                  'speech helper stopped answering pings')
                return
        elif (self.ping_interval and now >= self._next_ping_at
                and not self._helper_busy()):
            self._ping_seq += 1
            self._ping_deadline = now + self.ping_timeout
            if not self._send({'cmd': 'ping', 'seq': self._ping_seq}):
                return

        if self._recycle_reason and not self._helper_busy():
            reason = self._recycle_reason
            log.info('Recycling the speech helper after %s', reason)
            self._fail_helper(E_HELPER_CRASHED,
                              'recycled after %s' % reason, count=False)
            return

        if (self._failures and self._ready_at is not None
                and now - self._ready_at >= self.stable_after):
            self._failures = 0

    def _next_timeout(self):
        """Seconds until the next thing _tick has to look at."""
        now = time.monotonic()
        due = [now + (0.2 if self._zombies else 5.0)]
        add = due.append
        s = self._active
        if s is not None and s.state == _PENDING and not s.deferred:
            add(s.deadline)
        if self._proc is None:
            if self._want_proc and self.unavailable is None:
                add(self._spawn_at)
        elif not self._ready:
            add(self._ready_deadline)
        else:
            if self._tx:
                add(self._tx_since + self.write_timeout)
            if s is not None:
                if s.state == _STARTING:
                    add(s.deadline)
                elif s.state == _RECORDING:
                    if self.level_stall_timeout and self._levels_seen:
                        add(s.last_level + self.level_stall_timeout)
                    if s.auto_stop_at is not None:
                        add(s.auto_stop_at)
            for fs in self._finalizing.values():
                add(fs.deadline)
            for deadline in self._cancel_wait.values():
                add(deadline)
            if self._prewarm_deadline is not None:
                add(self._prewarm_deadline)
            for last in self._installs.values():
                add(last + self.install_stall_timeout)
            if self._perm_pending_until is not None:
                add(self._perm_pending_until)
            if self._ping_deadline is not None:
                add(self._ping_deadline)
            elif self.ping_interval and not self._helper_busy():
                add(self._next_ping_at)
            if self._recycle_reason and not self._helper_busy():
                add(now)
            if self._failures and self._ready_at is not None:
                add(self._ready_at + self.stable_after)
        return max(0.0, min(due) - now)


# ----------------------------------------------------------------------
# Helpers that do not need a running engine
# ----------------------------------------------------------------------

# Used only when the user's own region has no variant of the language.
_DEFAULT_REGION = {
    'en': 'US', 'hi': 'IN', 'es': 'ES', 'fr': 'FR', 'de': 'DE', 'zh': 'CN',
    'ja': 'JP', 'ar': 'SA', 'pt': 'BR', 'it': 'IT', 'ko': 'KR', 'ru': 'RU',
}


def _max_languages(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return 1
    return value


def _split_tag(tag):
    parts = str(tag).replace('_', '-').split('-')
    lang = parts[0].lower()
    region = ''
    for p in parts[1:]:
        if len(p) == 2 and p.isalpha() or len(p) == 3 and p.isdigit():
            region = p.upper()
            break
    return lang, region


def pick_locale(language, supported, installed=(), system_locale=None):
    """Map a language setting to one of the helper's ``supported`` BCP-47 tags.

    ``language`` may be a bare code ('en', 'hi'), a full tag ('en-IN',
    'en_IN'), or 'auto'/empty meaning "follow ``system_locale``". Preference
    order among the variants of that language: exact tag, the user's own
    region (from ``system_locale``), an already-installed variant, the
    conventional default region, then the first alphabetically. Returns None
    when the language has no supported variant at all.
    """
    supported = [str(t) for t in (supported or ())]
    if not supported:
        return None
    sys_lang, sys_region = _split_tag(system_locale) if system_locale else ('', '')
    if not language or str(language).lower() == 'auto':
        language = system_locale or 'en-US'
    lang, region = _split_tag(language)

    by_norm = {}
    for tag in supported:
        by_norm.setdefault(tag.replace('_', '-').lower(), tag)
    if region:
        exact = by_norm.get('%s-%s' % (lang, region.lower()))
        if exact:
            return exact

    variants = sorted(t for t in supported if _split_tag(t)[0] == lang)
    if not variants:
        return None
    installed_set = set(str(t).replace('_', '-').lower() for t in installed or ())

    def rank(tag):
        _, r = _split_tag(tag)
        return (
            0 if (sys_region and r == sys_region) else 1,
            0 if tag.replace('_', '-').lower() in installed_set else 1,
            0 if r == _DEFAULT_REGION.get(lang) else 1,
            tag,
        )
    return min(variants, key=rank)


def transcribe_file(helper_path, wav_path, locale=None, context=None,
                    timeout=120.0, languages=None, explain=False):
    """Run ``freeflo-helper transcribe-file`` once and return its JSON result
    (``{'text', 'audio_sec', 'elapsed_ms', 'locale'}``, plus 'languages',
    'used', 'dominant' and 'sources' with two ``languages``). BLOCKS until
    done: for tests and diagnostics only, never the hotkey or main thread.

    ``explain`` (two languages only) adds why each sentence came from which
    recogniser: 'decisions' (one dict per sentence with 'rule' and 'used'),
    'primary_text' / 'secondary_text' and the word-level
    'primary_segments' / 'secondary_segments'. These contain the transcript,
    so they are for a diagnostic the user asked for, never for the log.

    Raises RuntimeError on a non-zero exit, a timeout, or unparseable output.
    """
    if isinstance(helper_path, (list, tuple)):
        argv = [str(a) for a in helper_path]
    else:
        argv = [str(helper_path)]
    argv += ['transcribe-file', str(wav_path)]
    if locale:
        argv += ['--locale', str(locale)]
    langs = _clean_languages(languages)
    if langs:
        argv += ['--languages', ','.join(langs)]
        if explain:
            argv.append('--explain')
    words = _clean_context(context, 200, 8000)
    if words:
        argv += ['--context', ','.join(words)]
    try:
        # Explicit utf-8: a frozen py2app bundle runs under an ASCII locale
        # and would otherwise choke on the first non-ASCII character.
        res = subprocess.run(argv, capture_output=True, encoding='utf-8',
                             errors='replace', timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError('transcribe-file timed out after %.0fs' % timeout)
    except OSError as e:
        raise RuntimeError('could not run the speech helper: %s' % e)
    if res.returncode != 0:
        err = (res.stderr or '').strip().splitlines()
        raise RuntimeError(err[-1] if err
                           else 'speech helper exit %d' % res.returncode)
    for line in reversed(res.stdout.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and 'text' in obj:
            return obj
    raise RuntimeError('speech helper returned no result')
