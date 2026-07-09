import threading

import Quartz
from CoreFoundation import (
    CFRunLoopAddSource,
    CFRunLoopGetCurrent,
    CFRunLoopRun,
    CFRunLoopStop,
    kCFRunLoopCommonModes,
)


class HotkeyListener:
    """
    Two configurable, independent hotkeys on a raw Quartz CGEventTap:

      * PTT key    — push-to-talk. Recording runs only while held: on_ptt_start
        on key-down, on_ptt_stop on release. If another key is pressed during
        the hold, on_ptt_cancel fires instead (it was a shortcut, not dictation).
      * TOGGLE key — a clean tap (press+release with nothing else) fires
        on_toggle: once to start, again to stop.

    Each binding is (keycode, mask): the physical key's virtual keycode and the
    device-independent modifier bit it sets. Role is resolved by keycode, so any
    modifier key can be assigned to either role. Bindings are hot-swappable via
    set_bindings() — no tap restart needed (the tap already sees every key).

    Modifier events are never swallowed, so the keys keep working as modifiers.
    The tap runs its own CFRunLoop on a background thread; start()/stop() and
    set_bindings() are safe from the main thread. Requires Accessibility.
    """

    def __init__(self, on_ptt_start, on_ptt_stop, on_ptt_cancel, on_toggle,
                 ptt_keycode, ptt_mask, toggle_keycode, toggle_mask):
        self._on_ptt_start = on_ptt_start
        self._on_ptt_stop = on_ptt_stop
        self._on_ptt_cancel = on_ptt_cancel
        self._on_toggle = on_toggle

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

        # Hold state, only touched from the tap thread.
        self._ptt_down = False
        self._ptt_dirtied = False
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
        self._ptt_down = self._ptt_dirtied = False
        self._toggle_down = self._toggle_dirtied = False

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._runloop_ready.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        if self._tap is not None:
            try:
                Quartz.CGEventTapEnable(self._tap, False)
            except Exception:
                pass
        if self._runloop is not None:
            try:
                CFRunLoopStop(self._runloop)
            except Exception:
                pass
        self._thread = None
        self._runloop = None
        self._tap = None
        self._ptt_down = self._ptt_dirtied = False
        self._toggle_down = self._toggle_dirtied = False

    def is_running(self):
        return bool(self._thread and self._thread.is_alive())

    # ------------------------------------------------------------------
    # Background runloop
    # ------------------------------------------------------------------

    def _run(self):
        mask = (
            Quartz.CGEventMaskBit(Quartz.kCGEventKeyDown)
            | Quartz.CGEventMaskBit(Quartz.kCGEventKeyUp)
            | Quartz.CGEventMaskBit(Quartz.kCGEventFlagsChanged)
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
            self._runloop_ready.set()
            return
        self._tap = tap
        source = Quartz.CFMachPortCreateRunLoopSource(None, tap, 0)
        self._runloop = CFRunLoopGetCurrent()
        CFRunLoopAddSource(self._runloop, source, kCFRunLoopCommonModes)
        Quartz.CGEventTapEnable(tap, True)
        self._runloop_ready.set()
        CFRunLoopRun()

    # ------------------------------------------------------------------
    # Tap callback — runs on the background runloop thread
    # ------------------------------------------------------------------

    def _safe(self, fn):
        try:
            fn()
        except Exception:
            pass

    def _callback(self, proxy, etype, event, refcon):
        # Must always return the event (we swallow nothing) and never raise.
        try:
            if etype == Quartz.kCGEventTapDisabledByTimeout:
                if self._tap is not None:
                    Quartz.CGEventTapEnable(self._tap, True)
                return event

            if etype == Quartz.kCGEventFlagsChanged:
                keycode = Quartz.CGEventGetIntegerValueField(
                    event, Quartz.kCGKeyboardEventKeycode
                )
                flags = Quartz.CGEventGetFlags(event)

                if keycode == self._ptt_keycode:
                    if flags & self._ptt_mask:
                        self._ptt_down = True
                        self._ptt_dirtied = False
                        self._safe(self._on_ptt_start)
                    else:
                        was, dirty = self._ptt_down, self._ptt_dirtied
                        self._ptt_down = self._ptt_dirtied = False
                        if was:
                            self._safe(self._on_ptt_cancel if dirty else self._on_ptt_stop)

                elif keycode == self._toggle_keycode:
                    if flags & self._toggle_mask:
                        self._toggle_down = True
                        self._toggle_dirtied = False
                    else:
                        was, dirty = self._toggle_down, self._toggle_dirtied
                        self._toggle_down = self._toggle_dirtied = False
                        if was and not dirty:
                            self._safe(self._on_toggle)

                else:
                    # A different modifier changed while a role key is held.
                    if self._ptt_down:
                        self._ptt_dirtied = True
                    if self._toggle_down:
                        self._toggle_dirtied = True
                return event

            if etype in (Quartz.kCGEventKeyDown, Quartz.kCGEventKeyUp):
                if self._ptt_down:
                    self._ptt_dirtied = True
                if self._toggle_down:
                    self._toggle_dirtied = True
                return event

            return event

        except Exception:
            return event
