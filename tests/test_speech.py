"""SpeechEngine supervisor tests, run against tests/fake_helper.py.

No microphone, no real helper, no Accessibility. Run with:

    python3 -m unittest discover -s tests
"""
import os
import random
import signal
import stat
import subprocess
import sys
import textwrap
import threading
import time
import unittest

from support import (FAKE_HELPER, REPO_ROOT, Harness, pid_alive, wait_dead)

from engine import speech
from engine.speech import SpeechEngine


class EngineCase(unittest.TestCase):
    def setUp(self):
        self.h = Harness()

    def tearDown(self):
        # Every test also proves it left no helper process behind (a zombie
        # still answers kill(pid, 0), so this checks reaping too).
        self.assertTrue(self.h.close(), 'a helper process was left behind')

    def assert_one_terminal(self, ev, sid, kind):
        self.assertTrue(ev.wait_terminal(sid), 'session %s never finished' % sid)
        time.sleep(0.05)
        terms = ev.terminals(sid)
        self.assertEqual(len(terms), 1, terms)
        self.assertEqual(terms[0][0], kind, terms)
        return terms[0][1]

    def dictate(self, eng, ev, hold=0.05):
        """One healthy start -> stop -> final round trip."""
        sid = eng.start('en-US')
        self.assertTrue(ev.wait_for('started', sid), 'no started for %s' % sid)
        time.sleep(hold)
        self.assertTrue(eng.stop(sid))
        return sid, self.assert_one_terminal(ev, sid, 'final')


class HappyPath(EngineCase):
    def test_full_dictation(self):
        self.h.scenario(default={'text': 'hello there',
                                 'partials': ['hel', 'hello']})
        eng, ev = self.h.engine()
        sid = eng.start('en-IN', context=['Kiwi', 'UPI'], partials=True)
        self.assertEqual(sid, 1)
        self.assertTrue(ev.wait_for('started', sid))
        self.assertTrue(ev.wait_for('level', sid, count=3))
        self.assertTrue(ev.wait_for('partial', sid, count=2))
        self.assertTrue(eng.stop())
        args = self.assert_one_terminal(ev, sid, 'final')
        self.assertEqual(args[1], 'hello there')
        self.assertEqual(args[2], 1.5)
        self.assertGreaterEqual(args[3], 0.0)
        self.assertEqual([a[1] for a in ev.of('partial')], ['hel', 'hello'])
        for _sid, rms in ev.of('level'):
            self.assertTrue(0.0 <= rms <= 1.0)
        start = self.h.commands('start')[0]
        self.assertEqual(start, {'cmd': 'start', 'id': sid, 'locale': 'en-IN',
                                 'context': ['Kiwi', 'UPI'], 'partials': True})

    def test_callbacks_run_on_the_callback_thread_only(self):
        eng, ev = self.h.engine()
        self.dictate(eng, ev)
        self.assertEqual(ev.threads, {'speech-cb'})

    def test_spawns_lazily_and_keeps_the_helper_alive(self):
        eng, ev = self.h.engine()
        time.sleep(0.2)
        self.assertEqual(self.h.spawns(), [], 'spawned before first use')
        self.assertEqual(eng.state, speech.STATE_IDLE)
        for _ in range(3):
            self.dictate(eng, ev)
        self.assertEqual(len(self.h.spawns()), 1)
        self.assertEqual(eng.spawn_count, 1)
        self.assertEqual(eng.restart_count, 0)
        self.assertEqual(eng.helper_version, 'fake-1.0')

    def test_unicode_and_split_writes_survive_the_pipe(self):
        text = 'नमस्ते — café \U0001f600'
        self.h.scenario(default={'text': text, 'split_writes': True})
        eng, ev = self.h.engine()
        sid = eng.start('hi-IN', context=['कीवी'])
        self.assertTrue(ev.wait_for('started', sid))
        eng.stop(sid)
        args = self.assert_one_terminal(ev, sid, 'final')
        self.assertEqual(args[1], text)
        self.assertEqual(self.h.commands('start')[0]['context'],
                         ['कीवी'])

    def test_status_is_fetched_and_cached(self):
        self.h.scenario(default={'status': {'mic': 'denied'}})
        eng, ev = self.h.engine()
        self.assertIsNone(eng.last_status)
        eng.ensure_running()
        self.assertTrue(ev.wait_for('status'))
        self.assertEqual(eng.last_status['mic'], 'denied')
        self.assertIn('hi-IN', eng.last_status['supported'])
        self.assertIn('en-IN', eng.last_status['installed'])

    def test_status_extras_are_passed_through(self):
        self.h.scenario(default={'status': {
            'default_locale': 'en-IN', 'input_device': 'Test Mic'}})
        eng, ev = self.h.engine()
        eng.ensure_running()
        self.assertTrue(ev.wait_for('status'))
        self.assertEqual(eng.last_status['default_locale'], 'en-IN')
        self.assertEqual(eng.last_status['input_device'], 'Test Mic')

    def test_status_extras_default_to_none(self):
        eng, ev = self.h.engine()
        eng.ensure_running()
        self.assertTrue(ev.wait_for('status'))
        self.assertIsNone(eng.last_status['default_locale'])
        self.assertIsNone(eng.last_status['input_device'])

    def test_final_hints_arrive_just_before_the_final(self):
        self.h.scenario(default={'text': '', 'final_extra': {
            'silent': True, 'mic_lost': True, 'other': True}})
        eng, ev = self.h.engine()
        sid = eng.start('en-IN')
        self.assertTrue(ev.wait_for('started', sid))
        eng.stop(sid)
        self.assert_one_terminal(ev, sid, 'final')
        self.assertEqual(ev.of('final_info'),
                         [(sid, {'silent': True, 'mic_lost': True})])
        names = [n for n, _ in ev.items if n in ('final_info', 'final')]
        self.assertEqual(names, ['final_info', 'final'])

    def test_an_ordinary_final_has_no_hints(self):
        eng, ev = self.h.engine()
        sid = eng.start('en-IN')
        self.assertTrue(ev.wait_for('started', sid))
        eng.stop(sid)
        self.assert_one_terminal(ev, sid, 'final')
        self.assertEqual(ev.of('final_info'), [])

    def test_prewarm_carries_the_partials_choice(self):
        eng, ev = self.h.engine()
        eng.prewarm('en-IN', partials=False)
        self.assertTrue(ev.wait_for('prewarmed'))
        eng.prewarm('en-IN')
        self.assertTrue(ev.wait_for('prewarmed', count=2))
        cmds = self.h.commands('prewarm')
        self.assertIs(cmds[0].get('partials'), False)
        self.assertNotIn('partials', cmds[1])

    def test_prewarm_install_and_permissions(self):
        eng, ev = self.h.engine()
        eng.prewarm('en-IN')
        self.assertTrue(ev.wait_for('prewarmed'))
        self.assertEqual(ev.of('prewarmed')[0], ('en-IN',))
        eng.install('hi-IN')
        self.assertTrue(ev.wait_for('installed'))
        self.assertEqual(ev.of('installed')[0], ('hi-IN',))
        self.assertEqual([a[1] for a in ev.of('install_progress')],
                         [0.25, 0.5, 1.0])
        before = len(ev.of('status'))
        eng.request_permissions()
        self.assertTrue(ev.wait_for('status', count=before + 1))
        self.assertEqual(ev.of('error'), [])

    def test_commands_issued_before_the_helper_is_up_are_not_lost(self):
        self.h.scenario(default={'ready_delay': 0.3})
        eng, ev = self.h.engine()
        eng.prewarm('en-IN')
        eng.install('hi-IN')
        self.assertTrue(ev.wait_for('prewarmed'))
        self.assertTrue(ev.wait_for('installed'))


class Languages(EngineCase):
    """Two-language dictation and language downloads (helper protocol 1.1)."""

    def engine(self, **kw):
        eng, ev = self.h.engine(**kw)
        # Not in support.Events.NAMES: attached by hand.
        eng.on_started_info = ev._make('started_info')
        return eng, ev

    def test_start_with_languages_puts_them_on_the_wire(self):
        eng, ev = self.engine()
        sid = eng.start('fr-FR', context=['Kiwi'], languages=['en-IN', ' hi-IN ', 'en_in', 7])
        self.assertTrue(ev.wait_for('started', sid))
        eng.stop(sid)
        self.assert_one_terminal(ev, sid, 'final')
        self.assertEqual(self.h.commands('start')[0], {
            'cmd': 'start', 'id': sid, 'locale': 'en-IN', 'context': ['Kiwi'],
            'partials': True, 'languages': ['en-IN', 'hi-IN']})

    def test_one_language_is_an_ordinary_dictation(self):
        eng, ev = self.engine()
        for langs in (['hi-IN'], [], None, 'en-IN', ['en-IN', 'EN_in']):
            sid = eng.start('en-IN', languages=langs)
            self.assertTrue(ev.wait_for('started', sid))
            eng.stop(sid)
            self.assert_one_terminal(ev, sid, 'final')
        for cmd in self.h.commands('start'):
            self.assertNotIn('languages', cmd)
            self.assertEqual(cmd['locale'], 'en-IN')
        self.assertEqual(ev.of('started_info'), [])

    def test_prewarm_with_languages_is_repeated_on_the_replacement(self):
        eng, ev = self.engine()
        eng.prewarm('xx', languages=['en-IN', 'hi-IN'], partials=False)
        self.assertTrue(ev.wait_for('prewarmed'))
        self.assertEqual(eng.last_prewarm, {'locale': 'en-IN', 'resolved': 'en-IN',
                                            'languages': ['en-IN', 'hi-IN'],
                                            'missing': []})
        os.kill(self.h.pids()[0], signal.SIGKILL)
        self.assertTrue(ev.wait_for('prewarmed', count=2),
                        'model not reloaded after the respawn')
        cmds = self.h.commands('prewarm')
        self.assertEqual(len(cmds), 2)
        for cmd in cmds:
            self.assertEqual(cmd, {'cmd': 'prewarm', 'locale': 'en-IN',
                                   'languages': ['en-IN', 'hi-IN'],
                                   'partials': False})

    def test_a_dictation_with_languages_is_what_gets_reloaded(self):
        eng, ev = self.engine()
        sid = eng.start('en-IN', languages=['en-IN', 'hi-IN'])
        self.assertTrue(ev.wait_for('started', sid))
        eng.stop(sid)
        self.assert_one_terminal(ev, sid, 'final')
        os.kill(self.h.pids()[0], signal.SIGKILL)
        self.assertTrue(ev.wait_for('prewarmed'))
        self.assertEqual(self.h.commands('prewarm')[0]['languages'],
                         ['en-IN', 'hi-IN'])

    def test_started_info_reports_languages_and_missing(self):
        self.h.scenario(default={'started_extra': {'missing': ['hi-IN']}})
        eng, ev = self.engine()
        sid = eng.start('en-IN', languages=['en-IN', 'hi-IN'])
        self.assertTrue(ev.wait_for('started', sid))
        self.assertEqual(ev.of('started_info'), [(sid, {
            'locale': 'en-IN', 'languages': ['en-IN', 'hi-IN'],
            'missing': ['hi-IN']})])
        names = [n for n, _ in ev.items if n in ('started_info', 'started')]
        self.assertEqual(names, ['started_info', 'started'])
        eng.stop(sid)
        self.assert_one_terminal(ev, sid, 'final')

    def test_final_extras_reach_final_info(self):
        self.h.scenario(default={'text': 'Okay. Kal milte hain.', 'final_extra': {
            'languages': ['en-IN', 'hi-IN'], 'used': ['en-IN', 'hi-IN'],
            'dominant': 'hi-IN'}})
        eng, ev = self.engine()
        sid = eng.start('en-IN', languages=['en-IN', 'hi-IN'])
        self.assertTrue(ev.wait_for('started', sid))
        eng.stop(sid)
        self.assert_one_terminal(ev, sid, 'final')
        self.assertEqual(ev.of('final_info'), [(sid, {
            'languages': ['en-IN', 'hi-IN'], 'used': ['en-IN', 'hi-IN'],
            'dominant': 'hi-IN'})])

    def test_one_language_final_extras_are_ignored(self):
        self.h.scenario(default={'final_extra': {'languages': ['en-IN'],
                                                 'used': ['en-IN']}})
        eng, ev = self.engine()
        self.dictate(eng, ev)
        self.assertEqual(ev.of('final_info'), [])

    def test_installed_updates_the_status_first(self):
        self.h.scenario(default={'installed_list': ['en-IN', 'en-US', 'hi-IN', 'mul-IN']})
        eng, ev = self.engine()
        eng.ensure_running()
        self.assertTrue(ev.wait_for('status'))
        eng.install('hi-IN')
        self.assertTrue(ev.wait_for('installed'))
        names = [n for n, _ in ev.items if n in ('status', 'installed')]
        self.assertEqual(names[-2:], ['status', 'installed'])
        before = [a[0] for n, a in ev.items if n == 'status'][-1]
        self.assertEqual(before['installed'], ['en-IN', 'en-US', 'hi-IN', 'mul-IN'])
        self.assertTrue(ev.wait_for('status', count=3), 'no fresh status after install')
        self.assertEqual(len(self.h.commands('status')), 2)

    def test_installed_without_a_list_adds_the_locale(self):
        eng, ev = self.engine()
        eng.ensure_running()
        self.assertTrue(ev.wait_for('status'))
        eng.install('hi-IN')
        self.assertTrue(ev.wait_for('installed'))
        status = [a[0] for n, a in ev.items if n == 'status'][1]
        self.assertEqual(status['installed'], ['en-US', 'en-IN', 'hi-IN'])
        self.assertIn('hi-IN', eng.last_status['installed'])

    def test_installs_track_a_download(self):
        self.h.scenario(default={'install_delay': 0.15, 'install_steps': [0.0, 0.5, 1.0]})
        eng, ev = self.engine()
        self.assertEqual(eng.installs(), {})
        eng.install('hi-IN')
        self.assertTrue(ev.wait_for('install_progress', count=2))
        state = eng.installs()['hi-IN']
        self.assertEqual(state['state'], 'downloading')
        self.assertIn(state['fraction'], (0.0, 0.5))
        state['state'] = 'mutated'                   # a copy: no effect
        self.assertEqual(eng.installs()['hi-IN']['state'], 'downloading')
        self.assertTrue(ev.wait_for('installed'))
        self.assertEqual(eng.installs()['hi-IN'], {
            'state': 'installed', 'fraction': 1.0, 'already': False,
            'code': None, 'message': None})
        self.assertEqual([a[1] for a in ev.of('install_progress')], [0.0, 0.5, 1.0])

    def test_an_already_installed_language(self):
        self.h.scenario(default={'install_already': True})
        eng, ev = self.engine()
        eng.install('en-IN')
        self.assertTrue(ev.wait_for('installed'))
        self.assertEqual(ev.of('install_progress'), [])
        self.assertTrue(eng.installs()['en-IN']['already'])

    def test_installs_record_a_failure(self):
        self.h.scenario(default={'install_error': {
            'code': 'network', 'message': 'offline', 'tagged': True}})
        eng, ev = self.engine()
        eng.install('hi-IN')
        self.assertTrue(ev.wait_for('install_failed'))
        self.assertEqual(ev.of('install_failed')[0], ('hi-IN', 'network', 'offline'))
        self.assertEqual(eng.installs()['hi-IN']['state'], 'failed')
        self.assertEqual(eng.installs()['hi-IN']['code'], 'network')
        self.assertIn('internet', speech.user_message('network').lower())

    def test_installs_record_a_helper_failure(self):
        self.h.scenario(spawns=[{'hang': ['install']}])
        eng, ev = self.engine(install_stall_timeout=0.3)
        eng.install('hi-IN')
        self.assertTrue(ev.wait_for('install_failed'))
        self.assertEqual(eng.installs()['hi-IN']['state'], 'failed')
        self.assertEqual(eng.installs()['hi-IN']['code'], speech.E_HELPER_TIMEOUT)

    def test_status_groups_and_max_languages(self):
        self.h.scenario(default={'status': {
            'groups': [['hi-IN', 'mul-IN'], 'junk'], 'max_languages': 2}})
        eng, ev = self.engine()
        eng.ensure_running()
        self.assertTrue(ev.wait_for('status'))
        self.assertEqual(eng.last_status['groups'], [['hi-IN', 'mul-IN']])
        self.assertEqual(eng.last_status['max_languages'], 2)

    def test_status_defaults_for_an_older_helper(self):
        eng, ev = self.engine()
        eng.ensure_running()
        self.assertTrue(ev.wait_for('status'))
        self.assertEqual(eng.last_status['groups'], [])
        self.assertEqual(eng.last_status['max_languages'], 1)

    def test_language_cleaning(self):
        clean = speech._clean_languages
        self.assertEqual(clean(['en-IN', 'hi-IN']), ('en-IN', 'hi-IN'))
        self.assertEqual(clean(['en-IN', 'en_in', 'hi-IN']), ('en-IN', 'hi-IN'))
        self.assertEqual(clean(['en-IN']), ())
        self.assertEqual(clean('en-IN'), ())
        self.assertEqual(clean(None), ())
        self.assertEqual(clean(['', ' ', None, 'en-IN']), ())
        self.assertEqual(len(clean(['a-%d' % i for i in range(9)])), speech.MAX_LANGUAGES)

    def test_transcribe_file_with_languages(self):
        helper = [sys.executable, FAKE_HELPER]
        res = speech.transcribe_file(helper, FAKE_HELPER, locale='fr-FR',
                                     languages=['en-IN', 'hi-IN'])
        self.assertEqual(res['languages'], ['en-IN', 'hi-IN'])
        self.assertEqual(res['locale'], 'en-IN')
        self.assertNotIn('decisions', res)

    def test_transcribe_file_explain(self):
        """explain=True asks the helper why each sentence came from which
        recogniser; it only means something with two languages."""
        helper = [sys.executable, FAKE_HELPER]
        res = speech.transcribe_file(helper, FAKE_HELPER,
                                     languages=['en-IN', 'hi-IN'], explain=True)
        self.assertEqual([d['rule'] for d in res['decisions']], ['keep'])
        self.assertEqual(res['decisions'][0]['used'], 'P')
        for key in ('primary_text', 'secondary_text',
                    'primary_segments', 'secondary_segments'):
            self.assertIn(key, res)
        self.assertEqual(res['primary_segments'][0]['words'][0][0], 'fake')
        # One language: the flag is not even sent.
        res = speech.transcribe_file(helper, FAKE_HELPER, locale='en-IN',
                                     explain=True)
        self.assertNotIn('decisions', res)


class NonBlocking(EngineCase):
    def test_calls_return_immediately_even_with_a_hung_helper(self):
        # The helper never even says "ready": the worst case for a caller.
        self.h.scenario(default={'no_ready': True})
        eng, ev = self.h.engine(ready_timeout=30.0, start_timeout=30.0)
        worst = 0.0
        for fn in (lambda: eng.start('en-US'), eng.stop,
                   lambda: eng.start('en-US'), eng.cancel,
                   lambda: eng.prewarm('en-US'), eng.request_status,
                   eng.request_permissions, lambda: eng.install('hi-IN'),
                   eng.ensure_running):
            t0 = time.monotonic()
            fn()
            worst = max(worst, time.monotonic() - t0)
        self.assertLess(worst, 0.05, 'a public call blocked for %.3fs' % worst)

    def test_a_slow_callback_cannot_stall_supervision(self):
        self.h.scenario(spawns=[{'hang': ['start']}])
        eng, ev = self.h.engine()
        release = threading.Event()
        original = eng.on_state

        def slow_state(state, detail):
            original(state, detail)
            if state == speech.STATE_READY:
                release.wait(5.0)          # block the callback thread
        eng.on_state = slow_state
        sid = eng.start('en-US')
        try:
            # The start deadline must still fire and the helper be replaced
            # while the callback thread is stuck.
            self.assertTrue(self.h.wait_spawns(2, timeout=4.0),
                            'supervisor stalled behind a slow callback')
        finally:
            release.set()
        self.assert_one_terminal(ev, sid, 'error')

    def test_a_raising_callback_is_isolated(self):
        eng, ev = self.h.engine()

        def boom(sid):
            raise RuntimeError('callback bug')
        eng.on_started = boom
        sid = eng.start('en-US')
        time.sleep(0.4)
        eng.stop(sid)
        self.assert_one_terminal(ev, sid, 'final')


class SessionIds(EngineCase):
    def test_late_events_from_a_cancelled_session_are_dropped(self):
        self.h.scenario(default={'late_after_cancel': True, 'text': 'fresh'})
        eng, ev = self.h.engine()
        a = eng.start('en-US')
        self.assertTrue(ev.wait_for('started', a))
        self.assertTrue(eng.cancel(a))
        self.assert_one_terminal(ev, a, 'cancelled')
        n_levels = len([1 for s, _ in ev.of('level') if s == a])
        b = eng.start('en-US')
        self.assertTrue(ev.wait_for('started', b))
        time.sleep(0.4)                     # the fake's stale burst lands here
        eng.stop(b)
        args = self.assert_one_terminal(ev, b, 'final')
        self.assertEqual(args[1], 'fresh')
        self.assertEqual(len([1 for s, _ in ev.of('level') if s == a]), n_levels,
                         'level events leaked from a cancelled session')
        self.assertNotIn('STALE', [t for _s, t in ev.of('partial')])
        self.assertEqual([t[0] for t in ev.terminals(a)], ['cancelled'])

    def test_a_new_start_supersedes_the_recording_one(self):
        eng, ev = self.h.engine()
        a = eng.start('en-US')
        self.assertTrue(ev.wait_for('started', a))
        b = eng.start('en-US')
        self.assertNotEqual(a, b)
        self.assert_one_terminal(ev, a, 'cancelled')
        self.assertTrue(ev.wait_for('started', b))
        self.assertFalse(eng.stop(a), 'a superseded id must be dead')
        self.assertTrue(eng.stop(b))
        self.assert_one_terminal(ev, b, 'final')
        order = [(c['cmd'], c.get('id')) for c in self.h.commands()
                 if c['cmd'] in ('start', 'cancel', 'stop')]
        self.assertEqual(order, [('start', a), ('cancel', a), ('start', b),
                                 ('stop', b)])
        self.assertEqual(len(self.h.spawns()), 1)

    def test_unknown_and_finished_ids_are_ignored(self):
        eng, ev = self.h.engine()
        self.assertFalse(eng.stop())            # nothing started yet
        self.assertFalse(eng.cancel())
        self.assertFalse(eng.stop(999))
        self.assertFalse(eng.cancel(999))
        sid, _ = self.dictate(eng, ev)
        self.assertFalse(eng.stop(sid))
        self.assertFalse(eng.cancel(sid))
        time.sleep(0.1)
        self.assertEqual(len(ev.terminals(sid)), 1)
        self.assertEqual(len(self.h.commands('stop')), 1)

    def test_stop_is_idempotent(self):
        self.h.scenario(default={'final_delay': 0.2})
        eng, ev = self.h.engine()
        sid = eng.start('en-US')
        self.assertTrue(ev.wait_for('started', sid))
        self.assertTrue(eng.stop(sid))
        self.assertFalse(eng.stop(sid))
        self.assert_one_terminal(ev, sid, 'final')
        self.assertEqual(len(self.h.commands('stop')), 1)

    def test_stop_before_the_mic_ever_opened_yields_an_empty_final(self):
        self.h.scenario(default={'ready_delay': 0.4})
        eng, ev = self.h.engine()
        sid = eng.start('en-US')
        self.assertTrue(eng.stop(sid))          # tap released instantly
        args = self.assert_one_terminal(ev, sid, 'final')
        self.assertEqual(args[1:], ('', 0.0, 0.0))
        self.assertTrue(ev.wait_for('state', speech.STATE_READY))
        self.assertEqual(self.h.commands('start'), [])
        self.dictate(eng, ev)                   # and the engine is fine

    def test_cancel_after_stop_discards_the_transcript(self):
        self.h.scenario(default={'final_delay': 0.4})
        eng, ev = self.h.engine()
        sid = eng.start('en-US')
        self.assertTrue(ev.wait_for('started', sid))
        eng.stop(sid)
        self.assertTrue(eng.cancel(sid))
        self.assert_one_terminal(ev, sid, 'cancelled')
        time.sleep(0.6)                         # the fake's final arrives late
        self.assertEqual([t[0] for t in ev.terminals(sid)], ['cancelled'])
        self.assertEqual(len(self.h.spawns()), 1)

    def test_start_during_finalisation_waits_for_the_previous_final(self):
        self.h.scenario(default={'final_delay': 0.4})
        eng, ev = self.h.engine()
        a = eng.start('en-US')
        self.assertTrue(ev.wait_for('started', a))
        eng.stop(a)
        b = eng.start('en-US')                  # user is already talking again
        self.assertTrue(ev.wait_for('started', b))
        names = [(n, args[0]) for n, args in ev.items
                 if n in ('final', 'started', 'cancelled', 'error')]
        self.assertEqual(names, [('started', a), ('final', a), ('started', b)])
        self.assertEqual(self.h.commands('cancel'), [])
        eng.stop(b)
        self.assert_one_terminal(ev, b, 'final')

    def test_overlap_finalize_sends_the_start_straight_away(self):
        self.h.scenario(default={'final_delay': 0.4})
        eng, ev = self.h.engine(overlap_finalize=True)
        a = eng.start('en-US')
        self.assertTrue(ev.wait_for('started', a))
        eng.stop(a)
        b = eng.start('en-US')
        self.assertTrue(ev.wait_for('started', b))
        self.assertEqual(ev.terminals(a), [], 'B should start before A ends')
        self.assert_one_terminal(ev, a, 'final')
        eng.stop(b)
        self.assert_one_terminal(ev, b, 'final')

    def test_stop_that_beat_the_session_into_existence_is_repeated(self):
        # A quick tap: the key is released before the helper has set the
        # session up, so the helper drops the stop as an unknown id. Without
        # the repeat this would hang until the stop deadline and cost a
        # helper restart for every accidental tap.
        self.h.scenario(default={'async_start': 0.3, 'text': 'tap'})
        eng, ev = self.h.engine()
        eng.ensure_running()
        self.assertTrue(ev.wait_for('state', speech.STATE_READY))
        sid = eng.start('en-US')
        time.sleep(0.05)
        t0 = time.monotonic()
        self.assertTrue(eng.stop(sid))
        args = self.assert_one_terminal(ev, sid, 'final')
        self.assertEqual(args[1], 'tap')
        self.assertLess(time.monotonic() - t0, 0.9)
        self.assertEqual(ev.of('started'), [], 'started after stop must not surface')
        self.assertEqual(len(self.h.commands('stop')), 2)
        self.assertEqual(len(self.h.spawns()), 1)
        self.assertEqual(eng.restart_count, 0)

    def test_cancel_that_beat_the_session_into_existence_is_repeated(self):
        self.h.scenario(default={'async_start': 0.3})
        eng, ev = self.h.engine()
        eng.ensure_running()
        self.assertTrue(ev.wait_for('state', speech.STATE_READY))
        sid = eng.start('en-US')
        time.sleep(0.05)
        self.assertTrue(eng.cancel(sid))
        self.assert_one_terminal(ev, sid, 'cancelled')
        time.sleep(1.0)                         # past cancel_timeout (0.7s)
        self.assertEqual(len(self.h.commands('cancel')), 2)
        self.assertEqual(len(self.h.spawns()), 1, 'mic was only freed by a kill')
        self.assertEqual(ev.of('error'), [])
        self.assertEqual(ev.of('started'), [])
        self.dictate(eng, ev)

    def test_session_cap_stops_a_runaway_recording(self):
        self.h.scenario(default={'text': 'kept'})
        eng, ev = self.h.engine(max_session_sec=0.3)
        sid = eng.start('en-US')                # the key-release never comes
        args = self.assert_one_terminal(ev, sid, 'final')
        self.assertEqual(args[1], 'kept')
        self.assertFalse(eng.stop(sid))
        self.assertEqual(len(self.h.commands('stop')), 1)
        # ...and the caller is told WHY it ended without a stop().
        self.assertEqual(ev.of('final_info'), [(sid, {'capped': True})])
        names = [n for n, _ in ev.for_session(sid)]
        self.assertLess(names.index('final_info'), names.index('final'))

    def test_a_normal_stop_is_not_flagged(self):
        eng, ev = self.h.engine(max_session_sec=30.0, level_stall_timeout=30.0)
        self.dictate(eng, ev)
        self.assertEqual(ev.of('final_info'), [])


class Recovery(EngineCase):
    def recovers(self, eng, ev, spawns=2):
        """After a failure the very next dictation must just work."""
        sid, args = self.dictate(eng, ev)
        self.assertEqual(args[1], 'hello world')
        self.assertEqual(len(self.h.spawns()), spawns)
        return sid

    def test_start_that_hangs_is_killed_and_replaced(self):
        self.h.scenario(spawns=[{'hang': ['start']}])
        eng, ev = self.h.engine()
        t0 = time.monotonic()
        sid = eng.start('en-US')
        args = self.assert_one_terminal(ev, sid, 'error')
        took = time.monotonic() - t0
        self.assertEqual(args[1], speech.E_HELPER_TIMEOUT)
        self.assertTrue(0.9 <= took < 2.5, 'deadline fired after %.2fs' % took)
        self.assertTrue(wait_dead(self.h.pids()[0]), 'hung helper not killed')
        self.recovers(eng, ev)
        self.assertEqual(eng.restart_count, 1)

    def test_stop_that_hangs_is_killed_and_replaced(self):
        self.h.scenario(spawns=[{'hang': ['stop']}])
        eng, ev = self.h.engine()
        sid = eng.start('en-US')
        self.assertTrue(ev.wait_for('started', sid))
        eng.stop(sid)
        args = self.assert_one_terminal(ev, sid, 'error')
        self.assertEqual(args[1], speech.E_HELPER_TIMEOUT)
        self.assertTrue(wait_dead(self.h.pids()[0]))
        self.recovers(eng, ev)

    def test_cancel_that_hangs_still_releases_the_mic_by_killing(self):
        self.h.scenario(spawns=[{'hang': ['cancel']}])
        eng, ev = self.h.engine()
        sid = eng.start('en-US')
        self.assertTrue(ev.wait_for('started', sid))
        eng.cancel(sid)
        self.assert_one_terminal(ev, sid, 'cancelled')
        self.assertTrue(self.h.wait_spawns(2), 'unconfirmed cancel not healed')
        self.assertTrue(wait_dead(self.h.pids()[0]))
        self.assertEqual(ev.of('error'), [], 'a cancel must never report an error')
        self.recovers(eng, ev)

    def test_frozen_helper_that_stops_reading_is_replaced(self):
        self.h.scenario(spawns=[{'freeze': ['start']}])
        eng, ev = self.h.engine()
        sid = eng.start('en-US')
        args = self.assert_one_terminal(ev, sid, 'error')
        self.assertEqual(args[1], speech.E_HELPER_TIMEOUT)
        self.recovers(eng, ev)

    def test_crash_on_start(self):
        self.h.scenario(spawns=[{'crash': {'start': 3}}])
        eng, ev = self.h.engine()
        sid = eng.start('en-US')
        args = self.assert_one_terminal(ev, sid, 'error')
        self.assertEqual(args[1], speech.E_HELPER_CRASHED)
        self.assertIn('exit code 3', args[2])
        self.recovers(eng, ev)

    def test_crash_by_signal_while_recording(self):
        self.h.scenario(spawns=[{'crash': {'stop': 'kill'}}])
        eng, ev = self.h.engine()
        sid = eng.start('en-US')
        self.assertTrue(ev.wait_for('started', sid))
        eng.stop(sid)
        args = self.assert_one_terminal(ev, sid, 'error')
        self.assertEqual(args[1], speech.E_HELPER_CRASHED)
        self.assertIn('signal 9', args[2])
        self.recovers(eng, ev)

    def test_crash_while_idle_heals_silently(self):
        eng, ev = self.h.engine()
        eng.ensure_running()
        self.assertTrue(ev.wait_for('state', speech.STATE_READY))
        os.kill(self.h.pids()[0], signal.SIGKILL)
        self.assertTrue(ev.wait_for('state', speech.STATE_READY, count=2),
                        'helper was not respawned')
        self.assertEqual(ev.of('error'), [], 'idle crash must not surface')
        self.assertEqual([s[0] for s in ev.of('state')],
                         ['starting', 'ready', 'restarting', 'starting', 'ready'])
        self.recovers(eng, ev)

    def test_garbage_output_gets_the_helper_replaced(self):
        self.h.scenario(spawns=[{'garbage': ['start']}])
        eng, ev = self.h.engine()
        sid = eng.start('en-US')
        args = self.assert_one_terminal(ev, sid, 'error')
        self.assertEqual(args[1], speech.E_HELPER_PROTOCOL)
        self.assertTrue(wait_dead(self.h.pids()[0]))
        self.recovers(eng, ev)

    def test_one_stray_line_is_tolerated(self):
        self.h.scenario(default={'stray': ['start', 'stop', 'status']})
        eng, ev = self.h.engine()
        for _ in range(4):                      # non-consecutive junk lines
            self.dictate(eng, ev)
        self.assertEqual(len(self.h.spawns()), 1)
        self.assertEqual(ev.of('error'), [])

    def test_endless_line_gets_the_helper_replaced(self):
        # No newline ever arrives: the reader must give up on the stream
        # rather than buffer it without bound.
        self.h.scenario(spawns=[{'flood': ['start']}])
        eng, ev = self.h.engine(max_line_bytes=64 * 1024, start_timeout=5.0)
        sid = eng.start('en-US')
        args = self.assert_one_terminal(ev, sid, 'error')
        self.assertEqual(args[1], speech.E_HELPER_PROTOCOL)
        self.assertTrue(wait_dead(self.h.pids()[0]))
        self.recovers(eng, ev)

    def test_unanswered_ping_replaces_an_idle_helper_silently(self):
        self.h.scenario(spawns=[{'hang': ['ping']}])
        eng, ev = self.h.engine(ping_interval=0.1, ping_timeout=0.3)
        eng.ensure_running()
        self.assertTrue(self.h.wait_spawns(2), 'hung idle helper not replaced')
        self.assertTrue(wait_dead(self.h.pids()[0]))
        self.assertEqual(ev.of('error'), [])
        self.assertEqual(eng.last_failure[0], speech.E_HELPER_TIMEOUT)
        self.recovers(eng, ev)

    def test_answered_pings_leave_a_healthy_helper_alone(self):
        eng, ev = self.h.engine(ping_interval=0.05, ping_timeout=0.5)
        eng.ensure_running()
        time.sleep(0.7)
        self.assertGreaterEqual(len(self.h.commands('ping')), 4)
        self.assertEqual(len(self.h.spawns()), 1)
        self.assertEqual(eng.restart_count, 0)
        self.dictate(eng, ev)

    def test_helper_that_never_becomes_ready_is_replaced_and_start_survives(self):
        self.h.scenario(spawns=[{'no_ready': True}])
        eng, ev = self.h.engine(ready_timeout=0.4, start_timeout=3.0)
        sid = eng.start('en-US')                # issued while nothing is up
        self.assertTrue(ev.wait_for('started', sid, timeout=4.0),
                        'the queued start was lost with the dead spawn')
        eng.stop(sid)
        self.assert_one_terminal(ev, sid, 'final')
        self.assertEqual(len(self.h.spawns()), 2)
        self.assertTrue(wait_dead(self.h.pids()[0]))

    def test_helper_that_stops_draining_stdin_is_replaced(self):
        # Frozen on the automatic "status": nothing else has a deadline, so
        # only the write-stall watchdog can notice once the pipe fills up.
        self.h.scenario(spawns=[{'freeze': ['status']}])
        eng, ev = self.h.engine(write_timeout=0.4, install_stall_timeout=60.0)
        eng.ensure_running()
        self.assertTrue(ev.wait_for('state', speech.STATE_READY))
        for i in range(12):
            eng.install('x%d-' % i + 'y' * 20000)
        self.assertTrue(self.h.wait_spawns(2), 'write stall not detected')
        self.assertIn('stopped reading', eng.last_failure[1])
        self.assertTrue(wait_dead(self.h.pids()[0]))

    def test_level_meter_stall_salvages_the_text_instead_of_killing(self):
        # The mic stops delivering mid-dictation. The helper has already
        # recognised what was said: ask for it, do not kill the helper.
        self.h.scenario(default={'level_count': 5, 'text': 'said so far'})
        eng, ev = self.h.engine(level_stall_timeout=0.3)
        sid = eng.start('en-US')
        args = self.assert_one_terminal(ev, sid, 'final')
        self.assertEqual(args[1], 'said so far')
        self.assertEqual(ev.of('final_info'), [(sid, {'mic_lost': True})])
        self.assertEqual(ev.of('error'), [])
        self.assertEqual([c['id'] for c in self.h.commands('stop')], [sid])
        self.assertFalse(eng.stop(sid))         # already stopped for us
        self.assertEqual(len(self.h.spawns()), 1, 'helper was killed')

    def test_level_meter_stall_with_a_hung_helper_still_ends_in_a_kill(self):
        # Same stall, but the helper really is wedged: it never answers the
        # salvage stop, so the stop deadline replaces it.
        self.h.scenario(spawns=[{'level_count': 5, 'hang': ['stop']}])
        eng, ev = self.h.engine(level_stall_timeout=0.3, stop_timeout_base=0.4)
        sid = eng.start('en-US')
        args = self.assert_one_terminal(ev, sid, 'error')
        self.assertEqual(args[1], speech.E_HELPER_TIMEOUT)
        self.assertTrue(wait_dead(self.h.pids()[0]))
        self.recovers(eng, ev)

    def test_a_helper_without_level_events_is_not_mistaken_for_a_stall(self):
        self.h.scenario(default={'levels': False})
        eng, ev = self.h.engine(level_stall_timeout=0.2)
        sid = eng.start('en-US')
        self.assertTrue(ev.wait_for('started', sid))
        time.sleep(0.6)
        eng.stop(sid)
        self.assert_one_terminal(ev, sid, 'final')
        self.assertEqual(len(self.h.spawns()), 1)

    def test_start_behind_the_permission_prompt_is_given_time(self):
        # mic == undetermined: the helper is waiting for the user to answer
        # the system prompt. Killing it at the normal start deadline would
        # dismiss the prompt and fail the very first dictation.
        self.h.scenario(default={'status': {'mic': 'undetermined'},
                                 'delay': {'start': 1.2}})
        eng, ev = self.h.engine(start_timeout=0.3, permission_start_timeout=6.0)
        eng.ensure_running()
        self.assertTrue(ev.wait_for('status'))
        sid = eng.start('en-US')
        self.assertTrue(ev.wait_for('started', sid, timeout=4.0))
        self.assertEqual(len(self.h.spawns()), 1)
        eng.stop(sid)
        self.assert_one_terminal(ev, sid, 'final')
        # The mic went live, so the cached "undetermined" was stale: re-asked.
        self.assertGreaterEqual(len(self.h.commands('status')), 2)

    def test_key_released_while_the_permission_prompt_is_open(self):
        self.h.scenario(default={'status': {'mic': 'undetermined'},
                                 'delay': {'start': 1.2}})
        eng, ev = self.h.engine(start_timeout=0.3, stop_timeout_base=0.3,
                                permission_start_timeout=6.0)
        eng.ensure_running()
        self.assertTrue(ev.wait_for('status'))
        sid = eng.start('en-US')
        time.sleep(0.1)
        eng.stop(sid)                           # user let go to click "Allow"
        self.assert_one_terminal(ev, sid, 'final')
        self.assertEqual(len(self.h.spawns()), 1)
        self.assertEqual(eng.restart_count, 0)

    def test_permission_denial_refreshes_the_cached_status(self):
        self.h.scenario(default={'start_error': {'code': 'mic_denied',
                                                 'message': 'no access'}})
        eng, ev = self.h.engine()
        sid = eng.start('en-US')
        self.assert_one_terminal(ev, sid, 'error')
        self.assertTrue(ev.wait_for('status', count=2))

    def test_helper_errors_pass_through_without_a_restart(self):
        self.h.scenario(default={'start_error': {'code': 'mic_denied',
                                                 'message': 'no access'}})
        eng, ev = self.h.engine()
        sid = eng.start('en-US')
        args = self.assert_one_terminal(ev, sid, 'error')
        self.assertEqual(args[1:], ('mic_denied', 'no access'))
        time.sleep(0.2)
        self.assertEqual(len(self.h.spawns()), 1)
        self.assertEqual(eng.restart_count, 0)

    def test_engine_failed_recycles_the_helper_quietly(self):
        self.h.scenario(spawns=[{'stop_error': {'code': 'engine_failed',
                                                'message': 'tap died'}}])
        eng, ev = self.h.engine()
        sid = eng.start('en-US')
        self.assertTrue(ev.wait_for('started', sid))
        eng.stop(sid)
        args = self.assert_one_terminal(ev, sid, 'error')
        self.assertEqual(args[1], 'engine_failed')
        self.assertTrue(self.h.wait_spawns(2), 'helper was not recycled')
        self.assertEqual(len(ev.of('error')), 1)
        self.recovers(eng, ev)

    def test_prewarm_is_repeated_on_the_replacement_helper(self):
        eng, ev = self.h.engine()
        eng.prewarm('en-IN')
        self.assertTrue(ev.wait_for('prewarmed'))
        os.kill(self.h.pids()[0], signal.SIGKILL)
        self.assertTrue(ev.wait_for('prewarmed', count=2),
                        'model not reloaded after the respawn')
        self.assertEqual([c['locale'] for c in self.h.commands('prewarm')],
                         ['en-IN', 'en-IN'])

    def test_a_prewarm_that_hangs_does_not_cause_a_kill_loop(self):
        self.h.scenario(default={'hang': ['prewarm']})
        eng, ev = self.h.engine(prewarm_timeout=0.3)
        eng.prewarm('en-IN')
        self.assertTrue(self.h.wait_spawns(2))
        time.sleep(1.0)
        self.assertEqual(len(self.h.spawns()), 2, 'prewarm kill loop')
        self.assertEqual(len(self.h.commands('prewarm')), 1)

    def test_a_rejected_prewarm_is_reported_and_not_retried(self):
        self.h.scenario(default={'prewarm_error': {
            'code': 'locale_not_installed', 'message': 'hi-IN'}})
        eng, ev = self.h.engine()
        eng.prewarm('hi-IN')
        self.assertTrue(ev.wait_for('error'))
        self.assertEqual(ev.of('error')[0], (None, 'locale_not_installed', 'hi-IN'))
        os.kill(self.h.pids()[0], signal.SIGKILL)
        self.assertTrue(ev.wait_for('state', speech.STATE_READY, count=2))
        time.sleep(0.2)
        self.assertEqual(len(self.h.commands('prewarm')), 1)

    def test_a_prewarm_that_crashes_the_helper_is_not_repeated(self):
        self.h.scenario(default={'crash': {'prewarm': 1}})
        eng, ev = self.h.engine()
        eng.prewarm('en-IN')
        self.assertTrue(ev.wait_for('state', speech.STATE_READY, count=2))
        time.sleep(0.8)
        self.assertEqual(len(self.h.spawns()), 2, 'prewarm crash loop')
        self.assertEqual(len(self.h.commands('prewarm')), 1)
        sid = eng.start('en-US')                # dictation still works...
        self.assertTrue(ev.wait_for('started', sid))
        eng.stop(sid)
        self.assert_one_terminal(ev, sid, 'final')

    def test_a_failed_install_does_not_get_the_helper_killed_later(self):
        self.h.scenario(default={'install_error': {
            'code': 'locale_unsupported', 'message': 'xx-XX'}})
        eng, ev = self.h.engine(install_stall_timeout=0.3)
        eng.install('xx-XX')
        self.assertTrue(ev.wait_for('error'))
        self.assertEqual(ev.of('error')[0], (None, 'locale_unsupported', 'xx-XX'))
        time.sleep(0.8)                         # well past the stall timeout
        self.assertEqual(len(self.h.spawns()), 1)
        self.assertEqual(eng.restart_count, 0)

    def test_a_failed_install_names_its_locale(self):
        # The real helper tags id-less errors with the request they answer.
        self.h.scenario(default={'install_error': {
            'code': 'internal', 'message': 'no network', 'tagged': True}})
        eng, ev = self.h.engine(install_stall_timeout=0.3)
        eng.install('hi-IN')
        self.assertTrue(ev.wait_for('install_failed'))
        self.assertEqual(ev.of('install_failed')[0],
                         ('hi-IN', 'internal', 'no network'))
        self.assertEqual(ev.of('error'), [])    # reported once, as an install
        time.sleep(0.8)                         # well past the stall timeout
        self.assertEqual(len(self.h.spawns()), 1)

    def test_a_tagged_prewarm_error_does_not_fail_a_running_install(self):
        self.h.scenario(default={
            'hang': ['install'],
            'prewarm_error': {'code': 'locale_not_installed',
                              'message': 'hi-IN', 'tagged': True}})
        eng, ev = self.h.engine(install_stall_timeout=30.0)
        eng.install('ta-IN')
        eng.prewarm('hi-IN')
        self.assertTrue(ev.wait_for('error'))
        self.assertEqual(ev.of('error')[0],
                         (None, 'locale_not_installed', 'hi-IN'))
        self.assertEqual(ev.of('install_failed'), [])

    def test_an_untagged_error_fails_every_running_install(self):
        self.h.scenario(default={'install_error': {
            'code': 'locale_unsupported', 'message': 'xx-XX'}})
        eng, ev = self.h.engine()
        eng.install('xx-XX')
        self.assertTrue(ev.wait_for('install_failed'))
        self.assertEqual(ev.of('install_failed')[0][0:2],
                         ('xx-XX', 'locale_unsupported'))

    def test_a_download_cut_short_by_a_helper_failure_is_reported(self):
        self.h.scenario(spawns=[{'hang': ['install']}])
        eng, ev = self.h.engine(install_stall_timeout=0.3)
        eng.install('hi-IN')
        self.assertTrue(ev.wait_for('install_failed'))
        self.assertEqual(ev.of('install_failed')[0][0:2],
                         ('hi-IN', speech.E_HELPER_TIMEOUT))

    def test_a_stalled_download_is_noticed(self):
        self.h.scenario(spawns=[{'hang': ['install']}])
        eng, ev = self.h.engine(install_stall_timeout=0.3)
        eng.install('hi-IN')
        self.assertTrue(self.h.wait_spawns(2), 'stalled download not noticed')
        self.assertTrue(ev.wait_for('error'))
        self.assertEqual(ev.of('error')[0][0:2], (None, speech.E_HELPER_TIMEOUT))
        self.assertIn('hi-IN', ev.of('error')[0][2])

    def test_manual_restart(self):
        eng, ev = self.h.engine()
        eng.ensure_running()
        self.assertTrue(ev.wait_for('state', speech.STATE_READY))
        eng.restart('from menu')
        self.assertTrue(ev.wait_for('state', speech.STATE_READY, count=2))
        self.assertTrue(wait_dead(self.h.pids()[0]))
        self.recovers(eng, ev)


class Backoff(EngineCase):
    def test_crash_loop_backs_off_then_recovers(self):
        self.h.scenario(default={'exit_at_start': 1})
        eng, ev = self.h.engine(backoff=(0.0, 0.2, 0.4, 0.8))
        eng.ensure_running()
        time.sleep(1.2)
        n = len(self.h.spawns())
        # Unthrottled, a crash loop spawns ~20-30 Python processes a second.
        # With the schedule above: t=0, 0, 0.2, 0.6, (1.4) -> 4, maybe 5.
        self.assertTrue(3 <= n <= 5, 'spawned %d times in 1.2s' % n)
        self.assertEqual(eng.state, speech.STATE_RESTARTING)
        self.assertEqual(ev.of('error'), [], 'no session was affected')
        self.h.scenario()                       # the helper is fixed
        self.assertTrue(ev.wait_for('state', speech.STATE_READY, timeout=4.0),
                        'engine did not recover by itself')
        self.dictate(eng, ev)

    def test_start_during_a_crash_loop_gets_one_error_on_time(self):
        self.h.scenario(default={'exit_at_start': 'kill'})
        eng, ev = self.h.engine(start_timeout=0.8)
        t0 = time.monotonic()
        sid = eng.start('en-US')
        args = self.assert_one_terminal(ev, sid, 'error')
        took = time.monotonic() - t0
        self.assertEqual(args[1], speech.E_HELPER_UNAVAILABLE)
        self.assertTrue(0.7 <= took < 2.0, 'answered after %.2fs' % took)

    def test_start_skips_the_backoff_wait(self):
        self.h.scenario(spawns=[{'exit_at_start': 1}])
        eng, ev = self.h.engine(backoff=(20.0,))
        eng.ensure_running()
        self.assertTrue(ev.wait_for('state', speech.STATE_RESTARTING))
        time.sleep(0.3)
        self.assertEqual(len(self.h.spawns()), 1, 'backoff was not honoured')
        t0 = time.monotonic()
        sid = eng.start('en-US')                # the user wants it now
        self.assertTrue(ev.wait_for('started', sid, timeout=3.0))
        self.assertLess(time.monotonic() - t0, 2.0)
        eng.stop(sid)
        self.assert_one_terminal(ev, sid, 'final')

    def test_failure_count_resets_once_the_helper_is_stable(self):
        self.h.scenario(spawns=[{'exit_at_start': 1}, {'exit_at_start': 1}])
        eng, ev = self.h.engine(backoff=(0.0, 0.1, 5.0), stable_after=0.2)
        eng.ensure_running()
        self.assertTrue(ev.wait_for('state', speech.STATE_READY, timeout=4.0))
        time.sleep(0.5)                         # stable: failures forgotten
        os.kill(self.h.pids()[-1], signal.SIGKILL)
        t0 = time.monotonic()
        self.assertTrue(ev.wait_for('state', speech.STATE_READY, count=2,
                                    timeout=3.0),
                        'old failures still counted (5s backoff applied)')
        self.assertLess(time.monotonic() - t0, 2.0)


class Unavailable(EngineCase):
    def test_missing_binary_is_reported_clearly_and_heals(self):
        path = os.path.join(self.h.dir, 'freeflo-helper')
        eng, ev = self.h.engine(helper=path)
        self.assertTrue(eng.is_available)       # not known yet: lazy
        sid = eng.start('en-US')
        args = self.assert_one_terminal(ev, sid, 'error')
        self.assertEqual(args[1], speech.E_HELPER_MISSING)
        self.assertIn(path, args[2])
        self.assertEqual(eng.state, speech.STATE_UNAVAILABLE)
        self.assertFalse(eng.is_available)
        self.assertEqual(eng.unavailable[0], speech.E_HELPER_MISSING)
        self.assertEqual(self.h.spawns(), [])
        time.sleep(0.3)
        self.assertEqual(eng.restart_count, 0, 'must not retry by itself')

        # The binary appears (e.g. a rebuilt dev tree): next use just works.
        with open(path, 'w') as f:
            f.write('#!/bin/sh\nexec "%s" "%s" "$@"\n' % (sys.executable,
                                                         FAKE_HELPER))
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
        sid, args = self.dictate(eng, ev)
        self.assertEqual(args[1], 'hello world')
        self.assertTrue(eng.is_available)
        self.assertEqual(eng.state, speech.STATE_READY)

    def test_non_executable_binary_counts_as_missing(self):
        path = os.path.join(self.h.dir, 'freeflo-helper')
        with open(path, 'w') as f:
            f.write('not a program')
        eng, ev = self.h.engine(helper=path)
        sid = eng.start('en-US')
        args = self.assert_one_terminal(ev, sid, 'error')
        self.assertEqual(args[1], speech.E_HELPER_MISSING)

    def test_unrunnable_binary_is_unavailable_not_a_crash_loop(self):
        path = os.path.join(self.h.dir, 'freeflo-helper')
        with open(path, 'wb') as f:
            f.write(b'\x00\x01\x02 definitely not mach-o')
        os.chmod(path, 0o755)
        eng, ev = self.h.engine(helper=path)
        sid = eng.start('en-US')
        self.assertTrue(ev.wait_terminal(sid))
        self.assertEqual(ev.terminals(sid)[0][1][1], speech.E_HELPER_MISSING)
        self.assertEqual(eng.state, speech.STATE_UNAVAILABLE)

    def test_old_macos_is_a_permanent_clear_state(self):
        eng, ev = self.h.engine(macos=(15, 6))
        self.assertEqual(eng.state, speech.STATE_UNAVAILABLE)
        self.assertFalse(eng.is_available)
        self.assertEqual(eng.unavailable[0], speech.E_OS_TOO_OLD)
        self.assertIn('macOS 26', eng.unavailable[1])
        for _ in range(2):
            sid = eng.start('en-US')
            args = self.assert_one_terminal(ev, sid, 'error')
            self.assertEqual(args[1], speech.E_OS_TOO_OLD)
        eng.prewarm('en-US')
        time.sleep(0.2)
        self.assertEqual(self.h.spawns(), [], 'must never spawn on an old OS')

    def test_compatibility_version_answers_are_not_trusted(self):
        parse = speech.parse_macos_version
        self.assertEqual(parse('26.0'), (26, 0))
        self.assertEqual(parse('27.2.1'), (27, 2))
        self.assertEqual(parse('15.6.1'), (15, 6))      # genuinely too old
        self.assertEqual(parse('11'), (11, 0))
        self.assertEqual(parse('10.15.7'), (10, 15))
        # What macOS answers a binary linked against an older SDK - as the
        # packaged app's launcher is. Says nothing about the real system.
        self.assertIsNone(parse('10.16'))
        self.assertIsNone(parse('16.0'))
        self.assertIsNone(parse('16.4'))
        self.assertIsNone(parse('25.9'))
        self.assertIsNone(parse(''))
        self.assertIsNone(parse(None))

    def test_the_packaged_app_on_macos_26_is_not_called_too_old(self):
        real = speech.platform.mac_ver
        speech.platform.mac_ver = lambda: ('16.0', ('', '', ''), 'arm64')
        try:
            self.assertIsNone(speech.macos_version())
            eng, ev = self.h.engine(macos=speech.macos_version())
        finally:
            speech.platform.mac_ver = real
        self.assertTrue(eng.is_available)
        self.dictate(eng, ev)

    def test_a_helper_for_another_cpu_is_named_as_such(self):
        real = speech.subprocess.Popen

        def wrong_arch(*args, **kwargs):
            raise OSError(speech._EBADARCH, 'Bad CPU type in executable')
        speech.subprocess.Popen = wrong_arch
        try:
            eng, ev = self.h.engine()
            sid = eng.start('en-US')
            args = self.assert_one_terminal(ev, sid, 'error')
        finally:
            speech.subprocess.Popen = real
        self.assertEqual(args[1], speech.E_WRONG_ARCH)
        self.assertIn('Apple silicon', speech.user_message(args[1]))
        self.dictate(eng, ev)                   # not a permanent state

    def test_unknown_macos_version_does_not_block(self):
        eng, ev = self.h.engine(macos=None)
        self.dictate(eng, ev)
        eng2, ev2 = self.h.engine(macos=(26, 0))
        self.dictate(eng2, ev2)


class Shutdown(EngineCase):
    def test_shutdown_quits_the_helper(self):
        eng, ev = self.h.engine()
        self.dictate(eng, ev)
        pid = self.h.pids()[0]
        self.assertTrue(pid_alive(pid))
        t0 = time.monotonic()
        eng.shutdown()
        self.assertLess(time.monotonic() - t0, 2.0)
        self.assertTrue(wait_dead(pid, 1.0), 'helper survived shutdown')
        self.assertEqual(len(self.h.commands('quit')), 1)
        self.assertEqual(eng.state, speech.STATE_STOPPED)
        eng.shutdown()                          # idempotent
        self.assertIsNone(eng.start('en-US'))
        self.assertFalse(eng.stop())
        self.assertFalse(eng.prewarm('en-US'))

    def test_shutdown_kills_a_helper_that_refuses_to_quit(self):
        self.h.scenario(default={'ignore_quit': True, 'ignore_eof': True})
        eng, ev = self.h.engine(quit_grace=0.3)
        eng.ensure_running()
        self.assertTrue(ev.wait_for('state', speech.STATE_READY))
        pid = self.h.pids()[0]
        t0 = time.monotonic()
        eng.shutdown()
        self.assertLess(time.monotonic() - t0, 2.0)
        self.assertTrue(wait_dead(pid, 1.0), 'stubborn helper not SIGKILLed')

    def test_shutdown_mid_recording_is_quiet(self):
        eng, ev = self.h.engine()
        sid = eng.start('en-US')
        self.assertTrue(ev.wait_for('started', sid))
        eng.shutdown()
        self.assertTrue(wait_dead(self.h.pids()[0], 1.0))
        self.assertEqual(ev.terminals(sid), [])

    def test_shutdown_of_an_unused_engine(self):
        eng, ev = self.h.engine()
        eng.shutdown()
        self.assertEqual(eng.state, speech.STATE_STOPPED)
        self.assertEqual(self.h.spawns(), [])

    def test_helper_dies_when_the_app_dies_without_cleanup(self):
        # A separate "app" process that is SIGKILLed mid-recording: no atexit,
        # no shutdown(). The helper must still go away (its stdin hits EOF),
        # which only holds if no other descriptor keeps the pipe open.
        script = textwrap.dedent('''
            import os, sys, time
            sys.path.insert(0, %r)
            sys.path.insert(0, %r)
            from support import Harness
            h = Harness.__new__(Harness)
            h.dir = %r
            h.engines = []
            eng, ev = h.engine()
            sid = eng.start('en-US')
            assert ev.wait_for('started', sid)
            print(eng.helper_pid, flush=True)
            time.sleep(60)
        ''') % (REPO_ROOT, os.path.join(REPO_ROOT, 'tests'), self.h.dir)
        app = subprocess.Popen([sys.executable, '-c', script],
                               stdout=subprocess.PIPE)
        try:
            line = app.stdout.readline().strip()
            self.assertTrue(line, 'the app process never started a helper')
            helper_pid = int(line)
            self.assertTrue(pid_alive(helper_pid))
            app.kill()                          # as abrupt as it gets
            app.wait(timeout=3)
            self.assertTrue(wait_dead(helper_pid, 3.0),
                            'helper outlived the app (orphaned, mic held)')
        finally:
            if app.poll() is None:
                app.kill()
                app.wait(timeout=3)
            app.stdout.close()


class Stress(EngineCase):
    def test_every_session_gets_exactly_one_terminal_under_chaos(self):
        self.h.scenario(default={'final_delay': 0.01})
        eng, ev = self.h.engine(start_timeout=2.0, stop_timeout_base=2.0,
                                backoff=(0.0, 0.02, 0.05))
        ids, ids_lock = [], threading.Lock()
        stop_at = time.monotonic() + 2.5

        def user(seed):
            rng = random.Random(seed)
            while time.monotonic() < stop_at:
                sid = eng.start('en-US')
                with ids_lock:
                    ids.append(sid)
                time.sleep(rng.uniform(0.0, 0.05))
                roll = rng.random()
                if roll < 0.5:
                    eng.stop(sid)
                elif roll < 0.8:
                    eng.cancel(sid)
                elif roll < 0.9:
                    eng.stop()
                    eng.cancel()
                time.sleep(rng.uniform(0.0, 0.03))

        def chaos():
            rng = random.Random(99)
            while time.monotonic() < stop_at:
                time.sleep(rng.uniform(0.2, 0.5))
                pid = eng.helper_pid
                if pid:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

        threads = [threading.Thread(target=user, args=(i,)) for i in range(5)]
        threads.append(threading.Thread(target=chaos))
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # Close out whatever is still open, then everything must settle.
        last = eng.start('en-US')
        ids.append(last)
        self.assertTrue(ev.wait_for('started', last, timeout=5.0)
                        or ev.wait_terminal(last, timeout=0.1))
        eng.stop(last)

        def settled(e):
            done = set(a[0] for n, a in e.items
                       if n in ('final', 'cancelled', 'error') and a)
            return all(i in done for i in ids)
        self.assertTrue(ev.wait(settled, timeout=10.0),
                        'some sessions never got a terminal callback')
        time.sleep(0.2)
        counts = {}
        for n, a in ev.items:
            if n in ('final', 'cancelled', 'error') and a and a[0] is not None:
                counts[a[0]] = counts.get(a[0], 0) + 1
        self.assertGreater(len(ids), 50)
        self.assertEqual(sorted(counts), sorted(ids))
        doubles = dict((k, v) for k, v in counts.items() if v != 1)
        self.assertEqual(doubles, {}, 'sessions with != 1 terminal callback')
        self.assertEqual(len(set(ids)), len(ids), 'session ids were reused')
        # ...and after all that abuse the next dictation simply works.
        self.dictate(eng, ev)


class PureHelpers(unittest.TestCase):
    def test_stop_deadline_scales_with_audio_length(self):
        eng = SpeechEngine('/nonexistent', macos=None)
        self.assertEqual(eng.stop_timeout_for(0), 6.0)
        self.assertEqual(eng.stop_timeout_for(10), 8.0)
        self.assertEqual(eng.stop_timeout_for(70), 20.0)
        self.assertEqual(eng.stop_timeout_for(10 ** 6), 90.0)   # capped
        self.assertEqual(eng.stop_timeout_for(-5), 6.0)
        self.assertEqual(eng.stop_timeout_for(None), 6.0)
        self.assertLess(eng.stop_timeout_for(5), eng.stop_timeout_for(50))
        eng.shutdown()

    def test_unknown_option_is_rejected(self):
        with self.assertRaises(TypeError):
            SpeechEngine('/nonexistent', macos=None, start_timeuot=1)

    def test_context_is_sanitised_and_bounded(self):
        clean = speech._clean_context(
            ['  Kiwi ', 'kiwi', '', None, 7, 'U  P I', 'x' * 50], 10, 20)
        self.assertEqual(clean, ['Kiwi', 'U P I'])
        self.assertEqual(speech._clean_context(None, 5, 100), [])
        self.assertEqual(len(speech._clean_context(
            ['w%d' % i for i in range(500)], 200, 10 ** 6)), 200)

    def test_user_messages_cover_every_code(self):
        for code in speech.HELPER_ERROR_CODES + (
                speech.E_HELPER_MISSING, speech.E_OS_TOO_OLD,
                speech.E_HELPER_CRASHED, speech.E_HELPER_TIMEOUT,
                speech.E_HELPER_PROTOCOL, speech.E_HELPER_UNAVAILABLE):
            self.assertIn(code, speech.USER_MESSAGES)
            self.assertNotIn('helper', speech.user_message(code).lower())
        self.assertTrue(speech.user_message('something_new'))

    def test_macos_version_shape(self):
        v = speech.macos_version()
        self.assertTrue(v is None or (len(v) == 2 and v[0] >= 11))

    def test_pick_locale(self):
        sup = ['en-US', 'en-IN', 'en-GB', 'hi-IN', 'es-ES', 'es-MX', 'pt-BR',
               'zh-CN', 'mul-IN']
        pick = speech.pick_locale
        self.assertEqual(pick('en', sup, system_locale='en_IN'), 'en-IN')
        self.assertEqual(pick('en', sup, system_locale='fr_FR'), 'en-US')
        self.assertEqual(pick('en', sup), 'en-US')
        self.assertEqual(pick('en-GB', sup, system_locale='en_IN'), 'en-GB')
        self.assertEqual(pick('en_gb', sup), 'en-GB')
        self.assertEqual(pick('hi', sup, system_locale='en_IN'), 'hi-IN')
        self.assertEqual(pick('es', sup, system_locale='en_MX'), 'es-MX')
        self.assertEqual(pick('es', sup, installed=['es-MX']), 'es-MX')
        self.assertEqual(pick('es', sup), 'es-ES')
        self.assertEqual(pick('auto', sup, system_locale='en_IN'), 'en-IN')
        self.assertEqual(pick('', sup, system_locale='hi_IN'), 'hi-IN')
        self.assertEqual(pick(None, sup), 'en-US')
        self.assertEqual(pick('en-AU', sup, system_locale='en_IN'), 'en-IN')
        self.assertIsNone(pick('ja', sup))
        self.assertIsNone(pick('en', []))
        self.assertIsNone(pick('en', None))

    def test_transcribe_file_wrapper(self):
        helper = [sys.executable, FAKE_HELPER]
        res = speech.transcribe_file(helper, FAKE_HELPER, locale='en-IN',
                                     context=['Kiwi', ' UPI '])
        self.assertEqual(res['text'], 'fake transcript')
        self.assertEqual(res['locale'], 'en-IN')
        self.assertEqual(res['context'], ['Kiwi', 'UPI'])
        with self.assertRaises(RuntimeError):
            speech.transcribe_file(helper, '/nonexistent.wav')
        with self.assertRaises(RuntimeError):
            speech.transcribe_file('/nonexistent/helper', FAKE_HELPER)


@unittest.skipUnless(os.environ.get('FREEFLO_HELPER'),
                     'set FREEFLO_HELPER=/path/to/freeflo-helper to run '
                     'against the real binary')
class RealHelper(unittest.TestCase):
    """Opt-in checks against the real Swift helper. File transcription needs
    no permission; nothing here opens the microphone."""

    def test_version_and_serve_handshake(self):
        helper = os.environ['FREEFLO_HELPER']
        events = []
        cond = threading.Condition()

        def on_status(status):
            with cond:
                events.append(status)
                cond.notify_all()
        eng = SpeechEngine(helper, on_status=on_status)
        try:
            eng.ensure_running()
            with cond:
                cond.wait_for(lambda: events, timeout=15.0)
            self.assertTrue(events, 'no status from the real helper '
                            '(state=%s, %s)' % (eng.state, eng.last_failure))
            self.assertIn(events[0]['mic'],
                          ('granted', 'denied', 'undetermined'))
            self.assertTrue(events[0]['supported'])
            self.assertTrue(eng.helper_version)
        finally:
            eng.shutdown()

    @unittest.skipUnless(os.environ.get('FREEFLO_TEST_WAV'),
                         'set FREEFLO_TEST_WAV to a 16 kHz mono clip')
    def test_transcribe_file(self):
        res = speech.transcribe_file(os.environ['FREEFLO_HELPER'],
                                     os.environ['FREEFLO_TEST_WAV'],
                                     locale='en-US')
        self.assertTrue(res['text'].strip())
        self.assertGreater(res['audio_sec'], 0)

    @unittest.skipUnless(os.environ.get('FREEFLO_TEST_WAV_MIXED'),
                         'set FREEFLO_TEST_WAV_MIXED to a 16 kHz mono clip '
                         'with English and Hindi sentences')
    def test_transcribe_file_explains_two_languages(self):
        try:
            res = speech.transcribe_file(
                os.environ['FREEFLO_HELPER'], os.environ['FREEFLO_TEST_WAV_MIXED'],
                languages=['en-IN', 'hi-IN'], explain=True)
        except RuntimeError as e:
            if 'not installed' in str(e):
                self.skipTest('needs en-IN and hi-IN installed')
            raise
        self.assertEqual(res['languages'], ['en-IN', 'hi-IN'])
        self.assertEqual(res['used'], ['en-IN', 'hi-IN'])
        self.assertIn('S', res['sources'])
        self.assertIn('P', res['sources'])
        rules = set(d['rule'] for d in res['decisions'])
        self.assertTrue(rules <= {'keep', 'same', 'floor', 'margin', 'lexicon'}, rules)
        self.assertTrue(any(d['hindi'] for d in res['decisions']))
        self.assertTrue(res['primary_segments'] and res['secondary_segments'])

    # -- two languages / downloads, through the supervisor ---------------

    class _Seen(object):
        """Callback recorder for the real-helper checks."""

        def __init__(self):
            self.cond = threading.Condition()
            self.events = []

        def hook(self, name):
            def call(*args):
                with self.cond:
                    self.events.append((name,) + args)
                    self.cond.notify_all()
            return call

        def wait(self, name, timeout=20.0):
            def find():
                return [e for e in self.events if e[0] == name]
            with self.cond:
                self.cond.wait_for(find, timeout=timeout)
                found = find()
            return found[0] if found else None

    def _real_engine(self, **env):
        seen = self._Seen()
        full = dict(os.environ, FREEFLO_NO_RESERVE='1')    # never touch the
        full.update(env)                                   # shared asset table
        eng = SpeechEngine(
            os.environ['FREEFLO_HELPER'], env=full,
            **dict(('on_' + n, seen.hook(n)) for n in (
                'status', 'started', 'started_info', 'final', 'final_info',
                'error', 'installed', 'install_failed', 'install_progress')))
        self.addCleanup(eng.shutdown)
        eng.ensure_running()
        status = seen.wait('status', 15.0)
        self.assertTrue(status, 'no status from the real helper')
        return eng, seen, status[1]

    def test_install_of_an_installed_language_answers_at_once(self):
        """Only ever asks for a language that is already on this Mac, so
        nothing is downloaded."""
        eng, seen, status = self._real_engine(FREEFLO_FAKE_MIC='@none')
        installed = status['installed']
        if not installed:
            self.skipTest('no speech model installed on this Mac')
        tag = 'hi-IN' if 'hi-IN' in installed else installed[0]
        t0 = time.monotonic()
        eng.install(tag)
        done = seen.wait('installed', 15.0)
        self.assertTrue(done, 'no answer: %r' % (seen.events[-3:],))
        self.assertLess(time.monotonic() - t0, 5.0)
        self.assertEqual(done[1], tag)
        state = eng.installs()[tag]
        self.assertEqual((state['state'], state['already'], state['fraction']),
                         ('installed', True, 1.0))
        self.assertIn(tag, eng.last_status['installed'])
        self.assertFalse([e for e in seen.events if e[0] == 'install_progress'])

    @unittest.skipUnless(os.environ.get('FREEFLO_TEST_WAV_MIXED'),
                         'set FREEFLO_TEST_WAV_MIXED to a 16 kHz mono clip '
                         'with English and Hindi sentences')
    def test_two_languages_through_the_engine(self):
        import wave
        clip = os.environ['FREEFLO_TEST_WAV_MIXED']
        w = wave.open(clip)
        seconds = w.getnframes() / float(w.getframerate())
        w.close()
        eng, seen, status = self._real_engine(FREEFLO_FAKE_MIC=clip)
        both = ['en-IN', 'hi-IN']
        if not all(t in status['installed'] for t in both):
            self.skipTest('needs en-IN and hi-IN installed')
        self.assertGreaterEqual(status['max_languages'], 2)
        self.assertTrue(any('hi-IN' in g and 'mul-IN' in g
                            for g in status['groups']), status['groups'])
        sid = eng.start('en-IN', languages=both)
        self.assertTrue(seen.wait('started', 15.0), seen.events[-3:])
        info = seen.wait('started_info', 1.0)
        self.assertEqual(info[2]['languages'], both)
        time.sleep(seconds + 0.6)
        self.assertTrue(eng.stop(sid))
        final = seen.wait('final', 20.0)
        self.assertTrue(final, seen.events[-3:])
        text, elapsed_ms = final[2], final[4]
        self.assertTrue(text.strip())
        self.assertTrue(all(ord(c) < 128 for c in text), text)   # Latin letters
        self.assertLess(elapsed_ms, 1500.0)
        info = seen.wait('final_info', 1.0)[2]
        self.assertEqual(info['languages'], both)
        self.assertEqual(info['used'], both)
        self.assertIn(info['dominant'], both)


if __name__ == '__main__':
    unittest.main()
