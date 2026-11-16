"""engine/setup_state.py: setup takes one pass and nothing is asked twice.

Pure logic, no PyObjC: a fake clock and a tiny stand-in for the app (``Rig``)
that carries out the effects the way app.py does, so whole launches can be
replayed - including the one from the log that prompted this module.
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import setup_state as S  # noqa: E402


class Clock(object):
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def tick(self, seconds):
        self.now += seconds


class Rig(object):
    """What app.py does with a SetupFlow, minus AppKit.

    ``mic`` / ``trusted`` are the world (what macOS would report); the user
    is simulated by changing them. ``tap_works`` False makes every tap
    creation fail, as CGEventTapCreate returning NULL would."""

    def __init__(self, onboarded, mic=S.MIC_GRANTED, trusted=True, ledger=None,
                 version='1.5.1', enabled=True, engine_error=None):
        self.clock = Clock()
        self.mic = mic
        self.trusted = trusted
        self.enabled = enabled
        self.engine_error = engine_error
        self.tap_works = True
        self.tap_running = False
        self.window_open = False
        self.saved_ledger = ledger
        self.log = []                # every effect, in order
        self.flow = S.SetupFlow(ledger=ledger, version=version,
                                onboarded=onboarded, clock=self.clock,
                                ptt_label='Left Option')

    # -- the app side -----------------------------------------------------

    def run(self, effects):
        for effect in effects:
            name = effect[0]
            self.log.append(effect)
            if name == S.EFFECT_START_TAP:
                self.tap_running = bool(self.tap_works and self.trusted)
            elif name == S.EFFECT_STOP_TAP:
                self.tap_running = False
            elif name == S.EFFECT_OPEN_SETUP:
                self.window_open = True
            elif name == S.EFFECT_CLOSE_SETUP:
                self.window_open = False
                self.run(self.flow.window_closed())
            elif name == S.EFFECT_SAVE_LEDGER:
                self.saved_ledger = json.loads(json.dumps(self.flow.ledger()))
        return effects

    def poll(self, seconds=0.0):
        """One main-thread poll, ``seconds`` later."""
        self.clock.tick(seconds)
        return self.run(self.flow.update(
            mic=self.mic, accessibility=self.trusted,
            engine_error=self.engine_error, enabled=self.enabled,
            tap_running=self.tap_running))

    def spin(self, seconds, step=0.5):
        """Poll every ``step`` seconds for ``seconds``."""
        out = []
        for _ in range(int(round(seconds / step))):
            out.extend(self.poll(step))
        return out

    def press(self, action):
        return self.run(self.flow.action(action))

    def close_window(self):
        self.window_open = False
        return self.run(self.flow.window_closed())

    # -- reading the log ---------------------------------------------------

    def count(self, name):
        return len([e for e in self.log if e[0] == name])

    def notices(self, notice_id=None):
        return [e for e in self.log if e[0] == S.EFFECT_NOTICE
                and (notice_id is None or e[1] == notice_id)]

    def rows(self):
        return {r['id']: r for r in self.flow.view()['rows']}

    def next_id(self):
        nxt = self.flow.view()['next']
        return nxt['id'] if nxt else None


class FirstRun(unittest.TestCase):

    def test_a_fresh_install_is_one_pass_with_each_thing_asked_once(self):
        r = Rig(onboarded=False, mic=S.MIC_UNDETERMINED, trusted=False)
        r.poll()
        self.assertTrue(r.window_open)
        self.assertEqual(r.count(S.EFFECT_OPEN_SETUP), 1)
        view = r.flow.view()
        self.assertEqual(view['mode'], S.MODE_FIRST_RUN)
        self.assertEqual([x['id'] for x in view['rows']], ['mic', 'accessibility'])
        self.assertFalse(view['can_finish'])
        # One thing at a time: the microphone first, Accessibility held back.
        self.assertEqual(r.next_id(), S.ACT_GRANT_MIC)
        self.assertTrue(r.rows()['accessibility']['locked'])
        self.assertIsNone(r.rows()['accessibility']['action'])

        r.press(S.ACT_NEXT)
        self.assertEqual(r.count(S.EFFECT_REQUEST_MIC), 1)
        self.assertEqual(r.rows()['mic']['state'], S.ST_WAITING)
        self.assertIsNone(r.next_id())           # the system prompt is up
        # Impatient clicks do not ask again.
        r.press(S.ACT_GRANT_MIC)
        r.press(S.ACT_GRANT_MIC)
        self.assertEqual(r.count(S.EFFECT_REQUEST_MIC), 1)
        # ... and Accessibility cannot be started underneath the prompt.
        self.assertIsNone(r.rows()['accessibility']['action'])

        r.mic = S.MIC_GRANTED                    # "Allow"
        r.poll(3.0)
        self.assertEqual(r.rows()['mic']['state'], S.ST_GRANTED)
        self.assertEqual(r.next_id(), S.ACT_GRANT_ACCESSIBILITY)

        r.press(S.ACT_NEXT)
        self.assertEqual(r.count(S.EFFECT_PROMPT_ACCESSIBILITY), 1)
        self.assertEqual(r.count(S.EFFECT_OPEN_ACCESSIBILITY_SETTINGS), 0)
        self.assertEqual(r.rows()['accessibility']['state'], S.ST_WAITING)
        # The button is now the way to Settings - never a second prompt.
        self.assertEqual(r.next_id(), S.ACT_OPEN_ACCESSIBILITY_SETTINGS)
        r.press(S.ACT_NEXT)
        r.press(S.ACT_GRANT_ACCESSIBILITY)
        self.assertEqual(r.count(S.EFFECT_PROMPT_ACCESSIBILITY), 1)
        self.assertEqual(r.count(S.EFFECT_OPEN_ACCESSIBILITY_SETTINGS), 2)

        r.trusted = True                         # the switch in Settings
        r.poll(4.0)
        self.assertEqual(r.count(S.EFFECT_START_TAP), 1)   # at once, no restart
        self.assertTrue(r.tap_running)
        r.spin(1.5)
        view = r.flow.view()
        self.assertTrue(view['complete'] and view['ready'] and view['hotkeys_live'])
        self.assertFalse(view['restart_needed'])
        self.assertEqual(r.next_id(), S.ACT_FINISH)
        self.assertTrue(r.window_open)           # first run: the user closes it
        self.assertEqual(r.notices(), [])

        r.press(S.ACT_NEXT)
        self.assertEqual(r.count(S.EFFECT_COMPLETE_ONBOARDING), 1)
        self.assertFalse(r.window_open)
        self.assertEqual(len(r.notices(S.NOTICE_READY)), 1)
        self.assertIn('Left Option', r.notices(S.NOTICE_READY)[0][2])
        self.assertTrue(r.flow.onboarded)
        # ... and then nothing, however long it runs.
        before = len(r.log)
        r.spin(30.0)
        self.assertEqual([e for e in r.log[before:] if e[0] != S.EFFECT_LOG], [])
        self.assertEqual(r.count(S.EFFECT_RESTART_APP), 0)
        self.assertEqual(r.count(S.EFFECT_OPEN_SETUP), 1)
        self.assertEqual(r.flow.menu(), {'setup': False, 'restart': False})
        self.assertEqual(r.saved_ledger['granted'], {'mic': True, 'accessibility': True})
        self.assertEqual(r.saved_ledger['ready'], '1.5.1')

    def test_first_run_with_everything_already_granted_just_says_start(self):
        r = Rig(onboarded=False)
        r.poll()
        r.spin(1.5)
        self.assertTrue(r.window_open)
        self.assertEqual(r.next_id(), S.ACT_FINISH)
        self.assertEqual(r.count(S.EFFECT_REQUEST_MIC), 0)
        self.assertEqual(r.count(S.EFFECT_PROMPT_ACCESSIBILITY), 0)

    def test_closing_the_first_run_window_leaves_a_way_back_and_no_notice(self):
        r = Rig(onboarded=False, mic=S.MIC_UNDETERMINED, trusted=False)
        r.poll()
        r.close_window()
        r.spin(10.0)
        self.assertEqual(r.count(S.EFFECT_OPEN_SETUP), 1)      # not reopened
        self.assertEqual(r.flow.menu()['setup'], True)
        self.assertEqual(r.notices(), [])
        r.run(r.flow.open_window())                            # the menu item
        self.assertTrue(r.window_open)
        self.assertEqual(r.flow.view()['mode'], S.MODE_FIRST_RUN)

    def test_finish_is_refused_until_both_are_granted(self):
        r = Rig(onboarded=False, mic=S.MIC_GRANTED, trusted=False)
        r.poll()
        r.press(S.ACT_FINISH)
        self.assertEqual(r.count(S.EFFECT_COMPLETE_ONBOARDING), 0)
        self.assertTrue(r.window_open)

    def test_an_unusable_engine_does_not_trap_the_user_in_the_window(self):
        r = Rig(onboarded=False, mic=None, trusted=True,
                engine_error='freeflo needs macOS 26 or later')
        r.poll()
        row = r.rows()['mic']
        self.assertEqual(row['state'], S.ST_BLOCKED)
        self.assertEqual(row['hint'], 'freeflo needs macOS 26 or later')
        self.assertIsNone(row['action'])
        self.assertTrue(r.flow.view()['can_finish'])
        r.spin(1.5)
        self.assertEqual(r.next_id(), S.ACT_FINISH)
        r.press(S.ACT_GRANT_MIC)
        self.assertEqual(r.count(S.EFFECT_REQUEST_MIC), 0)


class SetUpUser(unittest.TestCase):

    def test_nothing_missing_means_nothing_shown(self):
        r = Rig(onboarded=True, ledger={'v': 1, 'version': '1.5.1'})
        r.poll()
        r.spin(60.0)
        self.assertFalse(r.window_open)
        self.assertEqual(r.count(S.EFFECT_OPEN_SETUP), 0)
        self.assertEqual(r.notices(), [])
        self.assertEqual(r.count(S.EFFECT_START_TAP), 1)       # the hotkeys
        self.assertEqual(r.count(S.EFFECT_STATUS), 0)          # status line untouched
        self.assertEqual(r.flow.menu(), {'setup': False, 'restart': False})
        self.assertFalse(r.flow.fast_poll)

    def test_a_tap_the_app_started_itself_at_launch_is_accepted(self):
        r = Rig(onboarded=True)
        r.tap_running = True
        r.poll()
        self.assertEqual(r.count(S.EFFECT_START_TAP), 0)
        self.assertTrue(r.flow.ready)

    def test_the_real_1_5_0_launch_replayed(self):
        # The user's first launch of 1.5.0 (ad-hoc signed update of a set-up
        # install): onboarded, no ledger yet, microphone "undetermined" and
        # Accessibility untrusted because the code identity changed. The old
        # app fired both system prompts within a second of each other, put
        # "Restart to Activate" in the menu, and the user went through 8
        # launches and a second onboarding.
        r = Rig(onboarded=True, mic=None, trusted=False, version='1.5.0')
        r.poll()                                 # before the helper's status
        self.assertFalse(r.window_open)          # no guessing
        self.assertEqual(r.count(S.EFFECT_REQUEST_MIC), 0)
        self.assertEqual(r.count(S.EFFECT_PROMPT_ACCESSIBILITY), 0)
        r.mic = S.MIC_UNDETERMINED               # helper status, ~100 ms in
        r.poll(0.15)
        self.assertTrue(r.window_open)
        view = r.flow.view()
        self.assertEqual(view['mode'], S.MODE_REPAIR)
        self.assertTrue(view['updated'])
        self.assertIn('update', view['subtitle'])
        self.assertEqual([x['id'] for x in view['rows']], ['mic', 'accessibility'])
        # Still nothing has been asked: the user starts each prompt.
        self.assertEqual(r.count(S.EFFECT_REQUEST_MIC), 0)
        self.assertEqual(r.count(S.EFFECT_PROMPT_ACCESSIBILITY), 0)

        r.press(S.ACT_NEXT)                      # Microphone: Allow
        r.mic = S.MIC_GRANTED
        r.poll(5.0)
        r.press(S.ACT_NEXT)                      # Accessibility: Allow
        r.spin(6.0)
        r.trusted = True
        r.poll(0.5)
        self.assertTrue(r.tap_running)
        r.spin(4.0)

        self.assertFalse(r.window_open)          # closed itself
        self.assertEqual(r.count(S.EFFECT_OPEN_SETUP), 1)
        self.assertEqual(r.count(S.EFFECT_CLOSE_SETUP), 1)
        self.assertEqual(r.count(S.EFFECT_REQUEST_MIC), 1)
        self.assertEqual(r.count(S.EFFECT_PROMPT_ACCESSIBILITY), 1)
        self.assertEqual(r.count(S.EFFECT_RESTART_APP), 0)
        self.assertEqual(r.count(S.EFFECT_COMPLETE_ONBOARDING), 0)   # not onboarded again
        self.assertEqual(len(r.notices(S.NOTICE_READY)), 1)
        self.assertEqual(r.flow.menu(), {'setup': False, 'restart': False})
        r.spin(120.0)
        self.assertEqual(len(r.notices()), 1)

    def test_only_the_missing_row_is_shown(self):
        r = Rig(onboarded=True, mic=S.MIC_GRANTED, trusted=False,
                ledger={'v': 1, 'version': '1.5.0'})
        r.poll()
        view = r.flow.view()
        self.assertEqual(view['mode'], S.MODE_REPAIR)
        self.assertEqual([x['id'] for x in view['rows']], ['accessibility'])
        self.assertEqual(view['title'], 'One quick step')
        self.assertFalse(r.rows()['accessibility']['locked'])
        self.assertEqual(r.next_id(), S.ACT_GRANT_ACCESSIBILITY)
        self.assertEqual(r.count(S.EFFECT_REQUEST_MIC), 0)

        r = Rig(onboarded=True, mic=S.MIC_UNDETERMINED, trusted=True)
        r.poll()
        self.assertEqual([x['id'] for x in r.flow.view()['rows']], ['mic'])

    def test_the_granted_row_stays_with_its_tick_until_the_window_closes(self):
        r = Rig(onboarded=True, mic=S.MIC_GRANTED, trusted=False)
        r.poll()
        r.press(S.ACT_NEXT)
        r.trusted = True
        r.poll(2.0)
        self.assertTrue(r.window_open)
        self.assertEqual(r.rows()['accessibility']['state'], S.ST_GRANTED)
        r.poll(S.TAP_VERIFY_SEC)                 # tap verified
        self.assertTrue(r.window_open)           # long enough to see the tick
        self.assertTrue(r.flow.view()['auto_close'])
        r.poll(S.REPAIR_CLOSE_SEC / 2)
        self.assertTrue(r.window_open)
        r.poll(S.REPAIR_CLOSE_SEC)
        self.assertFalse(r.window_open)
        self.assertEqual(len(r.notices(S.NOTICE_READY)), 1)

    def test_a_late_helper_status_does_not_hold_the_window_back_for_ever(self):
        r = Rig(onboarded=True, mic=None, trusted=False)
        r.poll()
        r.spin(S.LAUNCH_SETTLE_SEC - 1.0)
        self.assertFalse(r.window_open)
        r.spin(1.5)
        self.assertTrue(r.window_open)
        self.assertEqual([x['id'] for x in r.flow.view()['rows']], ['accessibility'])
        # The status arrives after all: its row joins, in order.
        r.mic = S.MIC_UNDETERMINED
        r.poll(1.0)
        self.assertEqual([x['id'] for x in r.flow.view()['rows']],
                         ['mic', 'accessibility'])

    def test_the_window_opens_by_itself_at_most_once_per_run(self):
        r = Rig(onboarded=True, mic=S.MIC_GRANTED, trusted=False)
        r.poll()
        r.close_window()                         # "not now"
        r.spin(30.0)
        self.assertEqual(r.count(S.EFFECT_OPEN_SETUP), 1)
        self.assertEqual(r.flow.menu()['setup'], True)   # the way back
        # Granted later from System Settings: tap starts, one notice, no window.
        r.trusted = True
        r.spin(3.0)
        self.assertTrue(r.tap_running)
        self.assertEqual(r.count(S.EFFECT_OPEN_SETUP), 1)
        self.assertEqual(len(r.notices(S.NOTICE_READY)), 1)
        self.assertEqual(r.flow.menu()['setup'], False)

    def test_losing_accessibility_while_running_never_pops_a_window(self):
        r = Rig(onboarded=True)
        r.poll()
        r.spin(2.0)
        self.assertTrue(r.tap_running)
        r.trusted = False                        # switched off in Settings
        r.poll(3.0)
        self.assertFalse(r.tap_running)
        self.assertEqual(r.count(S.EFFECT_STOP_TAP), 1)
        self.assertEqual(r.count(S.EFFECT_OPEN_SETUP), 0)
        self.assertEqual(len(r.notices(S.NOTICE_ACCESSIBILITY_LOST)), 1)
        self.assertIn((S.EFFECT_STATUS, 'Accessibility access is off'), r.log)
        self.assertEqual(r.flow.menu(), {'setup': True, 'restart': False})
        r.spin(20.0)
        self.assertEqual(r.count(S.EFFECT_STOP_TAP), 1)        # once
        self.assertEqual(len(r.notices()), 1)
        # Back on: a NEW tap, "Ready" again, one confirmation, no restart.
        r.trusted = True
        r.spin(3.0)
        self.assertTrue(r.tap_running)
        self.assertEqual(r.count(S.EFFECT_START_TAP), 2)
        self.assertIn((S.EFFECT_STATUS, 'Ready'), r.log)
        self.assertEqual(len(r.notices(S.NOTICE_READY)), 1)
        self.assertEqual(r.count(S.EFFECT_RESTART_APP), 0)

    def test_the_menu_item_shows_a_review_that_changes_nothing(self):
        r = Rig(onboarded=True)
        r.poll()
        r.spin(2.0)
        r.run(r.flow.open_window())
        view = r.flow.view()
        self.assertEqual(view['mode'], S.MODE_REVIEW)
        self.assertEqual([x['state'] for x in view['rows']],
                         [S.ST_GRANTED, S.ST_GRANTED])
        self.assertEqual(view['next'], {'id': S.ACT_FINISH, 'label': 'Done', 'row': None})
        r.spin(10.0)
        self.assertTrue(r.window_open)           # a review never closes itself
        r.press(S.ACT_NEXT)
        self.assertFalse(r.window_open)
        self.assertEqual(r.count(S.EFFECT_COMPLETE_ONBOARDING), 0)
        self.assertEqual(r.notices(), [])
        self.assertEqual(r.count(S.EFFECT_REQUEST_MIC), 0)
        self.assertEqual(r.count(S.EFFECT_PROMPT_ACCESSIBILITY), 0)

    def test_opening_an_open_window_only_brings_it_forward(self):
        r = Rig(onboarded=True, mic=S.MIC_GRANTED, trusted=False)
        r.poll()
        r.press(S.ACT_NEXT)
        r.run(r.flow.open_window())
        self.assertEqual(r.count(S.EFFECT_OPEN_SETUP), 2)
        self.assertEqual(r.rows()['accessibility']['state'], S.ST_WAITING)   # kept
        self.assertEqual(r.count(S.EFFECT_PROMPT_ACCESSIBILITY), 1)


class Microphone(unittest.TestCase):

    def test_denied_goes_to_settings_and_is_watched(self):
        r = Rig(onboarded=True, mic=S.MIC_DENIED, trusted=True)
        r.poll()
        self.assertTrue(r.window_open)
        row = r.rows()['mic']
        self.assertEqual(row['state'], S.ST_DENIED)
        self.assertEqual(row['action']['id'], S.ACT_OPEN_MIC_SETTINGS)
        r.press(S.ACT_NEXT)
        r.press(S.ACT_GRANT_MIC)                 # the old page's action name
        self.assertEqual(r.count(S.EFFECT_OPEN_MIC_SETTINGS), 2)
        self.assertEqual(r.count(S.EFFECT_REQUEST_MIC), 0)     # macOS would ignore it
        # The switch in Settings sends no event: the helper is asked again,
        # but not on every 150 ms poll.
        before = r.count(S.EFFECT_REQUEST_STATUS)
        r.spin(6.0, step=0.15)
        asked = r.count(S.EFFECT_REQUEST_STATUS) - before
        self.assertTrue(3 <= asked <= 5, asked)
        r.mic = S.MIC_GRANTED
        r.spin(4.0)
        self.assertFalse(r.window_open)
        self.assertEqual(len(r.notices(S.NOTICE_READY)), 1)
        before = r.count(S.EFFECT_REQUEST_STATUS)
        r.spin(10.0)
        self.assertEqual(r.count(S.EFFECT_REQUEST_STATUS), before)

    def test_a_replaced_helper_lets_the_user_ask_again(self):
        r = Rig(onboarded=False, mic=S.MIC_UNDETERMINED, trusted=False)
        r.poll()
        r.press(S.ACT_GRANT_MIC)
        self.assertEqual(r.rows()['mic']['state'], S.ST_WAITING)
        r.run(r.flow.helper_restarted())         # its prompt died with it
        self.assertEqual(r.rows()['mic']['state'], S.ST_NEEDED)
        r.press(S.ACT_GRANT_MIC)
        self.assertEqual(r.count(S.EFFECT_REQUEST_MIC), 2)

    def test_a_lost_prompt_does_not_block_accessibility_for_ever(self):
        r = Rig(onboarded=False, mic=S.MIC_UNDETERMINED, trusted=False)
        r.poll()
        r.press(S.ACT_GRANT_MIC)
        r.spin(S.MIC_WAIT_UNLOCK_SEC - 2.0)
        self.assertTrue(r.rows()['accessibility']['locked'])
        r.spin(3.0)
        self.assertFalse(r.rows()['accessibility']['locked'])
        self.assertEqual(r.next_id(), S.ACT_GRANT_ACCESSIBILITY)

    def test_unknown_values_are_not_treated_as_granted(self):
        r = Rig(onboarded=True, mic='restricted-or-new', trusted=True)
        r.poll()
        self.assertFalse(r.flow.complete)
        self.assertEqual(r.flow.view()['mic'], S.MIC_UNKNOWN)


class HotkeyTap(unittest.TestCase):

    def test_a_grant_always_makes_a_new_tap_even_if_one_is_running(self):
        # A tap created before the grant has its key events masked out for
        # good (CGEvent.h), so "is running" is not good enough.
        r = Rig(onboarded=True, mic=S.MIC_GRANTED, trusted=False)
        r.poll()
        r.tap_running = True                     # e.g. started by an old code path
        r.poll(0.5)
        self.assertEqual(r.count(S.EFFECT_STOP_TAP), 1)        # untrusted: out
        r.tap_running = True
        r.trusted = True
        r.poll(0.5)
        self.assertEqual(r.count(S.EFFECT_START_TAP), 1)

    def test_no_tap_while_untrusted_or_disabled(self):
        r = Rig(onboarded=True, mic=S.MIC_GRANTED, trusted=False)
        r.poll()
        r.spin(10.0)
        self.assertEqual(r.count(S.EFFECT_START_TAP), 0)

        r = Rig(onboarded=True, enabled=False)
        r.poll()
        r.spin(10.0)
        self.assertEqual(r.count(S.EFFECT_START_TAP), 0)
        self.assertTrue(r.flow.ready)            # nothing is missing
        r.enabled = True                         # "Enable Dictation"
        r.poll(0.15)
        self.assertEqual(r.count(S.EFFECT_START_TAP), 1)

    def test_restart_is_offered_only_after_the_tap_is_refused_repeatedly(self):
        r = Rig(onboarded=True, mic=S.MIC_GRANTED, trusted=False)
        r.tap_works = False
        r.poll()
        r.press(S.ACT_NEXT)
        r.trusted = True
        r.poll(1.0)
        self.assertEqual(r.count(S.EFFECT_START_TAP), 1)
        self.assertFalse(r.flow.restart_needed)
        self.assertEqual(r.flow.menu()['restart'], False)
        r.spin(S.TAP_RETRY_SEC * S.TAP_MAX_ATTEMPTS + 2.0, step=0.5)
        self.assertEqual(r.count(S.EFFECT_START_TAP), S.TAP_MAX_ATTEMPTS)
        self.assertTrue(r.flow.restart_needed)
        self.assertEqual(r.flow.menu(), {'setup': False, 'restart': True})
        row = r.rows()['accessibility']
        self.assertEqual(row['state'], S.ST_RESTART)
        self.assertEqual(r.next_id(), S.ACT_RESTART)
        self.assertTrue(r.window_open)           # not "all set"
        self.assertEqual(r.notices(), [])
        r.spin(30.0)
        self.assertEqual(r.count(S.EFFECT_START_TAP), S.TAP_MAX_ATTEMPTS)   # gave up
        r.press(S.ACT_NEXT)
        self.assertEqual(r.count(S.EFFECT_RESTART_APP), 1)

    def test_a_tap_that_comes_up_on_a_retry_needs_no_restart(self):
        r = Rig(onboarded=True, mic=S.MIC_GRANTED, trusted=False)
        r.tap_works = False
        r.poll()
        r.trusted = True
        r.poll(1.0)
        r.spin(S.TAP_RETRY_SEC - 0.5)
        r.tap_works = True                       # macOS caught up
        r.spin(4.0)
        self.assertEqual(r.count(S.EFFECT_START_TAP), 2)
        self.assertTrue(r.flow.ready)
        self.assertFalse(r.flow.restart_needed)
        self.assertEqual(r.count(S.EFFECT_RESTART_APP), 0)

    def test_a_restart_during_first_run_does_not_bring_onboarding_back(self):
        r = Rig(onboarded=False)
        r.tap_works = False
        r.poll()
        r.spin(S.TAP_RETRY_SEC * S.TAP_MAX_ATTEMPTS + 2.0)
        self.assertEqual(r.next_id(), S.ACT_RESTART)
        r.press(S.ACT_NEXT)
        names = [e[0] for e in r.log if e[0] in (S.EFFECT_COMPLETE_ONBOARDING,
                                                 S.EFFECT_RESTART_APP)]
        self.assertEqual(names, [S.EFFECT_COMPLETE_ONBOARDING, S.EFFECT_RESTART_APP])


class Hints(unittest.TestCase):

    def test_the_stale_entry_hint_appears_for_someone_who_had_it_before(self):
        r = Rig(onboarded=True, mic=S.MIC_GRANTED, trusted=False)   # an update
        r.poll()
        # Warned before they leave for System Settings, where this window
        # cannot be seen any more.
        self.assertIn('already switched on', r.rows()['accessibility']['hint'])
        r.press(S.ACT_NEXT)
        # They had it before, so System Settings very likely shows the old
        # entry switched on: say what to do at once, not after 20 seconds.
        hint = r.rows()['accessibility']['hint']
        self.assertIn('off and on again', hint)
        self.assertIn('remove freeflo', hint)
        pushes = r.count(S.EFFECT_PUSH_STATE)
        r.spin(S.AX_STALE_HINT_SEC + 5.0)
        self.assertEqual(r.rows()['accessibility']['hint'], hint)
        self.assertEqual(r.count(S.EFFECT_PUSH_STATE), pushes)       # no churn

    def test_a_first_timer_gets_the_plain_hint_first(self):
        r = Rig(onboarded=False, mic=S.MIC_GRANTED, trusted=False)
        r.poll()
        self.assertIsNone(r.rows()['accessibility']['hint'])
        r.press(S.ACT_NEXT)
        self.assertIn('Switch freeflo on', r.rows()['accessibility']['hint'])
        self.assertFalse(r.flow.view()['updated'])
        pushes = r.count(S.EFFECT_PUSH_STATE)
        r.spin(S.AX_STALE_HINT_SEC + 1.0)        # still nothing: help them out
        self.assertIn('off and on again', r.rows()['accessibility']['hint'])
        self.assertEqual(r.count(S.EFFECT_PUSH_STATE), pushes + 1)   # pushed once

    def test_someone_who_never_had_accessibility_is_not_a_stale_case(self):
        ledger = {'v': 1, 'version': '1.5.1',
                  'granted': {'mic': True, 'accessibility': False}}
        r = Rig(onboarded=True, mic=S.MIC_GRANTED, trusted=False, ledger=ledger)
        r.poll()
        r.press(S.ACT_NEXT)
        self.assertIn('Switch freeflo on', r.rows()['accessibility']['hint'])


class Ledger(unittest.TestCase):

    def test_one_time_notices_never_repeat_across_runs(self):
        r = Rig(onboarded=True)
        self.assertTrue(r.flow.notice_once('language_changed'))
        self.assertFalse(r.flow.notice_once('language_changed'))
        self.assertTrue(r.flow.ledger_dirty)
        r.poll()                                 # the save effect
        self.assertFalse(r.flow.ledger_dirty)
        stored = json.loads(json.dumps(r.saved_ledger))
        again = S.SetupFlow(ledger=stored, version='1.5.1', onboarded=True)
        self.assertTrue(again.notice_seen('language_changed'))
        self.assertFalse(again.notice_once('language_changed'))
        self.assertTrue(again.notice_once('something_else'))

    def test_per_version_notices_come_back_once_per_version(self):
        a = S.SetupFlow(version='1.5.1', onboarded=True)
        self.assertTrue(a.notice_once('whats_new', per_version=True))
        self.assertFalse(a.notice_once('whats_new', per_version=True))
        b = S.SetupFlow(ledger=a.take_ledger(), version='1.5.1', onboarded=True)
        self.assertFalse(b.notice_once('whats_new', per_version=True))
        c = S.SetupFlow(ledger=b.ledger(), version='1.6.0', onboarded=True)
        self.assertTrue(c.notice_once('whats_new', per_version=True))
        self.assertFalse(c.notice_once('whats_new', per_version=True))

    def test_the_ledger_survives_garbage_and_is_json(self):
        for junk in (None, 'x', 7, [], {'granted': 3, 'notices': [1], 'version': 5}):
            flow = S.SetupFlow(ledger=junk, version='1.5.1', onboarded=True)
            led = flow.ledger()
            self.assertEqual(json.loads(json.dumps(led)), led)
            self.assertEqual(led['v'], S.LEDGER_VERSION)
            self.assertEqual(led['version'], '1.5.1')

    def test_an_update_is_recognised_from_the_version(self):
        same = S.SetupFlow(ledger={'v': 1, 'version': '1.5.1'}, version='1.5.1',
                           onboarded=True)
        self.assertFalse(same.view()['updated'])
        newer = S.SetupFlow(ledger={'v': 1, 'version': '1.5.0'}, version='1.5.1',
                            onboarded=True)
        self.assertTrue(newer.view()['updated'])
        fresh = S.SetupFlow(version='1.5.1', onboarded=False)
        self.assertFalse(fresh.view()['updated'])

    def test_the_ready_confirmation_is_once_per_setup_pass(self):
        r = Rig(onboarded=True, mic=S.MIC_GRANTED, trusted=False)
        r.poll()
        r.trusted = True
        r.spin(5.0)
        self.assertEqual(len(r.notices(S.NOTICE_READY)), 1)
        # The next launch of the same build has nothing to say.
        nxt = Rig(onboarded=True, ledger=r.saved_ledger)
        nxt.poll()
        nxt.spin(30.0)
        self.assertEqual(nxt.notices(), [])
        self.assertEqual(nxt.count(S.EFFECT_OPEN_SETUP), 0)


class Payload(unittest.TestCase):

    def test_the_page_payload_has_the_documented_shape(self):
        r = Rig(onboarded=False, mic=S.MIC_UNDETERMINED, trusted=False)
        r.poll()
        view = r.flow.view()
        self.assertEqual(json.loads(json.dumps(view)), view)
        for key in ('v', 'mode', 'title', 'subtitle', 'rows', 'next', 'complete',
                    'ready', 'can_finish', 'auto_close', 'restart_needed',
                    'hotkeys_live', 'updated',
                    # what the page (and tests/test_app_flow.py) read before
                    'mic', 'mic_granted', 'accessibility', 'engine_error'):
            self.assertIn(key, view)
        self.assertEqual(view['v'], 2)
        for row in view['rows']:
            self.assertEqual(sorted(row), ['action', 'detail', 'granted', 'hint',
                                           'id', 'locked', 'state', 'title'])
        self.assertIs(view['mic_granted'], False)
        self.assertIs(view['accessibility'], False)
        self.assertEqual(view['next'], {'id': 'grant_mic', 'label': 'Allow', 'row': 'mic'})

    def test_a_push_is_sent_only_when_the_page_would_change(self):
        r = Rig(onboarded=False, mic=S.MIC_UNDETERMINED, trusted=False)
        r.poll()
        self.assertEqual(r.count(S.EFFECT_PUSH_STATE), 1)
        r.spin(10.0, step=0.15)
        self.assertEqual(r.count(S.EFFECT_PUSH_STATE), 1)
        r.press(S.ACT_GRANT_MIC)
        self.assertEqual(r.count(S.EFFECT_PUSH_STATE), 2)
        r.close_window()
        r.spin(5.0)
        self.assertEqual(r.count(S.EFFECT_PUSH_STATE), 2)      # no page, no push

    def test_the_summary_line_names_everything_the_old_log_lacked(self):
        r = Rig(onboarded=True, mic=S.MIC_GRANTED, trusted=False)
        r.poll()
        self.assertEqual(r.flow.summary(),
                         'setup: mode=repair onboarded=True mic=granted '
                         'accessibility=missing tap=off')
        logged = [e[1] for e in r.log if e[0] == S.EFFECT_LOG]
        self.assertIn('setup: accessibility unknown -> missing', logged)

    def test_unknown_actions_and_effects_are_harmless(self):
        r = Rig(onboarded=True)
        r.poll()
        self.assertEqual(r.press('no_such_action'), [])
        self.assertEqual(r.press(None), [])
        self.assertEqual(r.press(S.ACT_NEXT), [])     # no window, nothing next... but "Done"


class OnePass(unittest.TestCase):
    """The rules added after the 1.5.0 field report."""

    def test_closing_a_finished_first_run_window_finishes_it(self):
        r = Rig(onboarded=False, mic=S.MIC_UNDETERMINED, trusted=False)
        r.poll()
        r.press(S.ACT_NEXT)
        r.mic = S.MIC_GRANTED
        r.poll(2.0)
        r.press(S.ACT_NEXT)
        r.trusted = True
        r.spin(3.0)
        self.assertTrue(r.flow.ready)
        self.assertFalse(r.flow.onboarded)       # "Start dictating" not pressed
        r.close_window()                         # the red button instead
        self.assertTrue(r.flow.onboarded)
        self.assertEqual(r.count(S.EFFECT_COMPLETE_ONBOARDING), 1)
        self.assertEqual(len(r.notices(S.NOTICE_READY)), 1)
        r.close_window()                         # AppKit may report it twice
        r.spin(10.0)
        self.assertEqual(r.count(S.EFFECT_COMPLETE_ONBOARDING), 1)
        self.assertEqual(len(r.notices(S.NOTICE_READY)), 1)
        self.assertEqual(r.flow.menu(), {'setup': False, 'restart': False})

    def test_closing_an_unfinished_first_run_window_does_not_finish_it(self):
        r = Rig(onboarded=False, mic=S.MIC_GRANTED, trusted=False)
        r.poll()
        r.close_window()
        self.assertFalse(r.flow.onboarded)
        self.assertEqual(r.count(S.EFFECT_COMPLETE_ONBOARDING), 0)

    def test_finish_then_the_close_callback_completes_only_once(self):
        r = Rig(onboarded=False)
        r.poll()
        r.spin(1.5)
        r.press(S.ACT_FINISH)                    # Rig closes -> window_closed()
        self.assertEqual(r.count(S.EFFECT_COMPLETE_ONBOARDING), 1)
        self.assertEqual(len(r.notices(S.NOTICE_READY)), 1)

    def test_accessibility_cannot_be_asked_underneath_the_microphone_prompt(self):
        r = Rig(onboarded=False, mic=S.MIC_UNDETERMINED, trusted=False)
        r.poll()
        r.press(S.ACT_GRANT_ACCESSIBILITY)       # a page that ignores "locked"
        r.press(S.ACT_OPEN_ACCESSIBILITY_SETTINGS)
        r.press(S.ACT_GRANT_MIC)
        r.press(S.ACT_GRANT_ACCESSIBILITY)
        self.assertEqual(r.count(S.EFFECT_PROMPT_ACCESSIBILITY), 0)
        self.assertEqual(r.count(S.EFFECT_OPEN_ACCESSIBILITY_SETTINGS), 0)
        self.assertEqual(r.count(S.EFFECT_REQUEST_MIC), 1)
        r.mic = S.MIC_GRANTED
        r.poll(1.0)
        r.press(S.ACT_GRANT_ACCESSIBILITY)
        self.assertEqual(r.count(S.EFFECT_PROMPT_ACCESSIBILITY), 1)

    def test_the_flow_owns_the_tap_when_dictation_is_switched_off_and_on(self):
        r = Rig(onboarded=True)
        r.poll()
        r.spin(2.0)
        self.assertTrue(r.tap_running)
        r.enabled = False                        # "Disable Dictation"
        r.poll(0.15)
        self.assertFalse(r.tap_running)
        self.assertEqual(r.count(S.EFFECT_STOP_TAP), 1)
        r.spin(5.0)
        self.assertEqual(r.count(S.EFFECT_STOP_TAP), 1)
        r.enabled = True
        r.poll(0.15)
        self.assertTrue(r.tap_running)
        self.assertEqual(r.count(S.EFFECT_START_TAP), 2)
        self.assertEqual(r.notices(), [])

    def test_enabling_dictation_without_accessibility_makes_no_tap(self):
        # 1.5.0 called hotkey.start() here regardless.
        r = Rig(onboarded=True, mic=S.MIC_GRANTED, trusted=False, enabled=False)
        r.poll()
        r.enabled = True
        r.spin(5.0)
        self.assertEqual(r.count(S.EFFECT_START_TAP), 0)

    def test_nothing_opens_by_itself_while_dictation_is_switched_off(self):
        r = Rig(onboarded=True, mic=S.MIC_GRANTED, trusted=False, enabled=False)
        r.poll()
        r.spin(10.0)
        self.assertFalse(r.window_open)
        self.assertEqual(r.count(S.EFFECT_OPEN_SETUP), 0)
        self.assertEqual(r.count(S.EFFECT_STATUS), 0)
        self.assertEqual(r.flow.menu()['setup'], True)         # still reachable

    def test_the_status_line_says_what_is_needed_and_then_ready(self):
        cases = ((S.MIC_GRANTED, False, 'Accessibility access is needed'),
                 (S.MIC_UNDETERMINED, True, 'Microphone access is needed'),
                 (S.MIC_UNDETERMINED, False, 'Setup is not finished'))
        for mic, trusted, text in cases:
            r = Rig(onboarded=True, mic=mic, trusted=trusted)
            r.poll()
            self.assertEqual([e for e in r.log if e[0] == S.EFFECT_STATUS],
                             [(S.EFFECT_STATUS, text)])
            r.mic, r.trusted = S.MIC_GRANTED, True
            r.spin(5.0)
            self.assertEqual([e for e in r.log if e[0] == S.EFFECT_STATUS][-1],
                             (S.EFFECT_STATUS, 'Ready'))
            self.assertEqual(len(r.notices(S.NOTICE_READY)), 1)

    def test_the_page_is_told_what_it_is_waiting_for(self):
        r = Rig(onboarded=False, mic=S.MIC_UNDETERMINED, trusted=False)
        r.poll()
        self.assertIsNone(r.flow.view()['waiting'])            # a button to press
        r.press(S.ACT_NEXT)
        self.assertIn('microphone', r.flow.view()['waiting'])
        r.mic = S.MIC_GRANTED
        r.poll(1.0)
        self.assertIsNone(r.flow.view()['waiting'])
        r.press(S.ACT_NEXT)                      # next is now "Open System Settings"
        self.assertIsNone(r.flow.view()['waiting'])
        r.trusted = True
        r.poll(0.5)                              # granted; the tap is 0.5 s old
        view = r.flow.view()
        self.assertEqual(view['title'], 'Almost there')
        self.assertIn('dictation keys', view['waiting'])
        r.spin(1.5)
        self.assertEqual(r.flow.view()['title'], 'You\u2019re all set')

    def test_a_refused_tap_is_not_announced_as_all_set(self):
        r = Rig(onboarded=True, mic=S.MIC_GRANTED, trusted=False)
        r.tap_works = False
        r.poll()
        r.trusted = True
        r.spin(S.TAP_RETRY_SEC * S.TAP_MAX_ATTEMPTS + 3.0)
        view = r.flow.view()
        self.assertTrue(view['restart_needed'])
        self.assertEqual(view['title'], 'One last step')
        self.assertIn('Restart', view['subtitle'])

    def test_busy_covers_launch_and_an_open_window_only(self):
        r = Rig(onboarded=True, mic=None, trusted=False)
        self.assertTrue(r.flow.busy)             # not decided yet
        r.poll()
        self.assertTrue(r.flow.busy)
        r.mic = S.MIC_GRANTED
        r.poll(0.2)
        self.assertTrue(r.window_open and r.flow.busy)
        r.close_window()
        self.assertFalse(r.flow.busy)            # missing, but out of the way
        r = Rig(onboarded=True)
        r.poll()
        self.assertFalse(r.flow.busy)

    def test_fourteen_impatient_clicks_still_make_one_prompt_and_no_restart(self):
        # What the system log shows for the real 1.5.0 session: the old app
        # asked macOS for the Accessibility dialog 14 times (7 launches + 7
        # clicks on "Grant Accessibility") and was relaunched 7 times before
        # the hotkeys worked. Same user, same clicks, this flow.
        r = Rig(onboarded=True, mic=S.MIC_GRANTED, trusted=False, version='1.5.0')
        r.poll()
        for _ in range(14):
            r.press(S.ACT_GRANT_ACCESSIBILITY)
            r.poll(3.0)
        self.assertEqual(r.count(S.EFFECT_PROMPT_ACCESSIBILITY), 1)
        self.assertEqual(r.count(S.EFFECT_OPEN_ACCESSIBILITY_SETTINGS), 13)
        self.assertEqual(r.count(S.EFFECT_START_TAP), 0)      # never a deaf tap
        self.assertEqual(r.rows()['accessibility']['hint'], S._TEXT['ax_stale'])
        r.trusted = True                         # off and on again in Settings
        r.poll(0.5)
        self.assertTrue(r.tap_running)           # in the same process
        r.spin(4.0)
        self.assertTrue(r.flow.ready)
        self.assertEqual(r.count(S.EFFECT_RESTART_APP), 0)
        self.assertEqual(r.flow.menu(), {'setup': False, 'restart': False})
        self.assertEqual(len(r.notices(S.NOTICE_READY)), 1)


class PageState(unittest.TestCase):
    """page_state(): what app._send_ob_state pushes to ui/onboarding.html."""

    LABELS = {'left_option': 'Left Option (⌥)',
              'right_option': 'Right Option (⌥)',
              'right_command': 'Right Command (⌘)'}

    def page(self, rig, **settings):
        stored = {'onboarded': rig.flow.onboarded}
        stored.update(settings)
        return S.page_state(rig.flow.view(), stored,
                            lambda key: self.LABELS[key], '1.5.1')

    def test_it_adds_the_keys_the_version_and_keeps_the_view(self):
        r = Rig(onboarded=False, mic=S.MIC_UNDETERMINED, trusted=False)
        r.poll()
        view = r.flow.view()
        page = self.page(r, ptt_key='right_command')
        for key, value in view.items():
            self.assertEqual(page[key], value, key)
        self.assertEqual(page['keys'], {'ptt': 'Right Command (⌘)',
                                        'toggle': 'Right Option (⌥)'})
        self.assertEqual(page['ptt_key'], 'right_command')
        self.assertEqual(page['ptt_label'], 'Right Command')
        self.assertEqual(page['app_version'], '1.5.1')
        json.dumps(page)                         # it crosses to JavaScript

    def test_the_page_may_say_hold_the_key_once_the_keys_are_live(self):
        r = Rig(onboarded=False, mic=S.MIC_GRANTED, trusted=False)
        r.poll()
        self.assertIs(self.page(r)['try_hotkey'], False)
        r.trusted = True
        r.spin(S.TAP_VERIFY_SEC + 1.0)
        self.assertTrue(r.flow.view()['hotkeys_live'])
        self.assertIs(self.page(r)['try_hotkey'], True)

    def test_consent_is_asked_on_first_run_only(self):
        r = Rig(onboarded=False, mic=S.MIC_UNDETERMINED, trusted=False)
        r.poll()
        page = self.page(r, analytics_enabled=False)
        self.assertEqual(page['consent'],
                         {'show': True, 'analytics': False, 'crash': True})
        # Repair (after an update) and review (from the menu): never.
        r = Rig(onboarded=True, mic=S.MIC_GRANTED, trusted=False)
        r.poll()
        self.assertEqual(r.flow.view()['mode'], S.MODE_REPAIR)
        self.assertFalse(self.page(r)['consent']['show'])
        r = Rig(onboarded=True)
        r.spin(2.0)
        r.run(r.flow.open_window())
        self.assertEqual(r.flow.view()['mode'], S.MODE_REVIEW)
        self.assertFalse(self.page(r)['consent']['show'])

    def test_a_refused_tap_is_explained_once_and_offers_no_button(self):
        r = Rig(onboarded=True, mic=S.MIC_GRANTED, trusted=False)
        r.tap_works = False
        r.poll()
        r.trusted = True
        r.spin(S.TAP_RETRY_SEC * S.TAP_MAX_ATTEMPTS + 3.0)
        r.run(r.flow.open_window())
        view = r.flow.view()
        page = self.page(r)
        row = [x for x in page['rows'] if x['id'] == 'accessibility'][0]
        self.assertEqual(row['state'], S.ST_RESTART)
        self.assertIsNone(row['hint'])
        self.assertIsNone(row['action'])
        self.assertEqual(page['subtitle'], S.RESTART_SUBTITLE)
        # The flow's own view is not changed by decorating a copy of it.
        flow_row = [x for x in view['rows'] if x['id'] == 'accessibility'][0]
        self.assertEqual(flow_row['action']['id'], S.ACT_RESTART)
        self.assertEqual(r.flow.view(), view)


if __name__ == '__main__':
    unittest.main()
