"""Key logic of the hotkey listener (no event tap, no Quartz needed)."""
import unittest

import support  # noqa: F401  (puts the repo root on sys.path)
import hotkey

L_OPT, R_OPT, L_SHIFT, L_CMD = 58, 61, 56, 55
OPT, SHIFT, CMD = 0x80000, 0x20000, 0x100000
# Device-dependent bits, as real keyboard events carry them.
L_OPT_BIT, R_OPT_BIT, L_SHIFT_BIT = 0x20, 0x40, 0x02
BASE = 0x100


class Listener(unittest.TestCase):
    def setUp(self):
        self.ev = []
        self.now = 100.0
        self.h = hotkey.HotkeyListener(
            on_ptt_start=lambda: self.ev.append('start'),
            on_ptt_stop=lambda: self.ev.append('stop'),
            on_ptt_cancel=lambda: self.ev.append('cancel'),
            on_toggle=lambda: self.ev.append('toggle'),
            ptt_keycode=L_OPT, ptt_mask=OPT,
            toggle_keycode=R_OPT, toggle_mask=OPT,
            on_escape=lambda: self.ev.append('esc'),
            on_activity=lambda: self.ev.append('act'),
            on_ptt_interrupt=lambda: self.ev.append('interrupt'),
            clock=lambda: self.now,
        )

    def press_ptt(self):
        self.h.handle_flags(L_OPT, BASE | OPT | L_OPT_BIT)

    def release_ptt(self, extra=0):
        self.h.handle_flags(L_OPT, BASE | extra)

    def test_hold_and_release_dictates(self):
        self.press_ptt()
        self.release_ptt()
        self.assertEqual(self.ev, ['start', 'stop'])

    def test_another_key_cancels_at_once_and_release_is_then_silent(self):
        self.press_ptt()
        self.h.handle_key_down(0)
        self.assertEqual(self.ev, ['start', 'cancel', 'act'])
        self.h.handle_key_down(1)               # still only one cancel
        self.release_ptt()
        self.assertEqual(self.ev.count('cancel'), 1)
        self.assertNotIn('stop', self.ev)

    def test_a_mouse_click_during_the_hold_cancels(self):
        self.press_ptt()
        self.h.handle_mouse_down()
        self.release_ptt()
        self.assertEqual(self.ev, ['start', 'cancel', 'act'])

    def test_a_click_well_into_the_hold_does_not_discard_the_dictation(self):
        # Talking for ten seconds, then clicking into the text field.
        self.press_ptt()
        self.now += 10.0
        self.h.handle_mouse_down()
        self.assertEqual(self.ev, ['start', 'interrupt', 'act'])
        self.assertTrue(self.h.ptt_held())
        self.h.handle_mouse_down()              # reported once per hold
        self.release_ptt()
        self.assertEqual(self.ev, ['start', 'interrupt', 'act', 'act', 'stop'])

    def test_a_key_or_modifier_well_into_the_hold_does_not_discard_either(self):
        self.press_ptt()
        self.now += hotkey.PTT_MODIFIER_WINDOW_SEC + 0.05
        self.h.handle_key_down(0)
        self.h.handle_flags(L_CMD, BASE | OPT | CMD | L_OPT_BIT | 0x08)
        self.release_ptt(CMD | 0x08)
        self.assertEqual(self.ev, ['start', 'interrupt', 'act', 'stop'])

    def test_the_modifier_window_is_per_hold(self):
        self.press_ptt()
        self.now += 5.0
        self.h.handle_mouse_down()
        self.release_ptt()
        self.ev[:] = []
        self.press_ptt()                        # a new hold: window restarts
        self.now += hotkey.PTT_MODIFIER_WINDOW_SEC - 0.05
        self.h.handle_mouse_down()
        self.release_ptt()
        self.assertEqual(self.ev, ['start', 'cancel', 'act'])

    def test_escape_late_in_a_hold_is_still_reported(self):
        self.press_ptt()
        self.now += 5.0
        self.h.handle_key_down(hotkey.ESCAPE_KEYCODE)
        self.assertEqual(self.ev, ['start', 'esc', 'interrupt', 'act'])

    def test_autorepeat_of_an_earlier_key_does_not_cancel(self):
        self.press_ptt()
        self.h.handle_key_down(0, autorepeat=True)
        self.release_ptt()
        self.assertEqual(self.ev, ['start', 'stop'])

    def test_releasing_another_modifier_does_not_cancel(self):
        # Shift is still down from the capital just typed; it comes up after
        # the dictation key went down.
        self.h.handle_flags(L_OPT, BASE | OPT | SHIFT | L_OPT_BIT | L_SHIFT_BIT)
        self.h.handle_flags(L_SHIFT, BASE | OPT | L_OPT_BIT)
        self.release_ptt()
        self.assertEqual(self.ev, ['start', 'stop'])

    def test_pressing_another_modifier_cancels(self):
        self.press_ptt()
        self.h.handle_flags(L_CMD, BASE | OPT | CMD | L_OPT_BIT | 0x08)
        self.release_ptt(CMD | 0x08)
        self.assertEqual(self.ev, ['start', 'cancel'])

    def test_caps_lock_is_ignored(self):
        self.press_ptt()
        self.h.handle_flags(57, BASE | OPT | 0x10000 | L_OPT_BIT)
        self.release_ptt(0x10000)
        self.assertEqual(self.ev, ['start', 'stop'])

    def test_toggle_tap(self):
        self.h.handle_flags(R_OPT, BASE | OPT | R_OPT_BIT)
        self.h.handle_flags(R_OPT, BASE)
        self.assertEqual(self.ev, ['toggle'])

    def test_toggle_used_as_a_modifier_is_not_a_tap(self):
        self.h.handle_flags(R_OPT, BASE | OPT | R_OPT_BIT)
        self.h.handle_key_down(4)
        self.h.handle_flags(R_OPT, BASE)
        self.assertEqual(self.ev, ['act'])

    def test_both_role_keys_sharing_a_modifier(self):
        # Left Option down, Right Option down, Left up, Right up: the Option
        # flag stays set until the very end; only the per-side bits tell.
        self.press_ptt()
        self.h.handle_flags(R_OPT, BASE | OPT | L_OPT_BIT | R_OPT_BIT)
        self.h.handle_flags(L_OPT, BASE | OPT | R_OPT_BIT)
        self.h.handle_flags(R_OPT, BASE)
        self.assertEqual(self.ev, ['start', 'cancel'])     # no toggle
        self.assertFalse(self.h.ptt_held())
        self.ev[:] = []
        self.press_ptt()                        # and nothing is left stuck
        self.release_ptt()
        self.assertEqual(self.ev, ['start', 'stop'])

    def test_same_sequence_without_per_side_bits(self):
        # A remapper's synthetic events carry only the device-independent flag.
        self.h.handle_flags(L_OPT, OPT)
        self.h.handle_flags(R_OPT, OPT)
        self.h.handle_flags(L_OPT, OPT)
        self.h.handle_flags(R_OPT, 0)
        self.assertEqual(self.ev, ['start', 'cancel'])
        self.assertFalse(self.h.ptt_held())
        self.ev[:] = []
        self.h.handle_flags(L_OPT, OPT)
        self.h.handle_flags(L_OPT, 0)
        self.assertEqual(self.ev, ['start', 'stop'])

    def test_a_repeated_press_event_does_not_restart(self):
        self.press_ptt()
        self.press_ptt()
        self.release_ptt()
        self.assertEqual(self.ev, ['start', 'stop'])

    def test_escape_is_reported(self):
        self.h.handle_key_down(hotkey.ESCAPE_KEYCODE)
        self.assertEqual(self.ev, ['esc', 'act'])

    def test_reconcile_ends_a_hold_whose_key_up_was_lost(self):
        self.press_ptt()
        self.h.reconcile(BASE)                  # nothing is physically down
        self.assertEqual(self.ev, ['start', 'stop'])
        self.assertFalse(self.h.ptt_held())
        self.h.reconcile(BASE)                  # idempotent
        self.assertEqual(self.ev, ['start', 'stop'])

    def test_reconcile_keeps_a_hold_that_is_still_down(self):
        self.press_ptt()
        self.h.reconcile(BASE | OPT | L_OPT_BIT)
        self.assertEqual(self.ev, ['start'])
        self.assertTrue(self.h.ptt_held())

    def test_reconcile_after_a_cancel_is_silent(self):
        self.press_ptt()
        self.h.handle_key_down(0)
        self.h.reconcile(BASE)
        self.assertEqual(self.ev, ['start', 'cancel', 'act'])

    def test_rebinding_resets_a_hold(self):
        self.press_ptt()
        self.h.set_bindings(R_OPT, OPT, L_OPT, OPT)
        self.assertFalse(self.h.ptt_held())

    def test_a_raising_callback_never_escapes(self):
        def boom():
            raise RuntimeError('boom')
        self.h._on_ptt_start = boom
        self.press_ptt()                        # must not raise
        self.assertTrue(self.h.ptt_held())

    def test_modifier_flags_reads_the_hardware_state(self):
        # The combined session state also reflects synthetic events (our own
        # Cmd+V says "Option is up"), which ended held recordings. Only the
        # HID state says what is physically down.
        asked = []

        class FakeQuartz(object):
            kCGEventSourceStateHIDSystemState = 'hid'
            kCGEventSourceStateCombinedSessionState = 'combined'

            @staticmethod
            def CGEventSourceFlagsState(state):
                asked.append(state)
                return OPT | L_OPT_BIT

        real = hotkey.Quartz
        hotkey.Quartz = FakeQuartz
        try:
            self.assertEqual(hotkey.HotkeyListener.modifier_flags(),
                             OPT | L_OPT_BIT)
        finally:
            hotkey.Quartz = real
        self.assertEqual(asked, ['hid'])

    def test_a_key_only_counts_as_released_when_hardware_and_session_agree(self):
        # After the tap was switched off and on again: our own Cmd+V may have
        # wiped Option from the session state while the key is still held.
        states = {'hid': OPT | L_OPT_BIT, 'combined': CMD}

        class FakeQuartz(object):
            kCGEventSourceStateHIDSystemState = 'hid'
            kCGEventSourceStateCombinedSessionState = 'combined'

            @staticmethod
            def CGEventSourceFlagsState(state):
                return states[state]

        real = hotkey.Quartz
        hotkey.Quartz = FakeQuartz
        try:
            self.press_ptt()
            self.h.reconcile(hotkey.HotkeyListener.held_or_posted_flags())
            self.assertEqual(self.ev, ['start'])        # still held
            # A remapper holding the key down with posted events only.
            states.update(hid=0, combined=OPT)
            self.h.reconcile(hotkey.HotkeyListener.held_or_posted_flags())
            self.assertEqual(self.ev, ['start'])
            states.update(hid=0, combined=CMD)          # up in both
            self.h.reconcile(hotkey.HotkeyListener.held_or_posted_flags())
            self.assertEqual(self.ev, ['start', 'stop'])
        finally:
            hotkey.Quartz = real

    def test_synthetic_tag_matches_the_injector(self):
        from engine import injector
        self.assertEqual(hotkey._SYNTHETIC_EVENT_TAG, injector.SYNTHETIC_EVENT_TAG)


if __name__ == '__main__':
    unittest.main()
