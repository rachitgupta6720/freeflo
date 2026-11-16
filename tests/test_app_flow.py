"""The app's dictation flow, end to end, against the scriptable fake helper.

A real FreefloApp is constructed (menu objects and all) but its run loop is
never started: the hotkey callbacks are called directly, the pill and the
paste are replaced by recorders, and settings/history live in a temp folder.
Needs rumps + PyObjC, so it is skipped on a bare interpreter.
"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

import support
from support import FAKE_HELPER

try:
    import rumps  # noqa: F401
    import app as app_module
    import config
    from engine import history, injector
    HAVE_APP = True
except Exception:                                   # pragma: no cover
    HAVE_APP = False


class FakeHUD(object):
    def __init__(self, *args, **kwargs):
        self.calls = []

    def _note(self, name, *args):
        self.calls.append((name,) + args)

    def prewarm(self):
        self._note('prewarm')

    def show_recording(self):
        self._note('recording')

    def show_processing(self):
        self._note('processing')

    def hide(self):
        self._note('hide')

    def show_notice(self, text, kind='info', seconds=2.0):
        self._note('notice', text, kind)

    def set_level(self, rms):
        pass

    def set_partial(self, text):
        self._note('partial', text)

    def set_partials_enabled(self, on):
        self._note('partials_enabled', on)

    def set_look(self, theme=None, glass=None):
        self._note('look', theme, glass)

    def names(self):
        return [c[0] for c in self.calls]

    def notices(self):
        return [c[1:] for c in self.calls if c[0] == 'notice']


class FakeHotkey(object):
    """Stands in for hotkey.HotkeyListener: no event tap is ever created."""

    def __init__(self, *args, **kwargs):
        self.running = False
        self.starts = 0
        self.stops = 0

    def start(self):
        self.starts += 1
        self.running = True

    def stop(self):
        self.stops += 1
        self.running = False

    def is_running(self):
        return self.running

    def is_live(self):
        return self.running

    def set_bindings(self, *args):
        pass

    def reconcile(self, flags):
        pass

    @staticmethod
    def modifier_flags():
        return None


class FakeWindow(object):
    """Stands in for ui.window.WindowController: records what the page is sent."""

    def __init__(self):
        self.sent = []
        self.closed = False
        self.shown = 0
        self.styles = []

    def show(self):
        self.shown += 1

    def set_style(self, appearance=None, glass=None):
        self.styles.append((appearance, glass))

    def close(self):
        self.closed = True

    def send(self, name, payload):
        self.sent.append((name, payload))

    def of(self, name):
        return [payload for n, payload in self.sent if n == name]


@unittest.skipUnless(HAVE_APP, 'needs rumps + PyObjC')
class AppFlow(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='freeflo-app-')
        self.ctl = os.path.join(self.dir, 'helper')
        os.makedirs(self.ctl)
        self.data = os.path.join(self.dir, 'data')
        os.makedirs(self.data)
        self._saved = {}

        def patch(obj, name, value):
            self._saved.setdefault((obj, name), getattr(obj, name))
            setattr(obj, name, value)
        self.patch = patch

        # Settings, history and vocabulary go to the temp folder -- never the
        # user's real ones.
        patch(config, '_CONFIG_DIR', self.data)
        patch(config, '_CONFIG_FILE', os.path.join(self.data, 'config.json'))
        patch(config, 'get_helper_path', lambda: [sys.executable, FAKE_HELPER])
        patch(app_module, 'HUD', FakeHUD)
        # No real event tap, no real window, no real Accessibility prompt.
        patch(app_module, 'HotkeyListener', FakeHotkey)
        self.ax = {'trusted': True}
        patch(app_module, 'AXIsProcessTrusted', lambda: self.ax['trusted'])
        self.ax_prompts = []
        patch(app_module, 'AXIsProcessTrustedWithOptions',
              lambda options: self.ax_prompts.append(options) or self.ax['trusted'])
        self.windows = []

        def make_window(app_self):
            w = FakeWindow()
            self.windows.append(w)
            return w
        patch(app_module.FreefloApp, '_make_setup_window', make_window)
        self.opened = []        # System Settings panes the app tried to open
        patch(app_module.FreefloApp, '_open_url',
              lambda app_self, url: self.opened.append(url))
        # The India suggestion depends on this Mac's region: pin it.
        patch(app_module.locales, 'system_locale', lambda: 'en-IN')
        self.pasted = []
        self.inject_result = injector.InjectResult(injector.PASTED)

        def fake_inject(text, restore_clipboard=True):
            self.pasted.append(text)
            return self.inject_result
        patch(injector, 'inject', fake_inject)
        patch(injector, 'frontmost_app_id', lambda: 'test.app')
        self._old_env = os.environ.get('FAKE_HELPER_DIR')
        os.environ['FAKE_HELPER_DIR'] = self.ctl
        self.app = None
        self.scenario()

    def tearDown(self):
        try:
            if self.app is not None:
                self.app._engine.shutdown(timeout=3.0)
                self.app._deliver_q.put(None)
                self.app._deliver_thread.join(2.0)
        finally:
            for (obj, name), value in self._saved.items():
                setattr(obj, name, value)
            if self._old_env is None:
                os.environ.pop('FAKE_HELPER_DIR', None)
            else:
                os.environ['FAKE_HELPER_DIR'] = self._old_env
            pids = self.pids()
            self.assertTrue(support.wait_all_dead(pids), 'helper left behind')
            shutil.rmtree(self.dir, ignore_errors=True)

    # -- helpers --------------------------------------------------------

    def scenario(self, default=None, spawns=None):
        tmp = os.path.join(self.ctl, 'scenario.json.tmp')
        with open(tmp, 'w') as f:
            json.dump({'default': default or {}, 'spawns': spawns or []}, f)
        os.replace(tmp, os.path.join(self.ctl, 'scenario.json'))

    def _lines(self, name):
        try:
            with open(os.path.join(self.ctl, name)) as f:
                return [json.loads(line) for line in f if line.strip()]
        except OSError:
            return []

    def pids(self):
        return [s['pid'] for s in self._lines('spawns.log')]

    def commands(self, name):
        return [c['cmd'] for c in self._lines('commands.log')
                if c['cmd'].get('cmd') == name]

    def make(self, settings=None, expect_status=True):
        self.assertTrue(config._CONFIG_FILE.startswith(self.dir))
        # A set-up user of THIS build: onboarded, language already chosen.
        # ('auto' + onboarded means "picked Auto-detect on the Whisper build"
        # and is migrated with a notice - see the language tests.)
        base = dict(config._DEFAULTS, onboarded=True, language='en-IN')
        base.update(settings or {})
        config.save(base)
        self.app = app_module.FreefloApp()
        if expect_status:
            self.assertTrue(self.wait(lambda: self.app._speech_status is not None),
                            'helper never reported a status')
        return self.app

    def wait(self, predicate, timeout=6.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.01)
        return bool(predicate())

    def idle(self):
        a = self.app
        return a._rec_sid is None and not a._dicts

    def state(self):
        return self.app._display_state()[0]

    def dictate(self, mode='toggle', hold=0.05):
        a = self.app
        if mode == 'toggle':
            a._on_toggle()
            self.assertEqual(self.state(), 'recording')
            time.sleep(hold)
            a._on_toggle()
        else:
            a._on_ptt_start()
            self.assertEqual(self.state(), 'recording')
            time.sleep(hold)
            a._on_ptt_stop()
        self.assertTrue(self.wait(self.idle), 'dictation never finished')

    # -- tests ----------------------------------------------------------

    def test_toggle_dictation_is_pasted_and_logged(self):
        self.scenario(default={'text': ' hello world '})
        a = self.make()
        self.dictate('toggle')
        self.assertEqual(self.pasted, ['hello world'])
        self.assertEqual(self.state(), 'idle')
        self.assertEqual(a._hud.names()[-3:], ['recording', 'processing', 'hide'])
        self.assertEqual(a._hud.notices(), [])
        entries = history.list_entries(limit=5)
        self.assertEqual([e['text'] for e in entries], ['hello world'])
        self.assertEqual(entries[0]['mode'], 'toggle')
        self.assertEqual(a._last_text, 'hello world')
        self.assertIn('hello world', a._get_status())

    def test_push_to_talk(self):
        a = self.make()
        self.dictate('ptt', hold=app_module._PTT_MIN_HOLD_SEC + 0.1)
        self.assertEqual(self.pasted, ['hello world'])
        self.assertIn('recording', a._hud.names())     # the delayed pill came up

    def test_a_stray_tap_of_the_key_is_dropped_silently(self):
        a = self.make()
        a._on_ptt_start()
        time.sleep(0.03)
        a._on_ptt_stop()
        self.assertTrue(self.wait(self.idle))
        time.sleep(app_module._PTT_PILL_DELAY_SEC + 0.1)
        self.assertEqual(self.pasted, [])
        self.assertEqual(a._hud.notices(), [])
        self.assertNotIn('recording', a._hud.names())  # the pill never flashed
        self.assertTrue(self.wait(lambda: len(self.commands('cancel')) == 1))
        self.assertEqual(self.commands('stop'), [])

    def test_using_the_key_as_a_modifier_cancels(self):
        a = self.make()
        a._on_ptt_start()
        time.sleep(0.4)
        a._on_ptt_cancel()
        self.assertTrue(self.idle())
        time.sleep(0.2)
        self.assertEqual(self.pasted, [])
        self.assertEqual(a._hud.names()[-1], 'hide')

    def test_escape_cancels_a_recording(self):
        a = self.make()
        a._on_escape()                          # nothing recording: no-op
        a._on_toggle()
        time.sleep(0.05)
        a._on_escape()
        self.assertTrue(self.idle())
        time.sleep(0.2)
        self.assertEqual(self.pasted, [])
        self.assertEqual(self.state(), 'idle')
        self.assertTrue(self.wait(lambda: len(self.commands('cancel')) == 1))
        a._on_toggle()                          # and the next one works
        time.sleep(0.05)
        a._on_toggle()
        self.assertTrue(self.wait(self.idle))
        self.assertEqual(self.pasted, ['hello world'])

    def test_nothing_heard(self):
        self.scenario(default={'text': '   '})
        a = self.make()
        self.dictate()
        self.assertEqual(self.pasted, [])
        self.assertEqual(len(a._hud.notices()), 1)
        self.assertIn('catch', a._hud.notices()[0][0])
        self.assertEqual(history.list_entries(limit=5), [])

    def test_silent_microphone_gets_its_own_message(self):
        self.scenario(default={'text': '', 'final_extra': {'silent': True}})
        a = self.make()
        self.dictate()
        self.assertEqual(self.pasted, [])
        self.assertIn('microphone', a._hud.notices()[0][0].lower())
        self.assertEqual(a._hud.notices()[0][1], 'error')

    def test_vocabulary_is_applied_before_pasting(self):
        self.scenario(default={'text': 'pay by you pee eye on kiwi'})
        a = self.make()
        a._vocab.add('Kiwi')
        a._vocab.add_replacement('you pee eye', 'UPI')
        self.dictate()
        self.assertEqual(self.pasted, ['pay by UPI on Kiwi'])
        self.assertEqual(self.commands('start')[0].get('context'), ['Kiwi', 'UPI'])

    def test_paste_fallback_to_clipboard_tells_the_user(self):
        a = self.make()
        self.inject_result = injector.InjectResult(injector.COPIED_ONLY,
                                                   'accessibility')
        self.dictate()
        self.assertEqual(self.pasted, ['hello world'])
        self.assertEqual(len(a._hud.notices()), 1)
        self.assertIn('Copied', a._hud.notices()[0][0])
        self.assertEqual(len(history.list_entries(limit=5)), 1)   # still saved

    def test_consecutive_dictations_are_joined_with_a_space(self):
        a = self.make()
        self.dictate()
        self.dictate()
        self.assertEqual(self.pasted, ['hello world', ' hello world'])
        a._continuity.invalidate()              # a real key press / click
        self.dictate()
        self.assertEqual(self.pasted[-1], 'hello world')

    def test_mic_denied_is_explained_and_recovers(self):
        self.scenario(default={'start_error': {
            'code': 'mic_denied', 'message': 'no'}})
        a = self.make()
        a._on_toggle()
        self.assertTrue(self.wait(self.idle))
        self.assertEqual(self.state(), 'idle')
        self.assertTrue(self.wait(lambda: len(a._hud.notices()) == 1))
        self.assertIn('Microphone', a._hud.notices()[0][0])
        self.assertEqual(a._hud.notices()[0][1], 'error')
        a._on_toggle()                          # the tap that would have
        self.assertTrue(self.wait(self.idle))   # "stopped" just retries

    def test_helper_crash_mid_dictation_recovers_without_a_restart(self):
        self.scenario(spawns=[{'crash': {'stop': 'kill'}}])
        a = self.make()
        self.dictate()
        self.assertEqual(self.pasted, [])
        self.assertEqual(a._hud.notices()[-1][1], 'error')
        self.assertEqual(self.state(), 'idle')
        self.dictate()                          # replacement helper, no action
        self.assertEqual(self.pasted, ['hello world'])
        self.assertEqual(len(self.pids()), 2)

    def test_a_second_dictation_can_start_while_the_first_is_transcribed(self):
        self.scenario(default={'final_delay': 0.5})
        a = self.make()
        a._on_toggle()
        time.sleep(0.05)
        a._on_toggle()
        self.assertEqual(self.state(), 'processing')
        a._on_toggle()                          # no "busy": mic opens again
        self.assertEqual(self.state(), 'recording')
        time.sleep(0.7)
        self.assertEqual(self.pasted, ['hello world'])     # first one landed
        self.assertEqual(self.state(), 'recording')        # pill still ours
        self.assertNotEqual(a._hud.names()[-1], 'hide')
        a._on_toggle()
        self.assertTrue(self.wait(self.idle))
        self.assertEqual(len(self.pasted), 2)

    def test_disabling_drops_the_recording_and_keeps_the_mic_closed(self):
        a = self.make()
        a._on_toggle()
        time.sleep(0.05)
        a._toggle(a._toggle_item)               # menu: Disable Dictation
        self.assertTrue(self.idle())
        self.assertEqual(self.state(), 'disabled')
        self.assertTrue(self.wait(lambda: len(self.commands('cancel')) == 1))
        a._on_toggle()                          # hotkeys do nothing now
        a._on_ptt_start()
        self.assertTrue(self.idle())
        self.assertFalse(config.load()['enabled'])
        time.sleep(0.2)
        self.assertEqual(self.pasted, [])

    def test_window_mic_test_shows_the_text_and_never_pastes(self):
        self.scenario(default={'text': 'testing one two'})
        a = self.make()
        sent = []
        a._push_ui = lambda name, payload: sent.append((name, payload))
        self.assertTrue(a._begin_recording('test'))
        time.sleep(0.05)
        a._end_recording('test', transcribe=True)
        self.assertTrue(self.wait(self.idle))
        self.assertEqual(self.pasted, [])
        results = [p for n, p in sent if n == 'test_result']
        self.assertEqual(results, [{'text': 'testing one two'}])
        self.assertEqual(len(history.list_entries(limit=5)), 1)

    def test_the_watchdog_clears_a_dictation_that_never_finishes(self):
        a = self.make()
        a._on_toggle()
        time.sleep(0.05)
        # Lose the engine's callback on purpose: the app must still recover.
        a._engine.on_final = None
        a._on_toggle()
        time.sleep(0.3)
        self.assertEqual(self.state(), 'processing')
        with a._lock:
            for d in a._dicts.values():
                d.deadline = time.monotonic() - 1
        a._check_deadlines()
        self.assertTrue(self.idle())
        self.assertEqual(self.state(), 'idle')
        self.assertIn('Timed out', a._hud.notices()[-1][0])

    def test_old_macos_is_explained_instead_of_failing_silently(self):
        old = app_module.speech.macos_version
        app_module.speech.macos_version = lambda: (15, 6)
        try:
            a = self.make(expect_status=False)
        finally:
            app_module.speech.macos_version = old
        self.assertFalse(a._engine.is_available)
        self.assertIn('macOS 26', a._get_status())
        a._on_toggle()
        a._on_ptt_start()
        self.assertTrue(self.idle())
        self.assertIn('macOS 26', a._hud.notices()[-1][0])
        self.assertEqual(self.pids(), [])       # no helper was ever started
        self.assertEqual(a._home_alert()['text'], a._get_status())

    def test_a_missing_helper_is_reported_and_heals_when_it_reappears(self):
        path = os.path.join(self.dir, 'freeflo-helper')
        config.get_helper_path = lambda: path
        a = self.make(expect_status=False)
        a._on_toggle()
        self.assertTrue(self.wait(self.idle))
        self.assertTrue(self.wait(lambda: len(a._hud.notices()) >= 1))
        self.assertIn('reinstall', a._hud.notices()[-1][0])
        self.assertEqual(self.pasted, [])
        with open(path, 'w') as f:
            f.write('#!/bin/sh\nexec "%s" "%s" "$@"\n' % (sys.executable, FAKE_HELPER))
        os.chmod(path, 0o755)
        a._on_toggle()                          # no restart of the app needed
        self.assertTrue(self.wait(lambda: self.state() == 'recording'))
        self.assertTrue(self.wait(lambda: 'recording' in a._hud.names()),
                        'the replacement helper never opened the mic')
        a._on_toggle()
        self.assertTrue(self.wait(self.idle))
        self.assertEqual(self.pasted, ['hello world'])

    def test_legacy_language_setting_is_migrated(self):
        a = self.make({'language': 'auto', 'ptt_key': 'right_command',
                       'save_history': False})
        self.assertTrue(self.wait(lambda: config.load()['language'] != 'auto'))
        stored = config.load()
        self.assertIn(stored['language'], ('en-IN', 'en-US'))
        self.assertEqual(a._locale, stored['language'])
        self.assertEqual(stored['ptt_key'], 'right_command')    # others kept
        self.assertFalse(stored['save_history'])
        self.assertTrue(self.wait(lambda: len(self.commands('prewarm')) >= 1))
        warm = self.commands('prewarm')[0]
        self.assertEqual(warm['locale'], stored['language'])
        self.assertIs(warm.get('partials'), False)      # live text is off
        self.dictate()
        start = self.commands('start')[0]
        self.assertEqual(start['locale'], stored['language'])
        self.assertIs(start['partials'], False)

    def test_legacy_hindi_maps_to_its_locale_and_asks_for_a_download(self):
        self.scenario(default={'start_error': {
            'code': 'locale_not_installed', 'message': 'hi-IN'}})
        a = self.make({'language': 'hi'})
        self.assertTrue(self.wait(lambda: config.load()['language'] == 'hi-IN'))
        # Not installed: its download starts by itself (nothing to click).
        self.assertTrue(self.wait(lambda: self.commands('install')))
        self.assertEqual(self.commands('install')[0]['locale'], 'hi-IN')
        a._on_toggle()
        self.assertTrue(self.wait(self.idle))
        self.assertTrue(self.wait(lambda: len(a._hud.notices()) == 1))
        self.assertIn('download', a._hud.notices()[0][0])

    def test_downloading_a_language_selects_it(self):
        # The older page's "download this one" (no role) means "switch to it".
        a = self.make({'language': 'en-IN'})
        sent = []
        a._push_ui = lambda name, payload: sent.append((name, payload))
        a._install_language({'code': 'hi-IN'})
        self.assertTrue(self.wait(lambda: config.load()['language'] == 'hi-IN'))
        states = [p['state'] for n, p in sent if n == 'language_progress']
        self.assertEqual(states[0], 'downloading')
        self.assertEqual(states[-1], 'downloaded')
        self.assertEqual(a._downloading(), {})
        self.assertEqual(self.commands('install')[0]['locale'], 'hi-IN')

    def test_a_failed_download_is_reported_and_changes_nothing(self):
        self.scenario(default={'install_error': {
            'code': 'network', 'message': 'offline', 'tagged': True}})
        a = self.make({'language': 'en-IN'})
        sent = []
        a._push_ui = lambda name, payload: sent.append((name, payload))
        a._install_language({'id': 'hi-IN', 'code': 'hi-IN'})
        self.assertTrue(self.wait(lambda: any(
            n == 'language_progress' and p['state'] == 'failed' for n, p in sent)))
        failed = [p for n, p in sent if n == 'language_progress'
                  and p['state'] == 'failed'][-1]
        self.assertIn('internet', failed['message'])
        self.assertEqual(failed['id'], 'hi-IN')
        self.assertEqual(config.load()['language'], 'en-IN')
        self.assertEqual(a._downloading(), {})

    # -- onboarding -----------------------------------------------------

    def _keychain_is_off_limits(self):
        def boom():
            raise AssertionError('the Keychain was read')
        self.patch(app_module.gauth, 'is_connected', boom)

    def test_onboarding_gets_its_state_and_can_be_completed(self):
        # First run: the setup window opens by itself (once), shows both
        # rows from the setup flow, and "Done" finishes it - consent saved,
        # window closed by the flow, the hotkey tap started. No Keychain.
        self._keychain_is_off_limits()
        a = self.make({'onboarded': False})
        a._setup_poll()
        self.assertEqual(len(self.windows), 1)
        win = self.windows[0]
        self.assertEqual(win.shown, 1)
        for action in ('ob_get_state', 'ob_get_state_fresh', 'ob_grant_mic'):
            del win.sent[:]
            a._on_onboarding_message({'action': action})
            states = win.of('ob_state')
            self.assertTrue(states, '%s pushed no ob_state' % action)
            st = states[-1]
            self.assertIs(st['mic_granted'], True)
            self.assertEqual(st['mic'], 'granted')
            self.assertIs(st['accessibility'], True)
            self.assertIsNone(st['engine_error'])
            self.assertEqual(st['mode'], 'first_run')
            self.assertEqual([r['id'] for r in st['rows']], ['mic', 'accessibility'])
            self.assertNotIn('google_connected', st)
            shown = [st['title'], st['subtitle'], st.get('waiting') or '']
            shown += [r.get('hint') or '' for r in st['rows']]
            self.assertNotRegex(' '.join(shown), '(?i)restart')
        self.assertEqual(self.commands('request_permissions'), [])  # already granted
        self.assertEqual(a._hotkey.starts, 1)
        time.sleep(1.1)                         # the tap counts as live after 1 s
        a._on_onboarding_message({'action': 'ob_get_state'})
        self.assertTrue(win.of('ob_state')[-1]['can_finish'])
        a._on_onboarding_message({'action': 'ob_complete', 'analytics': False,
                                  'crash': False})
        stored = config.load()
        self.assertTrue(stored['onboarded'])
        self.assertFalse(stored['analytics_enabled'])
        self.assertFalse(stored['crash_enabled'])
        self.assertTrue(stored['install_id'])
        self.assertTrue(win.closed)
        self.assertEqual(len(self.windows), 1)
        self.assertEqual(self.ax_prompts, [])

    def test_onboarding_state_survives_a_denied_microphone(self):
        self._keychain_is_off_limits()
        self.scenario(default={'status': {'mic': 'denied'}, 'start_error': {
            'code': 'mic_denied', 'message': 'no'}})
        a = self.make({'onboarded': False})
        a._on_onboarding_message({'action': 'ob_get_state'})
        win = self.windows[0]
        a._on_onboarding_message({'action': 'ob_test_start'})
        self.assertTrue(self.wait(self.idle))
        self.assertTrue(self.wait(lambda: win.of('ob_test_result')))
        a._on_onboarding_message({'action': 'ob_get_state_fresh'})
        st = win.of('ob_state')[-1]
        self.assertIs(st['mic_granted'], False)
        self.assertEqual(st['mic'], 'denied')
        mic = [r for r in st['rows'] if r['id'] == 'mic'][0]
        self.assertEqual(mic['state'], 'denied')
        # "Allow" on a denied microphone opens System Settings; macOS will
        # not ask again.
        a._on_onboarding_message({'action': 'ob_grant_mic'})
        self.assertEqual(len(self.opened), 1)
        self.assertIn('Privacy_Microphone', self.opened[0])
        self.assertEqual(self.commands('request_permissions'), [])

    def test_an_update_that_lost_accessibility_asks_once_and_only_for_it(self):
        self.ax['trusted'] = False
        a = self.make()                         # onboarded, mic granted
        a._setup_poll()
        self.assertEqual(len(self.windows), 1)
        win = self.windows[0]
        self.assertEqual(win.shown, 1)
        st = win.of('ob_state')[-1]
        self.assertEqual(st['mode'], 'repair')
        self.assertEqual([r['id'] for r in st['rows']], ['accessibility'])
        self.assertEqual(self.ax_prompts, [])          # nothing without a click
        self.assertEqual(self.commands('request_permissions'), [])
        self.assertEqual(a._hotkey.starts, 0)          # no tap while untrusted
        self.assertEqual(a._home_alert()['action'], 'open_setup')
        a._setup_poll()
        a._setup_poll()
        self.assertEqual(len(self.windows), 1)
        self.assertEqual(win.shown, 1)                 # opened once
        a._on_onboarding_message({'action': 'ob_grant_accessibility'})
        self.assertEqual(len(self.ax_prompts), 1)      # first click: the prompt
        self.assertEqual(self.opened, [])
        a._on_onboarding_message({'action': 'ob_grant_accessibility'})
        self.assertEqual(len(self.ax_prompts), 1)      # never twice
        self.assertEqual(len(self.opened), 1)          # then System Settings
        self.assertIn('Privacy_Accessibility', self.opened[0])
        # Switched on in System Settings: the tap is made at once, no restart.
        self.ax['trusted'] = True
        a._setup_poll()
        self.assertEqual(a._hotkey.starts, 1)
        time.sleep(1.1)
        a._setup_poll()
        time.sleep(1.3)
        a._setup_poll()
        self.assertTrue(win.closed)                    # repair window closes itself
        self.assertNotIn(app_module._RESTART_ITEM_TITLE, a.menu)
        self.assertIn('ready', ' '.join(n[0] for n in a._hud.notices()))
        self.assertTrue(config.load()['setup']['granted']['accessibility'])

    def test_a_repair_or_review_window_never_writes_the_privacy_switches(self):
        # Analytics off, crash reports on (Settings > Privacy).
        a = self.make({'analytics_enabled': False, 'crash_enabled': True})
        a._open_setup()
        win = a._ob_ui
        st = win.of('ob_state')[-1]
        self.assertNotEqual(st['mode'], 'first_run')
        self.assertFalse(st['consent']['show'])
        a._on_onboarding_message({'action': 'ob_set_consent', 'analytics': True, 'crash': True})
        a._on_onboarding_message({'action': 'ob_complete', 'analytics': True, 'crash': True})
        stored = config.load()
        self.assertEqual((stored['analytics_enabled'], stored['crash_enabled']), (False, True))

    def test_first_run_saves_the_consent_answer(self):
        a = self.make({'onboarded': False})
        a._setup_poll()
        st = self.windows[0].of('ob_state')[-1]
        self.assertEqual(st['mode'], 'first_run')
        self.assertTrue(st['consent']['show'])
        a._on_onboarding_message({'action': 'ob_set_consent', 'analytics': False, 'crash': False})
        stored = config.load()
        self.assertEqual((stored['analytics_enabled'], stored['crash_enabled']), (False, False))

    def test_the_setup_confirmation_is_not_covered_by_a_queued_notice(self):
        a = self.make()
        a._queue_notice('Hindi + English is on', 'success', 3.5)
        a._run_setup([(app_module.setup_state.EFFECT_NOTICE, 'ready',
                       'freeflo is ready - hold Left Option and speak', 'success')])
        a._flush_notices()
        self.assertEqual([n[0] for n in a._hud.notices()],
                         ['freeflo is ready - hold Left Option and speak'])
        with a._lock:
            a._notice_until = 0.0                   # ... once it has been read
        a._flush_notices()
        self.assertEqual(a._hud.notices()[-1][0], 'Hindi + English is on')

    def test_restart_item_is_added_while_the_setup_item_is_retitled(self):
        a = self.make()
        self.patch(a._setup, 'menu', lambda: {'setup': True, 'restart': True})
        a._sync_setup_menu()
        self.assertEqual(a._setup_item.title, app_module._SETUP_NEEDED_TITLE)
        self.assertIn(app_module._RESTART_ITEM_TITLE, a.menu)
        self.patch(a._setup, 'menu', lambda: {'setup': False, 'restart': False})
        a._sync_setup_menu()
        self.assertNotIn(app_module._RESTART_ITEM_TITLE, a.menu)

    def test_an_unasked_microphone_is_not_requested_by_itself(self):
        self.scenario(default={'status': {'mic': 'undetermined'}})
        a = self.make()
        a._setup_poll()
        time.sleep(0.2)
        a._setup_poll()
        self.assertEqual(self.commands('request_permissions'), [])
        self.assertEqual(len(self.windows), 1)
        st = self.windows[0].of('ob_state')[-1]
        self.assertEqual([r['id'] for r in st['rows']], ['mic'])
        a._on_onboarding_message({'action': 'ob_grant_mic'})
        a._on_onboarding_message({'action': 'ob_grant_mic'})
        self.assertTrue(self.wait(lambda: len(self.commands('request_permissions')) == 1))
        time.sleep(0.2)
        self.assertEqual(len(self.commands('request_permissions')), 1)

    def test_everything_granted_opens_nothing_and_starts_the_tap_once(self):
        a = self.make()
        for _ in range(3):
            a._setup_poll()
        self.assertEqual(self.windows, [])
        self.assertEqual(a._hotkey.starts, 1)
        self.assertEqual(self.ax_prompts, [])
        self.assertFalse(a._setup.busy)
        self.assertNotIn(app_module._RESTART_ITEM_TITLE, a.menu)
        self.assertEqual(a._setup_item.title, app_module._SETUP_ITEM_TITLE)

    def test_enabling_dictation_without_accessibility_never_makes_a_tap(self):
        self.ax['trusted'] = False
        a = self.make({'enabled': False})
        a._setup_poll()
        self.assertEqual(self.windows, [])            # switched off: no window
        a._toggle(a._toggle_item)                     # Enable Dictation
        self.assertTrue(a._enabled)
        self.assertEqual(a._hotkey.starts, 0)
        self.assertEqual(a._get_status(), 'Accessibility access is needed')

    # -- push-to-talk: lost key-up check, late clicks --------------------

    def test_our_own_paste_does_not_end_a_held_push_to_talk(self):
        # A is being pasted while the key is already held again for B. The
        # paste is Cmd+V with Option cleared; the check must go by what is
        # PHYSICALLY held (see test_hotkey: modifier_flags reads the HID
        # state), which stays "Option down" throughout.
        self.scenario(default={'final_delay': 0.3})
        a = self.make()
        held = {'flags': a._ptt_mask}
        self.patch(app_module.HotkeyListener, 'modifier_flags',
                   staticmethod(lambda: held['flags']))
        a._on_ptt_start()
        time.sleep(app_module._PTT_MIN_HOLD_SEC + 0.05)
        a._on_ptt_stop()                        # A: being transcribed
        a._on_ptt_start()                       # B: key held again at once
        b_sid = a._rec_sid
        self.assertIsNotNone(b_sid)
        self.assertTrue(self.wait(lambda: len(self.pasted) == 1))    # A lands
        for _ in range(app_module._PTT_LOST_RELEASE_POLLS + 3):
            a._check_ptt_release()
        self.assertEqual(a._rec_sid, b_sid, 'B was cut off while still held')
        # The real safety net still works: the key reads up, B is ended.
        held['flags'] = 0
        for _ in range(app_module._PTT_LOST_RELEASE_POLLS):
            a._check_ptt_release()
        self.assertTrue(self.wait(self.idle))
        self.assertEqual(len(self.pasted), 2)

    def test_a_late_click_in_a_hold_keeps_the_dictation(self):
        self.scenario(default={'text': 'twenty seconds of speech'})
        a = self.make()
        a._on_ptt_start()
        time.sleep(app_module._PTT_MIN_HOLD_SEC + 0.1)
        a._on_ptt_interrupt()                   # clicked into the text field
        self.assertEqual(self.state(), 'recording')
        a._on_ptt_stop()
        self.assertTrue(self.wait(self.idle))
        self.assertEqual(self.pasted, ['twenty seconds of speech'])

    def test_a_slow_modifier_use_with_nothing_said_stays_quiet(self):
        self.scenario(default={'text': ''})
        a = self.make()
        a._on_ptt_start()
        time.sleep(app_module._PTT_MIN_HOLD_SEC + 0.1)
        a._on_ptt_interrupt()                   # a slow Option-click
        a._on_ptt_stop()
        self.assertTrue(self.wait(self.idle))
        time.sleep(0.1)
        self.assertEqual(self.pasted, [])
        self.assertEqual(a._hud.notices(), [])  # no "didn't catch that"
        self.assertEqual(a._get_status(), 'Ready')

    # -- the pill only says "recording" once the mic is live -------------

    def test_the_pill_waits_for_the_microphone(self):
        self.scenario(default={'async_start': 0.4})
        a = self.make()
        a._on_toggle()
        time.sleep(0.2)
        self.assertEqual(self.state(), 'recording')         # menu-bar icon
        self.assertNotIn('recording', a._hud.names())       # but no pill yet
        self.assertTrue(self.wait(lambda: 'recording' in a._hud.names()))
        self.assertEqual(a._hud.names().count('recording'), 1)
        a._on_toggle()
        self.assertTrue(self.wait(self.idle))
        self.assertEqual(self.pasted, ['hello world'])

    def test_no_pill_for_a_start_that_never_gets_a_microphone(self):
        self.scenario(default={'hang': ['start']})
        a = self.make()
        a._engine.start_timeout = 0.4
        a._on_toggle()
        self.assertTrue(self.wait(self.idle))
        self.assertNotIn('recording', a._hud.names())
        self.assertEqual(a._hud.notices()[-1][1], 'error')

    # -- recordings that end by themselves -------------------------------

    def test_hitting_the_length_cap_is_announced_and_the_stop_tap_is_swallowed(self):
        self.scenario(default={'text': 'five minutes of talking'})
        a = self.make()
        a._engine.max_session_sec = 0.4         # stands in for 300 s
        a._on_toggle()
        self.assertTrue(self.wait(self.idle))   # the cap ended it
        self.assertEqual(self.pasted, ['five minutes of talking'])
        self.assertTrue(self.wait(lambda: len(a._hud.notices()) == 1))
        self.assertIn('limit', a._hud.notices()[0][0])
        self.assertEqual(a._hud.notices()[0][1], 'error')
        # Still talking, the user now taps to "stop". That must not open the
        # microphone again behind their back.
        a._on_toggle()
        self.assertTrue(self.idle())
        self.assertEqual(self.state(), 'idle')
        self.assertEqual(len(self.commands('start')), 1)
        self.assertIn('already stopped', a._hud.notices()[-1][0])
        # A deliberate second tap starts a new dictation as usual.
        a._engine.max_session_sec = None
        a._on_toggle()
        self.assertEqual(self.state(), 'recording')
        time.sleep(0.05)
        a._on_toggle()
        self.assertTrue(self.wait(self.idle))
        self.assertEqual(len(self.pasted), 2)

    def test_a_normal_toggle_stop_arms_no_guard(self):
        a = self.make()
        self.dictate('toggle')
        a._on_toggle()                          # next tap records at once
        self.assertEqual(self.state(), 'recording')
        a._on_escape()

    def test_a_stalled_microphone_still_delivers_what_was_said(self):
        self.scenario(default={'level_count': 5, 'text': 'said before the stall'})
        a = self.make()
        a._engine.level_stall_timeout = 0.3
        a._on_toggle()
        self.assertTrue(self.wait(self.idle))
        self.assertEqual(self.pasted, ['said before the stall'])
        self.assertEqual(len(self.pids()), 1, 'the helper was killed')
        self.assertEqual(len(self.commands('stop')), 1)
        self.assertTrue(self.wait(lambda: len(a._hud.notices()) == 1))
        self.assertIn('Microphone', a._hud.notices()[0][0])

    # -- mic tests must not take the hotkeys hostage ----------------------

    def test_closing_the_window_ends_a_running_mic_test(self):
        a = self.make()
        win = a._ui = FakeWindow()
        self.assertTrue(a._begin_recording('test'))
        self.assertFalse(a._abort_test_recording('ob_test'))    # other window
        self.assertEqual(self.state(), 'recording')
        self.assertTrue(a._abort_test_recording('test'))        # windowWillClose
        self.assertTrue(self.idle())
        self.assertTrue(self.wait(lambda: len(self.commands('cancel')) >= 1))
        self.assertEqual(win.of('test_result')[-1].get('note'), 'Cancelled')
        self.assertFalse(a._abort_test_recording('test'))       # nothing left
        self.dictate('toggle')                  # an ordinary dictation is
        self.assertFalse(a._abort_test_recording())             # never touched
        self.assertEqual(self.pasted, ['hello world'])

    def test_a_hotkey_takes_over_from_a_forgotten_mic_test(self):
        for mode in ('ptt', 'toggle'):
            a = self.app or self.make()
            win = a._ob_ui = FakeWindow()
            self.assertTrue(a._begin_recording('ob_test'))
            test_sid = a._rec_sid
            if mode == 'ptt':
                a._on_ptt_start()
            else:
                a._on_toggle()
            self.assertNotEqual(a._rec_sid, test_sid)
            self.assertEqual(a._dicts[a._rec_sid].mode, mode)
            self.assertEqual(win.of('ob_test_result')[-1].get('note'), 'Cancelled')
            time.sleep(app_module._PTT_MIN_HOLD_SEC + 0.1)
            if mode == 'ptt':
                a._on_ptt_stop()
            else:
                a._on_toggle()
            self.assertTrue(self.wait(self.idle))
        self.assertEqual(len(self.pasted), 2)

    # -- the main-thread poll ---------------------------------------------

    def test_revoking_accessibility_takes_the_event_tap_out(self):
        # An active tap that macOS no longer lets run stalls keyboard and
        # mouse-button input for every app until it is removed.
        a = self.make()
        a._poll_state(None)
        tap = a._hotkey
        self.assertEqual(tap.starts, 1)
        a._on_toggle()
        self.assertEqual(self.state(), 'recording')
        self.ax['trusted'] = False
        stops = tap.stops
        a._setup_kick = True
        a._poll_state(None)
        self.assertEqual(tap.stops, stops + 1)
        self.assertFalse(tap.running)
        self.assertTrue(self.idle())            # and the mic is not left open
        self.assertTrue(self.wait(lambda: len(self.commands('cancel')) >= 1))
        self.assertEqual(a._get_status(), 'Accessibility access is off')
        self.assertEqual(self.windows, [])      # a notice, never a window
        a._setup_kick = True
        a._poll_state(None)
        self.assertEqual(tap.stops, stops + 1)  # once

    def test_the_language_notice_is_posted_from_the_main_thread_poll(self):
        posted = []
        self.patch(app_module.rumps, 'notification',
                   lambda *args, **kwargs: posted.append(args))
        self.scenario(default=self.MIC)
        a = self.make({'language': 'auto'})
        self.assertTrue(self.wait(lambda: a._lang_notice_dirty))
        a._poll_state(None)                     # setup decides: nothing to do
        self.assertEqual(len(posted), 1)
        self.assertIn('Auto-detect', posted[0][2])
        a._poll_state(None)
        self.assertEqual(len(posted), 1)        # once

    # -- language migration is never silent -------------------------------

    MIC = {'status': {'input_device': 'Test Microphone'}}

    def test_a_chosen_auto_detect_is_replaced_with_a_notice(self):
        self.scenario(default=self.MIC)
        a = self.make({'language': 'auto'})     # onboarded on the old build
        self.assertTrue(self.wait(lambda: config.load()['language'] != 'auto'))
        stored = config.load()
        self.assertEqual(stored['language_notice'],
                         {'was': 'auto', 'now': stored['language']})
        self.assertTrue(self.wait(lambda: a._lang_notice_dirty))  # notification queued
        self.assertEqual(a._hud.notices(), [])  # the pill waits for the poll
        a._poll_state(None)
        self.assertEqual(len(a._hud.notices()), 1)
        self.assertIn('Auto-detect', a._hud.notices()[0][0])
        alert = a._home_alert()
        self.assertIn('Auto-detect is no longer available', alert['text'])
        self.assertEqual(alert['action'], 'ack_language_notice')
        # The banner's button acknowledges it and opens Settings.
        win = a._ui = FakeWindow()
        a._on_ui_message({'action': 'ack_language_notice'})
        self.assertEqual(win.of('navigate'), [{'view': 'settings',
                                               'section': 'language'}])
        self.assertNotIn('language_notice', config.load())
        self.assertIsNone(a._home_alert())
        self.assertIsNone(win.of('home')[-1]['alert'])

    def test_choosing_a_language_settles_the_notice(self):
        self.scenario(default=self.MIC)
        a = self.make({'language': 'auto'})
        self.assertTrue(self.wait(lambda: 'language_notice' in config.load()))
        a._set_language('en-US')
        self.assertNotIn('language_notice', config.load())
        self.assertIsNone(a._home_alert())

    def test_an_unavailable_language_is_named_in_the_notice(self):
        self.scenario(default=self.MIC)
        a = self.make({'language': 'ar'})       # the fake helper has no Arabic
        self.assertTrue(self.wait(lambda: 'language_notice' in config.load()))
        self.assertEqual(config.load()['language_notice']['was'], 'ar')
        self.assertIn('not available for dictation', a._home_alert()['text'])

    def test_migrations_that_keep_the_language_say_nothing(self):
        self.scenario(default=self.MIC)
        a = self.make({'language': 'en'})       # the old default
        self.assertTrue(self.wait(lambda: config.load()['language'] != 'en'))
        time.sleep(0.1)
        a._poll_state(None)
        self.assertNotIn('language_notice', config.load())
        self.assertEqual(a._hud.notices(), [])
        self.assertIsNone(a._home_alert())

    def test_a_fresh_install_gets_no_language_notice(self):
        # 'auto' is also the new "not chosen yet" default.
        self.patch(app_module.gauth, 'is_connected', lambda: False)
        a = self.make({'language': 'auto', 'onboarded': False})
        self.assertTrue(self.wait(lambda: config.load()['language'] != 'auto'))
        time.sleep(0.1)
        self.assertNotIn('language_notice', config.load())
        self.assertEqual(a._hud.notices(), [])

    # -- two languages ------------------------------------------------------

    INDIA = {'status': {'input_device': 'Test Microphone', 'max_languages': 2,
                        'supported': ['en-US', 'en-IN', 'hi-IN', 'mul-IN', 'fr-FR'],
                        'installed': ['en-US', 'en-IN', 'hi-IN', 'mul-IN'],
                        'groups': [['hi-IN', 'mul-IN']]}}

    def test_hindi_is_added_for_an_english_speaker_in_india(self):
        # The user's real config: language chosen, no 'languages' yet.
        self.scenario(default=self.INDIA)
        a = self.make({'language': 'en-IN', 'theme': 'glass', 'glass': True})
        self.assertTrue(self.wait(lambda: config.load().get('languages') == ['en-IN', 'hi-IN']))
        self.assertEqual(config.load()['language'], 'en-IN')
        self.assertEqual(a._languages, ['en-IN', 'hi-IN'])
        self.assertTrue(self.wait(lambda: any(
            c.get('languages') == ['en-IN', 'hi-IN'] for c in self.commands('prewarm'))))
        a._poll_state(None)
        self.assertIn('Hindi + English is on', [n[0] for n in a._hud.notices()])
        self.dictate()
        start = self.commands('start')[-1]
        self.assertEqual(start['locale'], 'en-IN')
        self.assertEqual(start['languages'], ['en-IN', 'hi-IN'])
        # Removing Hindi is respected for good.
        a._on_ui_message({'action': 'set_languages', 'primary': 'en-IN', 'additional': []})
        self.assertEqual(config.load()['languages'], ['en-IN'])
        a._engine.request_status()
        time.sleep(0.3)
        self.assertEqual(config.load()['languages'], ['en-IN'])
        self.dictate()
        self.assertNotIn('languages', self.commands('start')[-1])

    def test_choosing_a_second_language_downloads_it_and_keeps_the_main_one(self):
        india = json.loads(json.dumps(self.INDIA))
        india['status']['installed'] = ['en-US', 'en-IN']
        india['install_delay'] = 0.05
        self.scenario(default=india)
        a = self.make({'language': 'en-IN', 'languages': ['en-IN']})
        win = a._ui = FakeWindow()
        a._on_ui_message({'action': 'get_languages'})
        p = win.of('languages')[-1]
        self.assertTrue(p['ready'])
        self.assertEqual((p['primary'], p['additional'], p['max_additional']),
                         ('en-IN', [], 1))
        self.assertEqual([r['id'] for r in p['languages']][:3], ['en-IN', 'hi-IN', 'mul-IN'])
        hi = [r for r in p['languages'] if r['id'] == 'hi-IN'][0]
        self.assertEqual((hi['name'], hi['state']), ('Hindi', 'available'))
        self.assertEqual(p['hint']['id'], 'hi-IN')
        self.assertIn('620 MB', p['hint']['text'])
        # One click: choose + download + use.
        a._on_ui_message({'action': 'set_languages', 'primary': 'en-IN',
                          'additional': ['hi-IN']})
        self.assertEqual(config.load()['languages'], ['en-IN', 'hi-IN'])
        self.assertTrue(self.wait(lambda: self.commands('install')))
        self.assertEqual(self.commands('install')[0]['locale'], 'hi-IN')
        prog = win.of('language_progress')
        self.assertEqual((prog[0]['id'], prog[0]['state'], prog[0]['progress']),
                         ('hi-IN', 'downloading', 0.0))
        # While it downloads, dictation uses the main language alone.
        self.dictate()
        self.assertNotIn('languages', self.commands('start')[-1])
        self.assertTrue(self.wait(lambda: any(
            q['state'] == 'downloaded' for q in win.of('language_progress'))))
        self.assertEqual(config.load()['language'], 'en-IN')    # main unchanged
        self.assertTrue(self.wait(lambda: any(
            c.get('languages') == ['en-IN', 'hi-IN'] for c in self.commands('prewarm'))))
        last = win.of('languages')[-1]
        self.assertEqual([r['state'] for r in last['languages'] if r['id'] == 'hi-IN'],
                         ['downloaded'])
        self.dictate()
        self.assertEqual(self.commands('start')[-1]['languages'], ['en-IN', 'hi-IN'])

    def test_a_language_left_out_of_a_dictation_is_downloaded_by_itself(self):
        india = json.loads(json.dumps(self.INDIA))
        india['started_extra'] = {'missing': ['fr-FR']}
        self.scenario(default=india)
        a = self.make({'language': 'en-IN', 'languages': ['en-IN']})
        a._on_ui_message({'action': 'noop'})
        a._sp_started_info(1, {'locale': 'en-IN', 'missing': ['fr-FR']})
        self.assertTrue(self.wait(lambda: self.commands('install')))
        self.assertEqual(self.commands('install')[0]['locale'], 'fr-FR')
        # An installed one (the helper fell back to one language) is not.
        a._sp_started_info(2, {'locale': 'en-IN', 'missing': ['hi-IN']})
        time.sleep(0.2)
        self.assertEqual(len(self.commands('install')), 1)

    def _no_fr(self, **extra):
        india = json.loads(json.dumps(self.INDIA))
        india.update(extra)
        return india

    def test_a_main_language_that_needs_a_download_keeps_the_old_one_until_it_lands(self):
        # fr-FR is supported but not installed: choosing it must not stop
        # dictation for the whole download (1.5.0 behaviour kept).
        self.scenario(default=self._no_fr(install_delay=0.15))
        a = self.make({'language': 'en-IN', 'languages': ['en-IN']})
        win = a._ui = FakeWindow()
        a._on_ui_message({'action': 'set_languages', 'primary': 'fr-FR', 'additional': []})
        self.assertEqual(config.load()['languages'], ['en-IN'])     # not yet
        self.assertEqual(a._languages, ['en-IN'])
        self.assertTrue(self.wait(lambda: self.commands('install')))
        self.assertEqual(self.commands('install')[0]['locale'], 'fr-FR')
        p = win.of('languages')[-1]
        self.assertEqual((p['primary'], p['pending']), ('en-IN', 'fr-FR'))
        self.assertIn('French', a._get_status())
        self.dictate()                                  # still works, in English
        self.assertEqual(self.commands('start')[-1]['locale'], 'en-IN')
        # The switch happens when the model is here.
        self.assertTrue(self.wait(lambda: config.load()['languages'] == ['fr-FR']))
        self.assertEqual(a._languages, ['fr-FR'])
        self.assertIsNone(win.of('languages')[-1]['pending'])
        self.dictate()
        self.assertEqual(self.commands('start')[-1]['locale'], 'fr-FR')

    def test_a_failed_main_language_download_leaves_the_old_one_on(self):
        self.scenario(default=self._no_fr(install_error={
            'code': 'network', 'message': 'offline', 'tagged': True}))
        a = self.make({'language': 'en-IN', 'languages': ['en-IN']})
        win = a._ui = FakeWindow()
        a._on_ui_message({'action': 'set_languages', 'primary': 'fr-FR', 'additional': []})
        self.assertTrue(self.wait(lambda: any(
            q['state'] == 'failed' for q in win.of('language_progress'))))
        self.assertEqual(config.load()['languages'], ['en-IN'])
        self.assertEqual(a._languages, ['en-IN'])
        self.assertIsNone(a._pending_languages)
        self.assertIsNone(win.of('languages')[-1]['pending'])
        a._poll_state(None)
        self.assertIn('Still using English (India)', ' '.join(n[0] for n in a._hud.notices()))
        self.assertNotIn('downloading', a._get_status())
        self.dictate()
        self.assertEqual(self.commands('start')[-1]['locale'], 'en-IN')

    def test_a_chosen_language_that_is_missing_is_downloaded_at_launch(self):
        # Hindi was chosen but its download never finished (quit, sleep,
        # helper restart): the next launch fetches it with no click.
        india = self._no_fr(install_delay=0.05)
        india['status']['installed'] = ['en-US', 'en-IN']
        self.scenario(default=india)
        a = self.make({'language': 'en-IN', 'languages': ['en-IN', 'hi-IN']})
        self.assertTrue(self.wait(lambda: self.commands('install')))
        self.assertEqual(self.commands('install')[0]['locale'], 'hi-IN')
        self.assertTrue(self.wait(lambda: any(
            c.get('languages') == ['en-IN', 'hi-IN'] for c in self.commands('prewarm'))))
        self.assertEqual(len(self.commands('install')), 1)
        self.assertIsNone(a._home_alert())
        self.assertEqual(a._get_status(), 'Language: English (India) + Hindi')

    def test_a_failed_second_language_is_explained_and_retried_after_a_restart(self):
        india = self._no_fr()
        india['status']['installed'] = ['en-US', 'en-IN']
        broken = json.loads(json.dumps(india))
        broken['install_error'] = {'code': 'network', 'message': 'offline', 'tagged': True}
        self.scenario(default=india, spawns=[broken])
        a = self.make({'language': 'en-IN', 'languages': ['en-IN', 'hi-IN']})
        self.assertTrue(self.wait(lambda: (a._install_state('hi-IN') or {}).get('state') == 'failed'))
        alert = a._home_alert()
        self.assertEqual((alert['action'], alert['code'], alert['button'], alert['level']),
                         ('install_language', 'hi-IN', 'Try again', 'info'))
        self.assertIn('listens for English (India) only', alert['text'])
        self.assertIn('couldn’t download', a._get_status())
        a._poll_state(None)
        self.assertIn('Hindi couldn’t download', ' '.join(n[0] for n in a._hud.notices()))
        # Backed off: another status does not hammer the network.
        a._engine.request_status()
        time.sleep(0.3)
        self.assertEqual(len(self.commands('install')), 1)
        # Once the back-off has passed, a new helper fetches it by itself.
        with a._lock:
            a._install_retry = dict((k, (0.0, v[1])) for k, v in a._install_retry.items())
        a._engine.restart('test')
        self.assertTrue(self.wait(lambda: len(self.commands('install')) >= 2, timeout=10))
        self.assertTrue(self.wait(lambda: any(
            c.get('languages') == ['en-IN', 'hi-IN'] for c in self.commands('prewarm')), timeout=10))
        self.assertTrue(self.wait(lambda: a._home_alert() is None))

    def test_removing_hindi_stops_the_suggestion(self):
        self.scenario(default=self.INDIA)
        a = self.make({'language': 'en-IN', 'languages': ['en-IN', 'hi-IN']})
        win = a._ui = FakeWindow()
        a._on_ui_message({'action': 'set_languages', 'primary': 'en-IN', 'additional': []})
        self.assertTrue(config.load().get('hindi_hint_dismissed'))
        a._on_ui_message({'action': 'get_languages'})
        self.assertIsNone(win.of('languages')[-1]['hint'])

    def test_settings_never_read_the_keychain(self):
        self._keychain_is_off_limits()
        a = self.make({'backup_account_email': 'someone@example.com',
                       'backup_enabled': False})
        win = a._ui = FakeWindow()
        a._on_ui_message({'action': 'get_backup_status'})
        st = win.of('backup_status')[-1]
        self.assertTrue(st['connected'])
        self.assertEqual(st['email'], 'someone@example.com')
        a._maybe_sync_backup(manual=False)          # backup off: no Keychain either

    def test_two_language_final_credits_the_dominant_language(self):
        india = json.loads(json.dumps(self.INDIA))
        india['text'] = 'kal raat ko server down ho gaya'
        india['final_extra'] = {'languages': ['en-IN', 'hi-IN'],
                                'used': ['hi-IN'], 'dominant': 'hi-IN'}
        self.scenario(default=india)
        self.make({'language': 'en-IN', 'languages': ['en-IN', 'hi-IN']})
        self.dictate()
        self.assertEqual(self.pasted, ['kal raat ko server down ho gaya'])
        self.assertEqual(history.list_entries(limit=1)[0]['language'], 'hi-IN')

    def test_language_menu_offers_one_click_modes(self):
        self.scenario(default=self.INDIA)
        a = self.make({'language': 'en-IN', 'languages': ['en-IN', 'hi-IN']})
        a._rebuild_language_menu()
        titles = [str(k) for k in a._lang_menu.keys()]
        self.assertTrue(titles[0].startswith('✓') and 'English (India) + Hindi' in titles[0],
                        titles)
        self.assertTrue(any('English (India) only' in t for t in titles), titles)
        self.assertTrue(any(t.strip().startswith('Hindi') and 'only' in t for t in titles), titles)
        self.assertIn('More Languages…', titles)
        a._set_languages(['en-IN'])
        a._rebuild_language_menu()
        titles = [str(k) for k in a._lang_menu.keys()]
        self.assertTrue(any('English (India) + Hindi' in t and not t.startswith('✓')
                            for t in titles), titles)

    # -- appearance, about ----------------------------------------------------

    def test_appearance_and_glass_reach_every_window_and_the_pill(self):
        a = self.make({'theme': 'glass', 'glass': True})
        win = a._ui = FakeWindow()
        ob = a._ob_ui = FakeWindow()
        a._on_ui_message({'action': 'get_theme'})
        self.assertEqual(win.of('theme')[-1],
                         {'theme': 'glass', 'glass': True, 'appearance': 'system'})
        a._on_ui_message({'action': 'set_theme', 'theme': 'dark',
                          'appearance': 'dark', 'glass': False})
        stored = config.load()
        self.assertEqual((stored['appearance'], stored['glass'], stored['theme']),
                         ('dark', False, 'dark'))
        self.assertEqual(ob.styles[-1], ('dark', False))
        self.assertEqual(a._hud.calls[-1], ('look', 'dark', False))
        self.assertEqual(win.of('theme')[-1],
                         {'theme': 'dark', 'glass': False, 'appearance': 'dark'})
        a._on_ui_message({'action': 'set_theme', 'theme': 'glass'})   # older page
        stored = config.load()
        self.assertEqual((stored['appearance'], stored['glass'], stored['theme']),
                         ('dark', True, 'glass'))

    # -- "hold the key and speak", tried in the setup window --------------

    def _setup_in_front(self, a):
        a._setup_poll()
        a._open_setup()
        win = self.windows[-1]
        win.is_key = True           # ui.window.WindowController.is_key
        del win.sent[:]
        return win

    def test_a_key_dictation_made_in_the_setup_window_is_shown_there(self):
        self.scenario(default={'text': ' namaste team '})
        a = self.make()
        win = self._setup_in_front(a)
        self.dictate('ptt', hold=app_module._PTT_MIN_HOLD_SEC + 0.2)
        self.assertTrue(self.wait(lambda: win.of('ob_test_result')))
        tried = [(n, p) for n, p in win.sent if n.startswith('ob_test')]
        self.assertEqual(tried, [
            ('ob_test_state', {'recording': True, 'via': 'hotkey'}),
            ('ob_test_state', {'recording': False}),
            ('ob_test_result', {'text': 'namaste team'})])
        self.assertEqual(self.pasted, [])                    # not into that window
        self.assertEqual(history.list_entries(limit=5), [])  # a try-out, not kept
        self.assertEqual(self.state(), 'idle')
        # The page is told that the key works there.
        a._on_onboarding_message({'action': 'ob_get_state'})
        self.assertIs(win.of('ob_state')[-1]['try_hotkey'], False)   # tap < 1 s old
        time.sleep(1.1)
        a._on_onboarding_message({'action': 'ob_get_state'})
        self.assertIs(win.of('ob_state')[-1]['try_hotkey'], True)
        # Another app in front (the window is open but not key): pasted.
        win.is_key = False
        del win.sent[:]
        self.dictate('toggle')
        self.assertEqual(self.pasted, ['namaste team'])
        self.assertEqual([n for n, _ in win.sent if n.startswith('ob_test')], [])
        self.assertEqual(len(history.list_entries(limit=5)), 1)

    def test_a_tap_of_the_key_in_the_setup_window_says_to_hold_it(self):
        a = self.make()
        win = self._setup_in_front(a)
        a._on_ptt_start()                # a stray tap: the page never hears of it
        time.sleep(0.03)
        a._on_ptt_stop()
        self.assertTrue(self.wait(self.idle))
        time.sleep(app_module._PTT_PILL_DELAY_SEC + 0.1)
        self.assertEqual([n for n, _ in win.sent if n.startswith('ob_test')], [])
        a._on_ptt_start()                # held, then used as a modifier
        self.assertTrue(self.wait(lambda: win.of('ob_test_state')))
        a._on_ptt_cancel()
        self.assertTrue(self.idle())
        self.assertEqual(win.of('ob_test_result'),
                         [{'text': '', 'note': app_module._TRY_HOLD_NOTE}])
        self.assertEqual(self.pasted, [])

    def test_closing_the_setup_window_mid_dictation_pastes_it_after_all(self):
        a = self.make()
        win = self._setup_in_front(a)
        a._on_toggle()
        time.sleep(0.05)
        win.is_key = False
        a._on_setup_window_closed()
        self.assertEqual(self.state(), 'recording')     # a real dictation: kept
        a._on_toggle()
        self.assertTrue(self.wait(self.idle))
        self.assertEqual(self.pasted, ['hello world'])
        self.assertEqual(win.of('ob_test_result'), [])

    def test_settings_messages_that_are_gone_do_nothing(self):
        # 'set_language' (one language, from the page before two existed)
        # is no longer part of the contract: the page sends set_languages.
        a = self.make()
        win = a._ui = FakeWindow()
        a._on_ui_message({'action': 'set_language', 'code': 'en-US'})
        self.assertEqual(a._languages[0], 'en-IN')
        self.assertEqual(win.sent, [])
        a._on_ui_message({'action': 'get_shortcuts'})
        self.assertEqual(sorted(win.of('shortcuts')[-1]),
                         ['keys', 'live_text', 'ptt_key', 'sound', 'toggle_key'])
        n = len(self.commands('status'))
        a._on_ui_message({'action': 'get_languages'})
        self.assertTrue(win.of('languages'))
        # ...and the helper is asked again, so a language pack added in
        # System Settings shows up without reopening the window.
        self.assertTrue(self.wait(lambda: len(self.commands('status')) > n))

    def test_about_lists_what_is_wired(self):
        a = self.make()
        win = a._ui = FakeWindow()
        a._on_ui_message({'action': 'get_about'})
        about = win.of('about')[-1]
        self.assertEqual(about['version'], config.get_version())
        self.assertEqual(sorted(about['can']), ['check_updates', 'copy_diagnostics',
                                                'open_setup', 'reveal_logs'])
        a._on_ui_message({'action': 'open_setup'})
        self.assertEqual(len(self.windows), 1)


if __name__ == '__main__':
    unittest.main()
