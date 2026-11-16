"""Headless check of the settings / onboarding pages: load them in an
off-screen WKWebView, push the payloads Python sends, read the DOM back and
the messages the page posts.

    python tests/ui_pages_check.py <repo root>

Prints PASS / FAIL lines and a final RESULT line (tests/test_ui_pages.py runs
it in a subprocess). Never shows a window: the web views are not in any
window, and the process uses the Accessory activation policy (no Dock icon).
"""
import json
import os
import re
import sys
import time

from Foundation import NSDate, NSObject, NSRunLoop, NSURL
from AppKit import NSApplication, NSApplicationActivationPolicyAccessory
from WebKit import (WKUserContentController, WKUserScript, WKWebView,
                    WKWebViewConfiguration)

REPO = sys.argv[1]
UI = os.path.join(REPO, 'ui')
messages = []
errors = []

# --------------------------------------------------------------------------
# The page <-> Python contract (app.py and ui/window.py). A page action or
# event outside these sets fails the static check below.
# --------------------------------------------------------------------------
INDEX_ACTIONS = {
    'ui_ready', 'test_start', 'test_stop', 'get_shortcuts', 'set_shortcuts',
    'install_language', 'set_sound', 'set_live_text', 'open_mic_settings',
    'ack_language_notice', 'get_vocabulary', 'vocab_add_word', 'vocab_remove_word',
    'vocab_add_replacement', 'vocab_remove_replacement', 'get_history',
    'clear_history', 'set_save_history', 'get_privacy', 'set_privacy',
    'get_backup_status', 'set_backup_enabled', 'google_connect',
    'google_disconnect', 'backup_sync_now', 'get_home',
    'get_theme', 'set_theme', 'feature_request', 'get_turbo', 'set_turbo',
    'set_turbo_style', 'download_model', 'get_saved_prompts',
    'save_prompt_from_history', 'unsave_prompt', 'add_manual_prompt',
    'remove_saved_prompt',
    # added in this version (see the integration notes)
    'get_languages', 'set_languages', 'get_about', 'reveal_logs',
    'copy_diagnostics', 'open_setup', 'check_updates', 'download_update',
    # handled inside ui/window.py, never reach app.py
    'window_style', 'copy_text',
}
INDEX_EVENTS = {
    'navigate', 'status', 'test_result', 'home', 'history', 'saved_prompts',
    'saved_uuids', 'shortcuts', 'shortcuts_saved', 'save_history_state',
    'backup_status', 'privacy', 'theme', 'feature_ack', 'turbo_status',
    'turbo_progress', 'vocabulary',
    'languages', 'language_progress', 'about', 'update_status',
    'window_style', 'window_shown',
}
OB_ACTIONS = {'ob_get_state', 'ob_get_state_fresh', 'ob_grant_mic',
              'ob_grant_accessibility', 'ob_set_consent', 'ob_test_start',
              'ob_test_stop', 'ob_complete'}
OB_EVENTS = {'ob_state', 'ob_test_state', 'ob_test_result',
             'window_style', 'window_shown'}

BOOT = {'native': True, 'glass': False, 'glass_wanted': False, 'appearance': 'system',
        'material': 'none', 'reduced': False, 'titlebar': 32, 'traffic': 78, 'accent': '#989898'}


class Handler(NSObject):
    def userContentController_didReceiveScriptMessage_(self, ucc, message):
        body = message.body()
        try:
            body = json.loads(json.dumps(dict(body), default=lambda o: list(o) if hasattr(o, '__iter__') else str(o)))
        except Exception:
            body = str(body)
        if message.name() == 'jserr':
            errors.append(body)
        else:
            messages.append(body)


def spin(sec):
    NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(sec))


_keep = []


def load(page, boot=BOOT, size=(820, 620)):
    handler = Handler.alloc().init()
    ucc = WKUserContentController.alloc().init()
    ucc.addScriptMessageHandler_name_(handler, 'freeflo')
    ucc.addScriptMessageHandler_name_(handler, 'jserr')
    hook = ("window.onerror=function(m,s,l,c){window.webkit.messageHandlers.jserr.postMessage({m:String(m),l:l});};"
            "window.addEventListener('unhandledrejection',function(e){window.webkit.messageHandlers.jserr.postMessage({m:String(e.reason)});});")
    if boot is not None:
        hook += 'window.__freefloWindow=%s;' % json.dumps(boot)
    ucc.addUserScript_(WKUserScript.alloc().initWithSource_injectionTime_forMainFrameOnly_(hook, 0, True))
    cfg = WKWebViewConfiguration.alloc().init()
    cfg.setUserContentController_(ucc)
    wv = WKWebView.alloc().initWithFrame_configuration_(((0, 0), size), cfg)
    wv.loadFileURL_allowingReadAccessToURL_(NSURL.fileURLWithPath_(os.path.join(UI, page)), NSURL.fileURLWithPath_(UI))
    t = time.time()
    while time.time() - t < 6 and not messages:
        spin(0.05)
    spin(0.3)
    _keep.append((wv, handler, ucc))
    return wv


def js(wv, code):
    out = {}

    def done(result, error):
        out['r'] = result
        out['e'] = error
    wv.evaluateJavaScript_completionHandler_(code, done)
    t = time.time()
    while 'r' not in out and time.time() - t < 5:
        spin(0.02)
    if out.get('e') is not None:
        errors.append({'m': 'evaluate failed: %s' % out['e'], 'code': code[:160]})
    return out.get('r')


def push(wv, name, payload):
    js(wv, 'window.freefloReceive(%s, %s); 1' % (json.dumps(name), json.dumps(payload)))
    spin(0.05)


def click(wv, selector):
    js(wv, 'document.querySelector(%s).click(); 1' % json.dumps(selector))
    spin(0.05)


def text(wv, selector):
    return js(wv, 'var n=document.querySelector(%s); n ? n.textContent : null' % json.dumps(selector))


def visible(wv, selector):
    return bool(js(wv, 'var n=document.querySelector(%s); !!(n && n.offsetParent !== null && !n.closest("[hidden]"))' % json.dumps(selector)))


def select(wv, selector, value):
    js(wv, 'var s=document.querySelector(%s); s.value=%s; s.dispatchEvent(new Event("change")); 1'
       % (json.dumps(selector), json.dumps(value)))
    spin(0.05)


def since(n):
    return messages[n:]


def acts(n=0):
    return [m.get('action') for m in messages[n:] if isinstance(m, dict)]


NSApplication.sharedApplication().setActivationPolicy_(NSApplicationActivationPolicyAccessory)
ok = True


def check(label, cond, detail=''):
    global ok
    ok = ok and bool(cond)
    print(('PASS ' if cond else 'FAIL ') + label + ((' :: ' + str(detail)[:600]) if detail and not cond else ''))


# ==========================================================================
# Static: every action / event a page uses is in the contract
# ==========================================================================
def page_contract(src):
    sent = set(re.findall(r"send\('([a-z_]+)'", src))
    handlers = re.search(r'const HANDLERS = \{(.*?)\n  \};', src, re.S)
    events = set(re.findall(r'^\s*([a-z_]+):', handlers.group(1), re.M)) if handlers else set()
    return sent, events


src_index = open(os.path.join(UI, 'index.html'), encoding='utf-8').read()
src_ob = open(os.path.join(UI, 'onboarding.html'), encoding='utf-8').read()
sent, events = page_contract(src_index)
check('index: every action is in the contract', sent and sent <= INDEX_ACTIONS, sorted(sent - INDEX_ACTIONS))
check('index: every handled event is in the contract', events and events <= INDEX_EVENTS, sorted(events - INDEX_EVENTS))
check('index: handles every event Python sends', INDEX_EVENTS - {'window_shown'} <= events | {'window_shown'} and INDEX_EVENTS <= events,
      sorted(INDEX_EVENTS - events))
sent, events = page_contract(src_ob)
check('onboarding: every action is in the contract', sent and sent <= OB_ACTIONS, sorted(sent - OB_ACTIONS))
check('onboarding: every handled event is in the contract', events and events <= OB_EVENTS, sorted(events - OB_EVENTS))
check('onboarding: handles ob_state / ob_test_state / ob_test_result / window_style',
      {'ob_state', 'ob_test_state', 'ob_test_result', 'window_style'} <= events, events)

# Both ways round. Every message goes out through send('<literal>'), so the
# lists above are complete: the only computed name is the setup page's
# send(GRANT[id]), whose two values are checked here. And nothing in the
# contract is dead: each action is really sent, each event really handled.
def computed_sends(src):
    return [m for m in re.findall(r"\bsend\(\s*([^'\s][^,)]*)", src) if m not in ('action', 'action, extra')]


index_sent, index_events = page_contract(src_index)
ob_sent, ob_events = page_contract(src_ob)
grant = re.search(r"const GRANT = \{([^}]*)\}", src_ob)
grant_names = set(re.findall(r"'(ob_[a-z_]+)'", grant.group(1))) if grant else set()
check('index: no message name is computed', computed_sends(src_index) == [], computed_sends(src_index))
check('onboarding: the only computed message name is GRANT[id]', computed_sends(src_ob) == ['GRANT[id]'], computed_sends(src_ob))
check('onboarding: GRANT holds the two permission messages', grant_names == {'ob_grant_mic', 'ob_grant_accessibility'}, grant_names)
check('index: every action in the contract is sent', INDEX_ACTIONS <= index_sent, sorted(INDEX_ACTIONS - index_sent))
check('index: every event in the contract is handled', INDEX_EVENTS <= index_events, sorted(INDEX_EVENTS - index_events))
check('onboarding: every action in the contract is sent', OB_ACTIONS <= ob_sent | grant_names, sorted(OB_ACTIONS - ob_sent - grant_names))
check('onboarding: every event in the contract is handled', OB_EVENTS <= ob_events, sorted(OB_EVENTS - ob_events))

# ...and against app.py itself: it handles what the pages send and the pages
# handle what it pushes (window_style / copy_text / window_shown belong to
# ui/window.py and never reach app.py).
src_app = open(os.path.join(REPO, 'app.py'), encoding='utf-8').read()


def app_handles(method):
    a = src_app.index('    def %s(' % method)
    body = src_app[a:src_app.index('\n    def ', a + 10)]
    names = set(re.findall(r"action == '([a-z_]+)'", body))
    for group in re.findall(r"action in \(([^)]*)\)", body):
        names |= set(re.findall(r"'([a-z_]+)'", group))
    return names


ui_handled = app_handles('_on_ui_message')
ob_handled = app_handles('_on_onboarding_message') | set(re.findall(r"'(ob_[a-z_]+)': setup_state\.ACT_", src_app))
WINDOW_ONLY = {'window_style', 'copy_text'}
check('app.py handles every action index.html sends', index_sent - WINDOW_ONLY <= ui_handled, sorted(index_sent - WINDOW_ONLY - ui_handled))
check('app.py handles every action onboarding.html sends', (ob_sent | grant_names) - WINDOW_ONLY <= ob_handled,
      sorted((ob_sent | grant_names) - WINDOW_ONLY - ob_handled))
check('index.html handles every event app.py pushes',
      set(re.findall(r"_push_ui\('([a-z_]+)'", src_app)) <= index_events,
      sorted(set(re.findall(r"_push_ui\('([a-z_]+)'", src_app)) - index_events))
check('onboarding.html handles every event app.py pushes',
      set(re.findall(r"_push_ob\('([a-z_]+)'", src_app)) <= ob_events,
      sorted(set(re.findall(r"_push_ob\('([a-z_]+)'", src_app)) - ob_events))


def token_block(src):
    a = src.find('/* tokens:start */')
    b = src.find('/* tokens:end */')
    return src[a:b] if a >= 0 and b > a else None


check('token blocks of both pages are identical',
      token_block(src_index) is not None and token_block(src_index) == token_block(src_ob))
check('no monospace / uppercase / gradient text in either page',
      not re.search(r'monospace|text-transform:\s*uppercase|background-clip:\s*text', src_index + src_ob))

# ==========================================================================
# index.html
# ==========================================================================
wv = load('index.html')
check('index boot order', acts()[:5] == ['get_theme', 'get_home', 'get_shortcuts', 'get_languages', 'ui_ready'], acts())
check('index: no window_style at boot', 'window_style' not in acts(), acts())
check('index: three areas', js(wv, "document.querySelectorAll('.nav-btn').length") == 3 and
      js(wv, "document.querySelectorAll('.view').length") == 3 and
      js(wv, "[...document.querySelectorAll('.nav-btn')].map(b=>b.dataset.view).join('|')") == 'home|history|settings')
check('index: no controls in the sidebar besides the three areas',
      js(wv, "document.querySelectorAll('.side button, .side select, .side input').length") == 3)

n = len(messages)
push(wv, 'navigate', {'view': 'settings'})
check('navigate settings fetches its data',
      js(wv, "document.getElementById('v-settings').classList.contains('on')") and
      acts(n) == ['get_shortcuts', 'get_languages', 'get_vocabulary', 'get_backup_status', 'get_privacy',
                  'get_turbo', 'get_theme', 'get_about'], acts(n))
push(wv, 'navigate', {'view': 'saved'})
check('alias saved -> History, Saved tab',
      js(wv, "document.getElementById('v-history').classList.contains('on')") and
      js(wv, "document.querySelector('#histTabs [data-tab=saved]').getAttribute('aria-pressed')") == 'true' and
      acts(len(messages) - 1) == ['get_saved_prompts'])
push(wv, 'navigate', {'view': 'request'})
spin(0.1)
check('alias request -> Settings, Advanced open, feedback focused',
      js(wv, "document.getElementById('v-settings').classList.contains('on')") and
      js(wv, "document.getElementById('advanced').open") is True and
      js(wv, "document.activeElement.id") == 'fbText')
js(wv, "document.getElementById('advanced').open=false; 1")
push(wv, 'navigate', {'view': 'settings', 'section': 'language'})
check('navigate with a section', js(wv, "document.getElementById('v-settings').classList.contains('on')"))

# ---- languages: before the first `languages` event ----
keys = [{'id': 'left_option', 'label': 'Left Option (⌥)'}, {'id': 'right_option', 'label': 'Right Option (⌥)'},
        {'id': 'right_command', 'label': 'Right Command (⌘)'}]
base = {'keys': keys, 'ptt_key': 'left_option', 'toggle_key': 'right_option', 'sound': False, 'live_text': False}
HOME0 = {'stats': {}, 'language': 'en-IN', 'language_name': 'English (India)', 'ptt_key': 'left_option',
         'enabled': True, 'recent': [], 'alert': None}
push(wv, 'home', HOME0)
push(wv, 'shortcuts', base)
check('not ready: main select disabled with the current name, foot says loading',
      js(wv, "document.querySelector('#langCard select[data-role=main]').disabled") is True and
      text(wv, '#langCard select[data-role=main] option') == 'English (India)' and
      text(wv, '#langFoot') == 'Loading languages…' and not js(wv, "!!document.getElementById('homeLangFoot')"))
check('not ready: no second-language rows, nothing to press',
      js(wv, "document.querySelectorAll('#langCard [data-code], #langCard select[data-role=add], #langCard button').length") == 0)
check('not ready: Home names the language', text(wv, '#homeLangCard [data-role=label]') == 'English (India)')
# The language fields older versions put in `shortcuts` mean nothing now.
push(wv, 'shortcuts', dict(base, sound=True, language='fr-FR', language_name='French', languages_ready=True,
                           languages=[{'code': 'fr-FR', 'name': 'French (France)', 'installed': True}]))
check('shortcuts carries keys and switches only: the picker is still loading',
      js(wv, "document.querySelector('#langCard select[data-role=main]').disabled") is True and
      text(wv, '#langCard select[data-role=main] option') == 'English (India)' and
      text(wv, '#langFoot') == 'Loading languages…')
check('switches reflect settings', js(wv, "document.getElementById('soundSwitch').getAttribute('aria-checked')") == 'true' and
      js(wv, "document.getElementById('liveSwitch').getAttribute('aria-checked')") == 'false')
check('the old language_install event is not handled any more',
      js(wv, "window.freefloReceive('language_install', {code: 'hi-IN', state: 'done'}); document.getElementById('langMsg').textContent") == '')
push(wv, 'languages', {'ready': True, 'error': 'freeflo needs macOS 26 or later', 'primary': 'en-IN',
                       'additional': [], 'max_additional': 0, 'languages': []})
check('error: foot shows it, controls disabled', text(wv, '#langFoot') == 'freeflo needs macOS 26 or later' and
      'err' in js(wv, "document.getElementById('langFoot').className") and
      js(wv, "[...document.querySelectorAll('#langCard button, #langCard select')].every(c=>c.disabled)") is True)

# ---- languages: the list from Python ----
LANGS = [{'id': 'en-IN', 'name': 'English (India)', 'state': 'downloaded', 'progress': 1},
         {'id': 'en-US', 'name': 'English (United States)', 'state': 'downloaded', 'progress': 1},
         {'id': 'hi-IN', 'name': 'Hindi (India)', 'state': 'downloading', 'progress': 0.42},
         {'id': 'mul-IN', 'name': 'Multilingual (India)', 'state': 'available', 'progress': 0},
         {'id': 'fr-FR', 'name': 'French (France)', 'state': 'available', 'progress': 0}]
L = {'ready': True, 'error': None, 'primary': 'en-IN', 'additional': [], 'max_additional': 2,
     'languages': LANGS, 'hint': None, 'note': ''}
push(wv, 'languages', L)
check('list: main select groups', js(wv, "[...document.querySelectorAll('#langCard select[data-role=main] optgroup')].map(g=>g.label+':'+[...g.children].map(o=>o.value).join(',')).join('|')")
      == 'Ready to use:en-IN,en-US|Download to use:hi-IN,mul-IN,fr-FR')
addopts = js(wv, "[...document.querySelectorAll('#langCard select[data-role=add] option')].map(o=>o.value+'='+o.textContent).join('|')")
check('list: add options exclude main, mark downloads',
      addopts == '=None|en-US=English (United States)|hi-IN=Hindi (India) (download)|mul-IN=Multilingual (India) (download)|fr-FR=French (France) (download)', addopts)
push(wv, 'home', {'stats': {}, 'language': 'fr-FR', 'language_name': 'French', 'ptt_key': 'left_option', 'enabled': True, 'recent': [], 'alert': None})
check('list: home does not change the picker', js(wv, "document.querySelector('#langCard select[data-role=main]').value") == 'en-IN')
check('home: one read-only line, no picker, no remove, no footnote',
      text(wv, '#homeLangCard [data-role=label]') == 'English (India)' and
      js(wv, "document.querySelectorAll('#homeLangCard select, #homeLangCard .x, #homeLangCard [data-role=hint]').length") == 0 and
      text(wv, '#homeLangCard [data-role=change]') == 'Change…')
n = len(messages)
click(wv, '#homeLangCard [data-role=change]')
check('home: Change… opens Settings at Language', js(wv, "document.getElementById('v-settings').classList.contains('on')") is True)
js(wv, "go('home'); 1")

n = len(messages)
select(wv, '#langCard select[data-role=add]', 'hi-IN')
check('add sends set_languages', since(n) == [{'action': 'set_languages', 'primary': 'en-IN', 'additional': ['hi-IN']}], since(n))
push(wv, 'languages', dict(L, additional=['hi-IN', 'mul-IN']))
rows = js(wv, "[...document.querySelectorAll('#langCard [data-code]')].map(r=>r.dataset.code+':'+r.querySelector('.lang-badge').textContent).join('|')")
check('additional rows show Downloading NN% / Download', rows == 'hi-IN:Downloading 42%|mul-IN:Download', rows)
check('limit reached: no add row, and no sentence about it',
      js(wv, "document.querySelectorAll('#langCard select[data-role=add]').length") == 0 and
      text(wv, '#langFoot') == '', text(wv, '#langFoot'))
# a download that did not finish: the same button says Try again
push(wv, 'languages', dict(L, additional=['mul-IN'], failed={'mul-in': 'No network.'}))
check('failed download (from the list): Try again', text(wv, '#langCard [data-code="mul-IN"] .lang-badge') == 'Try again')
n = len(messages)
click(wv, '#langCard [data-code="mul-IN"] [data-download]')
check('Try again sends install_language and shows Downloading',
      since(n) == [{'action': 'install_language', 'id': 'mul-IN', 'code': 'mul-IN'}] and
      'Downloading' in (text(wv, '#langCard [data-code="mul-IN"] .lang-badge') or ''), since(n))
push(wv, 'language_progress', {'id': 'mul-IN', 'state': 'failed', 'message': 'No network.'})
check('failed download (live): Try again + the message', text(wv, '#langCard [data-code="mul-IN"] .lang-badge') == 'Try again' and
      text(wv, '#langMsg') == 'No network.')
push(wv, 'languages', dict(L, primary='mul-IN', additional=[], failed={'mul-IN': 'No network.'}))
check('failed main language: says so, Try again',
      'The download did not finish.' in (text(wv, '#langCard [data-role=main-state]') or '') and
      text(wv, '#langCard [data-role=main-state] [data-download]') == 'Try again')
push(wv, 'languages', dict(L, additional=['hi-IN', 'mul-IN']))
js(wv, "setLangMsg(''); 1")
check('second language row: the language, what it is underneath, Remove on the right',
      text(wv, '#langCard [data-code="hi-IN"] .row-label') == 'Hindi (India)Second language' and
      js(wv, "document.querySelector('#langCard [data-code=\"hi-IN\"] .x').getAttribute('aria-label')") == 'Remove Hindi (India)')
push(wv, 'languages', dict(L, additional=[]))
check('no second language yet: the row is there, set to None',
      text(wv, '#langCard select[data-role=add]').startswith('None') and
      js(wv, "document.querySelector('#langCard select[data-role=add]').getAttribute('aria-label')") == 'Second language' and
      js(wv, "document.querySelector('#langCard select[data-role=add]').closest('.row').querySelector('.row-label').textContent") == 'Second language')
push(wv, 'languages', dict(L, additional=['hi-IN', 'mul-IN']))
home_txt = text(wv, '#homeLangCard') or ''
check('home: names every language and says which is not here yet',
      text(wv, '#homeLangCard [data-role=label]') == 'English (India) + Hindi (India) + Multilingual (India)' and
      'Hindi (India) is downloading (42%).' in home_txt and 'Multilingual (India) isn’t downloaded yet.' in home_txt, home_txt)
n = len(messages)
click(wv, '#langCard [data-code="mul-IN"] [data-download]')
check('Download sends install_language {id, code}', since(n) == [{'action': 'install_language', 'id': 'mul-IN', 'code': 'mul-IN'}], since(n))
push(wv, 'languages', dict(L, additional=['hi-IN', 'mul-IN']))
n = len(messages)
click(wv, '#langCard [data-code="hi-IN"] .x')
check('remove sends set_languages without it', since(n) == [{'action': 'set_languages', 'primary': 'en-IN', 'additional': ['mul-IN']}], since(n))
push(wv, 'languages', dict(L, additional=['hi-IN']))
n = len(messages)
select(wv, '#langCard select[data-role=main]', 'hi-IN')
check('main change to an additional language swaps them',
      since(n) == [{'action': 'set_languages', 'primary': 'hi-IN', 'additional': ['en-IN']}], since(n))
push(wv, 'languages', dict(L, additional=['hi-IN']))
n = len(messages)
select(wv, '#langCard select[data-role=main]', 'fr-FR')
check('main change to another language keeps the others',
      since(n) == [{'action': 'set_languages', 'primary': 'fr-FR', 'additional': ['hi-IN']}], since(n))
check('a not-downloaded main shows Downloading at once (no second click)',
      'Downloading' in (text(wv, '#langCard [data-role=main-state]') or ''), text(wv, '#langCard [data-role=main-state]'))
# Python keeps the old main language until the new one is downloaded.
push(wv, 'languages', dict(L, primary='en-IN', additional=['hi-IN'], pending='fr-FR'))
check('pending main: old one stays selected, the new one downloads',
      js(wv, "document.querySelector('#langCard select[data-role=main]').value") == 'en-IN' and
      'Downloading French (France)… 0%. freeflo switches to it when it is ready.' == text(wv, '#langCard [data-role=main-state]') and
      'Switching to French (France) when its download is done' in (text(wv, '#homeLangCard') or ''),
      (text(wv, '#langCard [data-role=main-state]'), text(wv, '#homeLangCard')))
push(wv, 'languages', dict(L, primary='en-IN', additional=['hi-IN']))
check('main downloaded: no badge', text(wv, '#langCard [data-role=main-state]') == '')
push(wv, 'languages', dict(L, primary='mul-IN', additional=[]))
check('main Needs a download + button', 'Needs a one-time download.' in text(wv, '#langCard [data-role=main-state]') and
      js(wv, "!!document.querySelector('#langCard [data-role=main-state] [data-download=\"mul-IN\"]')"))
push(wv, 'languages', dict(L, additional=['hi-IN']))
push(wv, 'language_progress', {'id': 'hi-IN', 'name': 'Hindi (India)', 'state': 'downloading', 'progress': 0.77})
check('language_progress downloading', text(wv, '#langCard [data-code="hi-IN"] .lang-badge') == 'Downloading 77%' and
      js(wv, "document.querySelector('#langCard [data-code=\"hi-IN\"] .bar i').style.width") == '77%')
push(wv, 'language_progress', {'id': 'hi-IN', 'name': 'Hindi (India)', 'state': 'downloaded', 'progress': 1})
check('language_progress downloaded: badge goes away', text(wv, '#langCard [data-code="hi-IN"] .lang-badge') == '' and
      text(wv, '#langMsg') == 'Hindi (India) is ready.')
push(wv, 'language_progress', {'id': 'mul-IN', 'name': 'Multilingual (India)', 'state': 'failed', 'progress': 0})
check('language_progress failed', text(wv, '#langMsg') == 'Couldn’t download Multilingual (India). Check your internet connection and try again.' and
      'err' in js(wv, "document.getElementById('langMsg').className"))
push(wv, 'languages', dict(L, additional=[], max_additional=1,
                           hint={'id': 'mul-IN', 'title': 'Mix English and Hindi?', 'text': 'Listens for both in one sentence.',
                                 'button': 'Use both', 'role': 'additional'}))
check('hint row from Python', text(wv, '#langCard [data-role=hint] .row-label') == 'Mix English and Hindi?Listens for both in one sentence.' and
      text(wv, '#langCard [data-role=hint] button') == 'Use both')
n = len(messages)
click(wv, '#langCard [data-role=hint] button')
check('hint role additional adds it', since(n) == [{'action': 'set_languages', 'primary': 'en-IN', 'additional': ['mul-IN']}], since(n))
push(wv, 'languages', dict(L, additional=[], max_additional=0, hint={'id': 'mul-IN', 'title': 'Switch?', 'role': 'primary'}))
n = len(messages)
click(wv, '#langCard [data-role=hint] button')
check('hint role primary changes main', since(n) == [{'action': 'set_languages', 'primary': 'mul-IN', 'additional': []}], since(n))
push(wv, 'languages', dict(L, max_additional=0))
check('max_additional 0: no extra rows', js(wv, "document.querySelectorAll('#langCard [data-code], #langCard select[data-role=add]').length") == 0)
push(wv, 'languages', dict(L, ready=False))
check('list not ready', js(wv, "document.querySelector('#langCard select[data-role=main]').disabled") is True and text(wv, '#langFoot') == 'Loading languages…')
push(wv, 'languages', dict(L, error='Speech is not available on this Mac.'))
check('list error', text(wv, '#langFoot') == 'Speech is not available on this Mac.')
push(wv, 'languages', dict(L, note='Hindi is used when you speak it.'))
check('note shown in the foot', text(wv, '#langFoot') == 'Hindi is used when you speak it.')

# ---- shortcuts ----
push(wv, 'shortcuts', base)
check('shortcut selects', js(wv, "document.getElementById('pttKey').value") == 'left_option' and
      js(wv, "document.getElementById('toggleKey').value") == 'right_option')
n = len(messages)
select(wv, '#pttKey', 'right_command')
check('change ptt key', since(n) == [{'action': 'set_shortcuts', 'ptt_key': 'right_command', 'toggle_key': 'right_option'}], since(n))
n = len(messages)
select(wv, '#toggleKey', 'right_command')
check('picking the other key swaps them', since(n) == [{'action': 'set_shortcuts', 'ptt_key': 'right_option', 'toggle_key': 'right_command'}], since(n))
n = len(messages)
push(wv, 'shortcuts_saved', {'ok': False, 'error': 'That key is taken.'})
check('shortcuts_saved error shown and refetched', text(wv, '#scMsg') == 'That key is taken.' and acts(n) == ['get_shortcuts'])
n = len(messages)
click(wv, '#liveSwitch')
click(wv, '#soundSwitch')
check('switches send their settings', since(n) == [{'action': 'set_live_text', 'on': True}, {'action': 'set_sound', 'on': True}], since(n))

# ---- vocabulary ----
push(wv, 'vocabulary', {'words': ['Kiwi', '<b>UPI</b>'], 'replacements': [{'heard': 'you pee eye', 'written': 'UPI'}],
                        'error': '', 'hint': ''})
check('words rendered as text (no HTML injection)',
      js(wv, "[...document.querySelectorAll('#vWords .chip span')].map(s=>s.textContent).join('|')") == 'Kiwi|<b>UPI</b>' and
      js(wv, "document.querySelectorAll('#vWords b').length") == 0)
n = len(messages)
js(wv, "document.getElementById('vWord').value='  Rachit '; document.getElementById('vWordAdd').click(); 1")
js(wv, "document.getElementById('vHeard').value='my mail'; document.getElementById('vWritten').value='a@b.co'; document.getElementById('vRepAdd').click(); 1")
js(wv, "document.querySelector('#vWords .chip button').click(); document.querySelector('#vReps .rep button').click(); 1")
check('vocabulary actions', since(n) == [
    {'action': 'vocab_add_word', 'word': 'Rachit'},
    {'action': 'vocab_add_replacement', 'heard': 'my mail', 'written': 'a@b.co'},
    {'action': 'vocab_remove_word', 'word': 'Kiwi'},
    {'action': 'vocab_remove_replacement', 'heard': 'you pee eye'}], since(n))
push(wv, 'vocabulary', {'words': [], 'replacements': [], 'error': 'That word is too long.', 'hint': ''})
check('vocabulary error shown', text(wv, '#vMsg') == 'That word is too long.' and 'err' in js(wv, "document.getElementById('vMsg').className"))

# ---- turbo ----
push(wv, 'turbo_status', {'enabled': True, 'style': 'natural', 'ready': False, 'available': False, 'lite': {'installed': False, 'size_gb': 1.1}})
check('turbo unavailable', js(wv, "document.getElementById('turboSwitch').disabled") is True and
      js(wv, "document.getElementById('turboSwitch').getAttribute('aria-checked')") == 'false' and
      text(wv, '#turboDesc') == 'Not included in this version of freeflo.')
push(wv, 'turbo_status', {'enabled': True, 'style': 'formal', 'ready': True, 'available': True, 'lite': {'installed': False, 'size_gb': 1.1}})
check('turbo available, needs download', js(wv, "document.getElementById('turboSwitch').getAttribute('aria-checked')") == 'true' and
      js(wv, "document.getElementById('turboStyle').value") == 'formal' and
      not js(wv, "document.getElementById('turboDownload').hidden") and text(wv, '#turboDlText') == 'Turbo needs a one-time download (1.1 GB).')
n = len(messages)
click(wv, '#turboDlBtn')
push(wv, 'turbo_progress', {'tier': 'lite', 'pct': 37.5})
check('turbo download progress', since(n) == [{'action': 'download_model', 'tier': 'lite'}] and text(wv, '#turboDlBtn') == '38%' and
      not js(wv, "document.getElementById('turboBar').hidden"))

# ---- about / updates / feedback ----
push(wv, 'about', {'version': '1.6.0', 'can': ['reveal_logs', 'open_setup']})
check('about: only listed rows', not js(wv, "document.getElementById('diagRow').hidden") and
      not js(wv, "document.getElementById('logsBtn').hidden") and js(wv, "document.getElementById('diagBtn').hidden") and
      not js(wv, "document.getElementById('setupRow').hidden") and js(wv, "document.getElementById('updateRow').hidden") and
      text(wv, '#versionLine') == 'freeflo 1.6.0')
push(wv, 'about', {'version': '1.6.0', 'can': ['reveal_logs', 'copy_diagnostics', 'open_setup', 'check_updates']})
n = len(messages)
click(wv, '#logsBtn'); click(wv, '#diagBtn'); click(wv, '#setupBtn'); click(wv, '#updateBtn')
push(wv, 'update_status', {'text': 'Version 1.7.0 is available.', 'available': True, 'busy': False})
click(wv, '#updateBtn')
check('advanced actions', acts(n) == ['reveal_logs', 'copy_diagnostics', 'open_setup', 'check_updates', 'download_update'], acts(n))
check('update status text', text(wv, '#updateMsg') == 'Version 1.7.0 is available.' and text(wv, '#updateBtn') == 'Download')
n = len(messages)
click(wv, '#fbType [data-kind="Bug report"]')
js(wv, "document.getElementById('fbText').value='It stopped'; document.getElementById('fbSend').click(); 1")
push(wv, 'feature_ack', {'ok': True})
check('feedback', since(n) == [{'action': 'feature_request', 'text': 'Bug report: It stopped'}] and text(wv, '#fbMsg') == 'Sent. Thank you!', since(n))

# ---- privacy + backup ----
n = len(messages)
click(wv, '#clearBtn')
check('delete history asks first', since(n) == [] and not js(wv, "document.getElementById('clearConfirm').hidden"))
click(wv, '#clearYes')
check('delete history confirmed', acts(n) == ['clear_history', 'get_home'], acts(n))
push(wv, 'privacy', {'analytics': True, 'crash': False})
n = len(messages)
click(wv, '#analyticsSwitch'); click(wv, '#crashSwitch'); click(wv, '#saveSwitch')
check('privacy switches', since(n) == [{'action': 'set_privacy', 'analytics': False}, {'action': 'set_privacy', 'crash': True},
                                      {'action': 'set_save_history', 'on': False}], since(n))
push(wv, 'backup_status', {'configured': False, 'connected': False})
check('backup not configured: local only', js(wv, "document.getElementById('bDisconnected').hidden && document.getElementById('bConnected').hidden"))
push(wv, 'backup_status', {'configured': True, 'connected': False})
n = len(messages)
click(wv, '#connectBtn')
check('backup connect', acts(n) == ['google_connect'] and text(wv, '#connectBtn') == 'Connecting…')
push(wv, 'backup_status', {'configured': True, 'connected': True, 'enabled': True, 'email': 'a@b.co', 'last_synced': 0, 'syncing': False})
sep = "getComputedStyle(document.querySelector(%s), '::before').content"
check('backup connected: a divider under "On this Mac", none above it',
      js(wv, sep % json.dumps('#bConnected > .row')) == '""' and
      js(wv, sep % json.dumps('#bLocal')) in ('none', 'normal'),
      (js(wv, sep % json.dumps('#bConnected > .row')), js(wv, sep % json.dumps('#bLocal'))))
n = len(messages)
click(wv, '#disconnectBtn')
check('disconnect asks first', since(n) == [] and not js(wv, "document.getElementById('bConfirm').hidden"))
click(wv, '#bConfirmYes')
click(wv, '#deleteRemoteBtn'); click(wv, '#bConfirmYes')
click(wv, '#syncNowBtn'); click(wv, '#bEnabledSwitch')
check('backup actions', since(n) == [{'action': 'google_disconnect', 'delete_remote': False},
                                    {'action': 'google_disconnect', 'delete_remote': True},
                                    {'action': 'backup_sync_now'}, {'action': 'set_backup_enabled', 'on': False}], since(n))

# ---- appearance ----
n = len(messages)
click(wv, '#appearanceSeg [data-appearance="dark"]')
check('Dark sends window_style then set_theme', since(n) == [
    {'action': 'window_style', 'appearance': 'dark', 'glass': False},
    {'action': 'set_theme', 'theme': 'dark', 'appearance': 'dark', 'glass': False}], since(n))
check('Dark applies at once', js(wv, "document.documentElement.dataset.mode") == 'dark')
n = len(messages)
click(wv, '#glassSwitch')
check('Glass sends theme glass', since(n) == [
    {'action': 'window_style', 'appearance': 'dark', 'glass': True},
    {'action': 'set_theme', 'theme': 'glass', 'appearance': 'dark', 'glass': True}], since(n))
check('glass on', js(wv, "document.documentElement.dataset.glass") == 'on')
push(wv, 'window_style', {'glass': False, 'glass_wanted': True, 'appearance': 'dark', 'material': 'none', 'reduced': True,
                          'titlebar': 32, 'traffic': 78, 'accent': '#989898'})
check('window cannot show glass: paused text, solid page',
      text(wv, '#glassDesc') == 'Paused while Reduce Transparency or Increase Contrast is on in System Settings.' and
      js(wv, "document.documentElement.dataset.glass") == 'off' and
      js(wv, "document.getElementById('glassSwitch').getAttribute('aria-checked')") == 'true')
push(wv, 'theme', {'theme': 'light', 'glass': False, 'appearance': 'light'})
check('theme event applies', js(wv, "document.documentElement.dataset.mode") == 'light' and
      js(wv, "document.getElementById('glassSwitch').getAttribute('aria-checked')") == 'false')
contrast = js(wv, """(function(){var s=getComputedStyle(document.documentElement);
  var a=s.getPropertyValue('--accent').trim(), f=s.getPropertyValue('--accent-fill').trim();
  var c=hexRgb(f); return a+'|'+f+'|'+contrastWithWhite(c).toFixed(2);})()""")
a, f, c = (contrast or '||0').split('|')
check('accent from the window, fill readable with white text', a == '#989898' and float(c) >= 4.5, contrast)

# ---- home ----
push(wv, 'navigate', {'view': 'home'})
recent = [{'uuid': 'u%d' % i, 'ts': int(time.time()) - i * 60, 'text': 'entry %d <i>x</i>' % i, 'language': 'en-IN', 'mode': 'ptt'} for i in range(7)]
HOME = {'stats': {'today': 3, 'words_week': 1200, 'total': 40}, 'language': 'en-IN', 'language_name': 'English (India)',
        'ptt_key': 'left_option', 'enabled': True, 'recent': recent, 'alert': None}
push(wv, 'shortcuts', base)
push(wv, 'home', HOME)
check('home ready line with the key', text(wv, '#statusLine') == 'Ready — hold Left ⌥ to dictate' and
      text(wv, '#statusLine .key') == 'Left ⌥' and text(wv, '#heroBtn') == 'Try it', text(wv, '#statusLine'))
check('at most 5 recent rows, each with a copy button, text as text',
      js(wv, "document.querySelectorAll('#recent .item').length") == 5 and
      js(wv, "document.querySelectorAll('#recent .item .copy').length") == 5 and
      js(wv, "document.querySelectorAll('#recent i').length") == 0 and
      visible(wv, '#showAll') and text(wv, '#recentAside') == '3 today')
n = len(messages)
click(wv, '#recent .item .copy')
check('copy sends copy_text', since(n) == [{'action': 'copy_text', 'text': 'entry 0 <i>x</i>'}], since(n))
n = len(messages)
click(wv, '#showAll')
check('Show all opens History', js(wv, "document.getElementById('v-history').classList.contains('on')"))
push(wv, 'navigate', {'view': 'home'})
push(wv, 'home', dict(HOME, alert={'text': 'The microphone is off for freeflo.', 'action': 'open_mic_settings', 'button': 'Open System Settings'}))
check('attention: one line, one button, no Try it',
      js(wv, "document.getElementById('hero').dataset.state") == 'attention' and
      text(wv, '#statusLine') == 'The microphone is off for freeflo.' and
      js(wv, "document.getElementById('statusSub').hidden") is True and
      text(wv, '#heroBtn') == 'Open System Settings' and
      js(wv, "[...document.querySelectorAll('#v-home button')].filter(b=>b.offsetParent&&b.textContent==='Try it').length") == 0)
n = len(messages)
click(wv, '#heroBtn')
check('attention button sends its action', since(n) == [{'action': 'open_mic_settings'}], since(n))
push(wv, 'home', dict(HOME, alert={'text': 'Accessibility is off, so your dictation keys can’t work.', 'action': 'open_setup', 'button': 'Fix'}))
n = len(messages)
click(wv, '#heroBtn')
check('Fix opens setup', since(n) == [{'action': 'open_setup'}], since(n))
push(wv, 'home', dict(HOME, alert={'text': 'Something else.', 'action': 'clear_history', 'button': 'Go'}))
n = len(messages)
click(wv, '#heroBtn')
check('an alert cannot make the page send a message of its choosing', since(n) == [], since(n))
push(wv, 'home', dict(HOME, alert={'text': 'Pick your language.', 'action': 'go_settings', 'button': 'Choose'}))
click(wv, '#heroBtn')
check('go_settings opens Settings', js(wv, "document.getElementById('v-settings').classList.contains('on')"))
push(wv, 'navigate', {'view': 'home'})
push(wv, 'home', dict(HOME, alert={'text': 'freeflo now listens in Hindi too.', 'action': 'ack_language_notice', 'button': 'OK'}))
n = len(messages)
check('language notice: still ready, callout shown', js(wv, "document.getElementById('hero').dataset.state") == 'ready' and
      visible(wv, '#homeNotice') and text(wv, '#homeNoticeText') == 'freeflo now listens in Hindi too.')
click(wv, '#homeNoticeBtn')
check('language notice button', since(n) == [{'action': 'ack_language_notice'}], since(n))
# A chosen language that is not here: information under a Ready hero, with
# its own button (dictation still works in the other language).
push(wv, 'languages', dict(L, additional=['mul-IN']))
push(wv, 'home', dict(HOME, alert={'text': 'Multilingual (India) isn’t downloaded yet, so freeflo listens for English (India) only.',
                                   'action': 'install_language', 'code': 'mul-IN', 'button': 'Download', 'level': 'info'}))
check('missing language: hero stays Ready, notice with Download',
      js(wv, "document.getElementById('hero').dataset.state") == 'ready' and visible(wv, '#homeNotice') and
      text(wv, '#homeNoticeBtn') == 'Download')
n = len(messages)
click(wv, '#homeNoticeBtn')
check('Download sends install_language and the notice goes away',
      since(n) == [{'action': 'install_language', 'id': 'mul-IN', 'code': 'mul-IN'}] and not visible(wv, '#homeNotice'), since(n))
push(wv, 'home', dict(HOME, enabled=False))
check('off state', js(wv, "document.getElementById('hero').dataset.state") == 'off' and
      text(wv, '#statusLine') == 'Dictation is turned off' and js(wv, "document.getElementById('heroBtn').hidden") is True)
push(wv, 'home', HOME)
n = len(messages)
click(wv, '#heroBtn')
push(wv, 'status', {'state': 'recording'})
check('test: recording', text(wv, '#statusLine') == 'Listening…' and text(wv, '#heroBtn') == 'Stop')
click(wv, '#heroBtn')
check('test: stop -> writing', text(wv, '#statusLine') == 'Writing…' and js(wv, "document.getElementById('heroBtn').disabled") is True)
push(wv, 'test_result', {'text': 'hello there'})
check('test flow messages', acts(n) == ['test_start', 'test_stop', 'get_home'], acts(n))
check('test result shown', text(wv, '#tryText') == '“hello there”' and text(wv, '#tryNote') == 'Just a test. Nothing was typed anywhere.' and
      text(wv, '#heroBtn') == 'Try it')
push(wv, 'test_result', {'text': '', 'note': 'No sound from the microphone. Is it muted?'})
check('test note', text(wv, '#tryNote') == 'No sound from the microphone. Is it muted?' and js(wv, "document.getElementById('tryText').hidden"))
push(wv, 'status', {'state': 'busy', 'message': 'Already listening.'})
check('busy', text(wv, '#tryNote') == 'Already listening.' and text(wv, '#heroBtn') == 'Try it')

# ---- history ----
push(wv, 'navigate', {'view': 'history'})
click(wv, '#histTabs [data-tab="all"]')
push(wv, 'history', {'entries': [{'uuid': 'u1', 'ts': 1794000000, 'text': 'one', 'language': 'en-IN', 'mode': 'ptt'},
                                  {'uuid': 'u2', 'ts': 1794000001, 'text': 'two', 'language': 'hi-IN', 'mode': 'test'}],
                     'save_history': True, 'saved_uuids': ['u2'], 'lang_names': {'en-IN': 'English (India)', 'hi-IN': 'Hindi (India)'}})
tags = js(wv, "[...document.querySelectorAll('#hlist .tag')].map(b=>b.textContent).join('|')")
check('history tags (language only when mixed, Test)', tags == 'English (India)|Hindi (India)|Test', tags)
check('star state', js(wv, "[...document.querySelectorAll('#hlist .star')].map(b=>b.getAttribute('aria-pressed')).join('|')") == 'false|true')
n = len(messages)
js(wv, "document.querySelectorAll('#hlist .star')[0].click(); document.querySelectorAll('#hlist .star')[1].click(); 1")
check('star save / unsave', since(n) == [{'action': 'save_prompt_from_history', 'text': 'one', 'history_uuid': 'u1'},
                                         {'action': 'unsave_prompt', 'history_uuid': 'u2'}], since(n))
push(wv, 'history', {'entries': [{'uuid': 'u1', 'ts': 1794000000, 'text': 'one', 'language': 'en-IN', 'mode': 'ptt'}], 'save_history': True})
check('single language: no language tag', js(wv, "document.querySelectorAll('#hlist .tag').length") == 0)
n = len(messages)
js(wv, "var s=document.getElementById('hsearch'); s.value='on'; s.dispatchEvent(new Event('input')); s.value='one'; s.dispatchEvent(new Event('input')); 1")
spin(0.1)
check('search is debounced (nothing yet)', acts(n) == [])
spin(0.3)
check('search sends one query', since(n) == [{'action': 'get_history', 'query': 'one'}], since(n))
n = len(messages)
js(wv, "document.getElementById('hsearch').dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true})); 1")
check('Esc clears the search', since(n) == [{'action': 'get_history', 'query': ''}] and js(wv, "document.getElementById('hsearch').value") == '')
n = len(messages)
click(wv, '#histTabs [data-tab="saved"]')
push(wv, 'saved_prompts', {'entries': [{'uuid': 's1', 'ts': 1794000000, 'text': 'kept'}]})
check('saved tab', acts(n) == ['get_saved_prompts'] and visible(wv, '#addSavedBtn') and text(wv, '#hlist .item-text') == 'kept')
n = len(messages)
click(wv, '#hlist .remove')
click(wv, '#addSavedBtn')
js(wv, "document.getElementById('addText').value='new one'; document.getElementById('addSaveBtn').click(); 1")
check('saved remove / add', since(n) == [{'action': 'remove_saved_prompt', 'uuid': 's1'}, {'action': 'add_manual_prompt', 'text': 'new one'}], since(n))

# ---- keyboard + refresh ----
js(wv, "document.dispatchEvent(new KeyboardEvent('keydown',{key:'1',metaKey:true,bubbles:true})); 1")
js(wv, "document.dispatchEvent(new KeyboardEvent('keydown',{key:'2',metaKey:true,bubbles:true})); 1")
check('Cmd+2 opens History', js(wv, "document.getElementById('v-history').classList.contains('on')"))
js(wv, "document.dispatchEvent(new KeyboardEvent('keydown',{key:',',metaKey:true,bubbles:true})); 1")
check('Cmd+, opens Settings', js(wv, "document.getElementById('v-settings').classList.contains('on')"))
js(wv, "document.dispatchEvent(new KeyboardEvent('keydown',{key:'f',metaKey:true,bubbles:true})); 1")
check('Cmd+F focuses search in History', js(wv, "document.getElementById('v-history').classList.contains('on') && document.activeElement.id") == 'hsearch')
js(wv, "document.querySelector('.nav-btn[data-view=history]').focus(); document.activeElement.dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowDown',bubbles:true})); 1")
check('ArrowDown moves between areas', js(wv, "document.activeElement.dataset.view") == 'settings')
push(wv, 'navigate', {'view': 'home'})
n = len(messages)
push(wv, 'window_shown', {})
check('window_shown refreshes the current view', acts(n) == ['get_home', 'get_shortcuts', 'get_languages'], acts(n))

# ---- layout sweep ----
js(wv, "document.getElementById('advanced').open = true; 1")
push(wv, 'about', {'version': '1.6.0', 'can': ['reveal_logs', 'copy_diagnostics', 'open_setup', 'check_updates']})
push(wv, 'languages', dict(L, additional=['hi-IN'], hint={'id': 'mul-IN', 'title': 'Mix?', 'text': 'Both.', 'role': 'additional'}))
SWEEP = """(function(){
  var bad=[];
  document.querySelectorAll('.row, .hero, .item, .callout, .add-form, summary, .empty').forEach(function(n){
    if (!n.offsetParent) return;
    var s=getComputedStyle(n), l=parseFloat(s.paddingLeft), r=parseFloat(s.paddingRight), t=parseFloat(s.paddingTop), b=parseFloat(s.paddingBottom);
    if (l<12||r<12||t<10||b<10) bad.push((n.id||n.className)+':'+[t,r,b,l].join(','));
  });
  document.querySelectorAll('.seg').forEach(function(n){ if (n.offsetParent && parseFloat(getComputedStyle(n).paddingTop)<3) bad.push('seg'); });
  var walker=document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  while (walker.nextNode()){
    var t=walker.currentNode, p=t.parentElement;
    if (!t.textContent.trim() || !p || !p.offsetParent || p.closest('script,style')) continue;
    var fs=parseFloat(getComputedStyle(p).fontSize); if (fs<12) bad.push('font '+fs+' '+t.textContent.trim().slice(0,30));
  }
  return bad.join(' | ');
})()"""
for v in ('home', 'history', 'settings'):
    push(wv, 'navigate', {'view': v})
    if v == 'home':
        push(wv, 'home', HOME)
        push(wv, 'test_result', {'text': 'hello'})
    bad = js(wv, SWEEP)
    check('padding / font sweep: ' + v, bad == '', bad)
JARGON = re.compile(r'helper|locale|engine|on-device|push-to-talk|hotkey|transcri|restart', re.I)
words = js(wv, "[...document.querySelectorAll('.view')].map(v=>{v.classList.add('on'); var t=v.innerText; return t;}).join('\\n')")
check('no jargon in visible text', words and not JARGON.search(words), JARGON.findall(words or ''))
check('index: no JS errors', not errors, errors)

# --- page without the native window (a plain browser / old window.py) ---
del messages[:]
wv_plain = load('index.html', boot=None)
check('no native window: same boot order, no window_style',
      acts()[:5] == ['get_theme', 'get_home', 'get_shortcuts', 'get_languages', 'ui_ready'] and 'window_style' not in acts(), acts())
check('index (plain): no JS errors', not errors, errors)

# ==========================================================================
# onboarding.html
# ==========================================================================
del messages[:]
del errors[:]
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ob_payloads  # noqa: E402  (real SetupFlow views, as app._send_ob_state sends them)
OB = ob_payloads.view


def ob_state(wv, scen, **settings):
    push(wv, 'ob_state', OB(scen, settings))


def row_state(wv, rid):
    return js(wv, "document.getElementById(%s).dataset.state" % json.dumps(rid))


def consent_msgs(n):
    return [m for m in since(n) if m.get('action') in ('ob_set_consent', 'ob_complete')]


wv2 = load('onboarding.html', size=(540, 640))
check('onboarding: first message ob_get_state', acts()[:1] == ['ob_get_state'], acts())
check('onboarding: one screen, two rows', js(wv2, "document.querySelectorAll('.screen').length") == 0 and
      js(wv2, "[...document.querySelectorAll('#checklist .perm')].map(r=>r.id).join('|')") == 'rowMic|rowAx')
check('before any state: both checking, no consent question',
      row_state(wv2, 'rowMic') == 'checking' and row_state(wv2, 'rowAx') == 'checking' and
      not visible(wv2, '#consentCard'))
ob_state(wv2, 'first-start')
check('first run: Allow, Accessibility locked behind it, Done disabled with hint',
      text(wv2, '#rowMic .row-btn') == 'Allow' and row_state(wv2, 'rowMic') == 'needed' and
      js(wv2, "!document.querySelector('#rowAx .row-btn')") and
      text(wv2, '#rowAx .row-hint') == 'Next, after the microphone.' and
      js(wv2, "document.getElementById('doneBtn').disabled") is True and
      text(wv2, '#doneHint') == 'Allow both to finish. You can also close this window and come back from the menu bar icon.')
check('first run: the consent question is shown, switch on', visible(wv2, '#consentCard') and
      js(wv2, "document.getElementById('consentSwitch').getAttribute('aria-checked')") == 'true')
check('try card inert until the microphone is allowed', js(wv2, "document.getElementById('tryCard').hasAttribute('inert')") is True)
n = len(messages)
click(wv2, '#rowMic .row-btn')
check('one ob_grant_mic per click', [m for m in since(n) if m.get('action') != 'ob_get_state_fresh'] == [{'action': 'ob_grant_mic'}], since(n))
ob_state(wv2, 'first-mic-waiting')
check('waiting after asking: spinner, hint, no button', row_state(wv2, 'rowMic') == 'waiting' and
      (text(wv2, '#rowMic .row-hint') or '').startswith('Answer the macOS prompt') and
      js(wv2, "!document.querySelector('#rowMic .row-btn')"))
ob_state(wv2, 'first-mic-denied')
check('denied: Open System Settings + hint', row_state(wv2, 'rowMic') == 'denied' and
      text(wv2, '#rowMic .row-btn') == 'Open System Settings' and
      (text(wv2, '#rowMic .row-hint') or '').startswith('Switch freeflo on under Microphone'))
ob_state(wv2, 'first-ax-waiting')
check('mic granted, Accessibility waiting', row_state(wv2, 'rowMic') == 'granted' and
      js(wv2, "!document.querySelector('#rowMic .row-btn')") and row_state(wv2, 'rowAx') == 'waiting' and
      text(wv2, '#rowAx .row-btn') == 'Open System Settings' and
      js(wv2, "document.getElementById('doneBtn').disabled") is True and
      js(wv2, "document.getElementById('tryCard').hasAttribute('inert')") is False)
n = len(messages)
click(wv2, '#rowAx .row-btn')
check('Accessibility button sends ob_grant_accessibility', 'ob_grant_accessibility' in acts(n), acts(n))
# Live status. The page asks again every 1.5 s while something is missing
# and it is on screen. WebKit holds back the timers of a web view that has
# never been on screen (as here), so the tick itself is not timed: the test
# runs what the timer runs.
check('the page polls on a timer', re.search(r'setInterval\(poll,\s*1500\)', src_ob) is not None)
n = len(messages)
js(wv2, "poll(); 1")
spin(0.1)
check('no polling while the page is not visible', 'ob_get_state_fresh' not in acts(n), acts(n))
js(wv2, "Object.defineProperty(document, 'visibilityState', {get: function(){ return 'visible'; }}); 1")
n = len(messages)
js(wv2, "poll(); 1")
spin(0.1)
check('polls while incomplete and visible', 'ob_get_state_fresh' in acts(n), acts(n))
# try it
n = len(messages)
click(wv2, '#tryBtn')
push(wv2, 'ob_test_state', {'recording': True})
check('try: listening + Stop', text(wv2, '#tryLine') == 'Listening…' and text(wv2, '#tryBtn') == 'Stop')
click(wv2, '#tryBtn')
check('try: writing', text(wv2, '#tryLine') == 'Writing…' and js(wv2, "document.getElementById('tryBtn').disabled") is True)
push(wv2, 'ob_test_result', {'text': 'hello world'})
check('try flow messages', [a for a in acts(n) if a != 'ob_get_state_fresh'] == ['ob_test_start', 'ob_test_stop'], acts(n))
check('try result', text(wv2, '#tryText') == '“hello world”' and text(wv2, '#tryNote') == 'That’s it. It works the same in any app.')
push(wv2, 'ob_test_state', {'recording': True, 'via': 'hotkey'})
check('try via the key hides Stop', js(wv2, "document.getElementById('tryBtn').hidden") is True)
push(wv2, 'ob_test_result', {'text': '', 'note': 'Nothing heard. Try again.'})
check('try empty', text(wv2, '#tryNote') == 'Nothing heard. Try again.' and js(wv2, "document.getElementById('tryText').hidden"))
# consent (first run)
n = len(messages)
click(wv2, '#consentSwitch')
check('consent payload', consent_msgs(n) == [{'action': 'ob_set_consent', 'analytics': False, 'crash': False}], since(n))
# both granted
ob_state(wv2, 'first-done', ptt_key='right_option')
check('both granted: title, Done enabled', text(wv2, '#obTitle') == 'You’re all set' and
      js(wv2, "document.getElementById('doneBtn').disabled") is False and text(wv2, '#doneHint') == '')
# The dictation keys are live (ob_state.try_hotkey): the page says to hold
# the key; the app shows such a dictation here instead of pasting it.
check('keys live: Try it says to hold the key',
      text(wv2, '#tryLine') == 'Hold Right Option and say something. Or click Try it.' and
      text(wv2, '#tryBtn') == 'Try it', text(wv2, '#tryLine'))
push(wv2, 'ob_test_state', {'recording': True, 'via': 'hotkey'})
check('held key: listening, no Stop button', text(wv2, '#tryLine') == 'Listening…' and
      js(wv2, "document.getElementById('tryBtn').hidden") is True)
push(wv2, 'ob_test_state', {'recording': False})
check('key let go: writing', text(wv2, '#tryLine') == 'Writing…')
push(wv2, 'ob_test_result', {'text': 'namaste team'})
check('held key: the words land on the Try it line, the button is back',
      text(wv2, '#tryText') == '“namaste team”' and js(wv2, "document.getElementById('tryBtn').hidden") is False and
      text(wv2, '#tryLine') == 'Hold Right Option and say something. Or click Try it.')
push(wv2, 'ob_test_state', {'recording': True, 'via': 'hotkey'})
push(wv2, 'ob_test_result', {'text': '', 'note': 'Keep the key held while you speak, then let go.'})
check('a tap of the key: says to keep it held', text(wv2, '#tryNote') == 'Keep the key held while you speak, then let go.' and
      js(wv2, "document.getElementById('tryBtn').hidden") is False)
n = len(messages)
js(wv2, "poll(); 1")
spin(0.1)
check('no polling once complete', 'ob_get_state_fresh' not in acts(n), acts(n))
n = len(messages)
js(wv2, "document.activeElement && document.activeElement.blur(); document.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true})); 1")
check('first run, Enter: Done sends consent then complete', [m for m in since(n) if m.get('action') != 'ob_get_state_fresh'] == [
    {'action': 'ob_set_consent', 'analytics': False, 'crash': False}, {'action': 'ob_complete', 'analytics': False, 'crash': False}], since(n))
check('Done disabled while finishing', js(wv2, "document.getElementById('doneBtn').disabled") is True)
# engine error
ob_state(wv2, 'first-engine-error')
check('engine error: blocked, can finish', row_state(wv2, 'rowMic') == 'blocked' and
      text(wv2, '#rowMic .row-hint') == 'freeflo needs macOS 26 or later' and js(wv2, "!document.querySelector('#rowMic .row-btn')"))
spin(2.1)
check('engine error: Done enabled', js(wv2, "document.getElementById('doneBtn').disabled") is False)

# Repair (after an update) and review (from the menu): never the consent
# question, and Done never writes the privacy switches - whatever they are.
for scen in ('repair-ax', 'repair-legacy', 'review'):
    for a_on, c_on in ((False, True), (True, False), (False, False)):
        wv3 = load('onboarding.html', size=(540, 640))
        ob_state(wv3, scen, analytics_enabled=a_on, crash_enabled=c_on)
        n = len(messages)
        if scen == 'review':
            js(wv3, "document.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true})); 1")
        else:
            click(wv3, '#rowAx .row-btn')
            js(wv3, "document.getElementById('doneBtn').click(); 1")
        spin(0.1)
        sent = [m for m in since(n) if m.get('action') in ('ob_set_consent', 'ob_complete')]
        check('%s (analytics=%s crash=%s): no consent card, Done sends no privacy values' % (scen, a_on, c_on),
              not visible(wv3, '#consentCard') and
              all(m == {'action': 'ob_complete'} for m in sent) and
              not any(m.get('action') == 'ob_set_consent' for m in sent), sent)
        check('%s: switch cannot be flipped' % scen,
              (click(wv3, '#consentSwitch') or True) and
              not [m for m in since(n) if m.get('action') == 'ob_set_consent'])
# repair with only Accessibility missing: the hidden microphone row leaves
# no hairline along the card's top edge
wv3 = load('onboarding.html', size=(540, 640))
ob_state(wv3, 'repair-ax')
check('repair: only the Accessibility row, no separator above it',
      js(wv3, "document.getElementById('rowMic').hidden") is True and
      js(wv3, "getComputedStyle(document.getElementById('rowAx'), '::before').content") in ('none', 'normal', None),
      js(wv3, "getComputedStyle(document.getElementById('rowAx'), '::before').content"))
ob_state(wv3, 'first-start')
check('first run: separator between the two rows',
      js(wv3, "getComputedStyle(document.getElementById('rowAx'), '::before').content") == '""')
# tap refused: still mark (no spinner), the sentence once
wv3 = load('onboarding.html', size=(540, 640))
ob_state(wv3, 'restart')
sub = text(wv3, '#obSub') or ''
body_text = js(wv3, 'document.body.innerText') or ''
check('tap refused: static mark, no button, the sentence said once',
      row_state(wv3, 'rowAx') == 'paused' and
      js(wv3, "getComputedStyle(document.querySelector('#rowAx .status')).animationName") in ('none', '') and
      js(wv3, "!document.querySelector('#rowAx .row-btn')") and
      sub.startswith('macOS has not handed the keyboard over yet') and body_text.count(sub) == 1, body_text)
# smallest window, tallest first-run state, solid and glass: the bottom bar
# is its own strip, so nothing is ever drawn under it (no veil needed), the
# column scrolls, and a long hint gets the full width of its row
GEOM = """(function(){
  var f=document.getElementById('foot').getBoundingClientRect(), s=document.getElementById('scroll'),
      sr=s.getBoundingClientRect(), d=document.getElementById('doneBtn').getBoundingClientRect(),
      h=document.querySelector('#rowAx .row-hint').getBoundingClientRect(),
      l=document.querySelector('#rowAx .row-label').getBoundingClientRect(),
      b=document.querySelector('#rowAx .row-btn'), br=b?b.getBoundingClientRect():null,
      t=document.getElementById('tryOut').getBoundingClientRect();
  return JSON.stringify({footTop:f.top, footBottom:f.bottom, scrollBottom:sr.bottom, winH:innerHeight, winW:innerWidth,
    doneBottom:d.bottom, doneRight:d.right, scrolls:s.scrollHeight>s.clientHeight+1, pageScrolls:document.documentElement.scrollHeight>innerHeight+1,
    hintLeft:h.left, hintRight:h.right, labelLeft:l.left, btnRight:br?br.right:0, hintTop:h.top, labelBottom:l.bottom, btnBottom:br?br.bottom:0,
    tryTop:t.top, tryBottom:t.bottom, edge:document.getElementById('foot').dataset.edge});
})()"""
for glass in (False, True):
    wv3 = load('onboarding.html', size=(480, 560), boot=dict(BOOT, glass=glass, glass_wanted=glass))
    ob_state(wv3, 'first-ax-stale')
    push(wv3, 'ob_test_result', {'text': 'Hello, this is my first dictation.'})
    spin(0.2)
    g = json.loads(js(wv3, GEOM) or '{}')
    tag = 'glass' if glass else 'solid'
    check('%s, smallest window: the bottom bar sits under the column, never over it' % tag,
          js(wv3, "document.documentElement.dataset.glass") == ('on' if glass else 'off') and
          abs(g.get('footTop', -1) - g.get('scrollBottom', -2)) < 1 and abs(g.get('footBottom', 0) - g.get('winH', -1)) < 1 and
          g.get('doneBottom', 9999) <= g.get('winH', 0) and g.get('doneRight', 9999) <= g.get('winW', 0) and
          not g.get('pageScrolls'), g)
    check('%s, smallest window: the column scrolls and the bar shows its hairline' % tag,
          g.get('scrolls') is True and g.get('edge') == '1', g)
    check('%s: the result of Try it is scrolled into view' % tag,
          g.get('tryTop', -1) >= 0 and g.get('tryBottom', 9999) <= g.get('footTop', 0) + 1, g)
    check('%s: a long hint runs under the row at full width, clear of the button' % tag,
          abs(g.get('hintLeft', -1) - g.get('labelLeft', -2)) < 1 and g.get('hintRight', 0) >= g.get('btnRight', 9999) - 1 and
          g.get('hintTop', 0) >= max(g.get('labelBottom', 9999), g.get('btnBottom', 9999)) - 1, g)
wv3 = load('onboarding.html', size=(540, 640))
ob_state(wv3, 'first-start')
g = json.loads(js(wv3, GEOM.replace("document.querySelector('#rowAx .row-btn')", "document.querySelector('#rowMic .row-btn')")) or '{}')
check('usual size, first run: everything fits without scrolling, no hairline', g.get('scrolls') is False and g.get('edge') == '0', g)
check('try card: one line and one button (no repeated title)',
      js(wv3, "document.querySelectorAll('#tryCard .row-title').length") == 0 and
      text(wv3, '#tryLine') == 'Click Try it and say something.' and text(wv3, '#tryBtn') == 'Try it')
check('consent is a quiet row, not a card', js(wv3, "document.getElementById('consentCard').classList.contains('card')") is False and
      visible(wv3, '#consentSwitch'))
ob_state(wv3, 'first-ax-waiting')
check('after the button: the row says what to do, the why is put away',
      row_state(wv3, 'rowAx') == 'waiting' and not visible(wv3, '#rowAx .row-detail') and visible(wv3, '#rowAx .row-hint') and
      visible(wv3, '#rowMic .row-detail'))
rendered = js(wv2, 'document.body.innerText')
check('onboarding never offers a restart button', not re.search(r'restart', src_ob, re.I) and
      not re.search(r'restart', rendered or '', re.I))
check('onboarding: no jargon', not JARGON.search(rendered or ''), JARGON.findall(rendered or ''))
bad = js(wv2, SWEEP)
check('onboarding padding / font sweep', bad == '', bad)
check('onboarding: no JS errors', not errors, errors)
print('RESULT:', 'OK' if ok else 'FAILED')
sys.exit(0 if ok else 1)
