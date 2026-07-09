"""The freeflo window: a native NSWindow hosting a WKWebView + a JS<->Python
bridge.

Constraints (verified during design review):
  * The app is LSUIElement (Accessory policy) — a window will not become key
    and cannot receive keystrokes unless we switch to Regular policy first.
  * All WKWebView work (creation, evaluateJavaScript) must happen on the main
    thread. send() marshals onto the main thread via performSelectorOnMainThread.
  * Every ObjC object we create (window, webview, content controller, bridge,
    delegate) is held by a long-lived Python reference or PyObjC collects it.
"""
import json
import os

import objc
from Foundation import NSObject, NSURL
from AppKit import (
    NSApplication,
    NSApplicationActivationPolicyRegular,
    NSApplicationActivationPolicyAccessory,
    NSWindow,
    NSWindowStyleMaskTitled,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskResizable,
    NSBackingStoreBuffered,
)
from WebKit import WKWebView, WKWebViewConfiguration, WKUserContentController

import config


class _Bridge(NSObject):
    """Receives JS->Python messages (WKScriptMessageHandler) and runs
    Python->JS evaluateJavaScript on the main thread."""

    def initWithCallback_(self, cb):
        self = objc.super(_Bridge, self).init()
        if self is None:
            return None
        self._cb = cb
        self._webview = None
        return self

    def setWebview_(self, wv):
        self._webview = wv

    # --- JS -> Python. WebKit delivers this on the main thread. ---
    def userContentController_didReceiveScriptMessage_(self, ucc, message):
        try:
            self._cb(message.body())
        except Exception:
            pass

    # --- Python -> JS. Always invoked on the main thread (see send()). ---
    def evalJS_(self, js):
        if self._webview is not None:
            self._webview.evaluateJavaScript_completionHandler_(js, None)


class _WindowDelegate(NSObject):
    """Reverts the app to Accessory (hides the temporary Dock icon) on close."""

    def windowWillClose_(self, notification):
        NSApplication.sharedApplication().setActivationPolicy_(
            NSApplicationActivationPolicyAccessory
        )


class WindowController:
    def __init__(self, on_message=None):
        self._on_message = on_message or (lambda body: None)
        self._window = None
        self._webview = None
        self._delegate = None
        self._bridge = None
        self._ucc = None

    def show(self):
        if self._window is None:
            self._build()
        self._present()

    def _build(self):
        rect = ((0.0, 0.0), (920.0, 660.0))

        bridge = _Bridge.alloc().initWithCallback_(self._on_message)
        ucc = WKUserContentController.alloc().init()
        ucc.addScriptMessageHandler_name_(bridge, 'freeflo')
        cfg = WKWebViewConfiguration.alloc().init()
        cfg.setUserContentController_(ucc)

        webview = WKWebView.alloc().initWithFrame_configuration_(rect, cfg)
        bridge.setWebview_(webview)

        style = (
            NSWindowStyleMaskTitled
            | NSWindowStyleMaskClosable
            | NSWindowStyleMaskResizable
        )
        window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, style, NSBackingStoreBuffered, False
        )
        window.setTitle_('freeflo')
        window.center()
        window.setReleasedWhenClosed_(False)
        window.setContentView_(webview)

        delegate = _WindowDelegate.alloc().init()
        window.setDelegate_(delegate)

        ui_dir = config.get_ui_dir()
        html = os.path.join(ui_dir, 'index.html')
        webview.loadFileURL_allowingReadAccessToURL_(
            NSURL.fileURLWithPath_(html),
            NSURL.fileURLWithPath_(ui_dir),
        )

        self._window = window
        self._webview = webview
        self._delegate = delegate
        self._bridge = bridge
        self._ucc = ucc

    def _present(self):
        app = NSApplication.sharedApplication()
        app.setActivationPolicy_(NSApplicationActivationPolicyRegular)
        app.activateIgnoringOtherApps_(True)
        self._window.makeKeyAndOrderFront_(None)

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
