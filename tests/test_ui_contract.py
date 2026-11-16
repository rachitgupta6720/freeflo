"""The pages and app.py agree on every message name (static, no WebKit).

tests/ui_pages_check.py drives the pages against a fixed contract; this
checks that app.py really implements that contract: every action a page
sends is handled, and every event app.py pushes has a handler in the page.
"""
import os
import re
import unittest

import support

ROOT = support.REPO_ROOT
# Handled inside ui/window.py; they never reach app.py.
WINDOW_ACTIONS = {'window_style', 'copy_text'}
WINDOW_EVENTS = {'window_style', 'window_shown'}


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding='utf-8') as f:
        return f.read()


def page_actions(src):
    return set(re.findall(r"send\('([a-z_]+)'", src))


def page_events(src):
    block = re.search(r'const HANDLERS = \{(.*?)\n  \};', src, re.S)
    return set(re.findall(r'^\s*([a-z_]+):', block.group(1), re.M)) if block else set()


def app_handled(src, method):
    """Action names compared against in one of app.py's message methods."""
    start = src.index('    def %s(' % method)
    end = src.index('\n    def ', start + 10)
    body = src[start:end]
    names = set(re.findall(r"action == '([a-z_]+)'", body))
    for group in re.findall(r"action in \(([^)]*)\)", body):
        names |= set(re.findall(r"'([a-z_]+)'", group))
    return names


class Contract(unittest.TestCase):
    def setUp(self):
        self.app = _read('app.py')
        self.index = _read('ui', 'index.html')
        self.ob = _read('ui', 'onboarding.html')

    def test_every_settings_action_is_handled(self):
        sent = page_actions(self.index) - WINDOW_ACTIONS
        handled = app_handled(self.app, '_on_ui_message')
        self.assertTrue(sent)
        self.assertEqual(sorted(sent - handled), [])

    def test_every_setup_action_is_handled(self):
        sent = page_actions(self.ob) - WINDOW_ACTIONS
        handled = app_handled(self.app, '_on_onboarding_message')
        # The older button names are mapped onto setup flow actions.
        aliases = set(re.findall(r"'(ob_[a-z_]+)': setup_state\.ACT_", self.app))
        self.assertTrue(sent)
        self.assertEqual(sorted(sent - handled - aliases), [])

    def test_every_pushed_event_has_a_page_handler(self):
        ui_events = set(re.findall(r"_push_ui\('([a-z_]+)'", self.app))
        ob_events = set(re.findall(r"_push_ob\('([a-z_]+)'", self.app))
        self.assertEqual(sorted(ui_events - page_events(self.index)), [])
        self.assertEqual(sorted(ob_events - page_events(self.ob)), [])
        self.assertTrue(WINDOW_EVENTS <= page_events(self.index))
        self.assertTrue(WINDOW_EVENTS <= page_events(self.ob))


if __name__ == '__main__':
    unittest.main()
