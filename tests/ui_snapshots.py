"""Off-screen PNG snapshots of the freeflo pages, for looking at by eye.

    .venv/bin/python tests/ui_snapshots.py <out_dir> [filter]

Writes index-<scenario>-<light|dark>[-glass].png and
onboarding-<scenario>-<light|dark>[-glass].png into <out_dir>. A filter
(substring of the file name) limits what is rendered.

Not a unit test. Nothing is ever put on screen: each page is loaded in a
WKWebView inside a window that is created far off-screen and never ordered
in, and the process runs with the Accessory activation policy (no Dock icon).

Glass: the real window material (ui/window.py) is not part of the page, so a
stand-in is painted under the page: a colourful wallpaper toned the way the
system glass tones it (measured on macOS 27: light glass reads 0.58-0.95
grey, dark 0.07-0.38), i.e. washed with white in light mode and with black in
dark mode. It shows whether the page's veils keep text readable; it is not a
picture of the real material.
"""
import json
import os
import sys
import time

from Foundation import NSDate, NSObject, NSRunLoop, NSURL
from AppKit import (NSApplication, NSApplicationActivationPolicyAccessory,
                    NSAppearance, NSBackingStoreBuffered, NSBitmapImageRep,
                    NSWindow)
from WebKit import (WKSnapshotConfiguration, WKUserContentController,
                    WKUserScript, WKWebView, WKWebViewConfiguration)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UI = os.path.join(REPO, 'ui')
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ob_payloads  # noqa: E402

WALL = {
    'light': ('linear-gradient(rgba(255,255,255,.55),rgba(255,255,255,.55)),'
              'linear-gradient(135deg,#f7b267 0%,#f25c54 30%,#5b5f97 62%,#2ec4b6 100%)'),
    'dark': ('linear-gradient(rgba(0,0,0,.62),rgba(0,0,0,.62)),'
             'linear-gradient(135deg,#3a1c71 0%,#d76d77 45%,#5b8fb9 80%,#2c5364 100%)'),
}

LANGS = [
    {'id': 'en-IN', 'name': 'English (India)', 'state': 'downloaded', 'progress': 1},
    {'id': 'en-US', 'name': 'English (United States)', 'state': 'downloaded', 'progress': 1},
    {'id': 'hi-IN', 'name': 'Hindi (India)', 'state': 'downloading', 'progress': 0.42},
    {'id': 'mul-IN', 'name': 'Multilingual (India)', 'state': 'available', 'progress': 0},
    {'id': 'fr-FR', 'name': 'French (France)', 'state': 'available', 'progress': 0},
]
LANGUAGES = {'ready': True, 'error': None, 'primary': 'en-IN', 'additional': ['hi-IN'],
             'max_additional': 1, 'languages': LANGS, 'hint': None, 'note': ''}
KEYS = [{'id': 'left_option', 'label': 'Left Option (⌥)'},
        {'id': 'right_option', 'label': 'Right Option (⌥)'},
        {'id': 'right_command', 'label': 'Right Command (⌘)'},
        {'id': 'right_control', 'label': 'Right Control (⌃)'}]
SHORTCUTS = {'keys': KEYS, 'ptt_key': 'left_option', 'toggle_key': 'right_option',
             'sound': False, 'live_text': True}
NOW = int(time.time())
RECENT = [{'uuid': 'u%d' % i, 'ts': NOW - 600 - i * 4000, 'text': t, 'language': l, 'mode': m}
          for i, (t, l, m) in enumerate([
              ('Okay, so I was thinking we should move the meeting to Thursday afternoon, '
               'kyunki Wednesday ko sab log busy hain.', 'en-IN', 'ptt'),
              ('Send the revised deck to the team before lunch.', 'en-IN', 'toggle'),
              ('Mujhe kal subah das baje office pahunchna hai.', 'hi-IN', 'ptt'),
              ('Remind me to call the bank about the card limit.', 'en-IN', 'test'),
              ('Thanks, that works for me.', 'en-IN', 'ptt'),
              ('One more thing, the invoice number is 4471.', 'en-IN', 'ptt')])]
HOME = {'stats': {'today': 3, 'words_week': 1200, 'total': 40}, 'language': 'en-IN',
        'language_name': 'English (India)', 'ptt_key': 'left_option', 'enabled': True,
        'recent': RECENT, 'alert': None}

INDEX_SCENARIOS = ['home', 'home-alert', 'home-try', 'history', 'settings', 'settings-adv']
# Setup window: payloads from the real setup flow (tests/ob_payloads.py),
# plus the smallest window size (MIN_SIZES in ui/window.py) for the tallest
# first-run state, where the column has to scroll above the bottom bar.
OB_SCENARIOS = list(ob_payloads.SCENARIOS) + ['first-ax-stale-min']


class _Handler(NSObject):
    def initWithSink_(self, sink):
        self = self.init()
        self.sink = sink
        return self

    def userContentController_didReceiveScriptMessage_(self, ucc, message):
        try:
            body = dict(message.body())
        except Exception:
            body = str(message.body())
        self.sink.append((str(message.name()), body))


def spin(sec):
    NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(sec))


class Page(object):
    def __init__(self, page, mode, glass, w, h):
        self.msgs = []
        self.handler = _Handler.alloc().initWithSink_(self.msgs)
        ucc = WKUserContentController.alloc().init()
        ucc.addScriptMessageHandler_name_(self.handler, 'freeflo')
        ucc.addScriptMessageHandler_name_(self.handler, 'jserr')
        state = {'native': True, 'glass': glass, 'glass_wanted': glass, 'appearance': mode,
                 'material': 'glass' if glass else 'none', 'reduced': False,
                 'titlebar': 32, 'traffic': 78, 'accent': '#007aff'}
        boot = ("window.onerror=function(m,s,l){window.webkit.messageHandlers.jserr.postMessage({m:String(m),l:l});};"
                "(function(){var s=%s;window.__freefloWindow=s;var r=document.documentElement;if(!r)return;"
                "r.style.setProperty('--titlebar-h',s.titlebar+'px');r.style.setProperty('--traffic-w',s.traffic+'px');"
                "r.setAttribute('data-glass',s.glass?'on':'off');r.setAttribute('data-theme',s.appearance);})();"
                ) % json.dumps(state)
        ucc.addUserScript_(WKUserScript.alloc().initWithSource_injectionTime_forMainFrameOnly_(boot, 0, True))
        cfg = WKWebViewConfiguration.alloc().init()
        cfg.setUserContentController_(ucc)
        self.wv = WKWebView.alloc().initWithFrame_configuration_(((0, 0), (w, h)), cfg)
        self.wv.setValue_forKey_(False, 'drawsBackground')
        self.win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            ((-6000, -6000), (w, h)), 0, NSBackingStoreBuffered, False)
        self.win.setAppearance_(NSAppearance.appearanceNamed_(
            'NSAppearanceNameDarkAqua' if mode == 'dark' else 'NSAppearanceNameAqua'))
        self.win.setContentView_(self.wv)
        self.wv.loadFileURL_allowingReadAccessToURL_(
            NSURL.fileURLWithPath_(os.path.join(UI, page)), NSURL.fileURLWithPath_(UI))
        t = time.time()
        while time.time() - t < 6 and not any(n == 'freeflo' for n, _ in self.msgs):
            spin(0.05)
        spin(0.2)
        self.js("var st=document.createElement('style');"
                "st.textContent='*,*::before,*::after{animation:none!important;transition:none!important}';"
                "document.head.appendChild(st); 1")
        # Solid: the window background (NSColor.windowBackgroundColor) would
        # show under transparent areas; glass: the stand-in wallpaper.
        bg = WALL[mode] if glass else ('#ececec' if mode == 'light' else '#2b2b2d')
        self.js('document.documentElement.style.background=%s; 1' % json.dumps(bg))

    def js(self, code):
        out = {}
        self.wv.evaluateJavaScript_completionHandler_(code, lambda r, e: out.update(r=r, e=e))
        t = time.time()
        while 'r' not in out and time.time() - t < 5:
            spin(0.02)
        if out.get('e') is not None:
            self.msgs.append(('jserr', {'m': str(out['e'])[:300], 'code': code[:100]}))
        return out.get('r')

    def push(self, name, payload):
        self.js('window.freefloReceive(%s, %s); 1' % (json.dumps(name), json.dumps(payload)))
        spin(0.03)

    def shot(self, path):
        spin(0.4)
        out = {}
        conf = WKSnapshotConfiguration.alloc().init()
        self.wv.takeSnapshotWithConfiguration_completionHandler_(
            conf, lambda img, err: out.update(img=img, err=err))
        t = time.time()
        while 'img' not in out and time.time() - t < 8:
            spin(0.05)
        img = out.get('img')
        if img is None:
            print('snapshot failed', path, out.get('err'))
            return False
        rep = NSBitmapImageRep.imageRepWithData_(img.TIFFRepresentation())
        rep.representationUsingType_properties_(4, None).writeToFile_atomically_(path, True)
        return True

    def errors(self):
        return [b for n, b in self.msgs if n == 'jserr']


def index_scenario(p, scen, mode, glass):
    p.push('theme', {'theme': 'glass' if glass else mode, 'appearance': mode, 'glass': glass})
    p.push('shortcuts', SHORTCUTS)
    p.push('languages', LANGUAGES)
    if scen.startswith('home'):
        p.push('navigate', {'view': 'home'})
        home = dict(HOME)
        if scen == 'home-alert':
            home['alert'] = {'text': 'freeflo can’t hear you: the microphone is off for freeflo.',
                             'action': 'open_mic_settings', 'button': 'Open System Settings'}
        p.push('home', home)
        if scen == 'home-try':
            p.push('status', {'state': 'recording'})
            p.push('test_result', {'text': 'This is a quick test of freeflo, ekdum simple.'})
    elif scen == 'history':
        p.push('navigate', {'view': 'history'})
        p.push('history', {'entries': RECENT, 'save_history': True, 'saved_uuids': ['u1'],
                           'lang_names': {'en-IN': 'English (India)', 'hi-IN': 'Hindi (India)'}})
    else:
        p.push('navigate', {'view': 'settings'})
        p.push('vocabulary', {'words': ['Kiwi', 'UPI', 'freeflo', 'JusPay'],
                              'replacements': [{'heard': 'you pee eye', 'written': 'UPI'},
                                               {'heard': 'my work email', 'written': 'someone@example.com'}],
                              'error': '', 'hint': ''})
        p.push('backup_status', {'configured': True, 'connected': False})
        p.push('privacy', {'analytics': True, 'crash': False})
        p.push('turbo_status', {'enabled': True, 'style': 'natural', 'ready': False, 'available': True,
                                'lite': {'installed': False, 'size_gb': 1.1}})
        p.push('about', {'version': '1.6.0', 'can': ['reveal_logs', 'copy_diagnostics', 'open_setup', 'check_updates']})
        if scen == 'settings-adv':
            p.js("document.getElementById('advanced').open=true;"
                 "document.getElementById('advanced').scrollIntoView({block:'start'}); 1")


def ob_scenario(p, scen):
    p.push('ob_state', ob_payloads.view(scen.replace('-min', '')))
    if scen.startswith('first-done') or scen.endswith('-min'):
        p.push('ob_test_result', {'text': 'Hello, this is my first dictation.'})


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    out_dir = argv[1]
    only = argv[2] if len(argv) > 2 else ''
    os.makedirs(out_dir, exist_ok=True)
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    jobs = []
    for mode in ('light', 'dark'):
        for glass in (False, True):
            for scen in INDEX_SCENARIOS:
                jobs.append(('index.html', scen, mode, glass))
            for scen in OB_SCENARIOS:
                jobs.append(('onboarding.html', scen, mode, glass))
    bad = 0
    for page, scen, mode, glass in jobs:
        name = '%s-%s-%s%s.png' % (page.split('.')[0], scen, mode, '-glass' if glass else '')
        if only and only not in name:
            continue
        if page == 'index.html':
            w, h = (820, 1950) if scen == 'settings' else (820, 620)
        else:
            w, h = (480, 560) if scen.endswith('-min') else (540, 640)
        p = Page(page, mode, glass, w, h)
        if page == 'index.html':
            index_scenario(p, scen, mode, glass)
        else:
            ob_scenario(p, scen)
        path = os.path.join(out_dir, name)
        ok = p.shot(path)
        errs = p.errors()
        print('wrote' if ok else 'FAILED', path, ('JS errors: %s' % errs) if errs else '')
        bad += (not ok) or bool(errs)
        p.win.close()
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
