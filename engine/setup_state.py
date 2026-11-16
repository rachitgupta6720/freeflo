"""Setup, as one small state machine the app drives.

freeflo needs two things from macOS before it can dictate: the Microphone
(asked for by the speech helper) and Accessibility (the hotkeys are an active
event tap, and the paste is a synthesised Cmd+V). This module decides, from
what the app observes, what the user should be shown and asked - so that
setup takes ONE pass and nothing is asked twice.

It is pure: no I/O, no AppKit, no clock of its own (one is injected). The app
feeds it observations and carries out the effects it returns:

    flow = SetupFlow(ledger=settings.get('setup'), version=cfg.get_version(),
                     onboarded=settings.get('onboarded'))
    effects = flow.update(mic='granted', accessibility=AXIsProcessTrusted(),
                          enabled=True, tap_running=hotkey.is_running())
    effects = flow.action('grant_mic')        # a button in the setup window
    payload = flow.view()                     # the ob_state push for the page

The rules it enforces (each one has a test in tests/test_setup_state.py):

  * The setup window opens by itself at most once per run, and only at
    launch: for a first run, or for a set-up user who is actually missing a
    required permission - and then it lists only the missing rows.
  * A permission is requested at most once per run. The second click on
    "Allow" for Accessibility opens System Settings instead of prompting
    again; a second click for the Microphone does nothing while the system
    prompt is still up.
  * One thing at a time: Accessibility is not offered while the Microphone
    prompt is waiting for an answer, so two system dialogs never stack.
  * No restart. The hotkey tap is (re)created the moment Accessibility
    appears. A restart is offered only if macOS then refuses the tap
    TAP_MAX_ATTEMPTS times in a row, which should not happen.
  * When everything is granted it says so once ("ready" notice), closes a
    window it opened itself, and stays out of the way.
  * The flow is the ONLY owner of the hotkey tap: the app never calls
    hotkey.start()/stop() itself, it carries out EFFECT_START_TAP /
    EFFECT_STOP_TAP. So a tap is never created while Accessibility is
    missing (switching dictation back on used to do exactly that).
  * A first run is finished by "Start dictating" or by simply closing the
    window once both permissions are granted - it never comes back after.
  * Nothing opens by itself while dictation is switched off.
  * One-time notices are recorded in the ledger (persisted by the app in the
    settings under 'setup') and never repeat.

Why the tap must be created AFTER the grant (CGEvent.h, macOS SDK):
    "If the tap is not permitted to monitor these events when the tap is
     created, then the appropriate bits in the mask are cleared. If that
     results in an empty mask, then NULL is returned."
The permission is evaluated when the tap is created, not when the process
starts. So a tap made after AXIsProcessTrusted() turns true works without a
relaunch - but a tap made BEFORE it (freeflo's mask also has mouse-button
events, so it is not NULL) stays deaf to keys for ever. Hence EFFECT_START_TAP
means "throw away any existing tap and create a new one", and it is emitted
on every False -> True change of Accessibility.
"""
import threading
import time

LEDGER_VERSION = 1

MIC_GRANTED = 'granted'
MIC_DENIED = 'denied'
MIC_UNDETERMINED = 'undetermined'
MIC_UNKNOWN = 'unknown'             # the helper has not reported yet
_MIC_VALUES = (MIC_GRANTED, MIC_DENIED, MIC_UNDETERMINED, MIC_UNKNOWN)

MODE_FIRST_RUN = 'first_run'        # never set up: both rows + "Start"
MODE_REPAIR = 'repair'              # set up before, something is missing now
MODE_REVIEW = 'review'              # opened from the menu, nothing missing

ROW_MIC = 'mic'
ROW_ACCESSIBILITY = 'accessibility'
ROW_ORDER = (ROW_MIC, ROW_ACCESSIBILITY)

# Row states (ob_state.rows[].state).
ST_GRANTED = 'granted'
ST_NEEDED = 'needed'                # not asked for yet in this run
ST_WAITING = 'waiting'              # asked; waiting for the user / macOS
ST_DENIED = 'denied'                # only System Settings can change it now
ST_CHECKING = 'checking'            # not known yet
ST_BLOCKED = 'blocked'              # cannot be granted (engine unavailable)
ST_RESTART = 'restart'              # granted, but macOS refused the tap

# Actions, for SetupFlow.action(). The setup page has one button per row and
# "Done": the app maps its three messages (ob_grant_mic,
# ob_grant_accessibility, ob_complete) onto ACT_GRANT_MIC,
# ACT_GRANT_ACCESSIBILITY and ACT_FINISH, and the flow works out what a
# "grant" click means right now (ask macOS, or open System Settings).
ACT_NEXT = 'next'                   # whatever view()['next'] currently is
ACT_GRANT_MIC = 'grant_mic'
ACT_OPEN_MIC_SETTINGS = 'open_mic_settings'
ACT_GRANT_ACCESSIBILITY = 'grant_accessibility'
ACT_OPEN_ACCESSIBILITY_SETTINGS = 'open_accessibility_settings'
ACT_FINISH = 'finish'
ACT_RESTART = 'restart'

# Effects: tuples whose first item is one of these. The app executes them in
# order; anything it does not know it must ignore.
EFFECT_REQUEST_MIC = 'request_mic'                  # engine.request_permissions()
EFFECT_REQUEST_STATUS = 'request_status'            # engine.request_status()
EFFECT_OPEN_MIC_SETTINGS = 'open_mic_settings'
EFFECT_PROMPT_ACCESSIBILITY = 'prompt_accessibility'   # AXIsProcessTrustedWithOptions(prompt)
EFFECT_OPEN_ACCESSIBILITY_SETTINGS = 'open_accessibility_settings'
EFFECT_START_TAP = 'start_tap'                      # hotkey.stop(); hotkey.start()
EFFECT_STOP_TAP = 'stop_tap'                        # hotkey.stop(); abort a recording
EFFECT_OPEN_SETUP = 'open_setup'                    # show the setup window
EFFECT_CLOSE_SETUP = 'close_setup'
EFFECT_COMPLETE_ONBOARDING = 'complete_onboarding'  # first run finished
EFFECT_RESTART_APP = 'restart_app'                  # last resort only
EFFECT_NOTICE = 'notice'                            # (.., id, text, kind)
EFFECT_STATUS = 'status'                            # (.., menu status line)
EFFECT_SAVE_LEDGER = 'save_ledger'                  # persist flow.ledger()
EFFECT_PUSH_STATE = 'push_state'                    # send view() to the page
EFFECT_LOG = 'log'                                  # (.., message)

NOTICE_READY = 'ready'
NOTICE_ACCESSIBILITY_LOST = 'accessibility_lost'

# Menu status lines (EFFECT_STATUS).
STATUS_READY = 'Ready'
STATUS_NEEDS = {
    (ROW_MIC,): 'Microphone access is needed',
    (ROW_ACCESSIBILITY,): 'Accessibility access is needed',
    (ROW_MIC, ROW_ACCESSIBILITY): 'Setup is not finished',
}
STATUS_ACCESSIBILITY_OFF = 'Accessibility access is off'

# A set-up user's window decision waits this long for the helper's first
# status, so the window never opens on a guess and then changes its mind.
LAUNCH_SETTLE_SEC = 4.0
# A tap that is still running this long after it was started was accepted.
TAP_VERIFY_SEC = 1.0
# Seconds between attempts, and how many before a restart is offered.
TAP_RETRY_SEC = 2.0
TAP_MAX_ATTEMPTS = 3
# A window the flow opened itself (repair) closes this long after the last
# missing row turned green - long enough to see the tick.
REPAIR_CLOSE_SEC = 1.2
# While the window is open and the microphone is switched off in System
# Settings, ask the helper again this often (that switch sends no event).
MIC_REFRESH_SEC = 1.5
# Accessibility is held back while the microphone prompt is unanswered, but
# not for ever: the prompt may have been lost behind another window.
MIC_WAIT_UNLOCK_SEC = 30.0
# Still not trusted this long after asking: explain the stale-entry trap.
# (Someone who had Accessibility before is told at once - see _ax_row.)
AX_STALE_HINT_SEC = 20.0

_KEEP = object()

_TEXT = {
    'mic_title': 'Microphone',
    'mic_detail': 'So freeflo can hear you while you hold the key. '
                  'Audio is never saved and never leaves this Mac.',
    'ax_title': 'Accessibility',
    'ax_detail': 'So freeflo can see your dictation key and type the text '
                 'where your cursor is.',
    'mic_waiting': 'Answer the macOS prompt. It may be behind this window.',
    'mic_denied': 'Switch freeflo on under Microphone, then come back.',
    'ax_waiting': 'Switch freeflo on in the list. It starts working at once.',
    'ax_stale': 'Already on in the list? Switch it off and on again. If this '
                'row still has no tick, remove freeflo from the list with '
                '\u2212 and add it again with +.',
    'ax_before': 'macOS may show freeflo as already switched on. If it '
                 'does, switch it off and on again.',
    'ax_restart': 'macOS has not handed the keyboard over yet. '
                  'Restarting freeflo fixes that.',
    'locked': 'Next, after the microphone.',
    'wait_mic': 'Waiting for your answer to the macOS microphone prompt\u2026',
    'wait_ax': 'Waiting for freeflo to be switched on in System Settings\u2026',
    'wait_tap': 'Starting the dictation keys\u2026',
    'wait_check': 'Checking\u2026',
}


def _mic(value):
    if value is None:
        return MIC_UNKNOWN
    value = str(value)
    return value if value in _MIC_VALUES else MIC_UNKNOWN


def normalize_ledger(ledger):
    """A well-formed copy of a stored ledger (anything else -> empty)."""
    out = {'v': LEDGER_VERSION, 'version': None, 'ready': None,
           'granted': {ROW_MIC: False, ROW_ACCESSIBILITY: False},
           'notices': {}}
    if not isinstance(ledger, dict):
        return out
    if isinstance(ledger.get('version'), str):
        out['version'] = ledger['version']
    if isinstance(ledger.get('ready'), str):
        out['ready'] = ledger['ready']
    granted = ledger.get('granted')
    if isinstance(granted, dict):
        for row in ROW_ORDER:
            out['granted'][row] = bool(granted.get(row))
    notices = ledger.get('notices')
    if isinstance(notices, dict):
        for key, value in notices.items():
            if isinstance(key, str) and value:
                out['notices'][key] = value if isinstance(value, str) else True
    return out


class SetupFlow(object):
    """See the module docstring. All methods are safe from any thread, but
    the effects they return must be carried out on the app's main thread."""

    def __init__(self, ledger=None, version=None, onboarded=False,
                 clock=time.monotonic, ptt_label=None):
        self._lock = threading.RLock()
        self._clock = clock
        self._born = clock()
        self._version = str(version) if version else None
        self.ptt_label = ptt_label          # e.g. 'Left Option'; for the notice

        self._had_ledger = isinstance(ledger, dict) and bool(ledger)
        self._ledger = normalize_ledger(ledger)
        self._dirty = False
        previous = self._ledger['version']
        # "An update": this version is not the one that last ran setup. A
        # set-up user from before the ledger existed counts as one as well.
        self._updated = bool(onboarded) and (
            not self._had_ledger or (previous is not None and previous != self._version))
        if self._version and previous != self._version:
            self._ledger['version'] = self._version
            self._dirty = True
        if onboarded and not self._had_ledger:
            # Set up on a build without a ledger: they had both permissions.
            self._ledger['granted'] = {ROW_MIC: True, ROW_ACCESSIBILITY: True}
            self._dirty = True
        # Granted in an EARLIER run (as stored, before this run adds to it):
        # if it is missing now, System Settings very likely still shows the
        # old entry switched on.
        self._had_accessibility = bool(self._ledger['granted'].get(ROW_ACCESSIBILITY))

        # Observations.
        self._onboarded = bool(onboarded)
        self._mic = MIC_UNKNOWN
        self._ax = None                     # None: not reported yet
        self._engine_error = None
        self._enabled = True
        self._tap_running = False

        # This run.
        self._launch_decided = False
        self._window_open = False
        self._mode = None
        self._rows = []                     # row ids shown in the open window
        self._mic_requested_at = None       # the system prompt was triggered
        self._ax_prompted_at = None         # ... for Accessibility
        self._mic_refresh_at = None
        self._needed = not self._onboarded  # something had to be set up
        self._confirmed = False             # "ready" notice sent this run
        self._ready_at = None               # when the open window went all green
        self._tap_started_at = None
        self._tap_attempts = 0
        self._tap_ok = False
        self._tap_failed = False
        self._lost_notified = False
        self._announce = False              # say 'Ready' when the tap is live
        self._pushed = None                 # last view sent to the page

    # ------------------------------------------------------------------
    # Read-only
    # ------------------------------------------------------------------

    @property
    def onboarded(self):
        return self._onboarded

    @property
    def window_open(self):
        return self._window_open

    @property
    def mode(self):
        with self._lock:
            return self._mode or self._mode_now()

    @property
    def complete(self):
        """Both required permissions are granted."""
        with self._lock:
            return self._complete()

    @property
    def ready(self):
        """Granted AND the hotkeys are live (or dictation is switched off)."""
        with self._lock:
            return self._ready()

    @property
    def restart_needed(self):
        return self._tap_failed

    @property
    def tap_ok(self):
        """The hotkey tap was started and accepted by macOS."""
        return self._tap_ok

    @property
    def busy(self):
        """True while setup has the user's attention (launch not decided
        yet, or the setup window is open). The app holds other one-off
        notices back until this is False, so nothing piles up on a prompt."""
        with self._lock:
            return bool(self._window_open or not self._launch_decided)

    @property
    def fast_poll(self):
        """True while the app should re-check often (about twice a second)
        instead of every few seconds: something is pending or on screen."""
        with self._lock:
            return (self._window_open or not self._launch_decided
                    or not self._ready() or self._tap_failed)

    def missing(self):
        """Row ids of the required permissions that are known to be missing."""
        with self._lock:
            return self._missing()

    def menu(self):
        """Which conditional menu items to show: {'setup', 'restart'}.
        'setup' ("Finish Setup...") opens the window; 'restart' appears only
        after macOS refused the hotkey tap despite Accessibility."""
        with self._lock:
            return {
                'setup': bool(self._launch_decided
                              and (self._missing() or not self._onboarded)),
                'restart': bool(self._tap_failed),
            }

    def ledger(self):
        """A JSON-serialisable copy, to store under settings['setup']."""
        with self._lock:
            return {
                'v': LEDGER_VERSION,
                'version': self._ledger['version'],
                'ready': self._ledger['ready'],
                'granted': dict(self._ledger['granted']),
                'notices': dict(self._ledger['notices']),
            }

    def summary(self):
        """One line for the log / Copy Diagnostics."""
        with self._lock:
            return ('setup: mode=%s onboarded=%s mic=%s accessibility=%s tap=%s'
                    % (self._mode or self._mode_now(), self._onboarded, self._mic,
                       {None: 'unknown', True: 'granted', False: 'missing'}[self._ax],
                       'failed' if self._tap_failed
                       else 'running' if self._tap_running
                       else 'off'))

    # ------------------------------------------------------------------
    # One-time notices
    # ------------------------------------------------------------------

    def notice_once(self, notice_id, per_version=False):
        """True the first time it is asked about ``notice_id`` - ever, or
        once per app version with ``per_version`` - and False from then on.
        Asking records it; persist ledger() afterwards (``ledger_dirty``)."""
        notice_id = str(notice_id)
        with self._lock:
            seen = self._ledger['notices'].get(notice_id)
            if per_version:
                if seen == (self._version or True):
                    return False
            elif seen:
                return False
            self._ledger['notices'][notice_id] = (
                (self._version or True) if per_version else True)
            self._dirty = True
            return True

    def notice_seen(self, notice_id):
        with self._lock:
            return bool(self._ledger['notices'].get(str(notice_id)))

    @property
    def ledger_dirty(self):
        return self._dirty

    def take_ledger(self):
        """ledger() and clear the dirty flag (for callers saving outside an
        effect list, e.g. after notice_once)."""
        with self._lock:
            self._dirty = False
            return self.ledger()

    # ------------------------------------------------------------------
    # Observations
    # ------------------------------------------------------------------

    def update(self, mic=_KEEP, accessibility=_KEEP, engine_error=_KEEP,
               enabled=_KEEP, tap_running=_KEEP, onboarded=_KEEP):
        """Feed what the app currently sees; returns the effects to carry
        out. Call it from the main-thread poll (cheap, idempotent)."""
        with self._lock:
            effects = []
            now = self._clock()
            was_ax = self._ax

            if mic is not _KEEP:
                new = _mic(mic)
                if new != self._mic:
                    effects.append((EFFECT_LOG, 'setup: microphone %s -> %s'
                                    % (self._mic, new)))
                    self._mic = new
                    if new == MIC_GRANTED:
                        self._granted_once(ROW_MIC)
                    elif new == MIC_DENIED:
                        self._confirmed = False
            if engine_error is not _KEEP:
                self._engine_error = str(engine_error) if engine_error else None
            if enabled is not _KEEP:
                self._enabled = bool(enabled)
            if tap_running is not _KEEP:
                self._tap_running = bool(tap_running)
            if onboarded is not _KEEP:
                self._onboarded = bool(onboarded)
            if accessibility is not _KEEP and accessibility is not None:
                new = bool(accessibility)
                if new != self._ax:
                    effects.append((EFFECT_LOG, 'setup: accessibility %s -> %s' % (
                        'unknown' if self._ax is None else
                        'granted' if self._ax else 'missing',
                        'granted' if new else 'missing')))
                    self._ax = new
                    if new:
                        self._granted_once(ROW_ACCESSIBILITY)

            self._drive_tap(now, was_ax, effects)
            self._decide_launch(now, effects)
            if self._launch_decided and self._missing():
                self._needed = True
            self._track_rows()
            self._refresh_mic(now, effects)
            self._settle(now, effects)
            return self._finish(effects)

    def helper_restarted(self):
        """The speech helper was replaced: a microphone prompt it had on
        screen is gone, so "Allow" may ask again."""
        with self._lock:
            self._mic_requested_at = None
            return self._finish([])

    # ------------------------------------------------------------------
    # The window
    # ------------------------------------------------------------------

    def open_window(self):
        """The user asked for the setup window (menu item). Returns effects."""
        with self._lock:
            effects = []
            if not self._window_open:
                self._open(effects, 'menu')
            else:
                effects.append((EFFECT_OPEN_SETUP,))     # bring it forward
            return self._finish(effects)

    def window_closed(self):
        """The setup window was closed (by the user or by the app)."""
        with self._lock:
            effects = []
            was_open = self._window_open
            self._window_open = False
            self._mode = None
            self._rows = []
            self._ready_at = None
            self._pushed = None
            if was_open and not self._onboarded and self._complete():
                # Everything is granted: closing the window IS finishing.
                self._onboarded = True
                effects.append((EFFECT_LOG, 'setup: first run finished '
                                            '(window closed)'))
                effects.append((EFFECT_COMPLETE_ONBOARDING,))
                if self._ready() and self._needed and not self._confirmed:
                    self._confirm(effects)
            return self._finish(effects)

    def action(self, action_id):
        """A button in the setup window (or the menu) was pressed."""
        with self._lock:
            effects = []
            action_id = str(action_id or '')
            if action_id == ACT_NEXT:
                nxt = self._view()['next']
                action_id = nxt['id'] if nxt else ''
            if action_id == ACT_GRANT_MIC:
                self._act_mic(effects)
            elif action_id == ACT_OPEN_MIC_SETTINGS:
                effects.append((EFFECT_OPEN_MIC_SETTINGS,))
            elif action_id in (ACT_GRANT_ACCESSIBILITY,
                               ACT_OPEN_ACCESSIBILITY_SETTINGS):
                self._act_accessibility(effects)
            elif action_id == ACT_FINISH:
                self._act_finish(effects)
            elif action_id == ACT_RESTART:
                if not self._onboarded and self._complete():
                    self._onboarded = True
                    effects.append((EFFECT_COMPLETE_ONBOARDING,))
                effects.append((EFFECT_LOG, 'setup: restart requested'))
                effects.append((EFFECT_RESTART_APP,))
            return self._finish(effects)

    def view(self):
        """The ob_state payload for the setup page (see _view)."""
        with self._lock:
            return self._view()

    # ------------------------------------------------------------------
    # Internals (lock held)
    # ------------------------------------------------------------------

    def _granted_once(self, row):
        if not self._ledger['granted'].get(row):
            self._ledger['granted'][row] = True
            self._dirty = True

    def _missing(self):
        missing = []
        if not self._engine_error and self._mic in (MIC_UNDETERMINED, MIC_DENIED):
            missing.append(ROW_MIC)
        if self._ax is False:
            missing.append(ROW_ACCESSIBILITY)
        return missing

    def _complete(self):
        return self._mic == MIC_GRANTED and self._ax is True

    def _ready(self):
        return self._complete() and (not self._enabled or self._tap_ok)

    def _mode_now(self):
        if not self._onboarded:
            return MODE_FIRST_RUN
        return MODE_REPAIR if self._missing() else MODE_REVIEW

    def _open(self, effects, why):
        self._mode = self._mode_now()
        self._rows = (self._missing() if self._mode == MODE_REPAIR
                      else list(ROW_ORDER))
        self._window_open = True
        self._ready_at = None
        self._pushed = None
        effects.append((EFFECT_LOG, 'setup: opening the setup window (%s, %s: %s)'
                        % (why, self._mode, ', '.join(self._rows))))
        effects.append((EFFECT_OPEN_SETUP,))

    def _decide_launch(self, now, effects):
        """Once per run: does the window open by itself?"""
        if self._launch_decided or self._ax is None:
            return
        if self._onboarded:
            mic_known = self._mic != MIC_UNKNOWN or bool(self._engine_error)
            if not mic_known and now - self._born < LAUNCH_SETTLE_SEC:
                return
            self._launch_decided = True
            missing = self._missing()
            if not missing:
                effects.append((EFFECT_LOG, 'setup: nothing to do'))
            elif not self._enabled:
                effects.append((EFFECT_LOG, 'setup: %s missing, but dictation is '
                                            'switched off - not opening the window'
                                % ' and '.join(missing)))
            else:
                effects.append((EFFECT_STATUS, STATUS_NEEDS[tuple(missing)]))
                if not self._window_open:
                    self._open(effects, 'launch')
        else:
            self._launch_decided = True
            if not self._window_open:
                self._open(effects, 'launch')

    def _track_rows(self):
        """A repair window lists what is missing; if something else goes
        missing while it is open, that row joins (in the fixed order)."""
        if self._window_open and self._mode == MODE_REPAIR:
            shown = set(self._rows) | set(self._missing())
            self._rows = [r for r in ROW_ORDER if r in shown]

    def _refresh_mic(self, now, effects):
        if not (self._window_open and self._mic == MIC_DENIED
                and not self._engine_error):
            self._mic_refresh_at = None
            return
        if self._mic_refresh_at is None or now - self._mic_refresh_at >= MIC_REFRESH_SEC:
            self._mic_refresh_at = now
            effects.append((EFFECT_REQUEST_STATUS,))

    def _drive_tap(self, now, was_ax, effects):
        if not self._ax:
            if self._ax is False and (self._tap_running or self._tap_started_at is not None):
                effects.append((EFFECT_LOG, 'setup: accessibility is off - '
                                            'removing the hotkey tap'))
                effects.append((EFFECT_STOP_TAP,))
                self._tap_running = False
            if self._ax is False and was_ax is True:
                # Lost while running. Say so once; never pop a window.
                self._needed = True
                self._confirmed = False
                self._announce = True
                effects.append((EFFECT_STATUS, STATUS_ACCESSIBILITY_OFF))
                if not self._lost_notified:
                    self._lost_notified = True
                    effects.append((EFFECT_NOTICE, NOTICE_ACCESSIBILITY_LOST,
                                    'Accessibility is off - dictation keys paused',
                                    'error'))
            self._tap_started_at = None
            self._tap_attempts = 0
            self._tap_ok = False
            self._tap_failed = False
            return
        if not self._enabled:
            if self._tap_running or self._tap_started_at is not None:
                effects.append((EFFECT_LOG, 'setup: dictation is switched off - '
                                            'removing the hotkey tap'))
                effects.append((EFFECT_STOP_TAP,))
                self._tap_running = False
            self._tap_started_at = None
            self._tap_attempts = 0
            self._tap_ok = False
            self._tap_failed = False
            return

        rising = was_ax is False
        if rising:
            # A tap made before the grant never gets key events (module
            # docstring): always make a new one, running or not.
            self._tap_attempts = 0
            self._tap_ok = False
            self._tap_failed = False
            self._lost_notified = False
            self._announce = True
            self._start_tap(now, effects)
            return
        if self._tap_started_at is None:
            if self._tap_running:
                # Started by the app while trusted (at launch): accept it.
                self._tap_started_at = now - TAP_VERIFY_SEC
                self._tap_attempts = max(1, self._tap_attempts)
            else:
                self._start_tap(now, effects)
                return
        age = now - self._tap_started_at
        if self._tap_running:
            if age >= TAP_VERIFY_SEC and not self._tap_ok:
                self._tap_ok = True
                self._tap_attempts = 0
                if self._tap_failed:
                    self._tap_failed = False
                effects.append((EFFECT_LOG, 'setup: hotkey tap is live'))
                if self._announce:
                    # Only after a change in this run - never over whatever
                    # the status line says at an ordinary launch.
                    self._announce = False
                    if self._mic == MIC_GRANTED:
                        effects.append((EFFECT_STATUS, STATUS_READY))
        else:
            self._tap_ok = False
            if self._tap_failed:
                return
            if self._tap_attempts >= TAP_MAX_ATTEMPTS:
                if age >= TAP_VERIFY_SEC:
                    self._tap_failed = True
                    effects.append((EFFECT_LOG,
                                    'setup: macOS refused the hotkey tap %d times '
                                    'although Accessibility is granted - offering '
                                    'a restart' % self._tap_attempts))
            elif age >= TAP_RETRY_SEC:
                self._start_tap(now, effects)

    def _start_tap(self, now, effects):
        self._tap_started_at = now
        self._tap_attempts += 1
        self._tap_running = False       # the app reports it on the next update
        effects.append((EFFECT_LOG, 'setup: starting the hotkey tap (attempt %d)'
                        % self._tap_attempts))
        effects.append((EFFECT_START_TAP,))

    def _settle(self, now, effects):
        """Everything granted: confirm once and get out of the way."""
        if not self._ready():
            self._ready_at = None
            return
        if self._window_open:
            if self._mode != MODE_REPAIR:
                return                  # first run / review: the user closes it
            if self._ready_at is None:
                self._ready_at = now
                return
            if now - self._ready_at < REPAIR_CLOSE_SEC:
                return
            self._window_open = False
            self._mode = None
            self._rows = []
            self._ready_at = None
            self._pushed = None
            effects.append((EFFECT_CLOSE_SETUP,))
        if self._onboarded and self._needed and not self._confirmed:
            self._confirm(effects)

    def _confirm(self, effects):
        self._confirmed = True
        self._needed = False
        if self._ledger['ready'] != self._version:
            self._ledger['ready'] = self._version
            self._dirty = True
        key = self.ptt_label or 'your dictation key'
        if self._enabled:
            text = 'freeflo is ready - hold %s and speak' % key
        else:
            text = 'freeflo is set up - dictation is switched off'
        effects.append((EFFECT_LOG, 'setup: complete'))
        if self._enabled:
            effects.append((EFFECT_STATUS, STATUS_READY))
        effects.append((EFFECT_NOTICE, NOTICE_READY, text, 'success'))

    def _act_mic(self, effects):
        if self._engine_error or self._mic == MIC_GRANTED:
            return
        if self._mic == MIC_DENIED:
            # macOS only asks once; after "Don't Allow" the switch is in Settings.
            effects.append((EFFECT_OPEN_MIC_SETTINGS,))
            return
        if self._mic_requested_at is not None:
            effects.append((EFFECT_LOG, 'setup: microphone was already asked '
                                        'for in this run - not asking again'))
            return
        self._mic_requested_at = self._clock()
        effects.append((EFFECT_LOG, 'setup: asking for the microphone'))
        effects.append((EFFECT_REQUEST_MIC,))

    def _act_accessibility(self, effects):
        if self._ax is True:
            return
        if self._ax_row(self._clock(), self._shown_mic_row())['locked']:
            effects.append((EFFECT_LOG, 'setup: Accessibility waits for the '
                                        'microphone prompt - not asking yet'))
            return
        if self._ax_prompted_at is None:
            # First time in this run: the system prompt. It also puts freeflo
            # in the Accessibility list, so it must come before Settings.
            self._ax_prompted_at = self._clock()
            effects.append((EFFECT_LOG, 'setup: asking for Accessibility'))
            effects.append((EFFECT_PROMPT_ACCESSIBILITY,))
        else:
            effects.append((EFFECT_OPEN_ACCESSIBILITY_SETTINGS,))

    def _act_finish(self, effects):
        view = self._view()
        if not view['can_finish']:
            return
        if not self._onboarded:
            self._onboarded = True
            effects.append((EFFECT_COMPLETE_ONBOARDING,))
        was_open = self._window_open
        self._window_open = False
        self._mode = None
        self._rows = []
        self._ready_at = None
        self._pushed = None
        if was_open:
            effects.append((EFFECT_CLOSE_SETUP,))
        if self._ready() and self._needed and not self._confirmed:
            self._confirm(effects)

    def _finish(self, effects):
        """Common tail: persist the ledger and refresh the page if needed."""
        if self._dirty:
            self._dirty = False
            effects.append((EFFECT_SAVE_LEDGER,))
        if self._window_open:
            view = self._view()
            if view != self._pushed:
                self._pushed = view
                effects.append((EFFECT_PUSH_STATE,))
        return effects

    # -- the page ---------------------------------------------------------

    def _mic_row(self, now):
        row = {'id': ROW_MIC, 'title': _TEXT['mic_title'],
               'detail': _TEXT['mic_detail'], 'action': None, 'hint': None,
               'locked': False}
        if self._engine_error:
            row.update(state=ST_BLOCKED, hint=self._engine_error)
        elif self._mic == MIC_GRANTED:
            row.update(state=ST_GRANTED)
        elif self._mic == MIC_DENIED:
            row.update(state=ST_DENIED, hint=_TEXT['mic_denied'],
                       action={'id': ACT_OPEN_MIC_SETTINGS,
                               'label': 'Open System Settings'})
        elif self._mic == MIC_UNDETERMINED:
            if self._mic_requested_at is None:
                row.update(state=ST_NEEDED,
                           action={'id': ACT_GRANT_MIC, 'label': 'Allow'})
            else:
                row.update(state=ST_WAITING, hint=_TEXT['mic_waiting'])
        else:
            row.update(state=ST_CHECKING)
        row['granted'] = row['state'] == ST_GRANTED
        return row

    def _ax_row(self, now, mic_row):
        row = {'id': ROW_ACCESSIBILITY, 'title': _TEXT['ax_title'],
               'detail': _TEXT['ax_detail'], 'action': None, 'hint': None,
               'locked': False}
        if self._ax is True:
            if self._tap_failed:
                row.update(state=ST_RESTART, hint=_TEXT['ax_restart'],
                           action={'id': ACT_RESTART, 'label': 'Restart freeflo'})
            else:
                row.update(state=ST_GRANTED)
        elif self._ax is None:
            row.update(state=ST_CHECKING)
        elif self._ax_prompted_at is None:
            # Had it before and lost it (an update): warn about the stale
            # entry BEFORE they leave for System Settings, where this window
            # can no longer be seen.
            row.update(state=ST_NEEDED,
                       hint=_TEXT['ax_before'] if self._had_accessibility else None,
                       action={'id': ACT_GRANT_ACCESSIBILITY, 'label': 'Allow'})
        else:
            hint = _TEXT['ax_waiting']
            if (self._had_accessibility
                    or now - self._ax_prompted_at >= AX_STALE_HINT_SEC):
                hint = _TEXT['ax_stale']
            row.update(state=ST_WAITING, hint=hint,
                       action={'id': ACT_OPEN_ACCESSIBILITY_SETTINGS,
                               'label': 'Open System Settings'})
        # One system dialog at a time: the microphone goes first.
        if mic_row is not None and row['state'] == ST_NEEDED:
            waiting_long = (mic_row['state'] == ST_WAITING
                            and self._mic_requested_at is not None
                            and now - self._mic_requested_at >= MIC_WAIT_UNLOCK_SEC)
            if mic_row['state'] in (ST_NEEDED, ST_WAITING) and not waiting_long:
                row.update(action=None, locked=True, hint=_TEXT['locked'])
        row['granted'] = row['state'] == ST_GRANTED
        return row

    def _shown(self):
        mode = self._mode or self._mode_now()
        return self._rows if self._window_open else (
            self._missing() if mode == MODE_REPAIR else list(ROW_ORDER))

    def _shown_mic_row(self):
        """The microphone row if it is on screen (it is what can hold the
        Accessibility row back), else None."""
        return self._mic_row(self._clock()) if ROW_MIC in self._shown() else None

    def _view(self):
        now = self._clock()
        mode = self._mode or self._mode_now()
        shown = self._shown()
        mic_row = self._mic_row(now)
        ax_row = self._ax_row(now, mic_row if ROW_MIC in shown else None)
        rows = [r for r in (mic_row, ax_row) if r['id'] in shown]

        complete = self._complete()
        ready = self._ready()
        if mode == MODE_FIRST_RUN:
            can_finish = complete or bool(self._engine_error)
        else:
            can_finish = True       # a set-up user may always close it

        nxt = None
        for row in rows:
            if row['action']:
                nxt = dict(row['action'], row=row['id'])
                break
        # Granted, but the tap is still being verified: nothing to press for
        # a second (a repair window then closes itself).
        settling = complete and not ready and not self._tap_failed
        if nxt is None and not settling and not any(
                r['state'] in (ST_WAITING, ST_CHECKING) or r['locked'] for r in rows):
            if mode == MODE_FIRST_RUN:
                if can_finish:
                    nxt = {'id': ACT_FINISH, 'label': 'Start dictating', 'row': None}
            else:
                nxt = {'id': ACT_FINISH, 'label': 'Done', 'row': None}

        waiting = None
        if nxt is None:
            states = dict((r['id'], r['state']) for r in rows)
            if states.get(ROW_MIC) == ST_WAITING:
                waiting = _TEXT['wait_mic']
            elif states.get(ROW_ACCESSIBILITY) == ST_WAITING:
                waiting = _TEXT['wait_ax']
            elif complete and not ready and not self._tap_failed:
                waiting = _TEXT['wait_tap']
            elif ST_CHECKING in states.values():
                waiting = _TEXT['wait_check']

        if self._tap_failed:
            title = 'One last step'
            subtitle = _TEXT['ax_restart']
        elif complete and not ready:
            title = 'Almost there'
            subtitle = _TEXT['wait_tap']
        elif complete:
            title = 'You’re all set'
            subtitle = ('Hold %s and speak.' % self.ptt_label if self.ptt_label
                        else 'Hold your dictation key and speak.')
        elif mode == MODE_FIRST_RUN:
            title = 'Two quick permissions'
            subtitle = 'macOS asks once for each. Then you can dictate.'
        else:
            title = ('One quick step' if len(rows) == 1 else 'Two quick steps')
            subtitle = ('macOS asks again after an update. It takes a few seconds.'
                        if self._updated else 'freeflo needs this to work.')

        return {
            'v': 2,
            'mode': mode,
            'title': title,
            'subtitle': subtitle,
            'rows': rows,
            'next': nxt,
            'waiting': waiting,
            'complete': complete,
            'ready': ready,
            'can_finish': can_finish,
            'auto_close': bool(mode == MODE_REPAIR and self._window_open),
            'restart_needed': bool(self._tap_failed),
            'hotkeys_live': bool(self._tap_ok),
            'updated': bool(self._updated),
            # Kept for the page as it was before (and for the app's tests).
            'mic': self._mic,
            'mic_granted': self._mic == MIC_GRANTED,
            'accessibility': bool(self._ax),
            'engine_error': self._engine_error,
        }


# ----------------------------------------------------------------------
# What the setup window (ui/onboarding.html) is sent: the flow's view plus
# the settings it shows. A free function so the app and the page checks /
# snapshot tool build exactly the same payload.
# ----------------------------------------------------------------------

# If macOS keeps refusing the hotkey tap (not expected: the tap is made
# after the grant), the menu gets "Restart freeflo" and the page explains it
# in plain words, once, in its subtitle.
RESTART_SUBTITLE = ('macOS has not handed the keyboard over yet. Quit '
                    'freeflo from its menu bar icon and open it again.')


def page_state(view, settings, key_label, version=None):
    """``view`` (SetupFlow.view()) decorated for the page. ``settings`` is
    the stored settings dict; ``key_label(key_id)`` names a dictation key
    ('Left Option (⌥)'). The consent question is asked on first run only
    (``consent.show``): a repair or review window must never write the
    privacy switches back."""
    view = dict(view)
    view['rows'] = [dict(r) for r in view.get('rows') or ()]
    if view.get('restart_needed'):
        view['subtitle'] = RESTART_SUBTITLE
        for row in view['rows']:
            if row.get('state') == ST_RESTART:
                row['hint'] = None          # said once, in the subtitle
                row['action'] = None
    ptt = settings.get('ptt_key', 'left_option')
    analytics = bool(settings.get('analytics_enabled', True))
    crash = bool(settings.get('crash_enabled', True))
    ptt_label = key_label(ptt)
    view.update({
        'consent': {'show': bool(view.get('mode') == MODE_FIRST_RUN
                                 and not settings.get('onboarded')),
                    'analytics': analytics, 'crash': crash},
        'analytics': analytics,
        'crash': crash,
        'keys': {'ptt': ptt_label,
                 'toggle': key_label(settings.get('toggle_key', 'right_option'))},
        'ptt_key': ptt,
        'ptt_label': ptt_label.split(' (')[0],
        # The dictation keys are live: a dictation made with them while the
        # setup window is in front is shown on its "Try it" line (the app
        # sends it there instead of pasting), so the page may say "hold the
        # key" rather than only "click Try it".
        'try_hotkey': bool(view.get('hotkeys_live')),
        'app_version': version,
    })
    return view
