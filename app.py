import os
import sys
import json
import time
import queue
import logging
import threading
import subprocess
import rumps
from ApplicationServices import AXIsProcessTrusted, AXIsProcessTrustedWithOptions

import config as cfg
from engine import speech, injector, locales
from engine.vocabulary import Vocabulary, VocabularyError
from engine import history, saved, gauth, backup, logs, updater, telemetry
from engine import refiner, model_manager
from engine import setup_state
from hotkey import HotkeyListener
from ui.hud import HUD
from ui import sounds

log = logging.getLogger('freeflo.app')

_ICONS = {
    'idle':       '🎙',
    'recording':  '🔴',
    'processing': '⏳',
    'disabled':   '○',
}

_ACCESSIBILITY_URL = (
    'x-apple.systempreferences:'
    'com.apple.preference.security?Privacy_Accessibility'
)
_MICROPHONE_URL = (
    'x-apple.systempreferences:'
    'com.apple.preference.security?Privacy_Microphone'
)

# Offered only when macOS refuses the hotkey tap although Accessibility is
# granted (see engine/setup_state.py); a grant itself never needs a restart.
_RESTART_ITEM_TITLE = '↺  Restart freeflo'
_SETUP_ITEM_TITLE = 'Check Permissions…'
_SETUP_NEEDED_TITLE = '⚠️  Finish Setup…'
_RECONNECT_ITEM_TITLE = '⚠️  Reconnect Google Backup…'
_PASTE_LAST_TITLE = 'Paste Last Transcript'

# A push-to-talk hold shorter than this was a stray tap of the key, not a
# dictation: it is dropped silently instead of answering "didn't catch that".
_PTT_MIN_HOLD_SEC = 0.30

# The recording pill appears this long after the push-to-talk key goes down.
# The microphone opens at once (nothing is clipped); only the pill waits, so
# using the key as an ordinary modifier (⌥←, ⌥-click) does not flash it.
_PTT_PILL_DELAY_SEC = 0.15

# After the transcript arrives it still has to be refined (Turbo, up to 8 s)
# and pasted. A dictation that is not finished this long after its transcript
# arrived is abandoned by the watchdog so the ⏳ icon can never stick.
_DELIVERY_GRACE_SEC = 45.0

# Extra slack on top of the speech engine's own stop deadline. The engine
# guarantees a terminal callback for every dictation; this only matters if
# that guarantee is ever broken by a bug.
_FINALIZE_GRACE_SEC = 10.0

# Dictations allowed in flight at once (one recording + ones still being
# transcribed / pasted). Past this a new one is refused as "busy".
_MAX_IN_FLIGHT = 4

# If the push-to-talk key reads as physically up for this many consecutive
# polls while a push-to-talk recording is still running, its key-up event was
# lost (secure text field, tap switched off) and the recording is ended.
_PTT_LOST_RELEASE_POLLS = 3

# Failed backup syncs retry with exponential backoff instead of a fixed 2 min —
# a long outage used to write a warning every 120 s all night.
_BACKUP_RETRY_MIN = 120.0
_BACKUP_RETRY_MAX = 1800.0

# Sinks that show their result in a window instead of pasting it.
_TEST_MODES = ('test', 'ob_test', 'selftest')
_HOTKEY_MODES = ('ptt', 'toggle')

# Shown on the setup window's "Try it" line when the dictation key was let
# go at once (a tap, or the key used as a modifier).
_TRY_HOLD_NOTE = 'Keep the key held while you speak, then let go.'

# A hands-free (toggle) recording that ended BY ITSELF - it reached the
# session length cap, or its microphone went away - leaves the user believing
# it is still running. Their next tap of the toggle key is then meant as
# "stop"; for this long after such an ending that tap is swallowed (with a
# notice) instead of silently opening the microphone again.
_TOGGLE_GUARD_SEC = 120.0

# Posted (distributed) by macOS when the keyboard input source changes.
_INPUT_SOURCE_CHANGED = 'com.apple.Carbon.TISNotifySelectedKeyboardInputSourceChanged'


def _look(settings):
    """(appearance, glass) from the settings. ui.window is imported lazily
    (it pulls in WebKit), so a broken window subsystem never stops dictation."""
    try:
        from ui.window import style_from_settings
        return style_from_settings(settings)
    except Exception:
        log.exception('Could not read the appearance settings')
        theme = str((settings or {}).get('theme') or 'system')
        return ('system', bool((settings or {}).get('glass')) or theme == 'glass')


def _bundle_path():
    """Return the .app bundle path when frozen, else None."""
    if getattr(sys, 'frozen', False):
        # sys.executable = .../freeflo.app/Contents/MacOS/python
        return os.path.normpath(
            os.path.join(os.path.dirname(sys.executable), '..', '..')
        )
    return None


class _Dictation(object):
    """One press-to-paste cycle. Lives in FreefloApp._dicts from the moment
    recording starts until its text has been delivered (or it was cancelled,
    failed, or abandoned by the watchdog)."""
    __slots__ = ('sid', 'mode', 'sink', 'locale', 't_start', 't_stop', 'phase',
                 'deadline', 'info', 'mic_live', 'pill_due', 'pill_shown',
                 'interrupted')

    RECORDING = 'recording'      # mic open
    FINALIZING = 'finalizing'    # stopped; waiting for the transcript
    DELIVERING = 'delivering'    # transcript in hand; refine / paste / log

    def __init__(self, sid, mode, locale, sink=None):
        self.sid = sid
        self.mode = mode             # what started it: ptt / toggle / a test
        # Where the text goes: the mode itself, except for a hotkey dictation
        # made with the setup window in front, which is shown there
        # ('ob_test') instead of being pasted into that window.
        self.sink = sink or mode
        self.locale = locale
        self.t_start = time.monotonic()
        self.t_stop = None
        self.phase = self.RECORDING
        self.deadline = None     # watchdog; None while recording
        self.info = {}           # engine hints: silent / mic_lost / capped
        self.mic_live = False    # the helper said "started": audio is captured
        self.pill_due = False    # the pill's start delay has passed
        self.pill_shown = False  # the recording pill went up for this one
        self.interrupted = False  # a click / key landed late in a PTT hold


class FreefloApp(rumps.App):
    def __init__(self, selftest=None):
        # quit_button=None — we install a custom Quit that flushes a final
        # snapshot + backup sync before exiting (see _quit).
        super().__init__('freeflo', title=_ICONS['idle'], quit_button=None)

        # FREEFLO_SELFTEST: run one scripted dictation and exit (see
        # _selftest_worker). Everything interactive stays off in that mode.
        self._selftest = selftest
        self._selftest_event = threading.Event()
        self._selftest_result = None
        self._selftest_done_at = 0.0
        self._quit_requested = False

        settings = cfg.load()
        self._enabled = settings.get('enabled', True)

        # Restore history from the local snapshot if the DB came up empty
        # (corruption / reset) — before anything reads or writes history.
        try:
            restored = history.restore_from_snapshot_if_empty()
            if restored:
                log.warning('Restored %d history entries from local snapshot', restored)
        except Exception:
            log.exception('Snapshot restore check failed')

        try:
            restored_saved = saved.restore_from_snapshot_if_empty()
            if restored_saved:
                log.warning('Restored %d saved prompts from local snapshot', restored_saved)
        except Exception:
            log.exception('Saved prompts snapshot restore check failed')

        # ------------------------------------------------------------------
        # Dictation state. Everything here is guarded by _lock; it is written
        # from the event-tap thread (hotkeys), the main thread (menu, window)
        # and the speech-cb / deliver threads, and nothing done under the lock
        # ever blocks.
        #
        # The menu-bar icon is DERIVED from this state on every poll (see
        # _display_state) rather than set imperatively, so there is no code
        # path that can leave it showing ⏳ with nothing in flight.
        # ------------------------------------------------------------------
        self._lock = threading.Lock()
        self._dicts = {}              # session id -> _Dictation, in flight
        self._rec_sid = None          # the one that is recording, if any
        self._last_status = 'Ready' if self._enabled else 'Dictation disabled'
        self._last_text = None        # for "Paste Last Transcript"
        self._ptt_seen_down = False   # lost-key-up detection, main thread only
        self._ptt_up_polls = 0
        self._toggle_guard_until = 0.0   # see _TOGGLE_GUARD_SEC
        self._lang_notice_dirty = False  # main thread posts the notification
        # One-off pill notices raised off the main thread (a language that
        # was switched on, a download that finished). _poll_state shows them
        # once nothing is recording and setup is not on screen, so they never
        # cover the recording pill or pile up on a permission prompt.
        self._notices = []
        self._notice_until = 0.0

        # Guards against overlapping backup syncs (e.g. rapid-fire dictation).
        # _backup_pending coalesces a sync requested while one is already
        # running, so the last utterance before quit is never left unsynced.
        self._backup_lock = threading.Lock()
        self._backup_pending = False
        self._backup_retry_scheduled = False
        self._backup_retry_delay = _BACKUP_RETRY_MIN
        # Worker → main thread hand-off for the reconnect menu item, which (like
        # every menu mutation and notification) may only be touched there.
        self._reauth_dirty = False
        self._hide_reconnect_dirty = False
        # Local snapshot writer — same coalescing pattern, off the dictation path.
        self._snapshot_lock = threading.Lock()
        self._snapshot_pending = False

        # Custom words + "heard → written" replacements, applied to every
        # transcript. Loaded once; edits from the settings window save it.
        self._vocab = Vocabulary(path=cfg.get_vocabulary_path())
        try:
            self._vocab.load()
        except Exception:
            log.exception('Could not load the vocabulary')
        # Remembers our own last paste so the next dictation can be joined to
        # it with a space. Forgotten on any real key press or click (the
        # hotkey tap reports those), so it never guesses about a moved cursor.
        self._continuity = injector.PasteContinuity(max_age=120.0)

        # ------------------------------------------------------------------
        # Speech: the microphone and the transcriber live in a child process
        # (freeflo-helper) supervised by SpeechEngine. If it ever hangs or
        # crashes it is killed and replaced; the app itself never has to be
        # restarted. All on_* callbacks arrive on the engine's speech-cb thread.
        # ------------------------------------------------------------------
        self._speech_status = None    # last helper status dict (None: not yet)
        # The languages dictation listens for, primary first (two at most are
        # used at once). Undecided until the helper reports what it supports.
        self._languages = locales.resolve_languages(
            settings.get('languages'), settings.get('language'), None)[0] or ['auto']
        self._locale = self._languages[0]
        self._prewarmed_for = None    # (languages tuple, live text) last prewarmed
        self._lang_dirty = True       # main thread rebuilds the Language menu
        # locale -> 'primary' | 'additional': what a download was asked for
        # (applied when it finishes). Download state itself lives in
        # self._engine.installs().
        self._install_roles = {}
        # A main language chosen while its model was missing: the languages
        # to switch to once that download lands. Until then the languages
        # that work stay in use (so dictation never stops for a download).
        self._pending_languages = None
        # locale (lower) -> monotonic time before which a chosen language's
        # download is not started again by itself, and the back-off used.
        self._install_retry = {}
        self._resume_tick = 0
        self._live_text = bool(settings.get('live_text'))
        sounds.set_enabled(bool(settings.get('sound_enabled')))

        # The floating recording pill. Its meter is fed by the helper's level
        # events, which are already scaled 0..1 (hence 'linear'). It follows
        # the window look (light / dark / glass).
        appearance, glass = _look(settings)
        self._hud = HUD(partials=self._live_text, level_curve='linear',
                        processing_timeout=120.0, glass=glass, theme=appearance)

        self._engine = speech.SpeechEngine(
            cfg.get_helper_path(),
            # The helper finalises one dictation while recording the next, so
            # a quick second press never waits for the first transcript.
            overlap_finalize=True,
            on_started=self._sp_started,
            on_started_info=self._sp_started_info,
            on_level=self._sp_level,
            on_partial=self._sp_partial,
            on_final_info=self._sp_final_info,
            on_final=self._sp_final,
            on_cancelled=self._sp_cancelled,
            on_error=self._sp_error,
            on_status=self._sp_status,
            on_state=self._sp_state,
            on_install_progress=self._sp_install_progress,
            on_installed=self._sp_installed,
            on_install_failed=self._sp_install_failed,
        )
        if not self._engine.is_available:
            # Permanent (macOS older than 26): say so instead of looking alive.
            self._last_status = speech.user_message(self._engine.unavailable[0])
            log.error('Dictation unavailable: %s', self._engine.unavailable[1])

        # Transcripts are refined, pasted and logged one at a time, in the
        # order they were spoken, on this thread — never on the hotkey tap,
        # the main thread or the engine's callback thread.
        self._deliver_q = queue.Queue()
        self._deliver_thread = threading.Thread(
            target=self._deliver_worker, name='deliver', daemon=True)
        self._deliver_thread.start()

        ptt = cfg.resolve_key(settings.get('ptt_key', 'left_option'))
        tog = cfg.resolve_key(settings.get('toggle_key', 'right_option'))
        self._ptt_mask = ptt['mask']
        # FREEFLO_FAKE_AX=<file>: Accessibility is read from that file ('1' =
        # granted) instead of macOS, for setup tests. The hotkeys are then
        # inert, so a simulated run can never record or paste on a real key
        # press (a build started from a terminal is often trusted for real).
        self._ax_sim = os.environ.get('FREEFLO_FAKE_AX') or None
        if self._ax_sim:
            def inert(*args):
                return None
            keys = dict(on_ptt_start=inert, on_ptt_stop=inert,
                        on_ptt_cancel=inert, on_ptt_interrupt=inert,
                        on_toggle=inert, on_escape=inert)
        else:
            keys = dict(
                on_ptt_start=self._on_ptt_start,     # push-to-talk key pressed
                on_ptt_stop=self._on_ptt_stop,       # push-to-talk key released
                on_ptt_cancel=self._on_ptt_cancel,   # key used as a modifier
                on_ptt_interrupt=self._on_ptt_interrupt,  # click late in a hold
                on_toggle=self._on_toggle,           # toggle key tapped
                on_escape=self._on_escape)           # Esc cancels a recording
        self._hotkey = HotkeyListener(
            ptt_keycode=ptt['keycode'], ptt_mask=ptt['mask'],
            toggle_keycode=tog['keycode'], toggle_mask=tog['mask'],
            on_activity=self._continuity.invalidate,
            **keys)

        # --- Menu items ---
        self._toggle_item = rumps.MenuItem(
            'Disable Dictation' if self._enabled else 'Enable Dictation',
            callback=self._toggle,
        )
        self._open_item = rumps.MenuItem('Open freeflo…', callback=self._open_window)
        self._ui = None   # lazily-created WindowController (holds ObjC objects)
        self._pending_nav = None   # (view, section) to show once the page has loaded
        self._status_item = rumps.MenuItem('● Ready')
        self._paste_last_item = rumps.MenuItem(
            _PASTE_LAST_TITLE, callback=self._paste_last)
        self._restart_item = rumps.MenuItem(
            _RESTART_ITEM_TITLE,
            callback=self._restart_app,
        )
        # Shown only when the Google refresh token is dead — retrying can't fix
        # that, so the user needs an explicit way back in.
        self._reconnect_item = rumps.MenuItem(
            _RECONNECT_ITEM_TITLE,
            callback=self._reconnect_google,
        )

        # Self-update. The title flips to a download prompt once a newer release
        # is found. Result is passed from the worker thread to the main-thread
        # poll timer via _pending_update / _update_dirty (no cross-thread UI).
        self._update_item = rumps.MenuItem('Check for Updates…', callback=self._on_update_menu)
        self._pending_update = None
        self._update_status = None
        self._update_dirty = False
        self._updating = False

        # Troubleshooting — local, zero-network diagnostics for bug reports.
        self._help_menu = rumps.MenuItem('Troubleshooting')
        self._help_menu.add(rumps.MenuItem('Reveal Logs', callback=self._reveal_logs))
        self._help_menu.add(rumps.MenuItem('Copy Diagnostics', callback=self._copy_diagnostics))
        self._help_menu.add(rumps.MenuItem('Restart Speech Engine',
                                           callback=self._restart_speech))

        # Setup (permissions). The window is opened by the setup flow, or
        # from this item; its title turns into a warning while something is
        # missing.
        self._setup_item = rumps.MenuItem(_SETUP_ITEM_TITLE, callback=self._open_setup)
        self._ob_ui = None            # setup WindowController (lazily created)

        # Language submenu — rebuilt on the main thread whenever the helper
        # reports its locales or the selection changes (see _poll_state).
        self._lang_menu = rumps.MenuItem('Language')

        # Build the base menu; "Restart freeflo" is added only if macOS
        # refuses the hotkey tap (see _sync_setup_menu).
        self.menu = [
            self._open_item,
            None,
            self._toggle_item,
            None,
            self._status_item,
            self._paste_last_item,
            None,
            self._lang_menu,
            self._update_item,
            self._setup_item,
            self._help_menu,
            None,
            rumps.MenuItem('Quit freeflo', callback=self._quit),
        ]
        self._rebuild_language_menu()

        # Setup: ONE state machine decides what the setup window shows, what
        # macOS is asked and when, and when the hotkey tap (an active
        # CGEventTap, gated by Accessibility) is created. It is the only
        # owner of the tap: nothing else calls self._hotkey.start()/stop().
        # It is driven from _setup_poll on the main thread.
        self._setup = setup_state.SetupFlow(
            ledger=settings.get('setup'), version=cfg.get_version(),
            onboarded=bool(settings.get('onboarded')), ptt_label=ptt['label'])
        self._setup_tick = 0
        self._setup_kick = True       # poll it at the next main-thread tick
        self._helper_replaced = False
        self._ledger_unsaved = False
        self._layout_tick = 0         # keyboard-layout safety net (_poll_state)

        # A dead Google sign-in survives restarts, so the way back in must too.
        if settings.get('backup_auth_expired'):
            self.menu.add(self._reconnect_item)

        # Bring the helper up now (it reports permissions + languages, and the
        # speech model is loaded as soon as the language is known), so the
        # first dictation starts as fast as the hundredth.
        self._engine.ensure_running()
        self._install_wake_observer()
        self._install_input_source_observer()

        self._shown = None      # (icon state, status line) last put on screen
        rumps.Timer(self._poll_state, 0.15).start()
        rumps.Timer(self._after_launch, 0.3).start()

        if self._selftest:
            threading.Thread(target=self._selftest_worker, name='selftest',
                             daemon=True).start()
            return

        # Catch up on any backup that happened on another Mac since we last ran.
        self._maybe_sync_backup()

        # Turbo was left on: bring its local model server back up.
        if settings.get('turbo_enabled'):
            threading.Thread(target=self._autostart_turbo, daemon=True).start()

        # Check for a newer release: a rumps Timer fires once as soon as the
        # run loop is up (that is the launch check), then every 6 hours.
        rumps.Timer(self._periodic_update_check, 6 * 3600).start()

    def _after_launch(self, timer):
        """One-shot, once the run loop is up: build the pill so its first
        appearance is instant, and explain an unusable install."""
        timer.stop()
        self._hud.prewarm()
        injector.refresh_paste_keycode()    # main thread: which key is "V"
        if not self._engine.is_available and not self._selftest:
            code, detail = self._engine.unavailable
            message = detail or speech.user_message(code)
            if code == speech.E_OS_TOO_OLD:
                message += ('\n\nfreeflo uses the on-device speech recognition '
                            'built into macOS 26 and later. Update macOS to '
                            'dictate with it.')
            rumps.alert('freeflo can\u2019t dictate on this Mac', message)

    def _install_wake_observer(self):
        """After sleep the speech model may have been unloaded; reload it so
        the first dictation after opening the lid is not the slow one."""
        try:
            from AppKit import NSWorkspace
            center = NSWorkspace.sharedWorkspace().notificationCenter()
            self._wake_token = center.addObserverForName_object_queue_usingBlock_(
                'NSWorkspaceDidWakeNotification', None, None,
                lambda note: self._on_wake())
        except Exception:
            log.exception('Could not observe wake notifications')

    def _install_input_source_observer(self):
        """Cmd+V has to be posted with the key that types "v" on the layout
        in use (Dvorak, Turkish F... move it). That lookup is main-thread
        only, so it is redone here whenever the input source changes rather
        than on the paste path."""
        try:
            from Foundation import NSDistributedNotificationCenter, NSOperationQueue
            center = NSDistributedNotificationCenter.defaultCenter()
            self._input_source_token = (
                center.addObserverForName_object_queue_usingBlock_(
                    _INPUT_SOURCE_CHANGED, None, NSOperationQueue.mainQueue(),
                    lambda note: injector.refresh_paste_keycode()))
        except Exception:
            log.exception('Could not observe keyboard layout changes')

    def _on_wake(self):
        log.info('Woke from sleep - refreshing the speech engine')
        self._prewarmed_for = None
        self._engine.request_status()    # _sp_status prewarms the language

    # ------------------------------------------------------------------
    # Thread-safe state helpers
    # ------------------------------------------------------------------

    def _set_status(self, status):
        """Set the status line shown in the menu (any thread)."""
        if status:
            with self._lock:
                self._last_status = status

    def _display_state(self):
        """(icon state, status line), derived from what is actually in flight."""
        with self._lock:
            if self._rec_sid is not None:
                state = 'recording'
            elif self._dicts:
                state = 'processing'
            else:
                state = 'idle' if self._enabled else 'disabled'
            return state, self._last_status

    # ------------------------------------------------------------------
    # Timer — runs on main thread, safe to update UI
    # ------------------------------------------------------------------

    def _poll_state(self, _):
        if self._quit_requested:
            self._quit_requested = False
            self._quit(None)
            return

        # Only touch the status item when something changed: every assignment
        # makes AppKit re-render the menu-bar item (about 5% of a core when
        # done on each 150 ms poll, all day).
        shown = self._display_state()
        if shown != self._shown:
            self._shown = shown
            state, status = shown
            self.title = _ICONS.get(state, _ICONS['idle'])
            self._status_item.title = f'● {status}'

        if self._lang_dirty:
            self._lang_dirty = False
            self._rebuild_language_menu()

        # Setup first: until it has decided what launch needs, and while its
        # window is open, other one-off notices wait (see SetupFlow.busy).
        if not self._selftest:
            self._setup_tick += 1
            if self._setup_kick or self._setup_tick >= 3:     # ~0.45 s
                self._setup_tick = 0
                self._setup_poll()
        quiet = not self._selftest and not self._setup.busy

        if self._lang_notice_dirty and quiet:
            self._lang_notice_dirty = False
            text = self._language_notice_text()
            if text:
                rumps.notification('freeflo', 'Dictation language changed', text)

        if self._notices and quiet:
            self._flush_notices()

        # Reflect any update-check result produced on a worker thread. Touching
        # the menu / posting notifications must happen here on the main thread.
        if self._update_dirty:
            self._update_dirty = False
            if self._pending_update:
                v = self._pending_update['version']
                self._update_item.title = f'⬇  Download update (v{v})'
                # Announced once per version, not every 6 hours.
                if self._setup.notice_once('update:%s' % v):
                    self._save_setup_ledger()
                    rumps.notification(
                        'freeflo', f'Update available — v{v}',
                        (self._pending_update.get('notes') or '')[:140]
                        or 'Open the menu to download.',
                    )
            elif not self._updating:
                self._update_item.title = 'Check for Updates…'
            if self._update_status:
                rumps.notification('freeflo', self._update_status, '')
                self._update_status = None

        # Same worker → main-thread hand-off for a dead Google refresh token.
        if self._reauth_dirty:
            self._reauth_dirty = False
            self._show_menu_item(self._reconnect_item, _RECONNECT_ITEM_TITLE)
            rumps.notification(
                'freeflo', 'Google Backup disconnected',
                'Your Google sign-in expired. Open the menu to reconnect — '
                'dictation and local history are unaffected.',
            )
        if self._hide_reconnect_dirty:
            self._hide_reconnect_dirty = False
            if _RECONNECT_ITEM_TITLE in self.menu:
                del self.menu[_RECONNECT_ITEM_TITLE]

        self._check_deadlines()
        self._check_ptt_release()

        if self._selftest:
            return

        # Safety net for a layout change whose notification never arrived.
        self._layout_tick += 1
        if self._layout_tick >= 20:           # 20 x 150 ms = 3 s
            self._layout_tick = 0
            injector.refresh_paste_keycode()

        # A chosen language whose download failed is tried again by itself
        # once its back-off has passed (checked about once a minute).
        self._resume_tick += 1
        if self._resume_tick >= 400:          # 400 x 150 ms = 60 s
            self._resume_tick = 0
            self._resume_downloads()

    # ------------------------------------------------------------------
    # Setup (permissions + the hotkey tap) - engine/setup_state.SetupFlow
    # decides; these carry out its effects. Main thread.
    # ------------------------------------------------------------------

    def _ax_trusted(self):
        if self._ax_sim:
            try:
                with open(self._ax_sim) as f:
                    return f.read().strip() == '1'
            except OSError:
                return False
        return bool(AXIsProcessTrusted())

    def _setup_poll(self):
        """Feed the setup flow what is true right now and carry out what it
        decides. Cheap and idempotent; main thread only."""
        self._setup_kick = False
        if self._ledger_unsaved:
            self._save_setup_ledger()
        effects = []
        if self._helper_replaced:
            self._helper_replaced = False
            effects += self._setup.helper_restarted()
        try:
            running = self._hotkey.is_running()
            if running and not self._setup.tap_ok:
                # Being verified: is macOS really delivering keys to it?
                running = self._hotkey.is_live()
        except Exception:
            log.exception('Could not read the hotkey tap state')
            running = False
        engine_error = (None if self._engine.is_available
                        else speech.user_message(self._engine.unavailable[0]))
        effects += self._setup.update(
            mic=(self._speech_status or {}).get('mic'),
            accessibility=self._ax_trusted(),
            engine_error=engine_error,
            enabled=self._enabled,
            tap_running=running)
        self._run_setup(effects)
        self._sync_setup_menu()

    def _run_setup(self, effects):
        """Carry out the setup flow's effects, in order. Main thread."""
        for e in effects or ():
            name = e[0] if e else None
            try:
                if name == setup_state.EFFECT_LOG:
                    log.info('%s', e[1])
                elif name == setup_state.EFFECT_START_TAP:
                    self._hotkey.stop()
                    self._hotkey.start()
                elif name == setup_state.EFFECT_STOP_TAP:
                    self._hotkey.stop()
                    self._abort_recording()
                elif name == setup_state.EFFECT_REQUEST_MIC:
                    self._engine.request_permissions()
                elif name == setup_state.EFFECT_REQUEST_STATUS:
                    self._engine.request_status()
                elif name == setup_state.EFFECT_OPEN_MIC_SETTINGS:
                    self._open_url(_MICROPHONE_URL)
                elif name == setup_state.EFFECT_PROMPT_ACCESSIBILITY:
                    if self._ax_sim:
                        log.info('setup: (simulated) Accessibility prompt')
                    else:
                        AXIsProcessTrustedWithOptions(
                            {'AXTrustedCheckOptionPrompt': True})
                elif name == setup_state.EFFECT_OPEN_ACCESSIBILITY_SETTINGS:
                    self._open_url(_ACCESSIBILITY_URL)
                elif name == setup_state.EFFECT_OPEN_SETUP:
                    self._show_setup_window()
                elif name == setup_state.EFFECT_CLOSE_SETUP:
                    if self._ob_ui is not None:
                        self._ob_ui.close()
                elif name == setup_state.EFFECT_COMPLETE_ONBOARDING:
                    self._complete_onboarding()
                elif name == setup_state.EFFECT_RESTART_APP:
                    self._restart_app(None)
                elif name == setup_state.EFFECT_NOTICE:
                    # Shown at once (it is the confirmation that setup
                    # worked, and which key to hold); queued notices wait
                    # until it has been read.
                    self._hud.show_notice(e[2], kind=e[3], seconds=4.0)
                    with self._lock:
                        self._notice_until = max(self._notice_until,
                                                 time.monotonic() + 4.0 + 0.3)
                elif name == setup_state.EFFECT_STATUS:
                    self._set_status(e[1])
                elif name == setup_state.EFFECT_SAVE_LEDGER:
                    self._save_setup_ledger()
                elif name == setup_state.EFFECT_PUSH_STATE:
                    self._send_ob_state()
            except Exception:
                log.exception('Setup effect %r failed', name)

    def _open_url(self, url):
        """Open a System Settings pane without blocking the main thread."""
        if self._ax_sim:
            log.info('setup: (simulated) open %s', url)
            return
        threading.Thread(target=subprocess.run, args=(['open', url],),
                         kwargs={'capture_output': True},
                         name='open-url', daemon=True).start()

    def _save_setup_ledger(self):
        try:
            s = cfg.load()
            s['setup'] = self._setup.ledger()
            cfg.save(s)
            self._ledger_unsaved = False
        except Exception:
            log.exception('Could not save the setup record')
            self._ledger_unsaved = True

    def _sync_setup_menu(self):
        """Menu items that follow the setup flow. Main thread."""
        try:
            want = self._setup.menu()
            title = _SETUP_NEEDED_TITLE if want['setup'] else _SETUP_ITEM_TITLE
            if self._setup_item.title != title:
                self._setup_item.title = title
            has_restart = _RESTART_ITEM_TITLE in self.menu
            if want['restart'] and not has_restart:
                # rumps keys an item by the title it was ADDED with, not the
                # one it shows now ('⚠️  Finish Setup…' after a retitle).
                self.menu.insert_after(_SETUP_ITEM_TITLE, self._restart_item)
            elif not want['restart'] and has_restart:
                del self.menu[_RESTART_ITEM_TITLE]
        except Exception:
            log.exception('Could not update the setup menu items')

    def _open_setup(self, _=None):
        """Menu / Settings / Home: show the setup window."""
        self._run_setup(self._setup.open_window())
        self._sync_setup_menu()

    def _make_setup_window(self):
        from ui.window import WindowController
        return WindowController(
            on_message=self._on_onboarding_message,
            html_file='onboarding.html',
            title='freeflo setup',
            on_close=self._on_setup_window_closed)

    def _show_setup_window(self):
        try:
            if self._ob_ui is None:
                self._ob_ui = self._make_setup_window()
            self._ob_ui.show()
            self._send_ob_state()
        except Exception:
            log.exception('Could not open the setup window')
            self._run_setup(self._setup.window_closed())

    def _setup_in_front(self):
        """True while the setup window is open and is the window the keyboard
        goes to. Any thread (two attribute reads; nothing is asked of AppKit)."""
        ui = self._ob_ui
        try:
            return bool(ui is not None and self._setup.window_open
                        and getattr(ui, 'is_key', False))
        except Exception:
            return False

    def _on_setup_window_closed(self):
        self._abort_test_recording('ob_test')
        self._run_setup(self._setup.window_closed())
        self._sync_setup_menu()

    # ------------------------------------------------------------------
    # One-off pill notices (queued from any thread, shown by _poll_state)
    # ------------------------------------------------------------------

    def _queue_notice(self, text, kind='info', seconds=3.5):
        if self._selftest or not text:
            return
        with self._lock:
            if all(n[0] != text for n in self._notices):
                self._notices.append((text, kind, seconds))

    def _flush_notices(self):
        """Show the first queued notice once nothing is recording or being
        transcribed (the pill belongs to dictation first). Main thread."""
        now = time.monotonic()
        with self._lock:
            if (self._rec_sid is not None or self._dicts or not self._notices
                    or now < self._notice_until):
                return
            text, kind, seconds = self._notices.pop(0)
            self._notice_until = now + seconds + 0.3
        self._hud.show_notice(text, kind=kind, seconds=seconds)

    def _show_menu_item(self, item, title):
        """Add a conditional menu item once. Main thread only."""
        if title not in self.menu:
            self.menu.add(item)

    def _check_deadlines(self):
        """Stall watchdog. The speech engine promises exactly one terminal
        callback per dictation and the deliver thread's work is bounded, so
        this should never fire; it exists so that a bug in either cannot
        strand a dictation (and the ⏳ icon) forever. Main thread."""
        now = time.monotonic()
        stuck = []
        with self._lock:
            for sid, d in list(self._dicts.items()):
                if d.deadline is not None and now > d.deadline:
                    stuck.append(d)
                    del self._dicts[sid]
                    if self._rec_sid == sid:
                        self._rec_sid = None
            idle = self._rec_sid is None and not self._dicts
        for d in stuck:
            log.error('Dictation %s stalled in phase=%s (mode=%s) — abandoned',
                      d.sid, d.phase, d.mode)
            if d.phase == _Dictation.FINALIZING:
                self._engine.cancel(d.sid)      # no late transcript, mic freed
            self._set_status('Timed out — try again')
            self._report_test_result(d.sink, '', 'Timed out — try again')
        if stuck and idle:
            self._hud.show_notice('Timed out — try again', kind='error')

    def _check_ptt_release(self):
        """Lost-key-up safety net for push-to-talk. If macOS never delivered
        the release (a secure text field took the keyboard, or the event tap
        was switched off), the recording would run until the engine's session
        cap. The physical modifier state is polled instead: once it has been
        seen down for this hold and then reads up for a few polls in a row,
        the hold is over. Main thread."""
        with self._lock:
            d = self._dicts.get(self._rec_sid) if self._rec_sid is not None else None
            is_ptt = d is not None and d.mode == 'ptt'
        if not is_ptt:
            self._ptt_seen_down = False
            self._ptt_up_polls = 0
            return
        flags = HotkeyListener.modifier_flags()
        if flags is None:
            return
        if flags & self._ptt_mask:
            self._ptt_seen_down = True
            self._ptt_up_polls = 0
            return
        if not self._ptt_seen_down:
            return      # never confirmed down: do not trust the reading
        self._ptt_up_polls += 1
        if self._ptt_up_polls >= _PTT_LOST_RELEASE_POLLS:
            log.warning('Push-to-talk key-up was lost — ending the recording')
            self._ptt_seen_down = False
            self._ptt_up_polls = 0
            self._hotkey.reconcile(flags)                 # resync the tap's view
            self._end_recording('ptt', transcribe=True)   # no-op if that did it

    # ------------------------------------------------------------------
    # Menu callbacks
    # ------------------------------------------------------------------

    def _toggle(self, sender):
        self._enabled = not self._enabled
        sender.title = 'Disable Dictation' if self._enabled else 'Enable Dictation'
        if not self._enabled:
            # Drop a recording that is still running: the mic must not stay
            # open once dictation is switched off. Anything already being
            # transcribed still finishes (it is logged, not pasted).
            self._abort_recording()
        settings = cfg.load()
        settings['enabled'] = self._enabled
        cfg.save(settings)
        # The setup flow owns the hotkey tap: it starts it again only when
        # Accessibility is granted, and takes it out when disabled.
        self._setup_poll()
        if not self._enabled:
            self._set_status('Dictation disabled')
        else:
            missing = self._setup.missing()
            self._set_status(setup_state.STATUS_NEEDS.get(tuple(missing), 'Ready')
                             if missing else 'Ready')

    def _open_window(self, _=None, view=None, section=None):
        """Open the freeflo window. Imported lazily and guarded so a failure in
        the window/WebKit subsystem can never take down core dictation."""
        try:
            if self._ui is None:
                from ui.window import WindowController
                self._ui = WindowController(
                    on_message=self._on_ui_message,
                    on_close=lambda: self._abort_test_recording('test'))
                # Applied when the page says ui_ready.
                self._pending_nav = (view, section) if view else None
            elif view:
                self._push_ui('navigate', self._nav(view, section))
            self._ui.show()
        except Exception as e:
            log.exception('Could not open the window')
            rumps.alert('freeflo', f'Could not open the window:\n{e}')

    @staticmethod
    def _nav(view, section=None):
        payload = {'view': view}
        if section:
            payload['section'] = section
        return payload

    def _open_language_settings(self, _=None):
        self._open_window(view='settings', section='language')

    def _push_ui(self, name, payload):
        """Send an event to the window UI if it is open (never raises)."""
        if self._ui is None:
            return
        try:
            self._ui.send(name, payload)
        except Exception:
            pass

    def _paste_last(self, _):
        """Menu: paste the most recent transcript again (it also stays in
        History). Runs the paste off the main thread, after the menu closed."""
        text = self._last_text
        if not text:
            try:
                entries = history.list_entries(limit=1)
                text = entries[0]['text'] if entries else None
            except Exception:
                text = None
        if not text:
            self._hud.show_notice('Nothing to paste yet')
            return

        def work():
            time.sleep(0.25)    # let the menu close and focus settle
            res = injector.inject(text)
            if res.copied_only:
                self._hud.show_notice('Copied — press ⌘V to paste', seconds=2.5)
            elif res.failed:
                self._hud.show_notice('Couldn’t paste', kind='error')
        threading.Thread(target=work, name='paste-last', daemon=True).start()

    def _restart_speech(self, _):
        """Troubleshooting: kill and respawn the speech helper."""
        log.info('Speech engine restart requested from the menu')
        self._prewarmed_for = None
        self._engine.restart('menu')
        self._hud.show_notice('Speech engine restarted', kind='success')

    def _on_ui_message(self, body):
        """Handle a JS->Python message. Runs on the main thread (WebKit
        delivers script messages there)."""
        try:
            action = body.get('action') if hasattr(body, 'get') else None
        except Exception:
            action = None
        action = str(action) if action is not None else ''

        if action == 'ui_ready':
            nav, self._pending_nav = self._pending_nav, None
            if nav:
                self._push_ui('navigate', self._nav(*nav))
        elif action == 'test_start':
            if self._begin_recording('test'):
                self._push_ui('status', {'state': 'recording'})
            else:
                self._push_ui('status', {'state': 'busy',
                                         'message': self._get_status()})
        elif action == 'test_stop':
            self._push_ui('status', {'state': 'processing'})
            self._end_recording('test', transcribe=True)
        elif action == 'get_shortcuts':
            self._send_shortcuts()
        elif action == 'set_shortcuts':
            self._set_shortcuts(body)
        elif action == 'install_language':
            self._install_language(body)
        elif action == 'get_languages':
            # Answer with what is known now, and ask the helper again: a
            # language pack added or removed in System Settings shows up in
            # the 'languages' event that its answer triggers (_sp_status).
            self._send_languages()
            self._engine.request_status()
        elif action == 'set_languages':
            self._set_languages_from_ui(body)
        elif action == 'set_sound':
            on = self._body_flag(body, 'on', False)
            settings = cfg.load()
            settings['sound_enabled'] = on
            cfg.save(settings)
            sounds.set_enabled(on)
            if on:
                sounds.play_start()     # let them hear what they switched on
            self._send_shortcuts()
        elif action == 'set_live_text':
            on = self._body_flag(body, 'on', False)
            settings = cfg.load()
            settings['live_text'] = on
            cfg.save(settings)
            self._live_text = on
            self._hud.set_partials_enabled(on)
            # The recogniser is configured differently with live text on:
            # load that variant now (same languages) so the next dictation is
            # not the slow one.
            self._apply_prewarm()
            self._send_shortcuts()
        elif action == 'open_mic_settings':
            self._open_url(_MICROPHONE_URL)
        elif action == 'open_setup':
            self._open_setup()
        elif action == 'ack_language_notice':
            # The Home note about a changed dictation language: seen. When
            # Hindi would suit this person, the button added it (one click:
            # it downloads by itself if needed). Then show the languages.
            hindi = (locales.suggest_addition(self._languages, self._speech_status)
                     if self._speech_status else None)
            self._clear_language_notice()
            if hindi:
                self._set_languages(self._languages[:1] + [hindi])
            self._push_ui('navigate', self._nav('settings', 'language'))
            self._send_home()
        elif action == 'get_vocabulary':
            self._send_vocabulary()
        elif action == 'vocab_add_word':
            self._vocab_change(lambda: self._vocab.add(str(body.get('word') or '')),
                               hint_word=str(body.get('word') or ''))
        elif action == 'vocab_remove_word':
            self._vocab_change(lambda: self._vocab.remove(str(body.get('word') or '')))
        elif action == 'vocab_add_replacement':
            self._vocab_change(lambda: self._vocab.add_replacement(
                str(body.get('heard') or ''), str(body.get('written') or '')))
        elif action == 'vocab_remove_replacement':
            self._vocab_change(lambda: self._vocab.remove_replacement(
                str(body.get('heard') or '')))
        elif action == 'get_history':
            q = body.get('query') if hasattr(body, 'get') else None
            self._send_history(str(q) if q else None)
        elif action == 'clear_history':
            try:
                history.clear()
            except Exception:
                pass
            self._send_history(None)
        elif action == 'set_save_history':
            on = self._body_flag(body, 'on', True)
            settings = cfg.load()
            settings['save_history'] = on
            cfg.save(settings)
            self._push_ui('save_history_state', {'on': on})
        elif action == 'get_privacy':
            self._send_privacy()
        elif action == 'set_privacy':
            settings = cfg.load()
            try:
                if body.get('analytics') is not None:
                    settings['analytics_enabled'] = bool(body.get('analytics'))
                if body.get('crash') is not None:
                    settings['crash_enabled'] = bool(body.get('crash'))
            except Exception:
                pass
            cfg.save(settings)
            telemetry.identify()   # re-attach identity if analytics was just turned on
            self._send_privacy()
        elif action == 'get_backup_status':
            self._send_backup_status()
        elif action == 'set_backup_enabled':
            on = self._body_flag(body, 'on', False)
            settings = cfg.load()
            settings['backup_enabled'] = on
            cfg.save(settings)
            self._send_backup_status()
            if on:
                self._maybe_sync_backup(manual=True)
        elif action == 'google_connect':
            threading.Thread(target=self._connect_google, daemon=True).start()
        elif action == 'google_disconnect':
            delete_remote = self._body_flag(body, 'delete_remote', False)
            threading.Thread(
                target=self._disconnect_google, args=(delete_remote,), daemon=True
            ).start()
        elif action == 'backup_sync_now':
            self._maybe_sync_backup(manual=True)
        elif action == 'get_home':
            self._send_home()
        elif action == 'get_theme':
            self._send_theme()
        elif action == 'set_theme':
            self._set_theme(body)
        elif action == 'feature_request':
            self._handle_feature_request(body)
        elif action == 'get_about':
            self._send_about()
        elif action == 'reveal_logs':
            self._reveal_logs(None)
        elif action == 'copy_diagnostics':
            self._copy_diagnostics(None)
        elif action == 'check_updates':
            self._start_update_check(manual=True)
        elif action == 'download_update':
            self._start_update_download()
        elif action == 'get_turbo':
            self._send_turbo_status()
        elif action == 'set_turbo':
            self._set_turbo(bool(body.get('on')))
        elif action == 'set_turbo_style':
            style = str(body.get('style'))
            if style in cfg.REGISTER_KEYS:   # ignore stale/unknown values
                s = cfg.load(); s['turbo_style'] = style; cfg.save(s)
            self._send_turbo_status()
        elif action == 'download_model':
            self._download_model(str(body.get('tier')))
        elif action == 'get_saved_prompts':
            q = body.get('query') if hasattr(body, 'get') else None
            self._send_saved_prompts(str(q) if q else None)
        elif action == 'save_prompt_from_history':
            self._save_from_history(body)
        elif action == 'unsave_prompt':
            self._unsave_prompt(body)
        elif action == 'add_manual_prompt':
            self._add_manual_prompt(body)
        elif action == 'remove_saved_prompt':
            self._remove_saved_prompt(body)

    @staticmethod
    def _body_flag(body, key, default):
        try:
            value = body.get(key)
        except Exception:
            return default
        return default if value is None else bool(value)

    def _get_status(self):
        with self._lock:
            return self._last_status

    # ------------------------------------------------------------------
    # Vocabulary (settings window)
    # ------------------------------------------------------------------

    def _send_vocabulary(self, error=None, hint=None):
        try:
            data = self._vocab.to_dict()
        except Exception:
            log.exception('Could not read the vocabulary')
            data = {'words': [], 'replacements': [], 'limits': {}}
        data['error'] = error or ''
        data['hint'] = hint or ''
        self._push_ui('vocabulary', data)

    def _vocab_change(self, change, hint_word=None):
        """Run one vocabulary edit and echo the new list (with the reason, if
        it was refused). Main thread; the file write is a few KB."""
        error = hint = None
        try:
            change()
            if hint_word and self._vocab.is_ambiguous(hint_word):
                hint = ('“%s” is also an everyday word, so it is not re-spelled '
                        'automatically. Add a replacement for it instead.'
                        % hint_word.strip())
        except VocabularyError as e:
            error = str(e)
        except Exception:
            log.exception('Vocabulary change failed')
            error = 'Could not save that — try again.'
        self._send_vocabulary(error=error, hint=hint)

    def _autostart_turbo(self):
        """Turbo was enabled in a previous session: start its server again."""
        try:
            from engine import models as _models
            if _models.is_installed('lite'):
                self._start_turbo_server('lite')
        except Exception:
            log.exception('Could not bring Turbo back up')

    def _send_turbo_status(self):
        threading.Thread(target=self._send_turbo_status_now, daemon=True).start()

    def _send_turbo_status_now(self):
        from engine import models as _models
        s = cfg.load()
        m = _models.MODELS['lite']
        self._push_ui('turbo_status', {
            'enabled': s.get('turbo_enabled', False),
            'style': s.get('turbo_style', 'natural'),
            'ready': refiner.is_ready(),
            # False in a build made without the llama-server binary.
            'available': os.path.exists(cfg.get_llama_server()),
            'lite': {
                'installed': _models.is_installed('lite'),
                'size_gb': round(m['size_bytes'] / 1e9, 1),
            },
        })

    def _set_turbo(self, on):
        from engine import models as _models
        s = cfg.load()
        s['turbo_enabled'] = on
        cfg.save(s)
        if on:
            tier = 'lite'   # single model now
            if not _models.is_installed(tier):
                self._push_ui('turbo_status', {'needs_download': tier})
                self._send_turbo_status()
                return
            # start the server on a background thread (loading is slow)
            threading.Thread(target=self._start_turbo_server, args=(tier,),
                             daemon=True).start()
        else:
            refiner.stop()
        self._send_turbo_status()

    def _start_turbo_server(self, tier):
        try:
            ok = refiner.start(tier)
        except Exception:
            # e.g. a build without the llama-server binary.
            log.exception('Turbo server could not be launched')
            ok = False
        if not ok:
            log.error('Turbo server failed to start for tier %s', tier)
        self._send_turbo_status()

    def _download_model(self, tier):
        def worker():
            def progress(pct, done, total):
                self._push_ui('turbo_progress', {'tier': tier, 'pct': round(pct, 1)})
            ok, err = model_manager.download(tier, on_progress=progress)
            self._push_ui('turbo_progress',
                          {'tier': tier, 'pct': 100 if ok else 0, 'error': err})
            # If Turbo was switched on before the model existed, the toggle
            # bailed at needs_download. Now that it's here, start the server so
            # the user doesn't have to toggle again.
            if ok and cfg.load().get('turbo_enabled'):
                self._start_turbo_server(tier)   # this also re-sends turbo status
            else:
                self._send_turbo_status()
        threading.Thread(target=worker, daemon=True).start()

    def _send_privacy(self):
        s = cfg.load()
        self._push_ui('privacy', {
            'analytics': s.get('analytics_enabled', True),
            'crash': s.get('crash_enabled', True),
        })

    def _send_history(self, query):
        try:
            entries = history.list_entries(query=query, limit=300)
        except Exception:
            entries = []
        try:
            saved_uuids = list(saved.saved_history_uuids())
        except Exception:
            saved_uuids = []
        settings = cfg.load()
        self._push_ui('history', {
            'entries': entries,
            'save_history': settings.get('save_history', True),
            'saved_uuids': saved_uuids,
            'lang_names': self._lang_names(e.get('language') for e in entries),
        })

    @staticmethod
    def _lang_names(codes):
        """{code: friendly name} for the language badges in the window."""
        names = {}
        for code in codes:
            if code and code not in names and locales.is_concrete(code):
                names[code] = locales.display_name(code)
        return names

    # ------------------------------------------------------------------
    # Saved prompts
    # ------------------------------------------------------------------

    def _send_saved_prompts(self, query):
        try:
            entries = saved.list_entries(query=query, limit=300)
        except Exception:
            entries = []
        self._push_ui('saved_prompts', {'entries': entries})

    def _send_saved_uuids(self):
        try:
            uuids = list(saved.saved_history_uuids())
        except Exception:
            uuids = []
        self._push_ui('saved_uuids', {'uuids': uuids})

    def _save_from_history(self, body):
        try:
            text = str(body.get('text') or '').strip()
            history_uuid = str(body.get('history_uuid') or '')
        except Exception:
            return
        if not text or not history_uuid:
            return
        try:
            saved.add_from_history(text, history_uuid)
        except Exception:
            log.exception('Failed to save prompt from history')
            return
        self._write_snapshot_async()
        self._maybe_sync_backup()
        self._send_saved_uuids()

    def _unsave_prompt(self, body):
        try:
            history_uuid = str(body.get('history_uuid') or '')
        except Exception:
            return
        if not history_uuid:
            return
        try:
            saved.remove_by_history_uuid(history_uuid)
        except Exception:
            log.exception('Failed to unsave prompt')
            return
        self._write_snapshot_async()
        self._maybe_sync_backup()
        self._send_saved_uuids()

    def _add_manual_prompt(self, body):
        try:
            text = str(body.get('text') or '').strip()
        except Exception:
            return
        if not text:
            return
        try:
            saved.add_manual(text)
        except Exception:
            log.exception('Failed to add manual prompt')
            return
        self._write_snapshot_async()
        self._maybe_sync_backup()
        self._send_saved_prompts(None)

    def _remove_saved_prompt(self, body):
        try:
            uuid = str(body.get('uuid') or '')
        except Exception:
            return
        if not uuid:
            return
        try:
            saved.remove(uuid)
        except Exception:
            log.exception('Failed to remove saved prompt')
            return
        self._write_snapshot_async()
        self._maybe_sync_backup()
        self._send_saved_prompts(None)
        self._send_saved_uuids()

    # ------------------------------------------------------------------
    # Google backup
    # ------------------------------------------------------------------

    @staticmethod
    def _backup_connected(settings):
        """Connected to Google Backup, from the settings alone. Never the
        Keychain: every ad-hoc build is a new code identity, and reading
        the token from the main thread can put up a login-keychain password
        prompt that freezes the app. The token is read only by a sync or a
        connect, on a worker thread (backup.NotConnected if it is gone)."""
        return bool(settings.get('backup_account_email'))

    def _send_backup_status(self, extra=None):
        settings = cfg.load()
        payload = {
            'configured': gauth.is_configured(),
            'connected': self._backup_connected(settings),
            'enabled': settings.get('backup_enabled', False),
            'email': settings.get('backup_account_email'),
            'last_synced': settings.get('backup_last_synced'),
            'syncing': self._backup_lock.locked(),
            'reauth': bool(settings.get('backup_auth_expired')),
        }
        if extra:
            payload.update(extra)
        self._push_ui('backup_status', payload)

    def _connect_google(self):
        """Runs the OAuth loopback flow on a background thread — it blocks
        until the browser redirect lands, which would freeze the UI on the
        main thread."""
        try:
            email = gauth.connect()
        except gauth.NotConfigured as e:
            self._send_backup_status({'error': str(e)})
            return
        except Exception as e:
            self._send_backup_status({'error': f'Sign-in failed: {e}'})
            return
        settings = cfg.load()
        settings['backup_account_email'] = email
        settings['backup_enabled'] = True
        settings['backup_auth_expired'] = False   # fresh credentials
        cfg.save(settings)
        self._backup_retry_delay = _BACKUP_RETRY_MIN
        self._hide_reconnect_dirty = True
        telemetry.capture('backup_connected', {'source': 'settings'})
        self._send_backup_status()
        self._maybe_sync_backup(manual=True)

    def _disconnect_google(self, delete_remote):
        if delete_remote:
            try:
                backup.delete_remote()
                backup.delete_remote_saved()
            except Exception as e:
                self._send_backup_status(
                    {'error': f'Could not delete Drive backup: {e}. Still connected — try again.'}
                )
                return
        gauth.disconnect()
        settings = cfg.load()
        settings['backup_enabled'] = False
        settings['backup_account_email'] = None
        settings['backup_last_synced'] = None
        settings['backup_auth_expired'] = False   # no credentials to be stale now
        cfg.save(settings)
        self._hide_reconnect_dirty = True
        self._send_backup_status()

    def _maybe_sync_backup(self, manual=False):
        """Kick off a sync in the background if backup is on and connected.
        `manual` only affects whether we report back "not connected" — the
        automatic post-dictation trigger should stay silent."""
        settings = cfg.load()
        if not (settings.get('backup_enabled') and self._backup_connected(settings)):
            if manual:
                self._send_backup_status({'error': 'Connect Google Backup first.'})
            return
        # A dead refresh token can't be retried around, so automatic triggers
        # stand down until the user reconnects. An explicit "Sync now" always
        # gets one more attempt — this flag must never become a dead end.
        if settings.get('backup_auth_expired'):
            if not manual:
                return
            self._clear_backup_auth_expired()
        if not self._backup_lock.acquire(blocking=False):
            # A sync is already running; ask it to run once more when it
            # finishes so a dictation that landed mid-sync isn't left behind.
            self._backup_pending = True
            return
        self._backup_pending = False
        threading.Thread(target=self._sync_backup_worker, daemon=True).start()

    def _sync_backup_worker(self):
        self._send_backup_status()  # reflects syncing=True
        try:
            result = None
            while True:
                result = backup.sync()
                try:
                    backup.sync_saved()
                except Exception:
                    log.warning('Saved prompts sync failed', exc_info=True)
                if not self._backup_pending:
                    break
                self._backup_pending = False
        except Exception as e:
            self._backup_lock.release()
            if isinstance(e, backup.NotConnected):
                # The settings name an account but the Keychain has no token
                # (removed by hand, or another Mac's settings restored).
                # Retrying cannot help; say so once.
                log.warning('Backup sync skipped: no Google sign-in stored')
                self._send_backup_status({'error': str(e)})
                return
            if gauth.is_auth_expired(e):
                # Terminal: the refresh token is revoked or expired. Retrying
                # every 2 minutes forever (which it used to do, for 22 hours
                # straight) can't fix it — surface it and stand down.
                log.warning('Google Backup needs reconnecting: %s', e)
                settings = cfg.load()
                settings['backup_auth_expired'] = True
                cfg.save(settings)
                self._reauth_dirty = True   # main thread shows menu + notification
                self._send_backup_status(
                    {'error': 'Google sign-in expired — reconnect to resume backup.',
                     'reauth': True})
                return
            log.warning('Backup sync failed: %s', e)
            self._send_backup_status({'error': f'Sync failed: {e}'})
            self._schedule_backup_retry()
            return
        settings = cfg.load()
        settings['backup_last_synced'] = result['synced_at']
        cfg.save(settings)
        self._backup_retry_delay = _BACKUP_RETRY_MIN   # recovered — reset backoff
        self._backup_lock.release()
        self._send_backup_status()

    def _clear_backup_auth_expired(self):
        settings = cfg.load()
        if settings.get('backup_auth_expired'):
            settings['backup_auth_expired'] = False
            cfg.save(settings)
        self._backup_retry_delay = _BACKUP_RETRY_MIN

    def _reconnect_google(self, _):
        """Menu item shown when the stored Google credentials died."""
        self._clear_backup_auth_expired()
        threading.Thread(target=self._connect_google, daemon=True).start()

    def _schedule_backup_retry(self):
        """After a transient failure, retry later so unsynced entries aren't
        stranded until the next dictation or launch. Coalesced so repeated
        failures don't stack timers, and backed off so a long outage doesn't
        write a warning every 2 minutes all night."""
        if self._backup_retry_scheduled:
            return
        self._backup_retry_scheduled = True
        delay = self._backup_retry_delay
        self._backup_retry_delay = min(_BACKUP_RETRY_MAX, delay * 2)

        def _retry():
            self._backup_retry_scheduled = False
            self._maybe_sync_backup()
        t = threading.Timer(delay, _retry)
        t.daemon = True   # a pending retry must never hold up quitting
        t.start()

    # ------------------------------------------------------------------
    # Local snapshot safety net + clean-quit flush
    # ------------------------------------------------------------------

    def _write_snapshot_async(self):
        """Write a local history snapshot off the dictation path, coalescing
        overlapping requests (same pattern as backup sync)."""
        if not self._snapshot_lock.acquire(blocking=False):
            self._snapshot_pending = True
            return
        self._snapshot_pending = False
        threading.Thread(target=self._snapshot_worker, daemon=True).start()

    def _snapshot_worker(self):
        try:
            while True:
                history.write_snapshot()
                try:
                    saved.write_snapshot()
                except Exception:
                    log.warning('Saved prompts snapshot failed', exc_info=True)
                if not self._snapshot_pending:
                    break
                self._snapshot_pending = False
        except Exception as e:
            log.warning('History snapshot failed: %s', e)
        finally:
            self._snapshot_lock.release()

    def _flush_backup_blocking(self):
        try:
            settings = cfg.load()
            if settings.get('backup_auth_expired'):
                return   # would fail; don't spend the quit budget on it
            has_dirty = history.dirty_entries() or saved.dirty_entries()
            if (settings.get('backup_enabled') and has_dirty
                    and self._backup_connected(settings)):
                backup.sync()
                backup.sync_saved()
        except Exception as e:
            log.warning('Final backup flush failed: %s', e)

    def _quit(self, _):
        """Flush a final local snapshot + best-effort backup sync, then quit.
        Every step is bounded so nothing can make freeflo unquittable."""
        try:
            history.write_snapshot()
            saved.write_snapshot()
        except Exception:
            log.exception('Snapshot on quit failed')
        if not self._selftest:
            t = threading.Thread(target=self._flush_backup_blocking, daemon=True)
            t.start()
            t.join(timeout=6.0)
        # Stop the speech helper (quit, then SIGKILL after a grace period):
        # that is also what releases the microphone for certain.
        try:
            self._engine.shutdown(timeout=2.5)
        except Exception:
            log.exception('Stopping the speech engine failed')
        try:
            injector.flush()     # put the user's clipboard back if still pending
        except Exception:
            pass
        try:
            telemetry.flush()
        except Exception:
            pass
        refiner.stop()   # never leave a llama-server process behind
        rumps.quit_application()

    def _send_shortcuts(self):
        """The dictation keys and the two "while you dictate" switches for
        the Settings view. (Languages travel in their own 'languages' event,
        see _send_languages.) Safe from any thread."""
        if self._ui is None:
            return
        settings = cfg.load()
        keys = [{'id': k, 'label': v['label']} for k, v in cfg.HOTKEY_KEYS.items()]
        self._push_ui('shortcuts', {
            'keys': keys,
            'ptt_key': settings.get('ptt_key', 'left_option'),
            'toggle_key': settings.get('toggle_key', 'right_option'),
            'sound': bool(settings.get('sound_enabled')),
            'live_text': bool(settings.get('live_text')),
        })

    def _set_shortcuts(self, body):
        try:
            ptt = str(body.get('ptt_key'))
            tog = str(body.get('toggle_key'))
        except Exception:
            return
        if ptt not in cfg.HOTKEY_KEYS or tog not in cfg.HOTKEY_KEYS:
            self._push_ui('shortcuts_saved', {'ok': False, 'error': 'Unknown key.'})
            return
        if ptt == tog:
            self._push_ui('shortcuts_saved',
                          {'ok': False, 'error': 'Pick two different keys.'})
            return

        settings = cfg.load()
        settings['ptt_key'] = ptt
        settings['toggle_key'] = tog
        cfg.save(settings)

        # Apply live — no tap restart, just swap the bindings.
        p, t = cfg.resolve_key(ptt), cfg.resolve_key(tog)
        self._ptt_mask = p['mask']
        self._hotkey.set_bindings(p['keycode'], p['mask'], t['keycode'], t['mask'])
        self._setup.ptt_label = p['label']
        self._send_shortcuts()
        self._push_ui('shortcuts_saved', {'ok': True})

    def _send_home(self):
        """Dashboard payload: usage stats + recent activity for the Home view."""
        try:
            st = history.stats()
        except Exception:
            st = {'total': 0, 'today': 0, 'words_week': 0}
        try:
            recent = history.list_entries(limit=6)
        except Exception:
            recent = []
        settings = cfg.load()
        self._push_ui('home', {
            'stats': st,
            'language': self._locale,
            'language_name': locales.label(self._languages, self._speech_status),
            'ptt_key': settings.get('ptt_key', 'left_option'),
            'enabled': self._enabled,
            'recent': recent,
            'alert': self._home_alert(),
        })

    def _home_alert(self):
        """The one thing (if any) stopping dictation right now, for a banner
        on the Home view: {'text', 'action', 'button'} or None."""
        if not self._engine.is_available:
            return {'text': speech.user_message(self._engine.unavailable[0])}
        status = self._speech_status or {}
        if 'accessibility' in self._setup.missing():
            return {'text': 'Accessibility is off, so your dictation keys can’t work.',
                    'action': 'open_setup', 'button': 'Fix'}
        if status.get('mic') == 'undetermined':
            return {'text': 'freeflo has not been allowed to use the microphone yet.',
                    'action': 'open_setup', 'button': 'Fix'}
        if status.get('mic') == 'denied':
            return {'text': 'Microphone access is off, so freeflo can’t hear you.',
                    'action': 'open_mic_settings', 'button': 'Open System Settings'}
        if self._speech_status is not None and not status.get('input_device'):
            return {'text': 'No microphone found. Connect one to dictate.'}
        if self._speech_status is not None:
            ready, missing = locales.usable(self._languages, self._speech_status)
            if not ready:
                name = locales.short_name(self._locale, self._speech_status)
                fraction = self._downloading().get(self._locale)
                if fraction is not None:
                    return {'text': 'Downloading %s (%d%%). You can dictate '
                                    'when it is done.' % (name, round(fraction * 100))}
                return {'text': '%s needs a one-time download before you can '
                                'dictate in it.' % name,
                        'action': 'go_settings', 'button': 'Open Settings'}
            # A chosen second language that is not here and not on its way
            # (its download failed or was cut off): dictation works, in
            # fewer languages than the user picked - say so, with a button.
            for code in missing:
                st = self._install_state(code) or {}
                if st.get('state') == 'downloading':
                    continue
                failed = st.get('state') == 'failed'
                name = locales.short_name(code, self._speech_status)
                return {'text': '%s %s, so freeflo listens for %s only.' % (
                            name, 'couldn’t download' if failed else 'isn’t downloaded yet',
                            locales.label(ready, self._speech_status)),
                        'action': 'install_language', 'code': code,
                        'button': 'Try again' if failed else 'Download',
                        'level': 'info'}
        note = self._language_notice_text()
        if note:
            hindi = (locales.suggest_addition(self._languages, self._speech_status)
                     if self._speech_status else None)
            return {'text': note, 'action': 'ack_language_notice',
                    'button': 'Add Hindi' if hindi else 'Choose languages'}
        return None

    # ------------------------------------------------------------------
    # "Your dictation language changed" - shown once after an update that
    # could not keep the old choice (Auto-detect, or a language the on-device
    # recogniser does not have). Stored in the settings until acknowledged.
    # ------------------------------------------------------------------

    @staticmethod
    def _language_change(stored, locale):
        """What to tell the user when the stored language ``stored`` was
        migrated to ``locale``: 'auto', the old language code, or None when
        the coverage did not change ('en' -> 'en-IN')."""
        old = locales.normalize(stored)
        if not old:
            return None
        if old == 'auto':
            return 'auto'
        lang = old.split('-')[0]
        if lang == locales.normalize(locale).split('-')[0]:
            return None
        return lang

    def _language_notice_text(self, settings=None):
        note = (settings or cfg.load()).get('language_notice')
        if not isinstance(note, dict):
            return None
        status = self._speech_status
        now = locales.label(self._languages, status) or locales.display_name(
            note.get('now') or self._locale)
        was = note.get('was')
        if was == 'auto':
            text = 'Auto-detect is no longer available: freeflo now listens for %s.' % now
            if status and locales.suggest_addition(self._languages, status):
                text += ' Add Hindi to dictate Hindi or mixed speech.'
            else:
                text += ' You can change this in Settings.'
            return text
        return ('%s is not available for dictation in this version, so '
                'freeflo now listens for %s. Choose another language in '
                'Settings.' % (locales.display_name(was), now))

    def _clear_language_notice(self):
        settings = cfg.load()
        if settings.pop('language_notice', None) is not None:
            cfg.save(settings)

    def _send_theme(self):
        s = cfg.load()
        appearance, glass = _look(s)
        self._push_ui('theme', {
            'theme': 'glass' if glass else appearance,
            'glass': glass,
            'appearance': appearance,
        })

    def _set_theme(self, body):
        """{theme, appearance?, glass?} from the page. 'theme' alone is the
        older form ('glass' = glass on, following the system)."""
        try:
            theme = body.get('theme')
            appearance = body.get('appearance')
            glass = body.get('glass')
        except Exception:
            return
        theme = str(theme) if theme is not None else None
        if theme is not None and theme not in ('system', 'light', 'dark', 'glass'):
            theme = None
        s = cfg.load()
        old_appearance, old_glass = _look(s)
        if appearance not in ('system', 'light', 'dark'):
            appearance = (theme if theme in ('system', 'light', 'dark')
                          else old_appearance)
        if glass is None:
            glass = (theme == 'glass') if theme is not None else old_glass
        glass = bool(glass)
        if theme is None and body.get('appearance') is None and body.get('glass') is None:
            return
        s['appearance'] = appearance
        s['glass'] = glass
        s['theme'] = 'glass' if glass else appearance      # older builds read this
        cfg.save(s)
        log.info('Look: %s, glass %s', appearance, 'on' if glass else 'off')
        # The window that sent this already changed itself; bring the
        # other one and the recording pill along.
        if self._ob_ui is not None:
            try:
                self._ob_ui.set_style(appearance, glass)
            except Exception:
                log.exception('Could not restyle the setup window')
        self._hud.set_look(theme=appearance, glass=glass)
        self._send_theme()

    def _handle_feature_request(self, body):
        """Capture a feature request. For now (no backend) it goes to the log
        and — if analytics consent is on — a PostHog event. The text here is a
        user-authored feature idea, not a transcription."""
        try:
            text = str(body.get('text') or '').strip()
        except Exception:
            text = ''
        if not text:
            self._push_ui('feature_ack', {'ok': False})
            return
        settings = cfg.load()
        log.info('Feature request submitted (%d chars)', len(text))
        telemetry.capture('feature_requested', {
            'text': text[:1000],
            'role': settings.get('profile_role'),
        })
        self._push_ui('feature_ack', {'ok': True})

    def _restart_app(self, _):
        """Last resort, offered only when macOS keeps refusing the hotkey tap
        although Accessibility is granted: quit and reopen THIS bundle (not
        whatever is in /Applications) once this process has exited."""
        target = _bundle_path()
        if self._ax_sim or self._selftest or not target:
            log.info('Restart requested - not restarting (%s)',
                     'simulated setup' if self._ax_sim else
                     'self-test' if self._selftest else 'not running as an app')
            return
        log.info('Restarting freeflo (%s)', target)
        script = ('i=0; while kill -0 "$1" 2>/dev/null && [ $i -lt 150 ]; '
                  'do sleep 0.1; i=$((i+1)); done; open "$2"')
        try:
            subprocess.Popen(['/bin/sh', '-c', script, 'freeflo-restart',
                              str(os.getpid()), target], close_fds=True,
                             start_new_session=True)
        except Exception:
            log.exception('Could not schedule the relaunch')
            return
        self._quit(None)

    # ------------------------------------------------------------------
    # Self-update (GitHub Releases) + local diagnostics
    # ------------------------------------------------------------------

    def _periodic_update_check(self, _):
        # A rumps Timer fires once at start-up, so this is also the launch check.
        self._start_update_check(manual=False)

    def _start_update_check(self, manual):
        if self._updating:
            return
        if manual:
            self._push_update_status('Checking…', busy=True)
        threading.Thread(target=self._check_updates_worker, args=(not manual,),
                         name='update-check', daemon=True).start()

    def _push_update_status(self, text, available=None, busy=False):
        if available is None:
            available = bool(self._pending_update)
        self._push_ui('update_status', {'text': text, 'available': bool(available),
                                        'busy': bool(busy)})

    def _check_updates_worker(self, auto):
        """Query GitHub for a newer release. Runs off the main thread; hands the
        result back via _pending_update / _update_dirty for _poll_state."""
        try:
            info = updater.check_for_update(cfg.get_version())
        except Exception:
            log.exception('Update check failed')
            info = None
        self._pending_update = info
        if info:
            log.info('Update available: v%s', info['version'])
            self._push_update_status('Version %s is available.' % info['version'],
                                     available=True)
        else:
            if not auto:
                self._update_status = "You're up to date"
            self._push_update_status('You have the latest version (%s).'
                                     % cfg.get_version(), available=False)
        self._update_dirty = True

    def _on_update_menu(self, _):
        if self._updating:
            return
        if self._pending_update:
            self._update_item.title = 'Downloading update…'
            self._start_update_download()
        else:
            self._update_item.title = 'Checking…'
            self._start_update_check(manual=True)

    def _start_update_download(self):
        if self._updating:
            return
        if not self._pending_update:
            self._start_update_check(manual=True)
            return
        self._updating = True
        self._push_update_status('Downloading the update…', busy=True)
        threading.Thread(target=self._download_update_worker, name='update-download',
                         daemon=True).start()

    def _download_update_worker(self):
        info = self._pending_update or {}
        try:
            path = updater.download_update(info.get('download_url'), info.get('version'))
            updater.reveal(path)
            telemetry.capture('update_downloaded', {'to_version': info.get('version')})
            self._update_status = 'Update downloaded — open it, then drag to Applications'
        except Exception as e:
            log.warning('Update download failed (%s); opening release page', e)
            updater.open_url(info.get('url'))
            self._update_status = 'Opened the download page in your browser'
        finally:
            self._updating = False
            self._update_dirty = True
            self._push_update_status(self._update_status or '', busy=False)

    def _send_about(self):
        self._push_ui('about', {
            'version': cfg.get_version(),
            'can': ['reveal_logs', 'copy_diagnostics', 'open_setup', 'check_updates'],
        })
        if self._updating:
            self._push_update_status('Downloading the update…', busy=True)
        elif self._pending_update:
            self._push_update_status('Version %s is available.'
                                     % self._pending_update['version'], available=True)

    # ------------------------------------------------------------------
    # The setup window (ui/onboarding.html). What it shows comes from the
    # setup flow (SetupFlow.view()); its buttons become flow actions.
    # ------------------------------------------------------------------

    def _push_ob(self, name, payload):
        if self._ob_ui is None:
            return
        try:
            self._ob_ui.send(name, payload)
        except Exception:
            pass

    # The setup page's buttons -> setup flow actions. A row button always
    # sends its row's "grant" message; the flow decides what that means right
    # now (ask macOS, or open System Settings when asking is used up).
    _OB_ACTIONS = {
        'ob_grant_mic': setup_state.ACT_GRANT_MIC,
        'ob_grant_accessibility': setup_state.ACT_GRANT_ACCESSIBILITY,
        'ob_complete': setup_state.ACT_FINISH,
    }

    def _on_onboarding_message(self, body):
        """JS->Python for the setup window (runs on the main thread)."""
        try:
            action = body.get('action') if hasattr(body, 'get') else None
        except Exception:
            action = None
        action = str(action) if action is not None else ''

        if action in ('ob_get_state', 'ob_get_state_fresh'):
            # The page asks again while something is missing (a switch
            # flipped in System Settings sends no event of its own).
            self._setup_poll()
            self._send_ob_state()
        elif action in self._OB_ACTIONS:
            self._ob_action(self._OB_ACTIONS[action], body)
        elif action == 'ob_set_consent':
            if self._consent_asked():
                self._save_consent(body)
        elif action == 'ob_test_start':
            if self._begin_recording('ob_test'):
                self._push_ob('ob_test_state', {'recording': True})
            else:
                self._push_ob('ob_test_result',
                              {'text': '', 'note': self._get_status()})
        elif action == 'ob_test_stop':
            self._end_recording('ob_test', transcribe=True)

    def _ob_action(self, action_id, body):
        if action_id == setup_state.ACT_FINISH:
            # Consent is asked on first run only. A repair / review window
            # does not show the question, so its Done must never write the
            # two privacy switches (Settings > Privacy owns them by then).
            if self._consent_asked():
                self._save_consent(body)
        self._run_setup(self._setup.action(action_id))
        self._sync_setup_menu()
        self._send_ob_state()

    def _send_ob_state(self):
        """The setup flow's view for the setup window. Safe from any thread;
        a no-op while that window has never been opened."""
        if self._ob_ui is None:
            return
        view = setup_state.page_state(
            self._setup.view(), cfg.load(),
            lambda key: cfg.resolve_key(key)['label'], cfg.get_version())
        self._push_ob('ob_state', view)

    def _consent_asked(self):
        """True while the setup window asks the consent question: first run
        only (never in a repair or review window)."""
        try:
            return (self._setup.mode == setup_state.MODE_FIRST_RUN
                    and not cfg.load().get('onboarded'))
        except Exception:
            return False

    def _save_consent(self, body):
        if not hasattr(body, 'get'):
            return
        settings = cfg.load()
        try:
            if body.get('analytics') is not None:
                settings['analytics_enabled'] = bool(body.get('analytics'))
            if body.get('crash') is not None:
                settings['crash_enabled'] = bool(body.get('crash'))
        except Exception:
            return
        cfg.save(settings)

    def _complete_onboarding(self):
        """First run finished (the setup flow decides when; it also closes
        the window). Records it once, with the consent already saved."""
        settings = cfg.load()
        settings['onboarded'] = True
        settings['consent_version'] = 1
        cfg.save(settings)
        cfg.get_or_create_install_id()
        log.info('Onboarding complete (analytics=%s crash=%s role=%s)',
                 settings.get('analytics_enabled'), settings.get('crash_enabled'),
                 settings.get('profile_role'))
        telemetry.identify()
        telemetry.capture('onboarding_completed', {
            'mic': (self._speech_status or {}).get('mic') == 'granted',
            # From the settings, not the Keychain: reading the Keychain from
            # a re-signed build can raise a password prompt.
            'signed_in': bool(settings.get('backup_account_email')),
            'role': settings.get('profile_role'),
            'analytics': settings.get('analytics_enabled'),
            'crash': settings.get('crash_enabled'),
        })

    def _reveal_logs(self, _):
        subprocess.run(['open', logs.log_dir()], capture_output=True)

    def _copy_diagnostics(self, _):
        try:
            eng = self._engine.diagnostics()
            st = eng.get('status') or {}
            extra = [
                f'engine  : {eng.get("state")} {eng.get("detail") or ""}'.rstrip(),
                f'helper v: {eng.get("helper_version")} '
                f'(spawns {eng.get("spawns")}, restarts {eng.get("restarts")})',
                f'mic     : {st.get("mic")} / input '
                f'{"present" if st.get("input_device") else "none"}',
                f'language: {" + ".join(self._languages)} '
                f'({len(st.get("installed") or ())} installed, '
                f'up to {locales.max_languages(st)} at once)',
                self._setup.summary(),
            ]
            if eng.get('last_failure'):
                extra.append(f'last err: {eng["last_failure"][0]}')
            subprocess.run(['pbcopy'],
                           input=logs.diagnostics(extra=extra).encode('utf-8'),
                           capture_output=True)
            rumps.notification('freeflo', 'Diagnostics copied',
                               'Paste it into your bug report.')
        except Exception as e:
            log.warning('Copy diagnostics failed: %s', e)

    # ------------------------------------------------------------------
    # Languages — the list comes from the speech helper at runtime. Up to
    # two are listened for at once (primary first, e.g. en-IN + hi-IN): the
    # helper runs both recognisers on the same audio and keeps the better
    # one sentence by sentence.
    # ------------------------------------------------------------------

    def _downloading(self):
        """{locale: fraction} of the language downloads in progress."""
        try:
            installs = self._engine.installs()
        except Exception:
            return {}
        return dict((code, float(v.get('fraction') or 0.0))
                    for code, v in installs.items()
                    if v.get('state') == 'downloading')

    def _install_state(self, code):
        """The engine's record of a download of ``code`` (any spelling)."""
        try:
            installs = self._engine.installs()
        except Exception:
            return None
        key = locales.normalize(code).lower()
        for k, v in installs.items():
            if locales.normalize(k).lower() == key:
                return v
        return None

    def _canonical(self, code):
        """``code`` spelled as the helper lists it (when it does)."""
        code = locales.normalize(code)
        for tag in (self._speech_status or {}).get('supported') or ():
            if locales.normalize(tag).lower() == code.lower():
                return tag
        return code

    def _apply_prewarm(self, force=False):
        """Load the recognisers for the languages that are ready, so the next
        dictation starts at once. Re-issued only when what would be loaded
        changes (languages or live text). Any thread."""
        status = self._speech_status
        if status is None:
            return
        ready, _ = locales.usable(self._languages, status)
        if not ready:
            return
        key = (tuple(ready), bool(self._live_text))
        if not force and key == self._prewarmed_for:
            return
        self._prewarmed_for = key
        self._engine.prewarm(ready[0], partials=self._live_text,
                             languages=ready if len(ready) > 1 else None)

    def _rebuild_language_menu(self):
        """Fill the Language submenu with one-click modes: the languages in
        use together (✓), each of them alone, Hindi + English when it
        would suit, then the other downloaded languages. Main thread only."""
        menu = self._lang_menu
        try:
            if getattr(menu, '_menu', None) is not None:
                menu.clear()
            status = self._speech_status
            langs = list(self._languages)
            current = [locales.normalize(t).lower() for t in langs]
            modes, seen = [], set()

            def add(mode, title):
                key = tuple(locales.normalize(t).lower() for t in mode)
                if key in seen or not mode:
                    return
                seen.add(key)
                ready, missing = (locales.usable(mode, status) if status
                                  else (mode, []))
                if missing:
                    busy = any((self._install_state(t) or {}).get('state') == 'downloading'
                               for t in missing)
                    title += '  (downloading…)' if busy else '  (not downloaded)'
                modes.append((key == tuple(current), title, mode))

            def name(tag):
                return locales.short_name(tag, status)

            if len(langs) >= 2:
                add(langs, locales.label(langs, status))
                for tag in langs:
                    if locales.group_of(tag, status) == locales.INDIA_GROUP:
                        add([tag], '%s (with English mixed in) only' % name(tag))
                    else:
                        add([tag], '%s only' % name(tag))
            else:
                add(langs, name(langs[0]))
                hindi = (locales.suggest_addition(langs, status)
                         if status else None)
                if hindi:
                    add([langs[0], hindi], locales.label([langs[0], hindi], status))
            if status:
                for row in locales.choices(status, langs):
                    if row['installed'] and not any(
                            locales.same_model(row['code'], t, status) for t in langs):
                        add([row['code']], row['name'])
            for on, title, mode in modes:
                menu.add(rumps.MenuItem(
                    ('✓  ' if on else '    ') + title,
                    callback=lambda sender, m=list(mode): self._set_languages(m)))
            menu.add(None)
            menu.add(rumps.MenuItem('More Languages…',
                                    callback=self._open_language_settings))
        except Exception:
            log.exception('Could not rebuild the Language menu')

    def _set_language(self, code):
        """One language as the main one (menu / older pages), keeping the
        additional ones that are a different model."""
        code = locales.normalize(code)
        if not locales.is_concrete(code):
            return
        rest = [t for t in self._languages[1:]
                if not locales.same_model(t, code, self._speech_status)]
        self._set_languages([code] + rest)

    def _set_languages_from_ui(self, body):
        try:
            primary = str(body.get('primary') or '')
            additional = body.get('additional') or []
            if not isinstance(additional, (list, tuple)):
                additional = [additional]
            additional = [str(a) for a in additional if a]
        except Exception:
            return
        if not primary:
            primary = self._locale
        self._set_languages([primary] + additional)

    def _set_languages(self, wanted):
        """Choose the languages to dictate in (primary first). Persists them,
        starts the download of any that is missing - one click is choose +
        download + use - and loads what is ready. Any thread."""
        status = self._speech_status
        wanted = [locales.normalize(t) for t in wanted or () if t]
        wanted = [t for t in wanted if locales.is_concrete(t)]
        if not wanted:
            return
        if status and not locales.is_supported(wanted[0], status):
            self._set_status('That language is not available')
            self._send_languages()
            return
        langs, known = locales.resolve_languages(wanted, wanted[0], status)
        if not langs:
            return
        langs = [self._canonical(t) for t in langs]
        if (status and not locales.is_installed(langs[0], status)
                and locales.normalize(langs[0]).lower()
                != locales.normalize(self._languages[0]).lower()
                and locales.usable(self._languages, status)[0]):
            # A main language that still needs its download (hundreds of
            # MB) must not stop dictation until it lands: keep the languages
            # that work, download, and switch in _sp_installed. A failed
            # download simply leaves the old choice in place.
            with self._lock:
                self._pending_languages = list(langs)
            log.info('Main language %s needs a download; keeping %s until it is here',
                     langs[0], ' + '.join(self._languages))
            self._clear_install_retry(langs[0])
            self._start_install(langs[0])
            self._set_idle_status('%s is downloading' % locales.short_name(langs[0], status))
            self._lang_dirty = True
            self._send_languages()
            self._send_home()
            return
        with self._lock:
            self._pending_languages = None
        settings = cfg.load()
        if status and self._removed_hindi(langs, status):
            # They had Hindi and took it off: stop suggesting it.
            settings['hindi_hint_dismissed'] = True
        settings['languages'] = list(langs)
        settings['language'] = langs[0]
        settings.pop('language_notice', None)    # an explicit choice settles it
        cfg.save(settings)
        self._languages = langs
        self._locale = langs[0]
        log.info('Dictation languages: %s', ' + '.join(langs))
        if status:
            for code in locales.usable(langs, status)[1]:
                self._clear_install_retry(code)
                self._start_install(code)
        self._set_idle_status(self._language_status())
        self._apply_prewarm()
        self._lang_dirty = True
        self._send_languages()
        self._send_home()

    def _set_idle_status(self, text):
        """The menu status line, unless a dictation owns it right now."""
        if not text:
            return
        with self._lock:
            if self._rec_sid is None and not self._dicts:
                self._last_status = text

    def _language_status(self):
        """'Language: English (India) + Hindi', or what is still missing."""
        status = self._speech_status
        if not status:
            return 'Language: %s' % locales.label(self._languages, status)
        ready, missing = locales.usable(self._languages, status)
        if not missing:
            return 'Language: %s' % locales.label(self._languages, status)
        downloading = [m for m in missing
                       if (self._install_state(m) or {}).get('state') == 'downloading']
        names = ' and '.join(locales.short_name(t, status) for t in missing)
        if downloading:
            return '%s is downloading' % names
        return '%s is not downloaded yet' % names

    def _removed_hindi(self, langs, status):
        """True when ``langs`` would get the "Also speak Hindi?" suggestion
        although the languages in use now include Hindi's model: the user
        just took Hindi off."""
        hindi = locales.suggest_addition(langs, status)
        return bool(hindi) and any(
            locales.same_model(hindi, t, status) for t in self._languages)

    def _clear_install_retry(self, code):
        with self._lock:
            self._install_retry.pop(locales.normalize(code).lower(), None)

    # Back-off before a chosen language's failed download is tried again by
    # itself: a network problem doubles from a minute up to half an hour; an
    # interruption (the helper was restarted, the Mac slept) is resumed at
    # the next chance.
    _RETRY_FIRST_SEC = 60.0
    _RETRY_MAX_SEC = 1800.0
    _RETRY_INTERRUPTED_SEC = 5.0
    _RETRY_GUARD_SEC = 30.0     # a start just sent shows up as 'downloading' late

    def _resume_downloads(self):
        """Start the download of every chosen language that is missing and
        not downloading (a download cut off by quitting, sleep, a helper
        restart or the network), respecting the back-off after a failure.
        Any thread; cheap; a no-op once everything is here."""
        status = self._speech_status
        if status is None or self._selftest or not self._engine.is_available:
            return
        now = time.monotonic()
        for code in locales.usable(self._languages, status)[1]:
            if not locales.is_supported(code, status):
                continue
            key = locales.normalize(code).lower()
            if (self._install_state(code) or {}).get('state') == 'downloading':
                continue
            with self._lock:
                entry = self._install_retry.get(key)
                if entry and now < entry[0]:
                    continue
                delay = entry[1] if entry else 0.0
                self._install_retry[key] = (now + self._RETRY_GUARD_SEC, delay)
            log.info('Resuming the download of %s (chosen, not here yet)', code)
            self._start_install(code)

    def _languages_payload(self):
        """The 'languages' event for the settings window (and Home)."""
        status = self._speech_status
        error = (None if self._engine.is_available
                 else speech.user_message(self._engine.unavailable[0]))
        downloading = self._downloading()
        down_keys = dict((locales.normalize(k).lower(), v)
                         for k, v in downloading.items())
        rows, failed = [], {}
        if status:
            for r in locales.choices(status, self._languages):
                frac = down_keys.get(locales.normalize(r['code']).lower())
                state = ('downloaded' if r['installed'] else
                         'downloading' if frac is not None else 'available')
                rows.append({'id': r['code'], 'name': r['name'], 'state': state,
                             'progress': 1.0 if r['installed'] else (frac or 0.0)})
            try:
                for code, v in self._engine.installs().items():
                    if v.get('state') == 'failed':
                        failed[code] = speech.user_message(v.get('code') or 'install_failed')
            except Exception:
                pass
        multi = locales.max_languages(status) >= 2
        hint = None
        hindi = locales.suggest_addition(self._languages, status) if status else None
        if hindi and not cfg.load().get('hindi_hint_dismissed'):
            text = 'Hindi and English can be mixed; everything is written in English letters.'
            size = locales.download_note([hindi], status)
            if size:
                text += ' ' + size
            hint = {'id': hindi, 'title': 'Also speak Hindi?', 'text': text,
                    'button': 'Add Hindi', 'role': 'additional'}
        # One plain sentence at most (Settings only; Home shows none).
        notes = []
        for tag in self._languages:
            if locales.group_of(tag, status) == locales.INDIA_GROUP:
                if locales.normalize(tag) == locales.HINDI:
                    notes.append('Hindi and English can be mixed; everything is '
                                 'written in English letters.')
                else:
                    notes.append('English can be mixed in; everything is written '
                                 'in English letters.')
                size = locales.download_note([tag], status)
                if size:
                    notes.append(size)
                break
        with self._lock:
            pending = list(self._pending_languages or ())
        return {
            'pending': pending[0] if pending else None,
            'ready': status is not None,
            'error': error,
            'primary': self._languages[0],
            'additional': list(self._languages[1:]),
            'max_additional': 1 if multi else 0,
            'languages': rows,
            'installing': downloading,
            'failed': failed,
            'hint': hint,
            'note': ' '.join(notes),
        }

    def _send_languages(self):
        if self._ui is None:
            return
        try:
            payload = self._languages_payload()
        except Exception:
            log.exception('Could not build the language list')
            return
        self._push_ui('languages', payload)

    def _language_progress(self, code, state, progress=0.0, message=None):
        payload = {'id': code,
                   'name': locales.short_name(code, self._speech_status),
                   'state': state, 'progress': float(progress or 0.0)}
        if message:
            payload['message'] = message
        self._push_ui('language_progress', payload)

    def _start_install(self, code, role=None):
        """Download a language's speech model (no-op if one is running)."""
        code = self._canonical(code)
        if role:
            with self._lock:
                self._install_roles[locales.normalize(code).lower()] = role
        current = self._install_state(code)
        if current and current.get('state') == 'downloading':
            return
        log.info('Downloading the speech model for %s', code)
        self._engine.install(code)
        self._language_progress(code, 'downloading', 0.0)

    def _install_language(self, body):
        """Settings window: download a language ({id|code, role?}).
        role 'primary' / 'additional' says what to do with it once it is
        there; a language already in use just gets its download. Progress
        and the result arrive through the engine callbacks."""
        try:
            code = locales.normalize(str(body.get('id') or body.get('code') or ''))
            role = body.get('role')
        except Exception:
            return
        status = self._speech_status
        if not locales.is_concrete(code) or (status and not locales.is_supported(code, status)):
            self._language_progress(code, 'failed', 0.0, 'That language is not available.')
            return
        code = self._canonical(code)
        in_use = any(locales.normalize(t).lower() == code.lower() for t in self._languages)
        if role not in ('primary', 'additional'):
            # An older page: "download this one" meant "switch to it".
            role = None if in_use else 'primary'
        if status and locales.is_installed(code, status):
            if role:
                self._apply_install_role(code, role)
            self._language_progress(code, 'downloaded', 1.0)
            return
        self._clear_install_retry(code)      # asked for by hand: no back-off
        self._start_install(code, role)
        self._send_languages()
        self._send_home()

    def _apply_install_role(self, code, role):
        """What a finished (or already present) download was asked for."""
        if role == 'primary':
            self._set_language(code)
        elif role == 'additional':
            if not any(locales.same_model(t, code, self._speech_status)
                       for t in self._languages):
                self._set_languages(self._languages[:1] + [code])

    def _sp_install_progress(self, locale, fraction):
        self._language_progress(self._canonical(locale), 'downloading', fraction)

    def _sp_installed(self, locale):
        """A download finished (the engine already sent a status listing it).
        A language downloaded as an addition never changes the main one."""
        code = self._canonical(locale)
        status = self._speech_status
        with self._lock:
            role = self._install_roles.pop(code.lower(), None)
            self._install_retry.pop(locales.normalize(code).lower(), None)
            pending = self._pending_languages
            if pending and not (status and locales.is_installed(pending[0], status)):
                pending = None          # not the one being waited for
            if pending:
                self._pending_languages = None
        state = self._install_state(locale) or {}
        log.info('Speech model for %s is installed%s', code,
                 ' (it already was)' if state.get('already') else '')
        self._language_progress(code, 'downloaded', 1.0)
        wanted = bool(pending) or bool(role) or any(
            locales.same_model(t, code, status) for t in self._languages)
        if pending:
            self._set_languages(pending)         # the main language they chose
        elif role:
            self._apply_install_role(code, role)
        elif any(locales.same_model(t, code, status) for t in self._languages):
            self._apply_prewarm()
        if not state.get('already') and wanted:
            self._queue_notice('%s is ready' % locales.short_name(code, status),
                               'success', 3.0)
        self._set_idle_status(self._language_status())
        self._lang_dirty = True
        self._send_languages()
        self._send_home()

    _INSTALL_CODES = ('network', 'no_space', 'install_failed', 'cancelled')

    def _sp_install_failed(self, locale, code, message):
        tag = self._canonical(locale)
        status = self._speech_status
        key = locales.normalize(tag).lower()
        name = locales.short_name(tag, status)
        with self._lock:
            self._install_roles.pop(tag.lower(), None)
            pending = self._pending_languages
            was_pending = bool(pending) and locales.normalize(pending[0]).lower() == key
            if was_pending:
                self._pending_languages = None   # the old languages stay on
            chosen = was_pending or any(
                locales.normalize(t).lower() == key for t in self._languages)
            # Back-off before _resume_downloads tries again by itself.
            entry = self._install_retry.get(key)
            first = entry is None or entry[1] == 0.0
            if code in self._INSTALL_CODES:
                delay = min(self._RETRY_MAX_SEC, max(self._RETRY_FIRST_SEC,
                                                     (entry[1] if entry else 0.0) * 2))
            else:                               # interrupted, not refused
                delay = self._RETRY_INTERRUPTED_SEC
            self._install_retry[key] = (time.monotonic() + delay, delay)
        log.warning('Speech model download failed for %s (%s): %s', tag, code, message)
        if code in self._INSTALL_CODES:
            text = speech.user_message(code)
        else:
            text = ('Couldn’t download %s. Check your internet connection '
                    'and try again.' % name)
        self._language_progress(tag, 'failed', 0.0, text)
        if chosen and code in self._INSTALL_CODES and first:
            # Said once (not at every automatic retry), where it is seen:
            # the pill, the menu status line and Home.
            if was_pending:
                note = '%s couldn’t download. Still using %s.' % (
                    name, locales.label(self._languages, status))
            else:
                note = '%s couldn’t download. Check your internet connection.' % name
            self._queue_notice(note, 'error', 4.0)
            self._set_idle_status(note)
        else:
            self._set_idle_status(self._language_status())
        self._lang_dirty = True
        self._send_languages()
        self._send_home()

    def _sp_status(self, status):
        """The helper reported permissions + languages (at every helper start,
        whenever asked, and just before an install finishes). Resolves - and
        on first run migrates - the language settings and loads the models.
        Idempotent. speech-cb thread."""
        first = self._speech_status is None
        self._speech_status = status
        settings = cfg.load()
        stored = settings.get('language')
        stored_list = settings.get('languages')
        langs, known = locales.resolve_languages(stored_list, stored, status)
        changed = added = None
        if known and langs:
            if not (isinstance(stored_list, list) and stored_list):
                # Never chosen (a config from before two languages existed,
                # or a fresh install): an English speaker in India whose Mac
                # has the India model gets Hindi too, with no click.
                defaults = locales.default_languages(langs[0], status)
                if len(defaults) > len(langs):
                    added = defaults[1]
                    langs = defaults
            langs = [self._canonical(t) for t in langs]
            old = [locales.normalize(t) for t in stored_list] if isinstance(
                stored_list, list) else None
            # A list cut short only because this helper takes fewer languages
            # is not written back: the choice survives for a helper that can.
            if old is None or old[:len(langs)] != langs or stored != langs[0]:
                log.info('Dictation languages %r / %r resolved to %s',
                         stored_list, stored, ' + '.join(langs))
                settings['languages'] = list(langs)
                settings['language'] = langs[0]
                # A fresh install also starts on 'auto' ("not chosen yet");
                # only someone who set the app up on the old build can have
                # CHOSEN Auto-detect, and must be told that it is gone rather
                # than quietly getting English for their Hindi.
                if settings.get('onboarded') and stored != langs[0]:
                    changed = self._language_change(stored, langs[0])
                if changed:
                    settings['language_notice'] = {'was': changed, 'now': langs[0]}
                note = settings.get('language_notice')
                if (added and isinstance(note, dict) and note.get('was') == 'auto'):
                    # Hindi + English now follows mixed speech, which is
                    # what Auto-detect was used for: nothing left to say.
                    settings.pop('language_notice', None)
                    changed = None
                cfg.save(settings)
            self._languages = langs
            self._locale = langs[0]
            self._apply_prewarm()
        mic = status.get('mic')
        if mic == 'denied':
            with self._lock:
                if self._rec_sid is None and not self._dicts:
                    self._last_status = speech.user_message('mic_denied')
        if first:
            log.info('Speech helper status: mic=%s speech=%s input=%s '
                     'languages=%s (%d supported, %d installed, up to %d at once)',
                     status.get('mic'), status.get('speech'),
                     'present' if status.get('input_device') else 'none',
                     ' + '.join(self._languages), len(status.get('supported') or ()),
                     len(status.get('installed') or ()), locales.max_languages(status))
        self._setup_kick = True        # the microphone state may have changed
        self._lang_dirty = True
        # A chosen language that is still missing (its download was cut
        # off by quitting, sleep or a helper restart, or failed) is fetched
        # again by itself - otherwise the app says "English + Hindi" while
        # it quietly listens for English alone.
        try:
            self._resume_downloads()
        except Exception:
            log.exception('Could not resume the language downloads')
        self._send_languages()
        if added and not self._selftest:
            log.info('Added %s to the dictation languages (India model present)', added)
            self._queue_notice('Hindi + English is on', 'success', 3.5)
        if changed and not self._selftest:
            log.warning('Dictation language changed by the update: %s -> %s',
                        changed, langs[0])
            self._lang_notice_dirty = True      # system notification (main thread)
            self._queue_notice(
                ('Auto-detect is gone — now %s' if changed == 'auto'
                 else 'Language changed — now %s') % locales.label(langs, status),
                'info', 5.0)

    def _sp_state(self, state, detail):
        """Engine state changes (speech-cb thread)."""
        if state == speech.STATE_UNAVAILABLE:
            code = (self._engine.unavailable or ('', ''))[0]
            log.error('Speech engine unavailable (%s): %s', code, detail)
            self._set_status(speech.user_message(code))
            self._setup_kick = True
        elif state == speech.STATE_RESTARTING:
            # Silent by design: the helper is replaced in the background and
            # the next dictation just works. (The engine prewarms the same
            # languages on the new helper by itself.) A microphone prompt
            # the old helper had up is gone, so setup may ask again.
            self._prewarmed_for = None
            self._helper_replaced = True
            self._setup_kick = True
            log.warning('Speech helper is being restarted: %s', detail)

    # ------------------------------------------------------------------
    # Recording — shared by both hotkeys and by the window/onboarding mic
    # tests. _begin_recording / _end_recording run on the caller's thread: the
    # event-tap thread for hotkeys, the MAIN thread for the mic tests. They
    # only do bookkeeping under _lock and call into SpeechEngine / the pill,
    # whose methods queue work and return immediately. Nothing here may block:
    # a stalled tap callback kills the hotkey outright, and a stalled main
    # thread freezes the entire app.
    # ------------------------------------------------------------------

    def _begin_recording(self, mode):
        """Start a dictation. Returns True if recording started."""
        unavailable = self._engine.unavailable
        if unavailable and unavailable[0] == speech.E_OS_TOO_OLD:
            # Permanent. (A missing helper is not: the engine re-checks it on
            # every start and reports through on_error, so that case falls
            # through and heals by itself if the file reappears.)
            message = speech.user_message(unavailable[0])
            self._set_status(message)
            self._hud.show_notice(message, kind='error', seconds=3.0)
            return False
        sink = mode
        if mode in _HOTKEY_MODES:
            # A mic test left running in a window must not make the hotkeys
            # dead: the dictation takes the microphone over.
            self._abort_test_recording()
            if self._setup_in_front():
                # "Hold the key and speak", tried right in the setup window:
                # the words are shown on its Try it line. Pasting would send
                # them into that window, where nothing can take them.
                sink = 'ob_test'
        with self._lock:
            if self._rec_sid is not None:
                return False
            if len(self._dicts) >= _MAX_IN_FLIGHT:
                self._last_status = 'Busy — finishing the last one…'
                return False
            # The languages whose model is here; one still downloading is
            # left out until it lands (before the helper's first status,
            # the stored choice is passed on for the helper to resolve).
            status = self._speech_status
            ready = (locales.usable(self._languages, status)[0] if status
                     else list(self._languages))
            locale = ready[0] if ready else self._locale
            try:
                context = self._vocab.context()
            except Exception:
                context = None
            # The recogniser currently ignores the context hints (the words
            # are applied to the transcript instead), but they cost nothing
            # and a later macOS may start honouring them.
            sid = self._engine.start(locale, context=context,
                                     partials=self._live_text,
                                     languages=ready if len(ready) > 1 else None)
            if sid is None:
                return False            # engine already shut down (quitting)
            self._dicts[sid] = _Dictation(sid, mode, locale, sink)
            self._rec_sid = sid
            self._last_status = 'Listening…'
            self._toggle_guard_until = 0.0
        if mode == 'ptt':
            # The pill waits a beat (see _PTT_PILL_DELAY_SEC); the mic does not.
            t = threading.Timer(_PTT_PILL_DELAY_SEC, self._show_recording_pill,
                                args=(sid,))
            t.daemon = True
            t.start()
        else:
            self._show_recording_pill(sid)
        return True

    def _show_recording_pill(self, sid, started=False):
        """Put the recording pill up for dictation ``sid`` - once BOTH its
        start delay has passed (this is called without ``started``) and the
        helper has said the microphone is live (called with ``started``).
        The pill (and the start cue) must never claim freeflo is listening
        while nothing is being captured yet: a helper still coming up, the
        system permission prompt, a Bluetooth input that takes a second."""
        with self._lock:
            d = self._dicts.get(sid)
            if d is None or self._rec_sid != sid:
                return      # already released or cancelled
            if started:
                d.mic_live = True
            else:
                d.pill_due = True
            if d.pill_shown or not (d.mic_live and d.pill_due):
                return
            d.pill_shown = True
            if d.sink == 'ob_test' and d.mode in _HOTKEY_MODES:
                # Queued under the lock (it never blocks), so it cannot
                # overtake the result sent when the key is let go.
                self._push_ob('ob_test_state', {'recording': True, 'via': 'hotkey'})
        self._hud.show_recording()
        sounds.play_start()

    def _end_recording(self, mode, transcribe):
        """Stop recording iff the active session belongs to `mode`."""
        now = time.monotonic()
        with self._lock:
            sid = self._rec_sid
            d = self._dicts.get(sid) if sid is not None else None
            if d is None or d.mode != mode:
                return
            self._rec_sid = None
            held = now - d.t_start
            if transcribe and mode == 'ptt' and held < _PTT_MIN_HOLD_SEC:
                transcribe = False      # a stray tap, not a dictation
            if transcribe:
                d.phase = _Dictation.FINALIZING
                d.t_stop = now
                d.deadline = (now + self._engine.stop_timeout_for(held)
                              + _FINALIZE_GRACE_SEC)
                self._last_status = 'Transcribing…'
            else:
                del self._dicts[sid]
                self._last_status = 'Ready' if self._enabled else 'Dictation disabled'
            others = bool(self._dicts) and not transcribe
            if d.sink == 'ob_test' and mode in _HOTKEY_MODES and d.pill_shown:
                # The setup page was told "listening": move it on.
                if transcribe:
                    self._push_ob('ob_test_state', {'recording': False})
                else:
                    self._push_ob('ob_test_result',
                                  {'text': '', 'note': _TRY_HOLD_NOTE})

        if transcribe:
            self._engine.stop(sid)
            self._hud.show_processing()
            sounds.play_stop()
        else:
            self._engine.cancel(sid)
            if others:
                self._hud.show_processing()   # an earlier one is still coming
            else:
                self._hud.hide()

    def _abort_recording(self):
        """Cancel whatever is recording, whatever started it (Esc, Disable,
        quit). Returns True if there was something to cancel."""
        with self._lock:
            sid = self._rec_sid
            if sid is None:
                return False
            self._rec_sid = None
            d = self._dicts.pop(sid, None)
            others = bool(self._dicts)
            self._last_status = 'Cancelled'
        self._engine.cancel(sid)
        if others:
            self._hud.show_processing()
        else:
            self._hud.hide()
        if d is not None:
            self._report_test_result(d.sink, '', 'Cancelled')
        return True

    def _abort_test_recording(self, mode=None):
        """Cancel a window mic test that is still recording (``mode``: only
        that window's; None: either). Used when its window closes and when a
        hotkey wants the microphone - otherwise a forgotten test keeps the
        mic open and makes both hotkeys do nothing until the session cap."""
        with self._lock:
            d = self._dicts.get(self._rec_sid) if self._rec_sid is not None else None
            if d is None or d.mode not in ('test', 'ob_test'):
                return False
            if mode is not None and d.mode != mode:
                return False
        log.info('Mic test (%s) abandoned - cancelling it', d.mode)
        return self._abort_recording()

    def _report_test_result(self, sink, text, note=None):
        """A window waits on the result of a dictation whose sink is a test
        (_Dictation.sink), so every outcome has to reach it. A no-op for an
        ordinary dictation."""
        payload = {'text': text or ''}
        if note:
            payload['note'] = note
        if sink == 'test':
            self._push_ui('test_result', payload)
        elif sink == 'ob_test':
            self._push_ob('ob_test_result', payload)
        elif sink == 'selftest':
            self._selftest_result = payload
            self._selftest_done_at = time.monotonic()
            self._selftest_event.set()

    # ------------------------------------------------------------------
    # Hotkey callbacks — called from the event-tap thread
    # ------------------------------------------------------------------

    def _on_ptt_start(self):
        """Push-to-talk key pressed."""
        if not self._enabled:
            return
        self._begin_recording('ptt')

    def _on_ptt_stop(self):
        """Push-to-talk key released — stop and transcribe."""
        self._end_recording('ptt', transcribe=True)

    def _on_ptt_cancel(self):
        """The key was used as a shortcut modifier (another key or a click
        right after it went down) — discard the recording."""
        log.debug('Push-to-talk key used as a modifier - recording dropped')
        self._end_recording('ptt', transcribe=False)

    def _on_ptt_interrupt(self):
        """A click or another key landed well into a push-to-talk hold. The
        dictation carries on (the hotkey listener only cancels in the first
        moments of a hold); remember it, so that if nothing was said after
        all - a slow Option-click - the empty result is dropped quietly."""
        with self._lock:
            d = self._dicts.get(self._rec_sid) if self._rec_sid is not None else None
            if d is not None and d.mode == 'ptt':
                d.interrupted = True

    def _on_toggle(self):
        """Toggle key tapped — start, or stop if a toggle session is active."""
        if not self._enabled:
            return
        with self._lock:
            d = self._dicts.get(self._rec_sid) if self._rec_sid is not None else None
            mode = d.mode if d is not None else None
            guarded = (mode is None
                       and time.monotonic() < self._toggle_guard_until)
            self._toggle_guard_until = 0.0
        if guarded:
            # The last hands-free recording ended by itself (length cap, mic
            # lost). This tap was meant to stop it: do not open the mic again
            # behind the user's back.
            log.info('Toggle tap swallowed: the recording had already ended')
            self._hud.show_notice(
                'Dictation had already stopped — tap again to start',
                kind='info', seconds=3.5)
            return
        if mode == 'toggle':
            self._end_recording('toggle', transcribe=True)
        else:
            # Idle, or a window mic test (which the dictation takes over).
            # A push-to-talk recording keeps the mic: nothing happens.
            self._begin_recording('toggle')

    def _on_escape(self):
        """Esc while recording throws the recording away. (The key still
        reaches the frontmost app; freeflo swallows nothing.)"""
        if self._rec_sid is None:       # cheap exit: Esc is pressed a lot
            return
        if self._abort_recording():
            log.info('Recording cancelled with Esc')

    # ------------------------------------------------------------------
    # Speech engine callbacks — speech-cb thread. Keep them short: anything
    # slow (refine, paste, disk) is handed to the deliver thread.
    # ------------------------------------------------------------------

    def _sp_started(self, sid):
        """The microphone is live for ``sid``: now the pill may say so."""
        self._show_recording_pill(sid, started=True)

    def _sp_started_info(self, sid, info):
        """Just before on_started, when two languages are in use or one
        that was asked for was left out. A left-out language that is not on
        this Mac yet is downloaded now, without a click."""
        info = info or {}
        missing = [m for m in info.get('missing') or () if isinstance(m, str)]
        if info.get('languages') or missing:
            log.info('Dictation %s: languages=%s missing=%s', sid,
                     '+'.join(info.get('languages') or ()) or info.get('locale'),
                     ','.join(missing) or '-')
        status = self._speech_status
        for code in missing:
            if status is None or not locales.is_supported(code, status):
                continue
            if locales.is_installed(code, status):
                continue    # the helper fell back to one language; nothing to fetch
            state = self._install_state(code) or {}
            if state.get('state') == 'downloading':
                continue
            self._queue_notice('%s is not ready yet - downloading it'
                               % locales.short_name(code, status), 'info', 3.0)
            self._start_install(code)

    def _sp_level(self, sid, rms):
        if sid == self._rec_sid:
            self._hud.set_level(rms)

    def _sp_partial(self, sid, text):
        if self._live_text and sid == self._rec_sid:
            self._hud.set_partial(text)

    def _sp_final_info(self, sid, info):
        info = dict(info or {})
        if info.get('languages'):
            # Two-language dictation: which ones the text came from. No text.
            log.info('Dictation %s: languages=%s used=%s dominant=%s', sid,
                     '+'.join(info.get('languages') or ()),
                     '+'.join(info.get('used') or ()) or '-',
                     info.get('dominant') or '-')
        with self._lock:
            d = self._dicts.get(sid)
            if d is not None:
                d.info = info

    def _sp_final(self, sid, text, audio_sec, elapsed_ms):
        now = time.monotonic()
        with self._lock:
            d = self._dicts.get(sid)
            if d is None:
                return          # cancelled or abandoned meanwhile
            # The helper can end a session by itself (the mic disappeared, or
            # the session hit its length cap) while the key is still down.
            unsolicited = self._rec_sid == sid
            if unsolicited:
                self._rec_sid = None
                d.t_stop = now
                if d.mode == 'toggle':
                    self._toggle_guard_until = now + _TOGGLE_GUARD_SEC
            d.phase = _Dictation.DELIVERING
            d.deadline = now + _DELIVERY_GRACE_SEC
        if unsolicited:
            self._hud.show_processing()
        self._deliver_q.put((d, text, audio_sec, elapsed_ms))

    def _sp_cancelled(self, sid):
        # Our own cancels remove the dictation before asking, so getting here
        # means the engine dropped it by itself.
        with self._lock:
            d = self._dicts.pop(sid, None)
            if d is None:
                return
            if self._rec_sid == sid:
                self._rec_sid = None
            idle = self._rec_sid is None and not self._dicts
            self._last_status = 'Cancelled'
        if idle:
            self._hud.hide()
        self._report_test_result(d.sink, '', 'Cancelled')

    def _sp_error(self, sid, code, message):
        if sid is None:
            # Not tied to a dictation (a prewarm that could not load, ...).
            # The engine already logged it; there is nothing to show.
            return
        with self._lock:
            d = self._dicts.pop(sid, None)
            if d is None:
                return
            if self._rec_sid == sid:
                self._rec_sid = None
            recording = self._rec_sid is not None
        note = speech.user_message(code)
        if code == 'locale_not_installed':
            # Fetch it now: the next dictation works once it has landed.
            note = '%s is downloading — try again in a moment' % (
                locales.short_name(d.locale, self._speech_status))
            self._start_install(d.locale)
        elif code == 'locale_unsupported':
            # The stored language vanished (an OS update): fall back next time.
            self._prewarmed_for = None
            self._engine.request_status()
        log.warning('Dictation %s failed: %s', sid, code)
        self._set_status(note)
        self._report_test_result(d.sink, '', note)
        if not recording:
            self._hud.show_notice(note, kind='error', seconds=3.5)
            sounds.play_error()
        if code == 'mic_denied':
            # Refresh what the helper knows; the setup flow shows it.
            self._engine.request_status()
            self._setup_kick = True

    # ------------------------------------------------------------------
    # Delivery — the 'deliver' thread: one transcript at a time, in order
    # ------------------------------------------------------------------

    def _deliver_worker(self):
        while True:
            job = self._deliver_q.get()
            if job is None:
                return
            d = job[0]
            notice = None
            try:
                notice = self._deliver(*job)
            except Exception:
                log.exception('Delivering a dictation failed (mode=%s)', d.mode)
                notice = ('Something went wrong — try again', 'error', 3.0)
                self._set_status(notice[0])
                self._report_test_result(d.sink, '', notice[0])
            finally:
                with self._lock:
                    self._dicts.pop(d.sid, None)
                    recording = self._rec_sid is not None
                    pending = bool(self._dicts)
                # The pill belongs to a live recording first, then to work
                # still in flight; only otherwise does this result get it.
                if not recording:
                    if notice:
                        self._hud.show_notice(notice[0], kind=notice[1],
                                              seconds=notice[2])
                    elif not pending:
                        self._hud.hide()

    def _owns(self, d):
        """False once the watchdog (or a disable/quit) gave up on `d`."""
        with self._lock:
            return self._dicts.get(d.sid) is d

    def _cap_note(self, inserted):
        """Pill text for a recording that ran into the session length cap."""
        cap = self._engine.max_session_sec or 0
        limit = ('%d-minute' % round(cap / 60.0)) if cap >= 60 else 'time'
        return ('Reached the %s limit — text inserted' % limit if inserted
                else 'Stopped at the %s limit' % limit)

    def _empty_note(self, info):
        if info.get('mic_lost'):
            return 'Microphone disconnected'
        if info.get('capped'):
            return self._cap_note(False)
        if info.get('silent'):
            # Pure digital silence: muted input, or a MacBook with the lid
            # closed and no other microphone.
            return 'No sound from the microphone — is it muted?'
        return 'Didn’t catch that — try again'

    def _deliver(self, d, text, audio_sec, elapsed_ms):
        """Turn a finished transcript into text at the cursor. Returns an
        optional (message, kind, seconds) notice for the pill.

        A test (and a hotkey dictation tried out in the setup window) is
        shown in its window instead of being pasted. What is pasted, and
        a Home "Try it", is logged to history."""
        mode = d.mode
        sink = d.sink
        if sink != mode and not self._setup.window_open:
            sink = mode     # the setup window was closed meanwhile: paste it
        duration = audio_sec if audio_sec and audio_sec > 0 else max(
            0.0, (d.t_stop or time.monotonic()) - d.t_start)
        info = d.info or {}
        # Two languages: credit the one most of the text came from.
        language = info.get('dominant') or d.locale
        text = (text or '').strip()
        if text:
            text = (self._vocab.apply(text) or '').strip()
        flags = [k for k in ('silent', 'mic_lost', 'capped') if info.get(k)]
        if sink != mode:
            flags.append('shown-in-setup')
        log.info('Dictation %s: mode=%s locale=%s audio=%.1fs finalize=%.0fms chars=%d%s',
                 d.sid, mode, language, duration, elapsed_ms or 0.0, len(text),
                 ''.join(' %s' % k for k in flags))

        if sink in _TEST_MODES:
            note = None if text else self._empty_note(info)
            self._report_test_result(sink, text, note)
            if text and sink == 'test':
                self._log_history(text, mode, duration, language)
            self._set_status('Ready' if self._enabled else 'Dictation disabled')
            return None

        if not text:
            flagged = (info.get('silent') or info.get('mic_lost')
                       or info.get('capped'))
            if d.interrupted and not flagged:
                # The key was being used as a (slow) modifier after all:
                # nothing was said, so there is nothing to apologise for.
                self._set_status('Ready' if self._enabled else 'Dictation disabled')
                return None
            note = self._empty_note(info)
            self._set_status(note)
            if info.get('capped'):
                sounds.play_error()
            return (note, 'error' if flagged else 'info', 2.2)

        settings = cfg.load()
        if settings.get('turbo_enabled') and refiner.is_ready():
            text = refiner.refine(text, settings.get('turbo_style', 'natural'))

        notice = None
        if not self._enabled:
            # Switched off while this one was in flight: keep it, don't type it.
            self._set_status('Dictation disabled')
        elif not self._owns(d):
            # The stall watchdog already gave up on this one and the user has
            # moved on. Pasting now could land in a different app entirely, so
            # don't — the text is still in history.
            log.warning('Dropping a late transcript (dictation %s abandoned)', d.sid)
            self._set_status('Timed out — saved to history')
        else:
            app = injector.frontmost_app_id()
            out = injector.smart_spacing(text, self._continuity.previous(app))
            res = injector.inject(out)
            self._last_text = text
            if res.pasted:
                self._continuity.note(out, app)
                self._set_status(f'"{text[:40]}{"…" if len(text) > 40 else ""}"')
                if info.get('mic_lost'):
                    notice = ('Microphone disconnected', 'error', 3.0)
                elif info.get('capped'):
                    # The user is very likely still talking: say clearly that
                    # the recording is over, instead of pasting mid-sentence
                    # and going quiet.
                    notice = (self._cap_note(True), 'error', 5.0)
                    sounds.play_error()
                telemetry.dictation_completed(mode, language, duration, len(text))
            elif res.copied_only:
                log.warning('Paste fell back to the clipboard (%s)', res.reason)
                self._set_status('Copied — press ⌘V to paste')
                notice = ('Copied — press ⌘V to paste', 'info', 2.8)
            else:
                log.error('Could not paste or copy the transcript (%s)', res.reason)
                self._set_status('Couldn’t paste — saved to History')
                notice = ('Couldn’t paste — saved to History', 'error', 3.0)
        self._log_history(text, mode, duration, language)
        return notice

    def _log_history(self, text, mode, duration, locale=None):
        """Record a transcription to the local DB, honoring the save toggle.
        Guarded so a DB problem can never break dictation."""
        if not text:
            return
        try:
            settings = cfg.load()
            if not settings.get('save_history', True):
                return
            history.add(text, language=locale or settings.get('language'),
                        mode=mode, duration=duration)
        except Exception:
            log.exception('Failed to log transcription to history')
        self._write_snapshot_async()   # local safety net, every utterance
        self._maybe_sync_backup()      # cloud mirror if connected
        if self._ui is not None:
            self._send_home()          # an open window shows it at once

    # ------------------------------------------------------------------
    # Self-test (FREEFLO_SELFTEST=<result file>) — one scripted dictation
    # through the real engine, helper and delivery path, then quit. Used by
    # the build smoke test, usually with the helper's FREEFLO_FAKE_MIC=<wav>
    # so no microphone or permission is involved. Nothing is pasted.
    # ------------------------------------------------------------------

    def _selftest_worker(self):
        result = {'ok': False}
        try:
            hold = float(os.environ.get('FREEFLO_SELFTEST_HOLD') or 3.0)
            deadline = time.monotonic() + 15.0
            while self._speech_status is None and time.monotonic() < deadline:
                if not self._engine.is_available:
                    break
                time.sleep(0.05)
            status = self._speech_status or {}
            result.update({
                'engine_state': self._engine.state,
                'helper': cfg.get_helper_path(),
                'helper_version': self._engine.helper_version,
                'locale': self._locale,
                'mic': status.get('mic'),
                'supported': len(status.get('supported') or ()),
                'installed': len(status.get('installed') or ()),
            })
            if self._speech_status is None:
                result['error'] = 'speech helper did not report a status'
            elif not self._begin_recording('selftest'):
                result['error'] = 'could not start: %s' % self._get_status()
            else:
                t0 = time.monotonic()
                time.sleep(hold / 2.0)
                result['pill_available'] = bool(self._hud.available)
                result['pill_while_recording'] = self._hud.state
                time.sleep(hold / 2.0)
                self._end_recording('selftest', transcribe=True)
                t1 = time.monotonic()
                if self._selftest_event.wait(30.0):
                    payload = self._selftest_result or {}
                    time.sleep(0.5)
                    result['pill_afterwards'] = self._hud.state
                    result.update({
                        'ok': bool(payload.get('text')),
                        'text': payload.get('text', ''),
                        'note': payload.get('note'),
                        'held_sec': round(t1 - t0, 2),
                        'stop_to_result_ms': round(
                            (self._selftest_done_at - t1) * 1000.0, 1),
                    })
                else:
                    result['error'] = 'no result within 30 s of stopping'
            result['restarts'] = self._engine.restart_count
        except Exception as e:
            log.exception('Self-test failed')
            result['error'] = 'exception: %s' % e
        log.info('Self-test finished: ok=%s%s', result.get('ok'),
                 (' error=' + result['error']) if result.get('error') else '')
        try:
            with open(self._selftest, 'w', encoding='utf-8') as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
        except Exception:
            log.exception('Could not write the self-test result')
        self._quit_requested = True     # picked up by _poll_state (main thread)


if __name__ == '__main__':
    import atexit
    _selftest = os.environ.get('FREEFLO_SELFTEST') or None
    logs.setup_logging()
    logs.install_excepthooks()
    if not _selftest:
        telemetry.init()   # consent-gated + inert without keys; never sends text
    cfg.migrate_turbo_v2()   # one-shot: reclaim old multi-model disk before any server starts

    def _snapshot_on_exit():
        try:
            history.write_snapshot()
            saved.write_snapshot()
        except Exception:
            pass
    atexit.register(_snapshot_on_exit)   # backstop for exits that skip the Quit menu
    atexit.register(refiner.stop)        # never leave a llama-server process behind
    atexit.register(injector.flush)      # put the user's clipboard back

    try:
        FreefloApp(selftest=_selftest).run()
    except Exception:
        log.critical('Fatal error during startup/run', exc_info=True)
        raise
