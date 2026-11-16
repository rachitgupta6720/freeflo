"""The freeflo window: a native NSWindow hosting a WKWebView + a JS<->Python
bridge.

Constraints (verified during design review):
  * The app is LSUIElement (Accessory policy) — a window will not become key
    and cannot receive keystrokes unless we switch to Regular policy first.
  * All WKWebView work (creation, evaluateJavaScript) must happen on the main
    thread. send() marshals onto the main thread via performSelectorOnMainThread.
  * Every ObjC object we create (window, webview, content controller, bridge,
    delegate) is held by a long-lived Python reference or PyObjC collects it.

Look
----
The page draws everything, edge to edge: the window has a full-size content
view and a transparent title bar, so only the three traffic lights remain of
the native chrome. The page is told how much room they need (the
``--titlebar-h`` / ``--traffic-w`` CSS variables and ``window.__freefloWindow``
are injected before the page's own scripts run), and the web view never paints
a background of its own.

Two looks, switched live:

  solid   the window paints the system window background; the page paints
          opaque surfaces over it.
  glass   the window is see-through and a system material sits behind the web
          view: ``NSGlassEffectView`` (Liquid Glass, macOS 26+) or, where that
          class does not exist, an ``NSVisualEffectView`` blending with what is
          behind the window. The page then uses translucent surfaces.

Glass is switched off automatically while Reduce Transparency or Increase
Contrast is on in System Settings, and comes back when they are turned off.

The appearance (System / Light / Dark) is applied to the NSWindow, so the
material, the traffic lights, the scroll bars and the page's
``prefers-color-scheme`` all agree.

Page <-> window (never reaches the app's message handler):
  page -> window   {action: 'window_style', appearance: 'system'|'light'|'dark',
                    glass: bool}
                   {action: 'copy_text', text: str}   (written to the pasteboard)
  window -> page   freefloReceive('window_style', {glass, glass_wanted,
                    appearance, material, reduced, titlebar, traffic, accent})
                   freefloReceive('window_shown', {})  (an existing window was
                    brought back, so the page can refresh what it shows)

Native chrome the page cannot provide itself:
  * a drag strip over the title-bar area (right of the traffic lights), so
    the window can be moved and double-click zooms / minimises as the user
    chose in System Settings - the web view would otherwise take every click;
  * a minimal main menu (Close Window, and an Edit menu) when the app has
    none, so Cmd+C / Cmd+V / Cmd+X / Cmd+A / Cmd+Z work in the page's fields.
"""
import json
import logging
import os

import objc
from Foundation import (NSObject, NSURL, NSMakeRect, NSNotificationCenter,
                        NSUserDefaults)
from AppKit import (
    NSApplication,
    NSColorSpace,
    NSMenu,
    NSMenuItem,
    NSPasteboard,
    NSPasteboardTypeString,
    NSViewMinYMargin,
    NSWindowAbove,
    NSApplicationActivationPolicyRegular,
    NSApplicationActivationPolicyAccessory,
    NSAppearance,
    NSColor,
    NSView,
    NSWindow,
    NSWorkspace,
    NSWindowStyleMaskTitled,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskMiniaturizable,
    NSWindowStyleMaskResizable,
    NSWindowStyleMaskFullSizeContentView,
    NSWindowTitleHidden,
    NSWindowCloseButton,
    NSWindowZoomButton,
    NSBackingStoreBuffered,
    NSViewWidthSizable,
    NSViewHeightSizable,
    NSVisualEffectView,
    NSVisualEffectMaterialSidebar,
    NSVisualEffectBlendingModeBehindWindow,
    NSVisualEffectStateActive,
    NSWindowBelow,
    NSWorkspaceAccessibilityDisplayOptionsDidChangeNotification,
)
from WebKit import (
    WKWebView,
    WKWebViewConfiguration,
    WKUserContentController,
    WKUserScript,
)

try:                                    # AppKit constant; the literal is its value
    from AppKit import NSSystemColorsDidChangeNotification as _COLORS_CHANGED
except Exception:                       # pragma: no cover
    _COLORS_CHANGED = 'NSSystemColorsDidChangeNotification'

import config

log = logging.getLogger('freeflo.window')

APPEARANCES = ('system', 'light', 'dark')

# Content size used when the caller does not pass one.
DEFAULT_SIZE = (820.0, 620.0)
PAGE_SIZES = {
    'onboarding.html': (540.0, 640.0),
}
MIN_SIZE = (640.0, 480.0)
MIN_SIZES = {
    'onboarding.html': (480.0, 560.0),
}

# Fallbacks for the traffic-light geometry, used only if AppKit will not tell
# us (measured on macOS 27: title bar 32 pt, buttons end at x = 69).
_TITLEBAR_H = 32.0
_TRAFFIC_W = 78.0

_NSAppearanceNameAqua = 'NSAppearanceNameAqua'
_NSAppearanceNameDarkAqua = 'NSAppearanceNameDarkAqua'


def style_from_settings(settings):
    """(appearance, glass) from the stored settings.

    ``appearance`` is 'system' | 'light' | 'dark'; ``glass`` a bool. Reads the
    newer ``appearance`` key when present and otherwise the original
    ``theme`` / ``glass`` pair, where theme 'glass' meant "glass on, follow
    the system"."""
    settings = settings or {}
    theme = str(settings.get('theme') or 'system')
    appearance = settings.get('appearance')
    if appearance not in APPEARANCES:
        appearance = theme if theme in APPEARANCES else 'system'
    glass = bool(settings.get('glass')) or theme == 'glass'
    return appearance, glass


def _stored_style():
    try:
        return style_from_settings(config.load())
    except Exception:
        log.exception('Could not read the appearance settings')
        return 'system', False


def _hex(color):
    """'#rrggbb' for an NSColor, or None."""
    try:
        c = color.colorUsingColorSpace_(NSColorSpace.sRGBColorSpace())
        if c is None:
            return None
        return '#%02x%02x%02x' % tuple(
            int(round(max(0.0, min(1.0, float(v))) * 255))
            for v in (c.redComponent(), c.greenComponent(), c.blueComponent()))
    except Exception:
        return None


_menu_done = False


def _ensure_main_menu():
    """Give the app a minimal main menu if it has none (a menu-bar app built
    with rumps has none). Without an Edit menu, Cmd+C / Cmd+V never reach the
    web view's text fields. Runs once per process, main thread only."""
    global _menu_done
    if _menu_done:
        return
    _menu_done = True
    try:
        app = NSApplication.sharedApplication()
        if app.mainMenu() is not None:
            return
        main = NSMenu.alloc().initWithTitle_('MainMenu')

        def submenu(title, items):
            holder = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                title, None, '')
            menu = NSMenu.alloc().initWithTitle_(title)
            for item in items:
                if item is None:
                    menu.addItem_(NSMenuItem.separatorItem())
                else:
                    # nil target: the action goes down the responder chain,
                    # i.e. to the focused web view.
                    menu.addItemWithTitle_action_keyEquivalent_(*item)
            holder.setSubmenu_(menu)
            main.addItem_(holder)

        submenu('freeflo', [('Close Window', 'performClose:', 'w')])
        submenu('Edit', [('Undo', 'undo:', 'z'), ('Redo', 'redo:', 'Z'), None,
                         ('Cut', 'cut:', 'x'), ('Copy', 'copy:', 'c'),
                         ('Paste', 'paste:', 'v'),
                         ('Select All', 'selectAll:', 'a')])
        app.setMainMenu_(main)
    except Exception:
        log.warning('Could not install the Edit menu', exc_info=True)


def _accessibility_wants_opaque():
    """True while Reduce Transparency or Increase Contrast is on."""
    try:
        ws = NSWorkspace.sharedWorkspace()
        return bool(ws.accessibilityDisplayShouldReduceTransparency()
                    or ws.accessibilityDisplayShouldIncreaseContrast())
    except Exception:
        return False


class _DragStrip(NSView):
    """Sits over the title-bar area, right of the traffic lights, above the
    web view: a click-drag there moves the window and a double-click does what
    System Settings > Desktop & Dock says (zoom by default). The page keeps
    that strip free of controls."""

    def mouseDownCanMoveWindow(self):
        return False                    # we start the drag ourselves

    def mouseDown_(self, event):
        window = self.window()
        if window is None:
            return
        try:
            if event.clickCount() == 2:
                action = NSUserDefaults.standardUserDefaults().stringForKey_(
                    'AppleActionOnDoubleClick')
                action = str(action) if action is not None else ''
                if action == 'Minimize':
                    window.performMiniaturize_(None)
                elif action != 'None':
                    window.performZoom_(None)
                return
            window.performWindowDragWithEvent_(event)
        except Exception:
            log.debug('Title strip click failed', exc_info=True)

    def scrollWheel_(self, event):
        # Scrolling with the pointer over the strip still scrolls the page.
        target = getattr(self, '_scroll_target', None)
        if target is not None:
            target.scrollWheel_(event)

    def setScrollTarget_(self, view):
        self._scroll_target = view


class _Bridge(NSObject):
    """Receives JS->Python messages (WKScriptMessageHandler) and runs
    Python->JS evaluateJavaScript on the main thread."""

    def initWithCallback_(self, cb):
        self = objc.super(_Bridge, self).init()
        if self is None:
            return None
        self._cb = cb
        self._webview = None
        self._owner = None
        return self

    def setWebview_(self, wv):
        self._webview = wv

    def setOwner_(self, owner):
        self._owner = owner

    # --- JS -> Python. WebKit delivers this on the main thread. ---
    def userContentController_didReceiveScriptMessage_(self, ucc, message):
        try:
            self._cb(message.body())
        except Exception:
            # Never silently: a handler bug swallowed here used to leave the
            # page waiting forever with nothing in the log.
            log.exception('Window message handler failed')

    # --- Python -> JS. Always invoked on the main thread (see send()). ---
    def evalJS_(self, js):
        if self._webview is not None:
            self._webview.evaluateJavaScript_completionHandler_(js, None)

    # --- look: main-thread trampolines for WindowController ---
    def applyStyle_(self, _ignored):
        # set_style() stored the values already; just bring the window in line.
        owner = getattr(self, '_owner', None)
        if owner is None:
            return
        try:
            owner._apply_style(None, None)
        except Exception:
            log.exception('Could not apply the window style')

    def systemColorsChanged_(self, notification):
        # The accent colour changed in System Settings: tell the page.
        owner = getattr(self, '_owner', None)
        if owner is None:
            return
        try:
            owner._apply_style(None, None)
        except Exception:
            log.exception('Could not follow the accent colour')

    def accessibilityChanged_(self, notification):
        owner = getattr(self, '_owner', None)
        if owner is None:
            return
        try:
            owner._apply_style(None, None)
        except Exception:
            log.exception('Could not re-apply the window style')


# The freeflo windows that have been put on screen (NSWindow objects, most
# recently presented last). The app is Regular (Dock icon) while any of them
# is visible and goes back to Accessory when the last one closes.
_PRESENTED = []


def _note_presented(window):
    _forget(window)
    _PRESENTED.append(window)


def _forget(window):
    _PRESENTED[:] = [w for w in _PRESENTED if w is not window and w != window]


def _remaining_windows(closing):
    """The presented windows still on screen once ``closing`` has gone
    (``closing`` is also forgotten). Main thread."""
    if closing is not None:
        _forget(closing)
    alive = []
    for w in _PRESENTED:
        try:
            if w.isVisible():
                alive.append(w)
        except Exception:
            pass
    return alive


class _WindowDelegate(NSObject):
    """Reverts the app to Accessory (hides the temporary Dock icon) when the
    last freeflo window closes, tells the owner the window went away, and
    keeps WindowController.is_key up to date."""

    def initWithCallback_(self, cb):
        self = objc.super(_WindowDelegate, self).init()
        if self is None:
            return None
        self._cb = cb
        self._focus = None      # the owner's {'key': bool}, set by _build
        return self

    def windowDidBecomeKey_(self, notification):
        if self._focus is not None:
            self._focus['key'] = True

    def windowDidResignKey_(self, notification):
        if self._focus is not None:
            self._focus['key'] = False

    def windowWillClose_(self, notification):
        if self._focus is not None:
            self._focus['key'] = False
        try:
            closing = notification.object()
        except Exception:
            closing = None
        others = _remaining_windows(closing)
        app = NSApplication.sharedApplication()
        if others:
            # Another freeflo window is still up (Home's "Fix" opened setup,
            # or the reverse): keep the Dock icon and Cmd-Tab entry, and hand
            # it the focus instead of leaving it stranded behind other apps.
            try:
                others[-1].makeKeyAndOrderFront_(None)
            except Exception:
                log.exception('Could not bring the other window forward')
        else:
            app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
        cb = getattr(self, '_cb', None)
        if cb is not None:
            try:
                cb()
            except Exception:
                log.exception('Window close handler failed')


class WindowController:
    """One freeflo window.

    on_message   called on the main thread with each message the page posts
    html_file    page in the ui directory
    size         content size in points; None picks a size that suits the page
    on_close     called on the main thread each time the window closes
    appearance   'system' | 'light' | 'dark'; None reads the stored setting
    glass        True / False; None reads the stored setting
    """

    def __init__(self, on_message=None, html_file='index.html',
                 title='freeflo', size=None, on_close=None,
                 appearance=None, glass=None):
        self._on_message = on_message or (lambda body: None)
        self._on_close = on_close     # main thread, each time the window closes
        self._html_file = html_file
        self._title = title
        self._size = size or PAGE_SIZES.get(html_file, DEFAULT_SIZE)
        self._min_size = MIN_SIZES.get(html_file, MIN_SIZE)
        self._window = None
        # Whether the window is the key window; written by the window
        # delegate on the main thread, read from anywhere (see is_key).
        self._focus = {'key': False}
        self._drag_strip = None
        self._pasteboard = None         # None = the general pasteboard
        self._webview = None
        self._container = None
        self._delegate = None
        self._bridge = None
        self._ucc = None

        # Look. _appearance / _glass_wanted are what was asked for;
        # _glass_active is what is on screen (False while Reduce Transparency
        # or Increase Contrast is on).
        stored = None
        if appearance not in APPEARANCES or glass is None:
            stored = _stored_style()
        self._appearance = appearance if appearance in APPEARANCES else stored[0]
        self._glass_wanted = bool(glass) if glass is not None else stored[1]
        self._glass_active = False
        self._look_logged = False       # 'Window look:' is logged once, then on change
        self._backdrop = None
        self._material = 'none'       # 'glass' | 'vibrancy' | 'none'
        self._titlebar_h = _TITLEBAR_H
        self._traffic_w = _TRAFFIC_W

    def show(self):
        again = self._window is not None
        if not again:
            self._build()
        self._present()
        if again:
            # The page may have been sitting closed for a while.
            self.send('window_shown', {})

    # ------------------------------------------------------------------
    # Look — public
    # ------------------------------------------------------------------

    def set_style(self, appearance=None, glass=None):
        """Change the appearance ('system' | 'light' | 'dark') and/or glass
        (bool) of an open window. None leaves that part as it is. Safe from
        any thread; applies at once, without reopening."""
        if appearance in APPEARANCES:
            self._appearance = appearance
        if glass is not None:
            self._glass_wanted = bool(glass)
        if self._bridge is None:
            return                      # not built yet: _build applies it
        self._bridge.performSelectorOnMainThread_withObject_waitUntilDone_(
            'applyStyle:', None, False)

    @property
    def is_key(self):
        """True while this is the key window: on screen, in front, and the
        one typing goes to (it stops being key when another app is brought
        forward). Safe from any thread: nothing is asked of AppKit."""
        return bool(self._focus['key'])

    def style(self):
        """What the window looks like now (for diagnostics and tests)."""
        return {
            'glass': bool(self._glass_active),
            'glass_wanted': bool(self._glass_wanted),
            'appearance': self._appearance,
            'material': self._material if self._glass_active else 'none',
            'reduced': _accessibility_wants_opaque(),
            'titlebar': self._titlebar_h,
            'traffic': self._traffic_w,
            'accent': self._accent_hex(),
        }

    def _accent_hex(self):
        """The system accent colour as '#rrggbb' for this window's look
        (None if AppKit will not say)."""
        out = {}

        def grab():
            out['hex'] = _hex(NSColor.controlAccentColor())

        try:
            name = {'light': _NSAppearanceNameAqua,
                    'dark': _NSAppearanceNameDarkAqua}.get(self._appearance)
            appearance = NSAppearance.appearanceNamed_(name) if name else None
            if appearance is None and self._window is not None:
                appearance = self._window.effectiveAppearance()
            if appearance is None:
                appearance = NSApplication.sharedApplication().effectiveAppearance()
            appearance.performAsCurrentDrawingAppearance_(grab)
        except Exception:
            log.debug('Could not read the accent colour', exc_info=True)
        return out.get('hex') or _hex(NSColor.controlAccentColor())

    # ------------------------------------------------------------------
    # Construction (main thread)
    # ------------------------------------------------------------------

    def _build(self):
        rect = NSMakeRect(0.0, 0.0, float(self._size[0]), float(self._size[1]))

        style = (
            NSWindowStyleMaskTitled
            | NSWindowStyleMaskClosable
            | NSWindowStyleMaskMiniaturizable
            | NSWindowStyleMaskResizable
            | NSWindowStyleMaskFullSizeContentView
        )
        window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, style, NSBackingStoreBuffered, False
        )
        window.setTitle_(self._title)   # still named in Mission Control
        window.setTitlebarAppearsTransparent_(True)
        window.setTitleVisibility_(NSWindowTitleHidden)
        window.setReleasedWhenClosed_(False)
        try:
            window.setContentMinSize_(self._min_size)
        except Exception:
            pass
        self._window = window
        self._measure_titlebar()

        bridge = _Bridge.alloc().initWithCallback_(self._dispatch)
        bridge.setOwner_(self)
        ucc = WKUserContentController.alloc().init()
        ucc.addScriptMessageHandler_name_(bridge, 'freeflo')
        # Tell the page about the window before any of its own scripts run,
        # so the first paint already has the right insets and surfaces.
        ucc.addUserScript_(
            WKUserScript.alloc().initWithSource_injectionTime_forMainFrameOnly_(
                self._boot_script(), 0, True))   # 0 = at document start
        cfg = WKWebViewConfiguration.alloc().init()
        cfg.setUserContentController_(ucc)
        try:
            # Tab reaches buttons too, not only text fields (as in System
            # Settings with keyboard navigation on).
            cfg.preferences().setTabFocusesLinks_(True)
        except Exception:
            pass

        webview = WKWebView.alloc().initWithFrame_configuration_(rect, cfg)
        bridge.setWebview_(webview)
        if webview.respondsToSelector_('setUnderPageBackgroundColor:'):
            # No white flash when the page rubber-bands at the top/bottom.
            webview.setUnderPageBackgroundColor_(NSColor.clearColor())
        # The page floats on whatever is behind it (window background or the
        # glass material): the web view must not paint its own white sheet.
        try:
            webview.setValue_forKey_(False, 'drawsBackground')
        except Exception:
            log.warning('The web view cannot be made see-through; glass is off',
                        exc_info=True)
            self._glass_wanted = False
        webview.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)

        container = NSView.alloc().initWithFrame_(rect)
        container.setAutoresizesSubviews_(True)
        container.addSubview_(webview)
        # Above the web view: the title-bar area moves the window.
        w, h = float(self._size[0]), float(self._size[1])
        strip = _DragStrip.alloc().initWithFrame_(NSMakeRect(
            self._traffic_w, h - self._titlebar_h,
            max(0.0, w - self._traffic_w), self._titlebar_h))
        strip.setAutoresizingMask_(NSViewWidthSizable | NSViewMinYMargin)
        strip.setScrollTarget_(webview)
        container.addSubview_positioned_relativeTo_(strip, NSWindowAbove, webview)
        self._drag_strip = strip
        window.setContentView_(container)
        window.center()

        delegate = _WindowDelegate.alloc().initWithCallback_(self._on_close)
        delegate._focus = self._focus
        window.setDelegate_(delegate)

        self._webview = webview
        self._container = container
        self._delegate = delegate
        self._bridge = bridge
        self._ucc = ucc

        self._apply_style(None, None, notify=False)
        try:
            NSWorkspace.sharedWorkspace().notificationCenter() \
                .addObserver_selector_name_object_(
                    bridge, 'accessibilityChanged:',
                    NSWorkspaceAccessibilityDisplayOptionsDidChangeNotification,
                    None)
        except Exception:
            log.warning('Cannot follow accessibility display changes',
                        exc_info=True)
        try:
            NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(
                bridge, 'systemColorsChanged:', _COLORS_CHANGED, None)
        except Exception:
            log.warning('Cannot follow accent colour changes', exc_info=True)

        ui_dir = config.get_ui_dir()
        html = os.path.join(ui_dir, self._html_file)
        webview.loadFileURL_allowingReadAccessToURL_(
            NSURL.fileURLWithPath_(html),
            NSURL.fileURLWithPath_(ui_dir),
        )

    def _measure_titlebar(self):
        """How much of the top-left corner the traffic lights take up."""
        window = self._window
        try:
            frame = window.frame()
            layout = window.contentLayoutRect()
            h = float(frame.size.height - layout.size.height)
            if 16.0 <= h <= 120.0:
                self._titlebar_h = h
            zoom = (window.standardWindowButton_(NSWindowZoomButton)
                    or window.standardWindowButton_(NSWindowCloseButton))
            if zoom is not None:
                r = zoom.convertRect_toView_(zoom.bounds(), None)
                right = float(r.origin.x + r.size.width)
                if 20.0 <= right <= 200.0:
                    self._traffic_w = right + 9.0
        except Exception:
            log.debug('Could not measure the title bar', exc_info=True)

    def _boot_script(self):
        state = self.style()
        # The glass state at build time is not known yet (_apply_style runs
        # after this); what was asked for is the best first guess and the
        # window corrects it with a 'window_style' event if it differs.
        state['glass'] = bool(self._glass_wanted
                              and not _accessibility_wants_opaque())
        state['native'] = True
        return (
            '(function(){var s=%s;window.__freefloWindow=s;'
            'var r=document.documentElement;if(!r)return;'
            "r.style.setProperty('--titlebar-h',s.titlebar+'px');"
            "r.style.setProperty('--traffic-w',s.traffic+'px');"
            "r.setAttribute('data-glass',s.glass?'on':'off');"
            "r.setAttribute('data-theme',s.appearance);})();"
        ) % json.dumps(state)

    # ------------------------------------------------------------------
    # Look — main thread
    # ------------------------------------------------------------------

    def _dispatch(self, body):
        """Every message from the page lands here first. The window keeps the
        ones about its own look; the rest go to the app."""
        action = None
        try:
            if hasattr(body, 'get'):
                action = body.get('action')
        except Exception:
            action = None
        if action is not None and str(action) == 'copy_text':
            text = ''
            try:
                text = str(body.get('text') or '')
            except Exception:
                pass
            try:
                pb = self._pasteboard or NSPasteboard.generalPasteboard()
                pb.clearContents()
                pb.setString_forType_(text, NSPasteboardTypeString)
            except Exception:
                log.warning('Could not copy to the pasteboard', exc_info=True)
            return
        if action is not None and str(action) == 'window_style':
            appearance = glass = None
            try:
                appearance = body.get('appearance')
                glass = body.get('glass')
            except Exception:
                pass
            if appearance is not None:
                appearance = str(appearance)
                if appearance in APPEARANCES:
                    self._appearance = appearance
            if glass is not None:
                self._glass_wanted = bool(glass)
            self._apply_style(None, None)
            return
        self._on_message(body)

    def _apply_style(self, appearance=None, glass=None, notify=True):
        """Bring the native window in line with _appearance / _glass_wanted."""
        if appearance in APPEARANCES:
            self._appearance = appearance
        if glass is not None:
            self._glass_wanted = bool(glass)
        window = self._window
        if window is None:
            return

        name = {'light': _NSAppearanceNameAqua,
                'dark': _NSAppearanceNameDarkAqua}.get(self._appearance)
        try:
            window.setAppearance_(
                NSAppearance.appearanceNamed_(name) if name else None)
        except Exception:
            log.warning('Could not set the window appearance', exc_info=True)

        want = self._glass_wanted and not _accessibility_wants_opaque()
        backdrop = self._ensure_backdrop() if want else self._backdrop
        active = bool(want and backdrop is not None)
        try:
            if backdrop is not None:
                backdrop.setHidden_(not active)
            if active:
                window.setOpaque_(False)
                window.setBackgroundColor_(NSColor.clearColor())
            else:
                window.setOpaque_(True)
                window.setBackgroundColor_(NSColor.windowBackgroundColor())
            window.invalidateShadow()
        except Exception:
            log.exception('Could not switch the window material')
            active = False
        changed = active != self._glass_active or not self._look_logged
        self._glass_active = active
        if changed:
            self._look_logged = True
            log.info('Window look: %s, glass %s (%s)', self._appearance,
                     'on' if active else 'off',
                     self._material if active else 'solid')
        if notify:
            self.send('window_style', self.style())

    def _ensure_backdrop(self):
        """The material view behind the web view, created on first use."""
        if self._backdrop is not None:
            return self._backdrop
        container = self._container
        if container is None:
            return None
        bounds = container.bounds()
        view, material = None, 'none'
        choice = (os.environ.get('FREEFLO_GLASS_MATERIAL') or 'auto').lower()
        if choice in ('auto', 'glass'):
            view = self._make_glass(bounds)
            if view is not None:
                material = 'glass'
        if view is None:
            view = self._make_vibrancy(bounds)
            if view is not None:
                material = 'vibrancy'
        if view is None:
            return None
        try:
            view.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
            container.addSubview_positioned_relativeTo_(
                view, NSWindowBelow, self._webview)
        except Exception:
            log.exception('Could not install the glass material')
            return None
        self._backdrop = view
        self._material = material
        return view

    def _make_glass(self, bounds):
        """Liquid Glass (macOS 26+): NSGlassEffectView, regular style."""
        try:
            cls = objc.lookUpClass('NSGlassEffectView')
        except Exception:
            return None                 # macOS 15 and earlier
        try:
            # Two points larger all round and square-cornered: the window
            # clips it to its own rounded shape, so there is never a gap
            # between the glass and the window edge.
            frame = NSMakeRect(bounds.origin.x - 2.0, bounds.origin.y - 2.0,
                               bounds.size.width + 4.0,
                               bounds.size.height + 4.0)
            view = cls.alloc().initWithFrame_(frame)
            if view.respondsToSelector_('setStyle:'):
                view.setStyle_(0)       # NSGlassEffectViewStyleRegular
            if view.respondsToSelector_('setCornerRadius:'):
                view.setCornerRadius_(0.0)
            return view
        except Exception:
            log.warning('NSGlassEffectView could not be created', exc_info=True)
            return None

    def _make_vibrancy(self, bounds):
        """The classic translucent window: blends with what is behind it."""
        try:
            view = NSVisualEffectView.alloc().initWithFrame_(bounds)
            view.setBlendingMode_(NSVisualEffectBlendingModeBehindWindow)
            view.setMaterial_(NSVisualEffectMaterialSidebar)
            # Stay translucent when the window is not the key window.
            view.setState_(NSVisualEffectStateActive)
            return view
        except Exception:
            log.warning('NSVisualEffectView could not be created', exc_info=True)
            return None

    def _present(self):
        _ensure_main_menu()
        app = NSApplication.sharedApplication()
        app.setActivationPolicy_(NSApplicationActivationPolicyRegular)
        app.activateIgnoringOtherApps_(True)
        self._window.makeKeyAndOrderFront_(None)
        _note_presented(self._window)

    def close(self):
        """Close the window (safe from any thread)."""
        if self._window is not None:
            self._window.performSelectorOnMainThread_withObject_waitUntilDone_(
                'close', None, False
            )

    def send(self, name, payload):
        """Push an event to the web UI. Safe to call from any thread — the
        actual evaluateJavaScript hops to the main thread."""
        if self._bridge is None:
            return
        js = 'window.freefloReceive && window.freefloReceive(%s, %s)' % (
            json.dumps(name), json.dumps(payload),
        )
        self._bridge.performSelectorOnMainThread_withObject_waitUntilDone_(
            'evalJS:', js, False
        )
