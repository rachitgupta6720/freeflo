import logging
import os
import threading
import time

try:
    import Quartz
    from CoreFoundation import (
        CFRunLoopAddSource,
        CFRunLoopGetCurrent,
        CFMachPortInvalidate,
        CFRunLoopRunInMode,
        CFRunLoopStop,
        kCFRunLoopCommonModes,
        kCFRunLoopDefaultMode,
    )
except ImportError:          # PyObjC missing: the pure logic below still imports
    Quartz = None

log = logging.getLogger('freeflo.hotkey')

ESCAPE_KEYCODE = 53

# Marks the Cmd+V that engine/injector.py posts (kept in sync with
# injector.SYNTHETIC_EVENT_TAG; duplicated so the tap never imports on the
# event path).
_SYNTHETIC_EVENT_TAG = 0x66664C4F

# Physical modifier keys -> the device-independent flag each one sets. Used to
# tell a modifier *press* from a *release* in a flags-changed event.
_MODIFIER_FLAG = {
    56: 0x020000, 60: 0x020000,      # shift (left, right)
    59: 0x040000, 62: 0x040000,      # control
    58: 0x080000, 61: 0x080000,      # option
    55: 0x100000, 54: 0x100000,      # command
    63: 0x800000,                    # fn / globe
}
_CAPS_LOCK = 57

# Another key or a mouse click this soon after the push-to-talk key went down
# means the key is being used as a modifier (Option-click, Option-arrow): the
# recording is dropped. Later than this the hold is a dictation in progress
# and stays one - a click into the text field ten seconds into a sentence
# must not throw the sentence away.
PTT_MODIFIER_WINDOW_SEC = 0.5

# keycode -> (this key's device-dependent flag bit, both sides' bits). Real
# keyboard events carry these low bits, which is the only way to tell "left
# Option went up" from "left Option went down" while RIGHT Option is held (the
# device-independent Option flag stays set throughout).
_DEVICE_BITS = {
    58: (0x0020, 0x0060), 61: (0x0040, 0x0060),      # option  L, R
    55: (0x0008, 0x0018), 54: (0x0010, 0x0018),      # command L, R
    59: (0x0001, 0x2001), 62: (0x2000, 0x2001),      # control L, R
    56: (0x0002, 0x0006), 60: (0x0004, 0x0006),      # shift   L, R
}


def _is_press(keycode, flags, mask, currently_down):
    """Did the modifier key ``keycode`` just go DOWN, judging by the flags of
    its flags-changed event?"""
    if not (flags & mask):
        return False                    # that modifier is fully up
    bits = _DEVICE_BITS.get(keycode)
    if bits and (flags & bits[1]):
        return bool(flags & bits[0])    # exact: this side's own bit
    # No per-side bits (a remapper's synthetic event). The modifier is down
    # on some key; a key cannot go down twice, so a second event for a key we
    # believe is held is its release while the other side keeps the flag set.
    return not currently_down


class HotkeyListener:
    """
    Two configurable, independent hotkeys on a raw Quartz CGEventTap:

      * PTT key    — push-to-talk. Recording runs only while held: on_ptt_start
        on key-down, on_ptt_stop on release. If another key (or a mouse button)
        is pressed in the first moments of the hold (``modifier_window``
        seconds) it was a shortcut, not dictation: on_ptt_cancel fires AT ONCE
        (so the mic closes immediately instead of staying open until the
        modifier comes back up) and the release is then ignored. Later in the
        hold the same input does NOT end the dictation; on_ptt_interrupt
        (optional) reports it once and the release still fires on_ptt_stop.
      * TOGGLE key — a clean tap (press+release with nothing else) fires
        on_toggle: once to start, again to stop.

    Optional extras:
      * on_escape   — Escape was pressed (the app cancels a hands-free
        recording with it).
      * on_activity — a real key press or mouse click happened. The app uses
        it to forget "the cursor is still where I last pasted".

    Each binding is (keycode, mask): the physical key's virtual keycode and the
    device-independent modifier bit it sets. Role is resolved by keycode, so any
    modifier key can be assigned to either role. Bindings are hot-swappable via
    set_bindings() — no tap restart needed (the tap already sees every key).

    Nothing is ever swallowed: every event is returned untouched, so the keys
    keep working as modifiers and Escape still reaches the frontmost app.
    Callbacks run on the tap's runloop thread and MUST NOT block.
    Requires Accessibility.
    """

    def __init__(self, on_ptt_start, on_ptt_stop, on_ptt_cancel, on_toggle,
                 ptt_keycode, ptt_mask, toggle_keycode, toggle_mask,
                 on_escape=None, on_activity=None, on_ptt_interrupt=None,
                 modifier_window=PTT_MODIFIER_WINDOW_SEC,
                 clock=time.monotonic):
        self._on_ptt_start = on_ptt_start
        self._on_ptt_stop = on_ptt_stop
        self._on_ptt_cancel = on_ptt_cancel
        self._on_ptt_interrupt = on_ptt_interrupt
        self._modifier_window = modifier_window
        self._clock = clock
        self._on_toggle = on_toggle
        self._on_escape = on_escape
        self._on_activity = on_activity

        # Bindings (ints; assignment is atomic under the GIL, so the tap thread
        # can read them while the main thread swaps them).
        self._ptt_keycode = ptt_keycode
        self._ptt_mask = ptt_mask
        self._toggle_keycode = toggle_keycode
        self._toggle_mask = toggle_mask

        self._thread = None
        self._runloop = None
        self._tap = None
        self._runloop_ready = threading.Event()
        # start()/stop() bookkeeping. _gen changes on every stop(), so a tap
        # thread that is still coming up (or about to enter its run loop)
        # can tell it was cancelled; _life guards the hand-over of the tap.
        self._life = threading.Lock()
        self._gen = 0

        # Hold state, only touched from the tap thread.
        self._ptt_down = False
        self._ptt_down_at = 0.0         # clock() when the hold began
        self._ptt_cancelled = False     # on_ptt_cancel already fired this hold
        self._ptt_interrupted = False   # on_ptt_interrupt already fired
        self._toggle_down = False
        self._toggle_dirtied = False

    # ------------------------------------------------------------------
    # Lifecycle / config
    # ------------------------------------------------------------------

    def set_bindings(self, ptt_keycode, ptt_mask, toggle_keycode, toggle_mask):
        """Change the key bindings live. Resets any in-progress hold so a key
        that was mid-press under the old binding can't get stuck."""
        self._ptt_keycode = ptt_keycode
        self._ptt_mask = ptt_mask
        self._toggle_keycode = toggle_keycode
        self._toggle_mask = toggle_mask
        self._reset_holds()

    def _reset_holds(self):
        self._ptt_down = self._ptt_cancelled = self._ptt_interrupted = False
        self._toggle_down = self._toggle_dirtied = False

    def start(self):
        if Quartz is None:
            log.error('Quartz is not available - hotkeys disabled')
            return
        if self._thread and self._thread.is_alive():
            return
        self._runloop_ready.clear()
        with self._life:
            gen = self._gen
        self._thread = threading.Thread(target=self._run, args=(gen,),
                                        name='hotkey-tap', daemon=True)
        self._thread.start()

    def stop(self):
        with self._life:
            self._gen += 1              # cancels a tap thread at any stage
            tap, runloop = self._tap, self._runloop
            self._tap = None            # the callback must not re-enable it
            self._runloop = None
            self._thread = None
        if tap is not None:
            try:
                Quartz.CGEventTapEnable(tap, False)
            except Exception:
                pass
            try:
                # Takes the tap out of the window server for good; a tap that
                # is only disabled stays registered (see CGGetEventTapList).
                CFMachPortInvalidate(tap)
            except Exception:
                pass
        if runloop is not None:
            try:
                CFRunLoopStop(runloop)
            except Exception:
                pass
        self._reset_holds()

    def is_running(self):
        return bool(self._thread and self._thread.is_alive())

    def is_live(self):
        """True when the tap is registered with macOS, switched on, and
        still has the keyboard events in its mask (macOS clears them from a
        tap created without Accessibility - CGEvent.h). Falls back to
        is_running() when the tap list cannot be read."""
        if not self.is_running() or self._tap is None:
            return False
        try:
            err, taps, count = Quartz.CGGetEventTapList(64, None, None)
            if err:
                return True
            want = (Quartz.CGEventMaskBit(Quartz.kCGEventKeyDown)
                    | Quartz.CGEventMaskBit(Quartz.kCGEventFlagsChanged))
            pid = os.getpid()
            for info in list(taps)[:count]:
                if info.tappingProcess != pid or not info.enabled:
                    continue
                if (int(info.eventsOfInterest) & want) == want:
                    return True
            return False
        except Exception:
            return True

    def ptt_held(self):
        """True while the tap believes the push-to-talk key is down."""
        return self._ptt_down

    @staticmethod
    def modifier_flags():
        """The modifier keys that are PHYSICALLY down right now, or None if
        that cannot be read. Cheap; safe from any thread.

        This reads the hardware (HID) state on purpose. The combined session
        state also absorbs synthetic events: after freeflo's own Cmd+V (posted
        with flags = Cmd only) it reports "Option is up" until the next real
        event, which made the lost-key-up check end a push-to-talk recording
        the user was still holding. Posted events never touch the HID state."""
        if Quartz is None:
            return None
        try:
            return int(Quartz.CGEventSourceFlagsState(
                Quartz.kCGEventSourceStateHIDSystemState))
        except Exception:
            return None

    @staticmethod
    def held_or_posted_flags():
        """Modifiers that are down according to the hardware OR the session
        (posted events), or None. For deciding that a key has been RELEASED
        when we may have missed its key-up: a key some remapper holds down
        with posted events never shows in the hardware state, and a key that
        is physically held can be missing from the session state (see
        modifier_flags) - so it only counts as up when both agree."""
        if Quartz is None:
            return None
        flags = None
        for state in ('kCGEventSourceStateHIDSystemState',
                      'kCGEventSourceStateCombinedSessionState'):
            try:
                got = int(Quartz.CGEventSourceFlagsState(getattr(Quartz, state)))
            except Exception:
                continue
            flags = got if flags is None else (flags | got)
        return flags

    # ------------------------------------------------------------------
    # Background runloop
    # ------------------------------------------------------------------

    def _run(self, gen):
        mask = (
            Quartz.CGEventMaskBit(Quartz.kCGEventKeyDown)
            | Quartz.CGEventMaskBit(Quartz.kCGEventKeyUp)
            | Quartz.CGEventMaskBit(Quartz.kCGEventFlagsChanged)
            # Mouse buttons: a click while the dictation key is held means the
            # key was being used as a modifier (Option-click), and any click
            # may have moved the text cursor.
            | Quartz.CGEventMaskBit(Quartz.kCGEventLeftMouseDown)
            | Quartz.CGEventMaskBit(Quartz.kCGEventRightMouseDown)
            | Quartz.CGEventMaskBit(Quartz.kCGEventOtherMouseDown)
        )
        tap = Quartz.CGEventTapCreate(
            Quartz.kCGSessionEventTap,
            Quartz.kCGHeadInsertEventTap,
            Quartz.kCGEventTapOptionDefault,
            mask,
            self._callback,
            None,
        )
        if not tap:
            log.error('Could not create the keyboard event tap '
                      '(Accessibility permission missing?)')
            self._runloop_ready.set()
            return
        runloop = CFRunLoopGetCurrent()
        with self._life:
            cancelled = gen != self._gen
            if not cancelled:
                self._tap = tap
                self._runloop = runloop
        if cancelled:                   # stop() ran while the tap was made
            try:
                CFMachPortInvalidate(tap)
            except Exception:
                pass
            self._runloop_ready.set()
            return
        source = Quartz.CFMachPortCreateRunLoopSource(None, tap, 0)
        CFRunLoopAddSource(runloop, source, kCFRunLoopCommonModes)
        Quartz.CGEventTapEnable(tap, True)
        self._runloop_ready.set()
        # Not CFRunLoopRun(): a CFRunLoopStop() that lands before the loop is
        # entered is lost, which would leave this tap alive after stop().
        # Events are still handled the instant they arrive; the timeout only
        # bounds how long a lost stop can go unnoticed.
        while gen == self._gen:
            CFRunLoopRunInMode(kCFRunLoopDefaultMode, 1.0, False)
        try:
            CFMachPortInvalidate(tap)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Tap callback — runs on the background runloop thread
    # ------------------------------------------------------------------

    def _safe(self, fn):
        """Run a hotkey callback without ever letting it escape into the tap.

        Callbacks MUST NOT block: this runs on the tap's runloop thread, and a
        callback that doesn't return promptly stops the hotkey dead (macOS also
        disables a tap that stops responding)."""
        if fn is None:
            return
        try:
            fn()
        except Exception:
            # Never silently: a swallowed exception here used to be invisible.
            log.exception('Hotkey callback %s failed',
                          getattr(fn, '__name__', fn))

    def _callback(self, proxy, etype, event, refcon):
        # Must always return the event (we swallow nothing) and never raise.
        try:
            if etype in (Quartz.kCGEventTapDisabledByTimeout,
                         Quartz.kCGEventTapDisabledByUserInput):
                # macOS switched the tap off (a slow callback, or "user input"
                # such as a secure text field taking over). Switch it back on,
                # then make sure a key-up we may have missed cannot leave a
                # recording running.
                tap = self._tap
                if tap is None:
                    return event    # stop() took it out: never switch it back on
                Quartz.CGEventTapEnable(tap, True)
                log.warning('Event tap was disabled (%s) - re-enabled',
                            'timeout' if etype == Quartz.kCGEventTapDisabledByTimeout
                            else 'user input')
                flags = self.held_or_posted_flags()
                if flags is not None:
                    self.reconcile(flags)
                return event

            if etype == Quartz.kCGEventFlagsChanged:
                keycode = Quartz.CGEventGetIntegerValueField(
                    event, Quartz.kCGKeyboardEventKeycode)
                self.handle_flags(keycode, Quartz.CGEventGetFlags(event))
                return event

            if etype == Quartz.kCGEventKeyDown:
                # Skip the Cmd+V freeflo itself posts when it pastes: it must
                # not count as "another key was pressed" against a dictation
                # the user has already started while the last one was pasted.
                if Quartz.CGEventGetIntegerValueField(
                        event, Quartz.kCGEventSourceUserData) == _SYNTHETIC_EVENT_TAG:
                    return event
                keycode = Quartz.CGEventGetIntegerValueField(
                    event, Quartz.kCGKeyboardEventKeycode)
                repeat = bool(Quartz.CGEventGetIntegerValueField(
                    event, Quartz.kCGKeyboardEventAutorepeat))
                self.handle_key_down(keycode, repeat)
                return event

            if etype in (Quartz.kCGEventLeftMouseDown,
                         Quartz.kCGEventRightMouseDown,
                         Quartz.kCGEventOtherMouseDown):
                self.handle_mouse_down()
                return event

            # Key-up: nothing to do. (It used to count as "another key": the
            # tail end of the previous word's last letter, released just after
            # the dictation key went down, then cancelled the dictation.)
            return event

        except Exception:
            return event

    # ------------------------------------------------------------------
    # Pure key logic (no Quartz) — unit-tested in tests/test_hotkey.py
    # ------------------------------------------------------------------

    def handle_flags(self, keycode, flags):
        """A modifier key went down or up."""
        flags = int(flags)
        if keycode == self._ptt_keycode:
            if _is_press(keycode, flags, self._ptt_mask, self._ptt_down):
                if not self._ptt_down:
                    self._ptt_down = True
                    self._ptt_down_at = self._clock()
                    self._ptt_cancelled = self._ptt_interrupted = False
                    if self._toggle_down:
                        self._toggle_dirtied = True     # a chord, not a tap
                    self._safe(self._on_ptt_start)
            else:
                was, cancelled = self._ptt_down, self._ptt_cancelled
                self._ptt_down = self._ptt_cancelled = False
                self._ptt_interrupted = False
                if was and not cancelled:
                    self._safe(self._on_ptt_stop)

        elif keycode == self._toggle_keycode:
            if _is_press(keycode, flags, self._toggle_mask, self._toggle_down):
                if not self._toggle_down:
                    self._toggle_down = True
                    # Pressed during a push-to-talk hold: that is a chord. It
                    # ends the hold and must not count as a toggle tap.
                    self._toggle_dirtied = self._ptt_down
                    self._dirty(toggle=False)
            else:
                was, dirty = self._toggle_down, self._toggle_dirtied
                self._toggle_down = self._toggle_dirtied = False
                if was and not dirty:
                    self._safe(self._on_toggle)

        else:
            # A different modifier changed while a role key is held. Only a
            # PRESS makes the hold a shortcut; releasing the Shift of the
            # capital letter you just typed does not.
            if keycode == _CAPS_LOCK:
                return
            bit = _MODIFIER_FLAG.get(keycode)
            if bit is None or (flags & bit):
                self._dirty()

    def handle_key_down(self, keycode, autorepeat=False):
        """A real (non-synthetic) key went down."""
        if autorepeat:
            return      # a key held from before the hold started
        if keycode == ESCAPE_KEYCODE:
            self._safe(self._on_escape)
        self._dirty()
        self._safe(self._on_activity)

    def handle_mouse_down(self):
        self._dirty()
        self._safe(self._on_activity)

    def reconcile(self, flags):
        """Square the hold state with the modifiers that are really down
        (``flags``). Called after the tap was switched off and on again, when
        a key-up may have been missed."""
        flags = int(flags)
        if self._ptt_down and not (flags & self._ptt_mask):
            was_cancelled = self._ptt_cancelled
            self._ptt_down = self._ptt_cancelled = False
            self._ptt_interrupted = False
            if not was_cancelled:
                self._safe(self._on_ptt_stop)
        if self._toggle_down and not (flags & self._toggle_mask):
            self._toggle_down = self._toggle_dirtied = False

    def _dirty(self, ptt=True, toggle=True):
        """Another key / modifier / mouse button went down during a hold."""
        if ptt and self._ptt_down and not self._ptt_cancelled:
            if self._clock() - self._ptt_down_at <= self._modifier_window:
                # Right after the key went down: it is being used as a
                # modifier, not to dictate.
                self._ptt_cancelled = True
                self._safe(self._on_ptt_cancel)
            elif not self._ptt_interrupted:
                # Well into the hold: the user is dictating and clicked or
                # brushed a key. Keep recording; just let the app know.
                self._ptt_interrupted = True
                self._safe(self._on_ptt_interrupt)
        if toggle and self._toggle_down:
            self._toggle_dirtied = True
