"""Headless check of ui/window.py and the HUD's glass, without putting
anything on screen: windows are built but never shown, the HUD panel is
built but never presented, and the process has no Dock icon.

    python tests/ui_window_check.py <repo root>

Prints PASS / FAIL lines and a final RESULT line (run by tests/test_ui_pages.py
in a subprocess, with FREEFLO_DATA_DIR pointing at a temporary directory).
"""
import os
import re
import sys
import time

REPO = sys.argv[1]
sys.path.insert(0, REPO)

from Foundation import NSDate, NSRunLoop  # noqa: E402
from AppKit import (NSApplication, NSApplicationActivationPolicyAccessory,  # noqa: E402
                    NSPasteboard, NSPasteboardTypeString)

app = NSApplication.sharedApplication()
app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)

from ui import window as W  # noqa: E402

ok = True


def check(label, cond, detail=''):
    global ok
    ok = ok and bool(cond)
    print(('PASS ' if cond else 'FAIL ') + label + ((' :: ' + str(detail)[:600]) if detail and not cond else ''))


def spin(sec):
    NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(sec))


def js(c, code):
    out = {}
    c._webview.evaluateJavaScript_completionHandler_(code, lambda r, e: out.update(r=r, e=e))
    t = time.time()
    while 'r' not in out and time.time() - t < 5:
        spin(0.02)
    return out.get('r')


def inside(view, ancestor):
    while view is not None:
        if view == ancestor:
            return True
        view = view.superview()
    return False


for page in ('index.html', 'onboarding.html'):
    for material in ('auto', 'vibrancy'):
        os.environ['FREEFLO_GLASS_MATERIAL'] = material
        forwarded = []
        c = W.WindowController(on_message=forwarded.append, html_file=page,
                               appearance='light', glass=False)
        c._build()                      # never show(): nothing on screen
        spin(1.0)
        boot = [str(m.get('action')) for m in forwarded]
        check('%s/%s: the page boot messages reach the app' % (page, material),
              boot[:1] == (['get_theme'] if page == 'index.html' else ['ob_get_state']), boot)
        del forwarded[:]
        w = c._window
        tag = '%s/%s: ' % (page, material)
        size = W.PAGE_SIZES.get(page, W.DEFAULT_SIZE)
        frame = w.contentRectForFrameRect_(w.frame())
        check(tag + 'size', (round(frame.size.width), round(frame.size.height)) == tuple(int(v) for v in size),
              (frame.size.width, frame.size.height))
        check(tag + 'full-size content, transparent title bar',
              bool(w.styleMask() & (1 << 15)) and bool(w.titlebarAppearsTransparent()))
        check(tag + 'web view draws no background', not c._webview.valueForKey_('drawsBackground'))
        check(tag + 'solid: opaque, no material showing',
              bool(w.isOpaque()) and (c._backdrop is None or bool(c._backdrop.isHidden())))
        c.set_style(glass=True)
        spin(0.3)
        want = 'NSVisualEffectView' if material == 'vibrancy' else 'NSGlassEffectView'
        reduced = W._accessibility_wants_opaque()
        if reduced:
            check(tag + 'glass held back while Reduce Transparency / Increase Contrast is on', bool(w.isOpaque()))
        else:
            check(tag + 'glass: see-through window, material below the page',
                  not w.isOpaque() and c._backdrop is not None and str(c._backdrop.className()) == want and
                  not c._backdrop.isHidden() and
                  list(c._container.subviews()).index(c._backdrop) < list(c._container.subviews()).index(c._webview),
                  (w.isOpaque(), c._backdrop and c._backdrop.className()))
        c._dispatch({'action': 'window_style', 'appearance': 'dark', 'glass': False})
        spin(0.3)
        check(tag + 'page window_style applies and is not forwarded',
              bool(w.isOpaque()) and (c._backdrop is None or bool(c._backdrop.isHidden())) and
              c.style()['appearance'] == 'dark' and forwarded == [], forwarded)
        st = c.style()
        check(tag + 'style() keys', set(st) == {'glass', 'glass_wanted', 'appearance', 'material', 'reduced',
                                                'titlebar', 'traffic', 'accent'} and
              re.match(r'^#[0-9a-f]{6}$', st['accent'] or ''), st)
        # drag strip
        bounds = c._container.bounds()
        H = bounds.size.height
        x = c._traffic_w + 40
        top = c._container.hitTest_((x, H - c._titlebar_h / 2.0))
        below = c._container.hitTest_((x, H - c._titlebar_h - 20))
        check(tag + 'title strip right of the traffic lights is the drag strip',
              top is not None and str(top.className()) == '_DragStrip' and not top.mouseDownCanMoveWindow(), top)
        check(tag + 'below the strip is the page', below is not None and inside(below, c._webview), below)
        check(tag + 'Tab reaches buttons', bool(c._webview.configuration().preferences().tabFocusesLinks()))
        # copy_text -> a private pasteboard, never the user's clipboard
        pb = NSPasteboard.pasteboardWithUniqueName()
        c._pasteboard = pb
        c._dispatch({'action': 'copy_text', 'text': 'Hello ñ ✓'})
        check(tag + 'copy_text writes the pasteboard, not forwarded',
              pb.stringForType_(NSPasteboardTypeString) == 'Hello ñ ✓' and forwarded == [], forwarded)
        pb.releaseGlobally()
        c._dispatch({'action': 'get_home'})
        check(tag + 'other actions are forwarded', len(forwarded) == 1 and forwarded[0].get('action') == 'get_home')
        # the page saw the window
        seen = js(c, "JSON.stringify({a:(window.__freefloWindow||{}).accent, n:(window.__freefloWindow||{}).native,"
                     "t:getComputedStyle(document.documentElement).getPropertyValue('--titlebar-h').trim()})")
        check(tag + 'page got the boot state', seen and '"n":true' in seen and '"a":"#' in seen, seen)
        # window_shown on a second show
        sent = []
        c.send = lambda name, payload, _s=sent: _s.append(name)
        c._present = lambda: None
        c.show()
        check(tag + 'show() of a built window sends window_shown', sent == ['window_shown'], sent)
        # is_key follows the window delegate (the window is never shown
        # here, so the delegate is told what AppKit would tell it).
        d = w.delegate()
        was = c.is_key
        d.windowDidBecomeKey_(None)
        became = c.is_key
        d.windowDidResignKey_(None)
        resigned = c.is_key
        d.windowDidBecomeKey_(None)
        w.close()
        check(tag + 'is_key: off until key, on while key, off again when another window or app takes over or it closes',
              (was, became, resigned, c.is_key) == (False, True, False, False), (was, became, resigned, c.is_key))

W._ensure_main_menu()
menu = app.mainMenu()
items = []
if menu is not None:
    for i in range(menu.numberOfItems()):
        sub = menu.itemAtIndex_(i).submenu()
        items.append((str(menu.itemAtIndex_(i).title()),
                      [str(x.keyEquivalent()) for x in sub.itemArray() if not x.isSeparatorItem()]))
check('Edit menu with the standard shortcuts', items == [('freeflo', ['w']), ('Edit', ['z', 'Z', 'x', 'c', 'v', 'a'])], items)

check('style_from_settings', W.style_from_settings({'theme': 'glass'}) == ('system', True) and
      W.style_from_settings({'appearance': 'dark', 'glass': False, 'theme': 'dark'}) == ('dark', False) and
      W.style_from_settings({}) == ('system', False))

# ---------------- HUD ----------------
from ui import hud as H  # noqa: E402

h = H.HUD(glass=True)
h._ensure_built()
h._refresh_environment(force=True)
h._layout(200, False)
mat = h._material
reduced = W._accessibility_wants_opaque()
if reduced:
    check('HUD: glass held back while Reduce Transparency / Increase Contrast is on', mat is None or mat.isHidden())
else:
    f = mat.frame() if mat is not None else None
    check('HUD: material under the capsule',
          mat is not None and not mat.isHidden() and
          (f.origin.x, f.origin.y, f.size.width, f.size.height) == ((H.WIN_W - 200) / 2.0, H.PAD, 200.0, H.PILL_H) and
          list(h._container.subviews()).index(mat) < list(h._container.subviews()).index(h._view),
          (mat, f))
    check('HUD: glass palette', h._palette_key[3] is True and h._colors['shadow'] == 0.0)
h.set_look(glass=False)
spin(0.1)
check('HUD: set_look(glass=False) hides it', mat is None or bool(mat.isHidden()))
check('HUD: solid palette back', h._palette_key is not None and h._palette_key[3] is False and h._colors['shadow'] > 0)
h.set_look(theme='system')
check('HUD: system theme means auto', h._theme == 'auto')
check('HUD: panel never shown', not h._panel.isVisible())

h2 = H.HUD()                      # default: no glass, nothing created
h2._ensure_built()
check('HUD: no material unless asked for', h2._material is None and h2._palette_key[3] is False)

print('RESULT:', 'OK' if ok else 'FAILED')
sys.exit(0 if ok else 1)
