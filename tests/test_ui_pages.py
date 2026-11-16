"""The settings and onboarding pages, loaded in an off-screen WKWebView and
driven with the payloads Python sends (see tests/ui_pages_check.py).

Runs in a subprocess so its NSApplication never meets the other tests'.
Skipped where PyObjC's WebKit bridge is not installed."""
import os
import subprocess
import sys
import tempfile
import unittest

import support

try:
    import WebKit  # noqa: F401
    HAVE_WEBKIT = True
except Exception:
    HAVE_WEBKIT = False


@unittest.skipUnless(HAVE_WEBKIT, 'needs pyobjc-framework-WebKit')
class Pages(unittest.TestCase):
    def test_pages_render_and_talk_to_python(self):
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              'ui_pages_check.py')
        res = subprocess.run([sys.executable, script, support.REPO_ROOT],
                             capture_output=True, encoding='utf-8',
                             errors='replace', timeout=120)
        failed = [line for line in res.stdout.splitlines()
                  if line.startswith('FAIL')]
        self.assertEqual(failed, [], res.stdout[-3000:] + res.stderr[-2000:])
        self.assertEqual(res.returncode, 0, res.stdout[-3000:] + res.stderr[-2000:])
        self.assertIn('RESULT: OK', res.stdout)

    def test_window_and_hud_chrome(self):
        # ui/window.py (glass material, drag strip, pasteboard, accent) and
        # the HUD's glass, built but never shown. FREEFLO_DATA_DIR keeps it
        # away from the user's real settings.
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              'ui_window_check.py')
        with tempfile.TemporaryDirectory() as data:
            env = dict(os.environ, FREEFLO_DATA_DIR=data)
            res = subprocess.run([sys.executable, script, support.REPO_ROOT],
                                 capture_output=True, encoding='utf-8',
                                 errors='replace', timeout=120, env=env)
        failed = [line for line in res.stdout.splitlines()
                  if line.startswith('FAIL')]
        self.assertEqual(failed, [], res.stdout[-3000:] + res.stderr[-2000:])
        self.assertEqual(res.returncode, 0, res.stdout[-3000:] + res.stderr[-2000:])
        self.assertIn('RESULT: OK', res.stdout)

    def test_pages_against_the_real_app(self):
        # ui/index.html and ui/onboarding.html wired to a real FreefloApp
        # (fake helper, temp settings, no pill / tap / Keychain / System
        # Settings): every message round trip the user can trigger.
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              'ui_app_check.py')
        res = subprocess.run([sys.executable, script, support.REPO_ROOT],
                             capture_output=True, encoding='utf-8',
                             errors='replace', timeout=180)
        failed = [line for line in res.stdout.splitlines()
                  if line.startswith('FAIL')]
        self.assertEqual(failed, [], res.stdout[-3000:] + res.stderr[-2000:])
        self.assertEqual(res.returncode, 0, res.stdout[-3000:] + res.stderr[-2000:])
        self.assertIn('RESULT: OK', res.stdout)


if __name__ == '__main__':
    unittest.main()
