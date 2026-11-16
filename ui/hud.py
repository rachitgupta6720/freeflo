"""The recording HUD: a small floating "pill" shown while freeflo is listening.

What it is
----------
A borderless, non-activating NSPanel drawn entirely with Core Animation layers
(no WebKit, no nib). It floats above normal windows on every Space, including
over full-screen apps, ignores the mouse, and can never become key or main - so
it cannot take keyboard focus away from the app the user is dictating into
(which would break pasting).

States
------
  recording   live waveform bars driven by set_level(); optional one-line live
              transcript, truncated from the left so the newest words show
  processing  the same bars settle into a slow travelling wave
  done        a brief tick (optional)
  notice      a short message ("Copied - press Cmd+V", "Mic unavailable"),
              auto-hides
  hidden

Threading
---------
Every public method may be called from any thread and never raises.
  * State changes (show_*/hide) are queued and applied on the main thread in
    the order they were called.
  * set_level() and set_partial() only store a value; the main-thread
    animation timer picks it up on its next frame. They are safe to call at
    any rate from the helper's reader thread and cost nothing.

The window is built lazily on first use; call prewarm() once at startup (from
any thread) so the very first show is instant.

Usage
-----
    from ui.hud import HUD
    hud = HUD()                 # once, any thread
    hud.prewarm()
    hud.show_recording()        # hotkey down
    hud.set_level(rms)          # helper "level" events
    hud.set_partial(text)       # helper "partial" events
    hud.show_processing()       # hotkey up
    hud.hide()                  # text pasted
    hud.show_notice('Copied - press ⌘V')            # info, 2 s
    hud.show_notice('Mic unavailable', kind='error')

Manual demo:  python3 ui/hud.py --demo
"""
import functools
import logging
import math
import threading
import time
from collections import deque

log = logging.getLogger('freeflo.hud')

try:
    import objc
    from Foundation import (
        NSObject,
        NSNull,
        NSTimer,
        NSRunLoop,
        NSThread,
        NSNotificationCenter,
        NSAttributedString,
        NSUserDefaults,
        NSMakeRect,
        NSPointInRect,
        NSRunLoopCommonModes,
    )
    from AppKit import (
        NSApplication,
        NSPanel,
        NSView,
        NSColor,
        NSFont,
        NSScreen,
        NSEvent,
        NSWorkspace,
        NSBackingStoreBuffered,
        NSWindowStyleMaskBorderless,
        NSWindowStyleMaskNonactivatingPanel,
        NSStatusWindowLevel,
        NSWindowCollectionBehaviorCanJoinAllSpaces,
        NSWindowCollectionBehaviorStationary,
        NSWindowCollectionBehaviorIgnoresCycle,
        NSWindowCollectionBehaviorFullScreenAuxiliary,
        NSWindowAnimationBehaviorNone,
        NSApplicationDidChangeScreenParametersNotification,
        NSAppearance,
        NSAppearanceNameAqua,
        NSAppearanceNameDarkAqua,
        NSAnimationContext,
        NSVisualEffectView,
        NSWindowBelow,
        NSViewWidthSizable,
        NSViewHeightSizable,
        NSFontAttributeName,
        NSFontWeightRegular,
        NSFontWeightMedium,
    )
    from Quartz.QuartzCore import (
        CALayer,
        CATextLayer,
        CAShapeLayer,
        CATransaction,
        CABasicAnimation,
        CAMediaTimingFunction,
        kCAMediaTimingFunctionEaseOut,
        kCATruncationStart,
        kCATruncationEnd,
        kCAAlignmentLeft,
        kCALineCapRound,
        kCALineJoinRound,
    )
    from Quartz.CoreGraphics import (
        CGPathCreateMutable,
        CGPathMoveToPoint,
        CGPathAddLineToPoint,
        CGPathAddEllipseInRect,
        CGPathAddRoundedRect,
    )
    _HAVE_APPKIT = True
    _IMPORT_ERROR = None
except Exception as _e:                      # PyObjC missing / not on macOS
    _HAVE_APPKIT = False
    _IMPORT_ERROR = _e


# ---------------------------------------------------------------------------
# Geometry (points)
# ---------------------------------------------------------------------------
PILL_H = 38.0            # capsule height
PAD = 22.0               # transparent margin around the capsule (room for shadow)
MAX_PILL_W = 440.0       # widest the capsule may grow (long notice / partial)
WIN_W = MAX_PILL_W + 2 * PAD
WIN_H = PILL_H + 2 * PAD
HPAD = 16.0              # capsule inner horizontal padding
BOTTOM_GAP = 18.0        # capsule bottom edge above the Dock / screen bottom

BAR_COUNT = 11
BAR_W = 3.0
BAR_GAP = 2.5
BAR_MIN_H = 3.0          # a resting bar is a dot
BAR_MAX_H = 20.0
BARS_W = BAR_COUNT * BAR_W + (BAR_COUNT - 1) * BAR_GAP
COMPACT_W = 2 * HPAD + BARS_W

TEXT_GAP = 10.0          # between bars / icon and text
ICON = 16.0
FONT_SIZE = 13.0
PARTIAL_TAIL_CHARS = 160  # only the tail of a long partial is ever laid out
PARTIAL_QUANTUM = 8.0     # partial text width grows in steps (less jitter)

# ---------------------------------------------------------------------------
# Timing (seconds)
# ---------------------------------------------------------------------------
FADE_IN = 0.09
FADE_OUT = 0.11
MORPH = 0.16             # capsule width / content cross-fade
FRAME = 1.0 / 60.0
LEVEL_STALE = 0.30       # no set_level() for this long -> treat as silence
ENV_RELEASE = 0.14       # level envelope decay time-constant

# Level mapping: rms (0..1 linear amplitude) -> 0..1 bar drive.
LEVEL_DB_FLOOR = -52.0   # at or below: bars rest
LEVEL_DB_CEIL = -14.0    # at or above: bars full
LEVEL_GAMMA = 1.25


def _clamp(v, lo=0.0, hi=1.0):
    return lo if v < lo else (hi if v > hi else v)


def _even(w):
    """Round up to an even integer so a centred capsule lands on whole points."""
    return 2.0 * math.ceil(w / 2.0)


def _frac(v):
    return v - math.floor(v)


def _num(value, default):
    """float(value), or `default` when it is not a usable number."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    return default if v != v else v


def _safe(fn):
    """Public-API guard: a HUD failure must never break dictation."""
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        try:
            return fn(self, *args, **kwargs)
        except Exception:
            try:
                self._warn(fn.__name__)
            except Exception:
                pass
            return None
    return wrapper


# ---------------------------------------------------------------------------
# Objective-C classes
# ---------------------------------------------------------------------------
if _HAVE_APPKIT:

    def _objc_class(name, define):
        """Define an ObjC class once; reuse it if this module is loaded twice
        (e.g. as both `ui.hud` and `__main__`)."""
        try:
            return define()
        except objc.error:
            return objc.lookUpClass(name)

    def _define_panel():
        class FreefloHUDPanel(NSPanel):
            """A panel that refuses key/main status. Together with the
            non-activating style mask this guarantees the HUD never takes
            keyboard focus from the frontmost app."""

            def canBecomeKeyWindow(self):
                return False

            def canBecomeMainWindow(self):
                return False

        return FreefloHUDPanel

    def _define_target():
        class FreefloHUDTarget(NSObject):
            """Main-thread trampoline: timer / notification / cross-thread
            callbacks land here and are forwarded to the Python HUD."""

            def initWithOwner_(self, owner):
                self = objc.super(FreefloHUDTarget, self).init()
                if self is None:
                    return None
                self._owner = owner
                return self

            def drain_(self, _):
                self._owner._guard(self._owner._drain, 'drain')

            def tick_(self, _):
                self._owner._guard(self._owner._on_tick, 'tick')

            def autoHide_(self, _):
                self._owner._guard(self._owner._on_auto_hide, 'autoHide')

            def orderOut_(self, _):
                self._owner._guard(self._owner._on_order_out, 'orderOut')

            def screensChanged_(self, _):
                self._owner._guard(self._owner._on_screens_changed,
                                   'screensChanged')

        return FreefloHUDTarget

    _Panel = _objc_class('FreefloHUDPanel', _define_panel)
    _Target = _objc_class('FreefloHUDTarget', _define_target)


class _Txn(object):
    """CATransaction scope. duration=None disables implicit animations."""

    def __init__(self, duration=None):
        self._duration = duration

    def __enter__(self):
        CATransaction.begin()
        if self._duration is None or self._duration <= 0.0:
            CATransaction.setDisableActions_(True)
        else:
            CATransaction.setAnimationDuration_(self._duration)
            CATransaction.setAnimationTimingFunction_(
                CAMediaTimingFunction.functionWithName_(
                    kCAMediaTimingFunctionEaseOut))
        return self

    def __exit__(self, *exc):
        CATransaction.commit()
        return False


def _cg(rgba):
    r, g, b, a = rgba
    return NSColor.colorWithSRGBRed_green_blue_alpha_(r, g, b, a).CGColor()


def _palette(dark, contrast, opaque, glass=False):
    """Explicit colours for one appearance. `contrast` = Increase Contrast,
    `opaque` = Reduce Transparency, `glass` = a system material is behind the
    capsule (only ever True when neither of the other two is on)."""
    solid = contrast or opaque
    if glass and not solid:
        if dark:
            return {
                'bg': (0.10, 0.10, 0.11, 0.35),
                'border': (1.0, 1.0, 1.0, 0.22),
                'fg': (1.0, 1.0, 1.0, 0.96),
                'fg2': (1.0, 1.0, 1.0, 0.82),
                'error': (1.0, 0.27, 0.23, 1.0),
                'ok': (0.20, 0.84, 0.39, 1.0),
                'glyph': (1.0, 1.0, 1.0, 1.0),
                'shadow': 0.0,
            }
        return {
            'bg': (1.0, 1.0, 1.0, 0.30),
            'border': (0.0, 0.0, 0.0, 0.10),
            'fg': (0.0, 0.0, 0.0, 0.88),
            'fg2': (0.0, 0.0, 0.0, 0.70),
            'error': (0.84, 0.13, 0.10, 1.0),
            'ok': (0.10, 0.58, 0.24, 1.0),
            'glyph': (1.0, 1.0, 1.0, 1.0),
            'shadow': 0.0,
        }
    if dark:
        return {
            'bg': (0.085, 0.085, 0.095, 1.0 if solid else 0.90),
            'border': (1.0, 1.0, 1.0, 0.95 if contrast else 0.16),
            'fg': (1.0, 1.0, 1.0, 1.0 if contrast else 0.96),
            'fg2': (1.0, 1.0, 1.0, 1.0 if contrast else 0.80),
            'error': (1.0, 0.27, 0.23, 1.0),
            'ok': (0.20, 0.84, 0.39, 1.0),
            'glyph': (1.0, 1.0, 1.0, 1.0),
            'shadow': 0.36,
        }
    return {
        'bg': (0.985, 0.985, 0.99, 1.0 if solid else 0.94),
        'border': (0.0, 0.0, 0.0, 0.95 if contrast else 0.14),
        'fg': (0.0, 0.0, 0.0, 1.0 if contrast else 0.86),
        'fg2': (0.0, 0.0, 0.0, 1.0 if contrast else 0.66),
        'error': (0.84, 0.13, 0.10, 1.0),
        'ok': (0.10, 0.58, 0.24, 1.0),
        'glyph': (1.0, 1.0, 1.0, 1.0),
        'shadow': 0.22,
    }


class HUD(object):
    """Floating dictation pill. See the module docstring.

    partials            show the live transcript line (set_partial is a no-op
                        when False); change later with set_partials_enabled()
    theme               'auto' (follow the system appearance), 'dark', 'light'
    follow              'focus' -> the screen holding the keyboard-focus window
                        (falls back to the mouse screen); 'mouse' -> the screen
                        under the pointer
    level_curve         'db'     set_level() takes a raw linear RMS amplitude
                                 (speech is ~0.01-0.2) and is mapped through a
                                 dB curve
                        'linear' set_level() takes an already-normalised 0..1
                                 meter value, used as-is
    processing_timeout  safety net: a HUD left in "processing" hides itself
                        after this many seconds (None disables)
    glass               put a system glass material behind the capsule
                        (ignored while Reduce Transparency or Increase
                        Contrast is on); change later with set_look()
    """

    def __init__(self, partials=True, theme='auto', follow='focus',
                 level_curve='db', processing_timeout=60.0, glass=False):
        self._partials = bool(partials)
        if theme == 'system':
            theme = 'auto'
        self._theme = theme if theme in ('auto', 'dark', 'light') else 'auto'
        self._glass = bool(glass)
        self._glass_on = False            # material showing right now
        self._material = None             # NSGlassEffectView / NSVisualEffectView
        self._material_failed = False
        self._container = None
        self._follow = 'mouse' if follow == 'mouse' else 'focus'
        self._level_curve = 'linear' if level_curve == 'linear' else 'db'
        self._processing_timeout = processing_timeout

        # Cross-thread mailbox ------------------------------------------------
        self._qlock = threading.Lock()
        self._queue = deque(maxlen=256)
        self._level_target = 0.0          # written by set_level (any thread)
        self._level_time = 0.0
        self._pending_partial = ''        # written by set_partial (any thread)
        self._partial_dirty = False

        # Main-thread state ---------------------------------------------------
        self._state = 'hidden'
        self._visible = False
        self._built = False
        self._build_failures = 0
        self._warnings = 0
        self._panel = None
        self._view = None
        self._tick_timer = None
        self._hide_timer = None
        self._orderout_timer = None
        self._palette_key = None
        self._colors = None
        self._reduce_motion = False
        self._scale = 0.0
        self._pill_w = COMPACT_W
        self._shown_partial = ''
        self._partial_w = 0.0
        self._env = 0.0
        self._bar_h = [BAR_MIN_H] * BAR_COUNT
        self._drawn_h = [BAR_MIN_H] * BAR_COUNT    # what the layers hold
        self._drawn_o = [1.0] * BAR_COUNT
        self._last_tick = 0.0
        self._tick_count = 0
        self._t0 = time.monotonic()

        centre = (BAR_COUNT - 1) / 2.0
        self._bar_shape = []
        self._bar_freq = []
        self._bar_phase = []
        self._bar_rise = []
        for i in range(BAR_COUNT):
            d = abs(i - centre) / centre if centre else 0.0
            self._bar_shape.append(1.0 - 0.52 * (d ** 1.4))
            self._bar_freq.append(2.4 + 2.6 * _frac(i * 0.6180339887 + 0.11))
            self._bar_phase.append(2.0 * math.pi * _frac(i * 0.3819660113 + 0.29))
            self._bar_rise.append(0.022 + 0.030 * d)   # outer bars lag: ripple

        self._target = None
        if _HAVE_APPKIT:
            try:
                self._target = _Target.alloc().initWithOwner_(self)
            except Exception:
                self._warn('init')
        else:
            log.info('HUD disabled: AppKit unavailable (%s)', _IMPORT_ERROR)

    # ------------------------------------------------------------------
    # Public API - any thread, never raises
    # ------------------------------------------------------------------

    @_safe
    def prewarm(self):
        """Build the window now so the first show has nothing left to do."""
        self._post(self._ensure_built)

    @_safe
    def show_recording(self):
        """Mic is live: show the waveform."""
        # Reset the per-session mailbox at call time (not when the queued
        # command runs) so a set_partial()/set_level() that follows
        # immediately from the same thread is not wiped.
        self._pending_partial = ''
        self._partial_dirty = True
        self._level_target = 0.0
        self._post(self._do_recording)

    @_safe
    def set_level(self, rms):
        """Feed the waveform. ~15-20 calls per second while recording."""
        v = _num(rms, None)
        if v is None:                    # not a number: ignore the sample
            return
        v = _clamp(v)
        if self._level_curve == 'db':
            if v <= 1e-6:
                x = 0.0
            else:
                db = 20.0 * math.log10(v)
                x = _clamp((db - LEVEL_DB_FLOOR) / (LEVEL_DB_CEIL - LEVEL_DB_FLOOR))
                x = x ** LEVEL_GAMMA
        else:
            x = v
        self._level_target = x
        self._level_time = time.monotonic()

    @_safe
    def set_partial(self, text):
        """Running transcript so far (full text, not a delta). Shown on one
        line, truncated from the left. No-op when partials are disabled."""
        if not self._partials:
            return
        self._pending_partial = '' if text is None else str(text)
        self._partial_dirty = True

    @_safe
    def set_partials_enabled(self, enabled):
        self._partials = bool(enabled)
        if not self._partials:
            self._pending_partial = ''
            self._partial_dirty = True

    @_safe
    def show_processing(self):
        """Recording ended; waiting for the final transcript."""
        self._post(self._do_processing)

    @_safe
    def show_done(self, seconds=0.55):
        """Brief tick, then hide. Optional - hide() alone is fine."""
        self._post(self._do_done, _num(seconds, 0.55))

    @_safe
    def show_notice(self, text, kind='info', seconds=2.0):
        """Short message that hides itself. kind: 'info' | 'error' | 'success'.
        seconds=None (or <= 0) keeps it up until the next state change."""
        secs = None
        if seconds is not None:
            secs = _num(seconds, 2.0)
            if secs <= 0.0:
                secs = None
        self._post(self._do_notice, '' if text is None else str(text),
                   str(kind), secs)

    @_safe
    def hide(self):
        self._post(self._do_hide)

    @_safe
    def set_look(self, theme=None, glass=None):
        """Follow the app's appearance. theme: 'auto' | 'system' | 'light' |
        'dark' (None keeps it); glass: bool (None keeps it). Any thread."""
        if theme is not None:
            theme = 'auto' if theme == 'system' else str(theme)
            if theme in ('auto', 'dark', 'light'):
                self._theme = theme
        if glass is not None:
            self._glass = bool(glass)
        if self._built:
            self._post(self._refresh_environment, True)

    @property
    def state(self):
        """'hidden' | 'recording' | 'processing' | 'done' | 'notice' (as last
        applied on the main thread)."""
        return self._state

    @property
    def available(self):
        """False when AppKit could not be loaded or the window cannot be built;
        every method is then a silent no-op."""
        return bool(_HAVE_APPKIT and self._target is not None
                    and self._build_failures < 3)

    # ------------------------------------------------------------------
    # Marshalling
    # ------------------------------------------------------------------

    def _warn(self, where):
        self._warnings += 1
        if self._warnings <= 5:
            log.warning('HUD %s failed', where, exc_info=True)

    def _guard(self, fn, where):
        try:
            fn()
        except Exception:
            self._warn(where)

    def _post(self, fn, *args):
        if not self.available:
            return
        with self._qlock:
            self._queue.append((fn, args))
        if NSThread.isMainThread():
            # Drain everything (including commands other threads queued
            # earlier) so ordering is always call order.
            self._drain()
        else:
            self._target.performSelectorOnMainThread_withObject_waitUntilDone_(
                'drain:', None, False)

    def _drain(self):
        while True:
            with self._qlock:
                if not self._queue:
                    return
                fn, args = self._queue.popleft()
            try:
                fn(*args)
            except Exception:
                self._warn(getattr(fn, '__name__', 'command'))

    # ------------------------------------------------------------------
    # Construction (main thread)
    # ------------------------------------------------------------------

    def _ensure_built(self):
        if self._built:
            return True
        if self._build_failures >= 3:
            return False
        try:
            self._build()
            self._built = True
            return True
        except Exception:
            self._build_failures += 1
            self._warn('build')
            return False

    def _build(self):
        NSApplication.sharedApplication()
        rect = NSMakeRect(0.0, 0.0, WIN_W, WIN_H)
        style = NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel
        panel = _Panel.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, style, NSBackingStoreBuffered, False)
        panel.setReleasedWhenClosed_(False)
        panel.setFloatingPanel_(True)
        panel.setBecomesKeyOnlyIfNeeded_(True)
        panel.setWorksWhenModal_(True)
        panel.setHidesOnDeactivate_(False)      # we are almost never active
        panel.setCanHide_(False)
        panel.setLevel_(NSStatusWindowLevel)    # after setFloatingPanel_
        behavior = (NSWindowCollectionBehaviorCanJoinAllSpaces
                    | NSWindowCollectionBehaviorStationary
                    | NSWindowCollectionBehaviorIgnoresCycle
                    | NSWindowCollectionBehaviorFullScreenAuxiliary)
        panel.setCollectionBehavior_(behavior)
        panel.setOpaque_(False)
        panel.setBackgroundColor_(NSColor.clearColor())
        panel.setHasShadow_(False)              # the capsule draws its own
        panel.setIgnoresMouseEvents_(True)
        panel.setExcludedFromWindowsMenu_(True)
        panel.setAnimationBehavior_(NSWindowAnimationBehaviorNone)
        panel.setMovable_(False)

        # A plain container, so a system material can sit under the capsule
        # (see _ensure_material); the capsule itself is a layer-hosting view
        # whose layer tree we own.
        container = NSView.alloc().initWithFrame_(rect)
        container.setWantsLayer_(True)
        container.setAutoresizesSubviews_(True)
        view = NSView.alloc().initWithFrame_(rect)
        root = CALayer.layer()
        view.setLayer_(root)
        view.setWantsLayer_(True)
        view.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
        container.addSubview_(view)
        panel.setContentView_(container)

        no_actions = {k: NSNull.null() for k in
                      ('bounds', 'position', 'opacity', 'backgroundColor',
                       'cornerRadius')}

        with _Txn():
            pill = CALayer.layer()
            pill.setCornerRadius_(PILL_H / 2.0)
            pill.setBorderWidth_(1.0)
            pill.setShadowRadius_(9.0)
            pill.setShadowOffset_((0.0, -2.5))
            pill.setShadowColor_(_cg((0.0, 0.0, 0.0, 1.0)))
            pill.setOpacity_(0.0)
            pill.setActions_({'opacity': NSNull.null()})
            root.addSublayer_(pill)

            clip = CALayer.layer()
            clip.setCornerRadius_(PILL_H / 2.0)
            clip.setMasksToBounds_(True)
            pill.addSublayer_(clip)

            bars = CALayer.layer()
            bars.setFrame_(((HPAD, (PILL_H - BAR_MAX_H) / 2.0),
                            (BARS_W, BAR_MAX_H)))
            clip.addSublayer_(bars)
            bar_layers = []
            for i in range(BAR_COUNT):
                bar = CALayer.layer()
                bar.setActions_(no_actions)
                bar.setCornerRadius_(BAR_W / 2.0)
                bar.setBounds_(((0.0, 0.0), (BAR_W, BAR_MIN_H)))
                bar.setPosition_((i * (BAR_W + BAR_GAP) + BAR_W / 2.0,
                                  BAR_MAX_H / 2.0))
                bars.addSublayer_(bar)
                bar_layers.append(bar)

            text = CATextLayer.layer()
            text.setActions_({k: NSNull.null() for k in
                              ('bounds', 'position', 'string', 'contents',
                               'foregroundColor', 'font', 'fontSize')})
            text.setWrapped_(False)
            text.setAlignmentMode_(kCAAlignmentLeft)
            text.setFontSize_(FONT_SIZE)
            text.setOpacity_(0.0)
            clip.addSublayer_(text)

            # Tick (done / success).
            check = CAShapeLayer.layer()
            check.setBounds_(((0.0, 0.0), (ICON, ICON)))
            path = CGPathCreateMutable()
            CGPathMoveToPoint(path, None, 3.2, 8.3)
            CGPathAddLineToPoint(path, None, 6.6, 4.9)
            CGPathAddLineToPoint(path, None, 12.8, 11.4)
            check.setPath_(path)
            check.setFillColor_(None)
            check.setLineWidth_(2.0)
            check.setLineCap_(kCALineCapRound)
            check.setLineJoin_(kCALineJoinRound)
            check.setOpacity_(0.0)
            clip.addSublayer_(check)

            # Error glyph: filled disc with a "!" knocked in.
            alert = CALayer.layer()
            alert.setBounds_(((0.0, 0.0), (ICON, ICON)))
            alert.setOpacity_(0.0)
            disc = CAShapeLayer.layer()
            disc.setFrame_(((0.0, 0.0), (ICON, ICON)))
            path = CGPathCreateMutable()
            CGPathAddEllipseInRect(path, None, ((0.5, 0.5), (15.0, 15.0)))
            disc.setPath_(path)
            alert.addSublayer_(disc)
            bang = CAShapeLayer.layer()
            bang.setFrame_(((0.0, 0.0), (ICON, ICON)))
            path = CGPathCreateMutable()
            CGPathAddRoundedRect(path, None, ((7.1, 6.7), (1.8, 5.6)), 0.9, 0.9)
            CGPathAddEllipseInRect(path, None, ((6.95, 3.3), (2.1, 2.1)))
            bang.setPath_(path)
            alert.addSublayer_(bang)
            clip.addSublayer_(alert)

        self._panel = panel
        self._container = container
        self._view = view
        self._root = root
        self._pill = pill
        self._clip = clip
        self._bars = bars
        self._bar_layers = bar_layers
        self._text = text
        self._check = check
        self._alert = alert
        self._alert_disc = disc
        self._alert_bang = bang

        self._font_notice = NSFont.systemFontOfSize_weight_(
            FONT_SIZE, NSFontWeightMedium)
        self._font_partial = NSFont.systemFontOfSize_weight_(
            FONT_SIZE, NSFontWeightRegular)
        probe = NSAttributedString.alloc().initWithString_attributes_(
            'Ag', {NSFontAttributeName: self._font_notice})
        self._line_h = math.ceil(probe.size().height)

        self._refresh_environment(force=True)
        self._layout(COMPACT_W, animated=False)

        NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(
            self._target, 'screensChanged:',
            NSApplicationDidChangeScreenParametersNotification, None)

    # ------------------------------------------------------------------
    # Environment: appearance, accessibility, screen
    # ------------------------------------------------------------------

    def _is_dark(self):
        if self._theme == 'dark':
            return True
        if self._theme == 'light':
            return False
        try:
            app = NSApplication.sharedApplication()
            name = app.effectiveAppearance().bestMatchFromAppearancesWithNames_(
                [NSAppearanceNameAqua, NSAppearanceNameDarkAqua])
            if name is not None:
                return str(name) == str(NSAppearanceNameDarkAqua)
        except Exception:
            pass
        try:
            style = NSUserDefaults.standardUserDefaults().stringForKey_(
                'AppleInterfaceStyle')
            return bool(style) and 'dark' in str(style).lower()
        except Exception:
            return True

    def _accessibility_flags(self):
        """(increase contrast, reduce transparency, reduce motion)."""
        try:
            ws = NSWorkspace.sharedWorkspace()
            return (bool(ws.accessibilityDisplayShouldIncreaseContrast()),
                    bool(ws.accessibilityDisplayShouldReduceTransparency()),
                    bool(ws.accessibilityDisplayShouldReduceMotion()))
        except Exception:
            return (False, False, False)

    def _glass_failed(self, where):
        """Glass is decoration: on any failure, log once and fall back to the
        solid capsule for the rest of the run."""
        if not self._material_failed:
            log.warning('HUD glass disabled (%s)', where, exc_info=True)
        self._material_failed = True
        self._glass_on = False
        try:
            if self._material is not None:
                self._material.setHidden_(True)
        except Exception:
            pass

    def _ensure_material(self):
        """The material view under the capsule, created the first time glass
        is wanted. None when it cannot exist."""
        if self._material is not None or self._material_failed:
            return self._material
        if self._container is None:
            return None
        try:
            frame = NSMakeRect((WIN_W - self._pill_w) / 2.0, PAD,
                               self._pill_w, PILL_H)
            mat = None
            try:
                cls = objc.lookUpClass('NSGlassEffectView')    # macOS 26+
                mat = cls.alloc().initWithFrame_(frame)
                mat.setStyle_(0)                  # Regular (never Clear)
                mat.setCornerRadius_(PILL_H / 2.0)
            except Exception:
                mat = None
            if mat is None:
                mat = NSVisualEffectView.alloc().initWithFrame_(frame)
                mat.setMaterial_(13)              # HUD window
                mat.setBlendingMode_(0)           # behind window
                mat.setState_(1)                  # active
                mat.setWantsLayer_(True)
                mat.layer().setCornerRadius_(PILL_H / 2.0)
                mat.layer().setMasksToBounds_(True)
            mat.setHidden_(True)
            mat.setAlphaValue_(1.0 if self._visible else 0.0)
            self._container.addSubview_positioned_relativeTo_(
                mat, NSWindowBelow, self._view)
            self._material = mat
        except Exception:
            self._glass_failed('create')
            return None
        return self._material

    def _refresh_environment(self, force=False):
        """Re-read appearance + accessibility settings; recolour if changed.
        Called on every show, so a theme switch is picked up next time."""
        contrast, opaque, motion = self._accessibility_flags()
        self._reduce_motion = motion
        dark = self._is_dark()
        want = (self._glass and not contrast and not opaque
                and not self._material_failed)
        mat = self._ensure_material() if want else self._material
        glass_on = bool(want and mat is not None)
        if mat is not None:
            try:
                mat.setHidden_(not glass_on)
                mat.setAppearance_(NSAppearance.appearanceNamed_(
                    NSAppearanceNameDarkAqua if dark else NSAppearanceNameAqua))
                if glass_on and self._visible and mat.alphaValue() < 0.01:
                    mat.setAlphaValue_(1.0)
            except Exception:
                self._glass_failed('refresh')
                glass_on = False
        self._glass_on = glass_on
        key = (dark, contrast, opaque, glass_on)
        if not force and key == self._palette_key:
            return
        self._palette_key = key
        c = _palette(*key)
        self._colors = c
        with _Txn():
            self._pill.setBackgroundColor_(_cg(c['bg']))
            self._pill.setBorderColor_(_cg(c['border']))
            self._pill.setBorderWidth_(1.5 if contrast else 1.0)
            self._pill.setShadowOpacity_(c['shadow'])
            fg = _cg(c['fg'])
            for bar in self._bar_layers:
                bar.setBackgroundColor_(fg)
            self._alert_disc.setFillColor_(_cg(c['error']))
            self._alert_bang.setFillColor_(_cg(c['glyph']))

    def _mouse_screen(self):
        try:
            loc = NSEvent.mouseLocation()
            for s in NSScreen.screens():
                if NSPointInRect(loc, s.frame()):
                    return s
        except Exception:
            pass
        return None

    def _target_screen(self):
        screens = NSScreen.screens()
        if not screens:
            return None
        if self._follow == 'mouse':
            s = self._mouse_screen() or NSScreen.mainScreen()
        else:
            # mainScreen = the screen holding the window that currently has
            # keyboard focus, i.e. where the dictated text will land.
            s = NSScreen.mainScreen() or self._mouse_screen()
        return s or screens[0]

    def _place(self):
        """Bottom-centre of the target screen's visible frame (above the
        Dock). Returns False when there is no screen to show on."""
        screen = self._target_screen()
        if screen is None:
            return False
        vf = screen.visibleFrame()
        x = round(vf.origin.x + (vf.size.width - WIN_W) / 2.0)
        y = round(vf.origin.y + BOTTOM_GAP - PAD)
        # Compare with the real frame (not a cache): AppKit moves windows on
        # its own when displays come and go.
        cur = self._panel.frame()
        if (abs(cur.origin.x - x) > 0.5 or abs(cur.origin.y - y) > 0.5
                or abs(cur.size.width - WIN_W) > 0.5
                or abs(cur.size.height - WIN_H) > 0.5):
            self._panel.setFrame_display_(NSMakeRect(x, y, WIN_W, WIN_H), True)
        scale = float(screen.backingScaleFactor() or 1.0)
        if scale != self._scale:
            self._scale = scale
            with _Txn():
                for layer in ([self._text, self._check, self._alert_disc,
                               self._alert_bang, self._pill, self._clip]
                              + self._bar_layers):
                    layer.setContentsScale_(scale)
        return True

    # ------------------------------------------------------------------
    # Layout helpers (main thread)
    # ------------------------------------------------------------------

    def _layout(self, width, animated):
        width = min(MAX_PILL_W, max(PILL_H, _even(width)))
        self._pill_w = width
        dur = MORPH if (animated and self._visible) else None
        with _Txn(dur):
            self._pill.setBounds_(((0.0, 0.0), (width, PILL_H)))
            self._pill.setPosition_((WIN_W / 2.0, PAD + PILL_H / 2.0))
            self._clip.setFrame_(((0.0, 0.0), (width, PILL_H)))
        mat = self._material
        if mat is None:
            return
        try:
            frame = NSMakeRect((WIN_W - width) / 2.0, PAD, width, PILL_H)
            if dur and not self._reduce_motion and self._glass_on:
                def group(ctx):
                    ctx.setDuration_(MORPH)
                    ctx.setTimingFunction_(CAMediaTimingFunction.functionWithName_(
                        kCAMediaTimingFunctionEaseOut))
                    mat.animator().setFrame_(frame)
                NSAnimationContext.runAnimationGroup_completionHandler_(group, None)
            else:
                mat.setFrame_(frame)
        except Exception:
            self._glass_failed('layout')

    def _show_parts(self, bars=0.0, text=0.0, check=0.0, alert=0.0):
        dur = 0.10 if self._visible else None
        with _Txn(dur):
            self._bars.setOpacity_(bars)
            self._text.setOpacity_(text)
            self._check.setOpacity_(check)
            self._alert.setOpacity_(alert)

    def _measure(self, string, font):
        s = NSAttributedString.alloc().initWithString_attributes_(
            string, {NSFontAttributeName: font})
        return math.ceil(s.size().width) + 2.0

    def _set_text(self, string, font, color, truncation, x, width):
        with _Txn():
            self._text.setFont_(font)
            self._text.setFontSize_(FONT_SIZE)
            self._text.setForegroundColor_(_cg(color))
            self._text.setTruncationMode_(truncation)
            self._text.setString_(string)
            self._text.setFrame_(((x, math.floor((PILL_H - self._line_h) / 2.0)),
                                  (width, self._line_h)))

    def _center_icon(self, layer, x):
        with _Txn():
            layer.setPosition_((x + ICON / 2.0, PILL_H / 2.0))

    # ------------------------------------------------------------------
    # Timers
    # ------------------------------------------------------------------

    def _timer(self, delay, selector, repeats=False):
        t = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(
            delay, self._target, selector, None, repeats)
        # Common modes: keep animating while a menu is open or a modal runs.
        NSRunLoop.mainRunLoop().addTimer_forMode_(t, NSRunLoopCommonModes)
        return t

    def _cancel(self, attr):
        t = getattr(self, attr)
        if t is not None:
            setattr(self, attr, None)
            try:
                t.invalidate()
            except Exception:
                pass

    def _start_ticking(self):
        if self._tick_timer is None:
            self._last_tick = time.monotonic()
            self._tick_timer = self._timer(FRAME, 'tick:', True)
            try:
                self._tick_timer.setTolerance_(0.002)
            except Exception:
                pass

    def _auto_hide_after(self, seconds):
        self._cancel('_hide_timer')
        if seconds is not None and seconds > 0.0:
            self._hide_timer = self._timer(seconds, 'autoHide:')

    def _on_auto_hide(self):
        self._hide_timer = None
        if self._state != 'hidden':
            self._do_hide()

    # ------------------------------------------------------------------
    # Show / hide plumbing
    # ------------------------------------------------------------------

    def _fade(self, alpha, duration, start=None):
        """Fade the capsule. The model value is set to its final value at
        once and the transition is an explicit animation on top, so the HUD
        ends up in the right state even if the animation never gets to run
        (display asleep, screen locked). The window's own alpha is never
        animated for the same reason."""
        pill = self._pill
        if start is None:
            pres = pill.presentationLayer()
            start = pres.opacity() if pres is not None else pill.opacity()
        with _Txn():
            pill.setOpacity_(alpha)
        self._fade_material(alpha, duration if abs(start - alpha) >= 0.01 else None)
        if abs(start - alpha) < 0.01:
            pill.removeAnimationForKey_('fade')
            return
        anim = CABasicAnimation.animationWithKeyPath_('opacity')
        anim.setFromValue_(float(start))
        anim.setToValue_(float(alpha))
        anim.setDuration_(duration)
        anim.setTimingFunction_(CAMediaTimingFunction.functionWithName_(
            kCAMediaTimingFunctionEaseOut))
        pill.addAnimation_forKey_(anim, 'fade')

    def _fade_material(self, alpha, duration):
        """The material follows the capsule's opacity."""
        mat = self._material
        if mat is None:
            return
        try:
            if duration and self._glass_on:
                def group(ctx):
                    ctx.setDuration_(duration)
                    ctx.setTimingFunction_(CAMediaTimingFunction.functionWithName_(
                        kCAMediaTimingFunctionEaseOut))
                    mat.animator().setAlphaValue_(alpha)
                NSAnimationContext.runAnimationGroup_completionHandler_(group, None)
            else:
                mat.setAlphaValue_(alpha)
        except Exception:
            self._glass_failed('fade')

    def _present(self):
        """Put the panel on screen (if it is not) without activating anything.
        Returns False when it cannot be shown."""
        panel = self._panel
        self._cancel('_orderout_timer')
        if not self._place():
            return False
        was_visible = self._visible
        on_screen = bool(panel.isVisible())
        panel.orderFrontRegardless()        # never makeKey: focus stays put
        # Still on screen means a fade-out is in flight: continue from there.
        self._fade(1.0, FADE_IN, start=None if on_screen else 0.0)
        self._visible = True
        # No 'pop' over glass: the material view cannot scale with the layer.
        if not was_visible and not self._reduce_motion and not self._glass_on:
            pop = CABasicAnimation.animationWithKeyPath_('transform.scale')
            pop.setFromValue_(0.93)
            pop.setToValue_(1.0)
            pop.setDuration_(0.12)
            pop.setTimingFunction_(CAMediaTimingFunction.functionWithName_(
                kCAMediaTimingFunctionEaseOut))
            self._pill.addAnimation_forKey_(pop, 'pop')
        return True

    def _enter(self, state):
        """Common preamble for every visible state. Returns False if the HUD
        cannot be shown (not built / no screen)."""
        if not self._ensure_built():
            return False
        self._cancel('_hide_timer')
        self._refresh_environment()     # before any handler picks colours
        self._state = state
        return True

    def _on_order_out(self):
        self._orderout_timer = None
        if self._state == 'hidden' and self._panel is not None:
            self._panel.orderOut_(None)

    def _on_screens_changed(self):
        if self._built and self._visible:
            self._place()

    # ------------------------------------------------------------------
    # State handlers (main thread)
    # ------------------------------------------------------------------

    def _do_recording(self):
        if not self._enter('recording'):
            return
        self._shown_partial = ''
        self._partial_w = 0.0
        if not self._visible:
            self._env = 0.0
            self._bar_h = [BAR_MIN_H] * BAR_COUNT
            self._apply_bars(self._bar_h, None)
        self._layout(COMPACT_W, animated=True)
        self._show_parts(bars=1.0)
        if not self._present():
            self._do_hide()             # nowhere to show: make sure we are gone
            return
        self._start_ticking()

    def _do_processing(self):
        if not self._enter('processing'):
            return
        self._layout(COMPACT_W, animated=True)
        self._show_parts(bars=1.0)
        if not self._present():
            self._do_hide()             # nowhere to show: make sure we are gone
            return
        self._start_ticking()
        self._auto_hide_after(self._processing_timeout)

    def _do_done(self, seconds):
        if not self._enter('done'):
            return
        self._cancel('_tick_timer')
        c = self._colors
        with _Txn():
            self._check.setStrokeColor_(_cg(c['fg']))
        self._layout(COMPACT_W, animated=True)
        self._center_icon(self._check, (COMPACT_W - ICON) / 2.0)
        self._show_parts(check=1.0)
        if not self._present():
            self._do_hide()             # nowhere to show: make sure we are gone
            return
        if not self._reduce_motion:
            draw = CABasicAnimation.animationWithKeyPath_('strokeEnd')
            draw.setFromValue_(0.0)
            draw.setToValue_(1.0)
            draw.setDuration_(0.16)
            draw.setTimingFunction_(CAMediaTimingFunction.functionWithName_(
                kCAMediaTimingFunctionEaseOut))
            self._check.addAnimation_forKey_(draw, 'draw')
        self._auto_hide_after(max(0.2, seconds))

    def _do_notice(self, text, kind, seconds):
        text = ' '.join(text.split())
        if not text:
            self._do_hide()
            return
        if not self._enter('notice'):
            return
        self._cancel('_tick_timer')
        c = self._colors
        has_icon = kind in ('error', 'success')
        text_x = HPAD + (ICON + 8.0 if has_icon else 0.0)
        max_text = MAX_PILL_W - text_x - HPAD
        text_w = min(max_text, self._measure(text, self._font_notice))
        self._set_text(text, self._font_notice, c['fg'], kCATruncationEnd,
                       text_x, text_w)
        if kind == 'success':
            with _Txn():
                self._check.setStrokeColor_(_cg(c['ok']))
            self._center_icon(self._check, HPAD)
        elif kind == 'error':
            self._center_icon(self._alert, HPAD)
        self._layout(text_x + text_w + HPAD, animated=True)
        self._show_parts(text=1.0,
                         check=1.0 if kind == 'success' else 0.0,
                         alert=1.0 if kind == 'error' else 0.0)
        if not self._present():
            self._do_hide()             # nowhere to show: make sure we are gone
            return
        self._auto_hide_after(seconds)
        self._announce(text)

    def _do_hide(self):
        self._state = 'hidden'
        self._cancel('_tick_timer')
        self._cancel('_hide_timer')
        if not self._built or not self._visible:
            return
        self._visible = False
        self._fade(0.0, FADE_OUT)
        self._cancel('_orderout_timer')
        self._orderout_timer = self._timer(FADE_OUT + 0.03, 'orderOut:')

    def _announce(self, text):
        """Best-effort VoiceOver announcement for notices."""
        try:
            import AppKit
            AppKit.NSAccessibilityPostNotificationWithUserInfo(
                NSApplication.sharedApplication(),
                AppKit.NSAccessibilityAnnouncementRequestedNotification,
                {AppKit.NSAccessibilityAnnouncementKey: text,
                 AppKit.NSAccessibilityPriorityKey:
                     AppKit.NSAccessibilityPriorityMedium})
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Animation frame (main thread, 60 Hz while recording / processing)
    # ------------------------------------------------------------------

    def _apply_bars(self, heights, opacities):
        """Push bar heights (and, while processing, per-bar opacity) to the
        layers, skipping bars that did not visibly change."""
        drawn_h = self._drawn_h
        drawn_o = self._drawn_o
        with _Txn():
            for i, bar in enumerate(self._bar_layers):
                h = heights[i]
                if abs(h - drawn_h[i]) >= 0.1:
                    drawn_h[i] = h
                    bar.setBounds_(((0.0, 0.0), (BAR_W, h)))
                o = 1.0 if opacities is None else opacities[i]
                if abs(o - drawn_o[i]) >= 0.01:
                    drawn_o[i] = o
                    bar.setOpacity_(o)

    def _apply_partial(self):
        text = ' '.join(self._pending_partial.split())
        if text == self._shown_partial:
            return
        self._shown_partial = text
        if not text:
            self._partial_w = 0.0
            self._show_parts(bars=1.0)
            self._layout(COMPACT_W, animated=True)
            return
        tail = text[-PARTIAL_TAIL_CHARS:]
        text_x = HPAD + BARS_W + TEXT_GAP
        max_text = MAX_PILL_W - text_x - HPAD
        w = self._measure(tail, self._font_partial)
        w = min(max_text, math.ceil(w / PARTIAL_QUANTUM) * PARTIAL_QUANTUM)
        # Only ever grow within one recording: hypotheses get revised a few
        # pixels shorter/longer all the time and a breathing capsule is noise.
        self._partial_w = max(self._partial_w, w)
        self._set_text(tail, self._font_partial, self._colors['fg2'],
                       kCATruncationStart, text_x, self._partial_w)
        self._show_parts(bars=1.0, text=1.0)
        self._layout(text_x + self._partial_w + HPAD, animated=True)

    def _on_tick(self):
        state = self._state
        if state not in ('recording', 'processing'):
            self._cancel('_tick_timer')
            return
        now = time.monotonic()
        dt = _clamp(now - self._last_tick, 0.0, 0.1)
        self._last_tick = now
        t = now - self._t0
        self._tick_count += 1

        # Follow the focus to another display during a long recording.
        if self._tick_count % 45 == 0:
            self._place()

        calm = self._reduce_motion
        targets = [0.0] * BAR_COUNT
        opacities = None
        span = BAR_MAX_H - BAR_MIN_H

        if state == 'recording':
            if self._partial_dirty:
                self._partial_dirty = False     # clear first: no lost update
                self._apply_partial()
            target = self._level_target
            if now - self._level_time > LEVEL_STALE:
                target = 0.0
            if target >= self._env:
                self._env = target               # instant attack
            else:
                self._env += (target - self._env) * (
                    1.0 - math.exp(-dt / ENV_RELEASE))
            env = self._env
            for i in range(BAR_COUNT):
                if calm:
                    v = env * self._bar_shape[i]
                else:
                    osc = 0.5 + 0.5 * math.sin(
                        2.0 * math.pi * self._bar_freq[i] * t
                        + self._bar_phase[i])
                    v = 1.3 * env * self._bar_shape[i] * (0.5 + 0.5 * osc)
                    # Faint idle shimmer so silence still reads as "listening".
                    v += 0.08 * (1.0 - env) * (0.5 + 0.5 * math.sin(
                        2.0 * math.pi * 0.8 * t - i * 0.7))
                targets[i] = BAR_MIN_H + span * _clamp(v)
        else:  # processing
            opacities = [1.0] * BAR_COUNT
            if calm:
                pulse = 0.5 + 0.5 * math.sin(2.0 * math.pi * 0.6 * t)
                for i in range(BAR_COUNT):
                    targets[i] = BAR_MIN_H + 2.0
                    opacities[i] = 0.5 + 0.5 * pulse
            else:
                for i in range(BAR_COUNT):
                    s = 0.5 + 0.5 * math.sin(
                        2.0 * math.pi * 1.15 * t - i * 0.62)
                    targets[i] = BAR_MIN_H + 8.0 * s * s
                    opacities[i] = 0.45 + 0.55 * s

        # Per-bar smoothing: quick rise (outer bars slightly later -> ripple),
        # slower fall.
        for i in range(BAR_COUNT):
            cur = self._bar_h[i]
            tau = self._bar_rise[i] if targets[i] > cur else 0.085
            cur += (targets[i] - cur) * (1.0 - math.exp(-dt / tau))
            self._bar_h[i] = cur
        self._apply_bars(self._bar_h, opacities)

    # ------------------------------------------------------------------
    # Debug: render the layer tree off-screen (used by --snapshot)
    # ------------------------------------------------------------------

    def _debug_render_png(self, path, backdrop=(0.5, 0.5, 0.5), scale=2):
        from Quartz.CoreGraphics import (
            CGBitmapContextCreate, CGBitmapContextCreateImage,
            CGColorSpaceCreateDeviceRGB, CGContextScaleCTM,
            CGContextSetRGBFillColor, CGContextFillRect,
            kCGImageAlphaPremultipliedLast)
        from AppKit import NSBitmapImageRep, NSBitmapImageFileTypePNG
        w, h = int(WIN_W * scale), int(WIN_H * scale)
        ctx = CGBitmapContextCreate(None, w, h, 8, 0,
                                    CGColorSpaceCreateDeviceRGB(),
                                    kCGImageAlphaPremultipliedLast)
        CGContextScaleCTM(ctx, scale, scale)
        CGContextSetRGBFillColor(ctx, backdrop[0], backdrop[1], backdrop[2], 1.0)
        CGContextFillRect(ctx, ((0.0, 0.0), (WIN_W, WIN_H)))
        CATransaction.flush()
        self._root.renderInContext_(ctx)
        image = CGBitmapContextCreateImage(ctx)
        rep = NSBitmapImageRep.alloc().initWithCGImage_(image)
        data = rep.representationUsingType_properties_(
            NSBitmapImageFileTypePNG, {})
        return bool(data.writeToFile_atomically_(path, True))


# ---------------------------------------------------------------------------
# Manual demo / self-check
# ---------------------------------------------------------------------------

def _fake_rms(t):
    """Speech-like level: syllable bursts with pauses, plus a noise floor."""
    phrase = max(0.0, math.sin(2.0 * math.pi * 0.45 * t + 0.4))
    syll = 0.55 + 0.45 * math.sin(2.0 * math.pi * 4.3 * t)
    flutter = 0.85 + 0.15 * math.sin(2.0 * math.pi * 11.0 * t)
    return 0.0025 + 0.11 * (phrase ** 0.7) * syll * flutter


def _demo(argv):
    import argparse
    import os
    import sys

    ap = argparse.ArgumentParser(description='freeflo HUD demo')
    ap.add_argument('--demo', action='store_true',
                    help='cycle every HUD state on screen (about 12 s)')
    ap.add_argument('--snapshot', metavar='DIR',
                    help='render each state off-screen to PNG files in DIR')
    ap.add_argument('--theme', default='auto', choices=('auto', 'dark', 'light'))
    ap.add_argument('--follow', default='focus', choices=('focus', 'mouse'))
    ap.add_argument('--no-partials', action='store_true')
    ap.add_argument('--contrast', action='store_true',
                    help='force the Increase Contrast look')
    ap.add_argument('--reduce-motion', action='store_true',
                    help='force the Reduce Motion behaviour')
    args = ap.parse_args(argv)
    if not (args.demo or args.snapshot):
        ap.print_help()
        return 2

    logging.basicConfig(level=logging.INFO,
                        format='%(levelname)s %(name)s: %(message)s')
    if not _HAVE_APPKIT:
        print('PyObjC/AppKit not available in this interpreter: %r'
              % (_IMPORT_ERROR,))
        return 1

    from AppKit import (NSApplicationActivationPolicyAccessory,
                        NSEventTypeApplicationDefined)

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    hud = HUD(partials=not args.no_partials, theme=args.theme,
              follow=args.follow)
    if args.contrast or args.reduce_motion:
        real_flags = hud._accessibility_flags

        def forced_flags():
            c, o, m = real_flags()
            return (c or args.contrast, o, m or args.reduce_motion)
        hud._accessibility_flags = forced_flags

    if args.snapshot:
        return _snapshot(hud, args.snapshot)

    sentence = ('Okay, so I was thinking we should move the meeting to '
                'Thursday afternoon, and can you also remind the team to '
                'update the dashboard before the review?').split()
    front0 = NSWorkspace.sharedWorkspace().frontmostApplication()
    front0_pid = front0.processIdentifier() if front0 is not None else None
    report = {'samples': 0, 'visible': 0, 'key': 0, 'main': 0, 'active': 0,
              'front_changed': 0, 'states': [], 'frame': None, 'level': None,
              'active_space': None, 'shown': 0, 'transparent': 0,
              'peak_bar': 0.0, 'widest': 0.0}

    def sample():
        # Runs on the main thread (queued through the HUD's own mailbox).
        panel = hud._panel
        if panel is None:
            return
        report['samples'] += 1
        report['visible'] += 1 if panel.isVisible() else 0
        report['key'] += 1 if panel.isKeyWindow() else 0
        report['main'] += 1 if panel.isMainWindow() else 0
        report['active'] += 1 if app.isActive() else 0
        front = NSWorkspace.sharedWorkspace().frontmostApplication()
        pid = front.processIdentifier() if front is not None else None
        report['front_changed'] += 1 if pid != front0_pid else 0
        if hud.state not in report['states']:
            report['states'].append(hud.state)
        if hud.state != 'hidden':
            # While a state is showing the capsule must be fully opaque in
            # the model tree and its window fully opaque and on screen.
            report['shown'] += 1
            if (hud._pill.opacity() < 0.999 or panel.alphaValue() < 0.999
                    or not panel.isVisible()):
                report['transparent'] += 1
            report['peak_bar'] = max(report['peak_bar'], max(hud._bar_h))
            report['widest'] = max(report['widest'], hud._pill_w)
        if panel.isVisible():
            f = panel.frame()
            report['frame'] = (f.origin.x, f.origin.y, f.size.width,
                               f.size.height)
            report['level'] = panel.level()
            report['active_space'] = bool(panel.isOnActiveSpace())

    def run_for(seconds, level=False, partial=False):
        start = time.monotonic()
        nxt = 0
        while True:
            el = time.monotonic() - start
            if el >= seconds:
                break
            if level:
                hud.set_level(_fake_rms(el))
            if partial:
                n = int(el * 3.2)
                if n != nxt:
                    nxt = n
                    hud.set_partial(' '.join(sentence[:n]))
            hud._post(sample)
            time.sleep(0.05)                 # 20 Hz, like the helper

    def scenario():
        try:
            time.sleep(0.3)
            hud.prewarm()
            hud.show_recording()
            run_for(2.0, level=True)                     # waveform only
            run_for(4.5, level=True, partial=True)       # + live transcript
            hud.show_processing()
            run_for(1.4)
            hud.show_done()
            run_for(0.9)
            hud.show_notice('Copied — press ⌘V', kind='info',
                            seconds=1.3)
            run_for(1.6)
            hud.show_recording()
            run_for(0.8, level=True)
            hud.show_notice('Mic unavailable', kind='error', seconds=1.3)
            run_for(1.6)
            hud.show_recording()                         # rapid re-trigger
            hud.hide()
            hud.show_processing()
            hud.hide()
            run_for(0.4)
        finally:
            def stop():
                app.stop_(None)
                ev = NSEvent.otherEventWithType_location_modifierFlags_timestamp_windowNumber_context_subtype_data1_data2_(
                    NSEventTypeApplicationDefined, (0.0, 0.0), 0, 0.0, 0,
                    None, 0, 0, 0)
                app.postEvent_atStart_(ev, True)
            hud._post(stop)

    threading.Thread(target=scenario, daemon=True).start()
    app.run()

    ok = (hud._warnings == 0 and report['key'] == 0 and report['main'] == 0
          and report['active'] == 0 and report['visible'] > 0
          and report['transparent'] == 0 and hud.state == 'hidden'
          and not hud._panel.isVisible())
    print('demo finished')
    print('  states seen        : %s' % ', '.join(report['states']))
    print('  samples            : %d (panel visible in %d)'
          % (report['samples'], report['visible']))
    print('  panel became key   : %d   main: %d   app active: %d'
          % (report['key'], report['main'], report['active']))
    print('  frontmost app changed during demo: %d samples'
          % report['front_changed'])
    print('  shown samples      : %d (capsule not fully opaque in %d)'
          % (report['shown'], report['transparent']))
    print('  tallest bar        : %.1f of %.0f pt   widest capsule: %.0f pt'
          % (report['peak_bar'], BAR_MAX_H, report['widest']))
    print('  panel frame        : %s  level: %s  on active space: %s'
          % (report['frame'], report['level'], report['active_space']))
    print('  final state        : %s  panel visible: %s'
          % (hud.state, bool(hud._panel.isVisible()) if hud._panel else None))
    print('  HUD warnings       : %d' % hud._warnings)
    print('RESULT: %s' % ('OK' if ok else 'PROBLEM'))
    sys.stdout.flush()
    os._exit(0 if ok else 1)


def _snapshot(hud, out_dir):
    """Render each state's layer tree to PNG without needing to see the
    screen. Applies states synchronously on the main thread."""
    import os
    os.makedirs(out_dir, exist_ok=True)
    if not hud._ensure_built():
        print('could not build the HUD window')
        return 1

    def frames(n, rms=None):
        for k in range(n):
            if rms is not None:
                hud.set_level(rms(k / 60.0) if callable(rms) else rms)
            time.sleep(1.0 / 60.0)
            hud._on_tick()

    written = []

    def shot(name):
        for label, backdrop in (('on-light', (0.93, 0.93, 0.93)),
                                ('on-dark', (0.12, 0.12, 0.13))):
            p = os.path.join(out_dir, '%s-%s.png' % (name, label))
            if hud._debug_render_png(p, backdrop):
                written.append(p)

    hud.show_recording()
    frames(20, 0.0005)
    shot('1-recording-silence')
    frames(30, lambda t: 0.07)
    shot('2-recording-speech')
    hud.set_partial('Okay, so I was thinking')
    frames(6, 0.05)
    shot('3-recording-partial-short')
    hud.set_partial('Okay, so I was thinking we should move the meeting to '
                    'Thursday afternoon, and can you also remind the team to '
                    'update the dashboard before the review?')
    frames(6, 0.09)
    shot('4-recording-partial-long')
    hud.show_processing()
    frames(40)
    shot('5-processing')
    hud.show_done()
    shot('6-done')
    hud.show_notice('Copied — press ⌘V')
    shot('7-notice-info')
    hud.show_notice('Mic unavailable', kind='error')
    shot('8-notice-error')
    hud.show_notice('Hindi is ready', kind='success')
    shot('9-notice-success')
    hud.show_notice('This is a deliberately very long notice that cannot '
                    'possibly fit inside the capsule so it has to truncate '
                    'at the end', kind='error')
    shot('10-notice-long')
    hud.hide()
    for p in written:
        print(p)
    print('warnings: %d' % hud._warnings)
    return 0 if hud._warnings == 0 else 1


if __name__ == '__main__':
    import sys as _sys
    _sys.exit(_demo(_sys.argv[1:]))
