"""End to end: the real pages talking to the real app.py.

    python tests/ui_app_check.py <repo root>

Loads ui/index.html and ui/onboarding.html in off-screen WKWebViews (never in
a window, Accessory activation policy) and connects them to a real
FreefloApp: page messages go to its _on_ui_message / _on_onboarding_message,
its pushes come back into the page. Under the app is the scriptable fake
helper (tests/fake_helper.py); the pill, the event tap, the setup window and
System Settings are replaced by recorders, settings live in a temp folder,
and the Keychain is never read. Prints PASS / FAIL lines and a final RESULT
line (tests/test_ui_pages.py runs it in a subprocess).
"""
import json
import os
import queue
import shutil
import sys
import tempfile
import time

REPO = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))
DATA = tempfile.mkdtemp(prefix='freeflo-ui-app-')
CTL = os.path.join(DATA, 'helper')
os.makedirs(CTL)
os.environ['FREEFLO_DATA_DIR'] = os.path.join(DATA, 'data')   # before config loads
os.environ['FAKE_HELPER_DIR'] = CTL
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, 'tests'))

from Foundation import NSDate, NSObject, NSRunLoop, NSURL          # noqa: E402
from AppKit import NSApplication, NSApplicationActivationPolicyAccessory  # noqa: E402
from WebKit import (WKUserContentController, WKUserScript, WKWebView,  # noqa: E402
                    WKWebViewConfiguration)

import config                                                       # noqa: E402
import app as app_module                                            # noqa: E402
from engine import history                                          # noqa: E402
from test_app_flow import FakeHUD, FakeHotkey, FakeWindow           # noqa: E402
from support import FAKE_HELPER                                     # noqa: E402

UI = os.path.join(REPO, 'ui')
ok = True
errors = []


def check(label, cond, detail=''):
    global ok
    ok = ok and bool(cond)
    print(('PASS ' if cond else 'FAIL ') + label
          + ((' :: ' + str(detail)[:600]) if detail and not cond else ''))


def spin(sec):
    end = time.time() + sec
    while True:
        drain()
        left = end - time.time()
        if left <= 0:
            return
        NSRunLoop.currentRunLoop().runUntilDate_(
            NSDate.dateWithTimeIntervalSinceNow_(min(0.02, left)))


def wait(pred, timeout=6.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        spin(0.03)
    return bool(pred())


# -- the bridge: page <-> app, always on the main thread ---------------------

pending = queue.Queue()      # (webview, js) pushed from any thread


class Page(object):
    """Stands in for WindowController: send() evaluates into the web view."""

    def __init__(self, wv):
        self.wv = wv
        self.sent = []
        self.closed = False
        self.styles = []

    def send(self, name, payload):
        self.sent.append((name, payload))
        pending.put((self.wv, 'window.freefloReceive && window.freefloReceive(%s, %s)'
                     % (json.dumps(name), json.dumps(payload))))

    def show(self):
        pass

    def close(self):
        self.closed = True

    def set_style(self, appearance=None, glass=None):
        self.styles.append((appearance, glass))

    def of(self, name):
        return [p for n, p in self.sent if n == name]


def drain():
    while True:
        try:
            wv, code = pending.get_nowait()
        except queue.Empty:
            return
        wv.evaluateJavaScript_completionHandler_(code, None)


class Handler(NSObject):
    def initWithTarget_(self, target):
        self = self.init()
        self.target = target
        self.posted = []
        return self

    def userContentController_didReceiveScriptMessage_(self, ucc, message):
        body = message.body()
        try:
            body = json.loads(json.dumps(dict(body), default=lambda o: list(o)
                                         if hasattr(o, '__iter__') else str(o)))
        except Exception:
            body = {}
        if message.name() == 'jserr':
            errors.append(body)
            return
        self.posted.append(body)
        if body.get('action') in ('window_style', 'copy_text'):
            return                       # ui/window.py keeps these
        self.target(body)


_keep = []


def load(page, target, boot):
    handler = Handler.alloc().initWithTarget_(target)
    ucc = WKUserContentController.alloc().init()
    ucc.addScriptMessageHandler_name_(handler, 'freeflo')
    ucc.addScriptMessageHandler_name_(handler, 'jserr')
    hook = ("window.onerror=function(m,s,l,c){window.webkit.messageHandlers.jserr"
            ".postMessage({m:String(m),l:l});};window.__freefloWindow=%s;" % json.dumps(boot))
    ucc.addUserScript_(WKUserScript.alloc().initWithSource_injectionTime_forMainFrameOnly_(
        hook, 0, True))
    cfg = WKWebViewConfiguration.alloc().init()
    cfg.setUserContentController_(ucc)
    wv = WKWebView.alloc().initWithFrame_configuration_(((0, 0), (820, 900)), cfg)
    _keep.append((wv, handler, ucc))
    return wv, handler, (lambda: wv.loadFileURL_allowingReadAccessToURL_(
        NSURL.fileURLWithPath_(os.path.join(UI, page)), NSURL.fileURLWithPath_(UI)))


def js(wv, code):
    out = {}

    def done(result, error):
        out['r'] = result
        out['e'] = error
    wv.evaluateJavaScript_completionHandler_(code, done)
    end = time.time() + 5
    while 'r' not in out and time.time() < end:
        spin(0.02)
    if out.get('e') is not None:
        errors.append({'m': 'evaluate failed: %s' % out['e'], 'code': code[:120]})
    return out.get('r')


def text(wv, sel):
    return js(wv, 'var n=document.querySelector(%s); n ? n.textContent : null' % json.dumps(sel))


def visible(wv, sel):
    return bool(js(wv, 'var n=document.querySelector(%s); !!(n && n.offsetParent !== null)'
                   % json.dumps(sel)))


def click(wv, sel):
    js(wv, 'document.querySelector(%s).click(); 1' % json.dumps(sel))
    spin(0.1)


def commands(name):
    try:
        with open(os.path.join(CTL, 'commands.log')) as f:
            cmds = [json.loads(line)['cmd'] for line in f if line.strip()]
    except OSError:
        return []
    return [c for c in cmds if c.get('cmd') == name]


BOOT = {'native': True, 'glass': True, 'glass_wanted': True, 'appearance': 'system',
        'material': 'glass', 'reduced': False, 'titlebar': 32, 'traffic': 78,
        'accent': '#989898'}

# -- the app ------------------------------------------------------------------

scenario = {'default': {
    'text': 'kal raat ko server down ho gaya',
    'install_delay': 0.15,
    'status': {'input_device': 'Test Microphone', 'max_languages': 2,
               'supported': ['en-US', 'en-IN', 'hi-IN', 'mul-IN', 'fr-FR'],
               'installed': ['en-US', 'en-IN'],
               'groups': [['hi-IN', 'mul-IN']]}}}
with open(os.path.join(CTL, 'scenario.json'), 'w') as f:
    json.dump(scenario, f)

app_module.HUD = FakeHUD
app_module.HotkeyListener = FakeHotkey
app_module.AXIsProcessTrusted = lambda: True
app_module.AXIsProcessTrustedWithOptions = lambda options: True
app_module.locales.system_locale = lambda: 'en-IN'
app_module.gauth.is_connected = lambda: False
app_module.gauth.is_configured = lambda: False
config.get_helper_path = lambda: [sys.executable, FAKE_HELPER]
opened = []
app_module.FreefloApp._open_url = lambda self, url: opened.append(url)
app_module.FreefloApp._make_setup_window = lambda self: FakeWindow()

NSApplication.sharedApplication().setActivationPolicy_(NSApplicationActivationPolicyAccessory)

# The user's config shape: onboarded on 1.5.0, English (India), glass theme.
config.save(dict(config._DEFAULTS, onboarded=True, language='en-IN',
                 theme='glass', glass=True, live_text=False))
a = app_module.FreefloApp()
try:
    check('helper status arrives', wait(lambda: a._speech_status is not None))
    a._poll_state(None)                       # setup decides: nothing to do
    check('no setup window for a set-up user', not a._setup.busy and a._ob_ui is None)
    check('the event tap was started once', a._hotkey.starts == 1, a._hotkey.starts)

    wv, handler, go = load('index.html', a._on_ui_message, BOOT)
    page = a._ui = Page(wv)
    go()
    wait(lambda: any(m.get('action') == 'ui_ready' for m in handler.posted))
    spin(0.5)
    acts = [m.get('action') for m in handler.posted]
    check('boot order', acts[:5] == ['get_theme', 'get_home', 'get_shortcuts',
                                     'get_languages', 'ui_ready'], acts[:6])
    check('theme reaches the page as glass', page.of('theme')[-1] == {
        'theme': 'glass', 'glass': True, 'appearance': 'system'}, page.of('theme'))
    check('page shows glass on', js(wv, 'document.documentElement.dataset.glass') == 'on')
    langs = page.of('languages')
    check('languages pushed', bool(langs) and langs[-1]['ready'], langs[-1:] )
    check('home: one read-only line naming the language',
          text(wv, '#homeLangCard [data-role="label"]') == 'English (India)' and
          not js(wv, "!!document.querySelector('#homeLangCard select, #homeLangCard [data-role=hint]')"),
          text(wv, '#homeLangCard'))
    check('Hindi is offered in Settings (not installed yet)',
          bool(js(wv, "!!document.querySelector('#langCard [data-role=\"hint\"] button')")),
          text(wv, '#langCard'))
    check('ready line names the key', 'Left' in (text(wv, '#statusLine') or ''),
          text(wv, '#statusLine'))

    # One click: add Hindi -> download -> used.
    click(wv, '#langCard [data-role="hint"] button')
    check('set_languages posted', handler.posted[-1] == {
        'action': 'set_languages', 'primary': 'en-IN', 'additional': ['hi-IN']},
        handler.posted[-1])
    check('saved at once', config.load()['languages'] == ['en-IN', 'hi-IN'],
          config.load().get('languages'))
    check('download started without another click',
          wait(lambda: len(commands('install')) == 1) and
          commands('install')[0]['locale'] == 'hi-IN', commands('install'))
    wait(lambda: any(p['state'] == 'downloading' and p['progress'] > 0
                     for p in page.of('language_progress')))
    spin(0.1)
    check('progress shown in the card',
          'Hindi is downloading (' in (text(wv, '#homeLangCard') or ''), text(wv, '#homeLangCard'))
    check('download finishes', wait(lambda: any(
        p['state'] == 'downloaded' for p in page.of('language_progress'))))
    spin(0.3)
    check('both languages prewarmed', wait(lambda: any(
        c.get('languages') == ['en-IN', 'hi-IN'] for c in commands('prewarm'))))
    check('settings card lists Hindi, nothing left to download',
          (text(wv, '#langCard [data-code="hi-IN"]') or '').startswith('Hindi') and
          'Download' not in (text(wv, '#langCard [data-code="hi-IN"]') or ''),
          text(wv, '#langCard'))
    check('home names both', text(wv, '#homeLangCard [data-role="label"]') == 'English (India) + Hindi',
          text(wv, '#homeLangCard'))
    check('main language unchanged', config.load()['language'] == 'en-IN')

    # Try it on Home: a real (fake-helper) dictation with both languages.
    click(wv, '#heroBtn')
    check('test_start -> recording', wait(lambda: js(wv, "document.getElementById('hero').dataset.state") == 'recording'))
    spin(0.2)
    click(wv, '#heroBtn')
    check('test result shown', wait(lambda: 'server down' in (text(wv, '#tryText') or '')),
          text(wv, '#tryText'))
    start = commands('start')[-1] if commands('start') else {}
    check('dictation used both languages', start.get('languages') == ['en-IN', 'hi-IN'], start)
    check('history has it', wait(lambda: history.list_entries(limit=1) and
                                 history.list_entries(limit=1)[0]['text'].startswith('kal')))
    check('home refreshed with the entry', wait(lambda: 'server down' in (text(wv, '#recent') or '')))

    # Settings: appearance switch reaches config, the other window and the pill.
    js(wv, "document.querySelector('.nav-btn[data-view=settings]').click(); 1")
    spin(0.4)
    check('about lists the advanced rows', visible(wv, '#setupBtn') or
          bool(page.of('about')), page.of('about'))
    ob_stub = a._ob_ui = Page(wv)            # stands in for an open setup window
    js(wv, "document.querySelector('#appearanceSeg [data-appearance=dark]').click(); 1")
    spin(0.3)
    stored = config.load()
    check('appearance saved', (stored.get('appearance'), stored.get('glass'), stored.get('theme'))
          == ('dark', True, 'glass'), (stored.get('appearance'), stored.get('glass')))
    check('setup window restyled', ob_stub.styles[-1:] == [('dark', True)], ob_stub.styles)
    check('pill restyled', ('look', 'dark', True) in a._hud.calls, a._hud.calls[-3:])
    js(wv, "document.getElementById('glassSwitch').click(); 1")
    spin(0.3)
    check('glass off saved', config.load().get('glass') is False and
          config.load().get('theme') == 'dark')
    check('pill glass off', a._hud.calls[-1] == ('look', 'dark', False), a._hud.calls[-1])
    a._ob_ui = None

    # Remove Hindi from Settings; the next dictation is English only.
    click(wv, '#langCard [data-code="hi-IN"] .x')
    check('languages back to one', config.load()['languages'] == ['en-IN'])
    # Taking Hindi off is an answer: it is not suggested again.
    spin(0.3)
    check('Hindi not suggested again', config.load().get('hindi_hint_dismissed') is True and
          not js(wv, "!!document.querySelector('#langCard [data-role=\"hint\"]')"))

    # History view.
    js(wv, "document.querySelector('.nav-btn[data-view=history]').click(); 1")
    check('history lists the dictation', wait(lambda: 'server down' in (text(wv, '#hlist') or '')))
    check('no JS errors (settings)', not errors, errors[:3])

    # -- the setup window, first run --------------------------------------
    a._ob_ui = None
    s = config.load()
    s['onboarded'] = False
    config.save(s)
    a._setup = app_module.setup_state.SetupFlow(ledger=None, version='test',
                                                onboarded=False, ptt_label='Left Option')
    wv2, handler2, go2 = load('onboarding.html', a._on_onboarding_message, BOOT)
    ob = Page(wv2)
    app_module.FreefloApp._make_setup_window = lambda self: ob
    a._hotkey.running = False
    go2()
    check('setup page asks for its state', wait(lambda: any(
        m.get('action') == 'ob_get_state' for m in handler2.posted)))
    spin(0.3)
    check('setup window opened by the flow (first run)', a._ob_ui is ob)
    st = ob.of('ob_state')[-1] if ob.of('ob_state') else {}
    check('both rows', [r['id'] for r in st.get('rows', [])] == ['mic', 'accessibility'], st)
    check('rows granted on screen', js(wv2, "document.getElementById('rowMic').dataset.state") == 'granted'
          and js(wv2, "document.getElementById('rowAx').dataset.state") == 'granted')
    a._setup_poll()                          # the tap was started ...
    spin(1.2)
    a._setup_poll()                          # ... and is live a second later
    check('hotkeys live', a._setup.ready)
    a._on_onboarding_message({'action': 'ob_get_state'})
    spin(0.3)
    check('Done enabled', js(wv2, "document.getElementById('doneBtn').disabled") is False)
    page_text = js(wv2, 'document.body.innerText') or ''
    check('no restart wording', 'restart' not in page_text.lower(), page_text[:200])
    click(wv2, '#doneBtn')
    check('first run finished', wait(lambda: config.load().get('onboarded') is True))
    check('setup window closed by the flow', ob.closed)
    check('no JS errors (setup)', not errors, errors[:3])
    check('System Settings never opened', opened == [], opened)
finally:
    try:
        a._engine.shutdown(timeout=3.0)
        a._deliver_q.put(None)
    except Exception:
        pass
    shutil.rmtree(DATA, ignore_errors=True)

print('RESULT: %s' % ('OK' if ok else 'FAIL'))
sys.exit(0 if ok else 1)
