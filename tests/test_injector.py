"""Injector tests.

The paste logic runs against an in-memory fake pasteboard, so nothing here
needs Accessibility, posts a keystroke, or touches the user's clipboard.

The ``RealPasteboard`` cases exercise the actual NSPasteboard code on a
private, uniquely named pasteboard (never the general one). They are skipped
automatically where PyObjC is not installed.
"""
import threading
import time
import unittest

import support  # noqa: F401  (puts the repo root on sys.path)

from engine import injector
from engine.injector import (COPIED_ONLY, FAILED, PASTED, InjectResult,
                             Injector, PasteContinuity, needs_leading_space,
                             smart_spacing)

try:
    import AppKit  # noqa: F401
    import Quartz  # noqa: F401
    HAVE_PYOBJC = True
except Exception:
    HAVE_PYOBJC = False


class FakePasteboard(object):
    """Same surface as NSPasteboardBackend, backed by a list.

    Contents are a list of items; an item is a list of (type, bytes)."""

    TEXT = 'public.utf8-plain-text'

    def __init__(self, items=None):
        self.items = [list(i) for i in (items or [])]
        self.count = 10
        self.transient = False
        self.fail_writes = 0          # refuse this many write_text calls
        self.corrupt_writes = 0       # accept, but store something else
        self.unreadable = False       # snapshot() -> None ("too large")
        self.raise_on = None          # method name that raises
        self.log = []

    def _maybe_raise(self, name):
        if self.raise_on == name:
            raise RuntimeError('pasteboard exploded in %s' % name)

    def change_count(self):
        self._maybe_raise('change_count')
        return self.count

    def snapshot(self, max_bytes=None):
        self._maybe_raise('snapshot')
        self.log.append('snapshot')
        if self.unreadable:
            return None
        return [list(i) for i in self.items]

    def write_text(self, text, transient):
        self._maybe_raise('write_text')
        self.log.append(('write', text, transient))
        if self.fail_writes:
            self.fail_writes -= 1
            self.items = []           # a refused write still cleared it
            self.count += 1
            return None
        stored = text
        if self.corrupt_writes:
            self.corrupt_writes -= 1
            stored = text + '?'
        self.items = [[(self.TEXT, stored.encode('utf-8'))]]
        self.transient = transient
        self.count += 1
        return self.count

    def read_text(self):
        self._maybe_raise('read_text')
        for item in self.items:
            for ptype, data in item:
                if ptype == self.TEXT:
                    return data.decode('utf-8')
        return None

    def restore(self, snapshot):
        self._maybe_raise('restore')
        self.log.append('restore')
        self.items = [list(i) for i in snapshot]
        self.transient = False
        self.count += 1
        return True

    # what "the user" does
    def user_copies(self, text):
        self.items = [[(self.TEXT, text.encode('utf-8'))]]
        self.transient = False
        self.count += 1


RICH = [
    [('public.utf8-plain-text', b'old text'), ('public.rtf', b'{\\rtf1 old}')],
    [('public.png', b'\x89PNG\r\n\x1a\n' + b'\x00' * 64)],
]


class Rig(object):
    def __init__(self, items=RICH, trusted=True, paste_ok=True, **kw):
        self.pb = FakePasteboard(items)
        self.trusted = trusted
        self.paste_ok = paste_ok
        self.pastes = []              # clipboard text at each Cmd+V
        self.paste_was_transient = []
        self.sleeps = []
        self.mods = 0                 # modifier reads that return "held"
        kw.setdefault('defer_restore', False)
        self.inj = Injector(pasteboard=self.pb, post_paste=self._post,
                            is_trusted=lambda: self.trusted,
                            modifiers_down=self._mods, sleep=self.sleeps.append,
                            **kw)

    def _post(self):
        self.pastes.append(self.pb.read_text())
        self.paste_was_transient.append(self.pb.transient)
        return self.paste_ok

    def _mods(self):
        if self.mods > 0:
            self.mods -= 1
            return True
        return False


class InjectCase(unittest.TestCase):
    def test_pastes_then_restores_everything_including_non_text(self):
        r = Rig()
        res = r.inj.inject('dictated words')
        self.assertEqual(res.status, PASTED)
        self.assertTrue(res.pasted)
        self.assertTrue(res.will_restore)
        self.assertEqual(r.pastes, ['dictated words'],
                         'Cmd+V must fire while OUR text is on the clipboard')
        self.assertEqual(r.paste_was_transient, [True],
                         'temporary write must carry the transient markers')
        self.assertEqual(r.pb.items, RICH,
                         'every item and type must come back, not just text')

    def test_restore_waits_after_the_paste_not_before(self):
        r = Rig(restore_delay=0.6)
        r.inj.inject('x')
        order = [e if isinstance(e, str) else e[0] for e in r.pb.log]
        self.assertEqual(order, ['snapshot', 'write', 'restore'])
        self.assertIn(0.6, r.sleeps)
        self.assertEqual(r.sleeps[0], injector.PRE_PASTE_SETTLE)

    def test_empty_clipboard_is_restored_to_empty(self):
        r = Rig(items=[])
        res = r.inj.inject('x')
        self.assertTrue(res.will_restore)
        self.assertEqual(r.pb.items, [])

    def test_does_not_clobber_something_the_user_copied_meanwhile(self):
        r = Rig(defer_restore=True, restore_delay=30.0)
        res = r.inj.inject('dictated')
        self.assertTrue(res.will_restore)
        r.pb.user_copies('user copied this after the paste')
        r.inj.flush()                           # run the restore now
        self.assertEqual(r.pb.read_text(), 'user copied this after the paste')
        self.assertNotIn('restore', r.pb.log)

    def test_no_accessibility_leaves_the_text_as_a_real_copy(self):
        r = Rig(trusted=False)
        res = r.inj.inject('dictated')
        self.assertEqual(res.status, COPIED_ONLY)
        self.assertEqual(res.reason, 'accessibility')
        self.assertTrue(res.copied_only)
        self.assertFalse(res.will_restore)
        self.assertEqual(r.pastes, [], 'must not paste into the void')
        self.assertEqual(r.pb.read_text(), 'dictated')
        self.assertFalse(r.pb.transient,
                         'a copy the user will paste must not be marked temporary')
        self.assertNotIn('restore', r.pb.log)

    def test_failed_keystroke_leaves_the_text_as_a_real_copy(self):
        r = Rig(paste_ok=False)
        res = r.inj.inject('dictated')
        self.assertEqual((res.status, res.reason), (COPIED_ONLY, 'paste_failed'))
        self.assertEqual(r.pb.read_text(), 'dictated')
        self.assertFalse(r.pb.transient)
        self.assertNotIn('restore', r.pb.log)

    def test_clipboard_write_failure_is_reported_and_old_content_put_back(self):
        r = Rig()
        r.pb.fail_writes = 99
        res = r.inj.inject('dictated')
        self.assertEqual((res.status, res.reason),
                         (FAILED, 'clipboard_write_failed'))
        self.assertTrue(res.failed)
        self.assertEqual(r.pastes, [], 'never paste a clipboard we did not set')
        self.assertEqual(r.pb.items, RICH)

    def test_a_single_failed_write_is_retried(self):
        r = Rig()
        r.pb.fail_writes = 1
        self.assertEqual(r.inj.inject('dictated').status, PASTED)
        self.assertEqual(r.pastes, ['dictated'])

    def test_write_is_verified_by_reading_back(self):
        r = Rig()
        r.pb.corrupt_writes = 99                # write "succeeds", wrong text
        res = r.inj.inject('dictated')
        self.assertEqual(res.status, FAILED)
        self.assertEqual(r.pastes, [])

    def test_empty_text_is_a_no_op(self):
        r = Rig()
        for text in ('', None):
            res = r.inj.inject(text)
            self.assertEqual((res.status, res.reason), (FAILED, 'empty'))
        self.assertEqual(r.pb.log, [])
        self.assertEqual(r.pb.items, RICH)

    def test_backend_exceptions_never_escape(self):
        for where in ('write_text', 'read_text'):
            r = Rig()
            r.pb.raise_on = where
            res = r.inj.inject('dictated')      # must not raise
            self.assertEqual(res.status, FAILED, where)
            self.assertEqual(r.pastes, [], where)
        r = Rig()
        r.pb.raise_on = 'change_count'          # only the restore needs it
        res = r.inj.inject('dictated')
        self.assertEqual(res.status, PASTED)
        self.assertEqual(r.pastes, ['dictated'])
        r = Rig()
        r.pb.raise_on = 'snapshot'              # cannot read old clipboard
        res = r.inj.inject('dictated')
        self.assertEqual(res.status, PASTED)    # still paste...
        self.assertFalse(res.will_restore)      # ...but admit no restore

    def test_unsnapshottable_clipboard_still_pastes_without_restore(self):
        r = Rig()
        r.pb.unreadable = True                  # e.g. larger than the cap
        res = r.inj.inject('dictated')
        self.assertEqual(res.status, PASTED)
        self.assertFalse(res.will_restore)
        self.assertEqual(r.pb.read_text(), 'dictated')
        self.assertFalse(r.pb.transient, 'it stays, so it is a real copy')

    def test_a_slow_clipboard_owner_cannot_hold_the_paste_back(self):
        # The app that owns the clipboard takes its time rendering what it
        # promised (or is hung). The dictation is pasted regardless.
        r = Rig(snapshot_timeout=0.05)
        release = threading.Event()
        real_snapshot = r.pb.snapshot

        def slow_snapshot(max_bytes=None):
            release.wait(5.0)
            return real_snapshot()
        r.pb.snapshot = slow_snapshot
        t0 = time.monotonic()
        res = r.inj.inject('dictated')
        took = time.monotonic() - t0
        release.set()
        self.assertEqual(res.status, PASTED)
        self.assertFalse(res.will_restore)      # no snapshot, so no restore
        self.assertEqual(r.pastes, ['dictated'])
        self.assertFalse(r.pb.transient, 'it stays, so it is a real copy')
        self.assertLess(took, 1.0)

    def test_restore_can_be_declined(self):
        r = Rig()
        res = r.inj.inject('dictated', restore_clipboard=False)
        self.assertEqual(res.status, PASTED)
        self.assertFalse(res.will_restore)
        self.assertEqual(r.pb.read_text(), 'dictated')
        self.assertNotIn('snapshot', r.pb.log)

    def test_two_quick_dictations_restore_the_users_clipboard_not_the_first(self):
        r = Rig(defer_restore=True, restore_delay=30.0)
        r.inj.inject('first')
        r.inj.inject('second')                  # first restore has not fired
        self.assertEqual(r.pastes, ['first', 'second'])
        r.inj.flush()
        self.assertEqual(r.pb.items, RICH,
                         'restored the first dictation instead of the '
                         "user's clipboard")

    def test_user_copy_between_two_dictations_becomes_the_new_baseline(self):
        r = Rig(defer_restore=True, restore_delay=30.0)
        r.inj.inject('first')
        r.pb.user_copies('fresh copy')
        r.inj.inject('second')
        r.inj.flush()
        self.assertEqual(r.pb.read_text(), 'fresh copy')

    def test_deferred_restore_fires_by_itself(self):
        r = Rig(defer_restore=True, restore_delay=0.05)
        t0 = time.monotonic()
        res = r.inj.inject('dictated')
        self.assertLess(time.monotonic() - t0, 0.04,
                        'inject() must not wait for the restore')
        self.assertTrue(res.will_restore)
        self.assertEqual(r.pb.read_text(), 'dictated')
        end = time.monotonic() + 2.0
        while time.monotonic() < end and r.pb.items != RICH:
            time.sleep(0.01)
        self.assertEqual(r.pb.items, RICH)

    def test_flush_without_pending_is_harmless(self):
        r = Rig()
        r.inj.flush()
        self.assertEqual(r.pb.log, [])

    def test_waits_for_a_held_modifier_but_only_briefly(self):
        r = Rig()
        r.mods = 3                              # released after 3 polls
        r.inj.inject('x')
        self.assertEqual(r.sleeps.count(injector.MODIFIER_POLL), 3)
        r = Rig()
        r.mods = 10 ** 6                        # never released
        self.assertEqual(r.inj.inject('x').status, PASTED)
        waited = r.sleeps.count(injector.MODIFIER_POLL) * injector.MODIFIER_POLL
        self.assertLessEqual(waited, injector.MODIFIER_RELEASE_WAIT + 1e-9)
        self.assertGreater(waited, 0)

    def test_total_blocking_time_is_small(self):
        r = Rig(defer_restore=True, restore_delay=30.0)
        r.inj.inject('x')
        self.assertLessEqual(sum(r.sleeps), 0.05,
                             'inject() should cost a few tens of ms at most')
        r.inj.flush()

    def test_copy_is_a_plain_copy_and_cancels_a_pending_restore(self):
        r = Rig(defer_restore=True, restore_delay=30.0)
        r.inj.inject('dictated')
        res = r.inj.copy('keep me')
        self.assertEqual(res.status, COPIED_ONLY)
        r.inj.flush()
        self.assertEqual(r.pb.read_text(), 'keep me')
        self.assertFalse(r.pb.transient)
        self.assertEqual(r.inj.copy('').status, FAILED)

    def test_concurrent_injections_are_serialised(self):
        r = Rig(defer_restore=True, restore_delay=30.0)
        results = []

        def go(i):
            results.append(r.inj.inject('text %d' % i))
        threads = [threading.Thread(target=go, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual([x.status for x in results], [PASTED] * 8)
        self.assertEqual(sorted(r.pastes), sorted('text %d' % i for i in range(8)))
        r.inj.flush()
        self.assertEqual(r.pb.items, RICH)

    def test_module_level_inject_keeps_working(self):
        r = Rig()
        saved = injector._default
        injector._default = r.inj
        try:
            res = injector.inject('via the old entry point')
            self.assertEqual(res, PASTED)
            self.assertEqual(r.pastes, ['via the old entry point'])
            self.assertEqual(injector.copy('c').status, COPIED_ONLY)
            injector.flush()
        finally:
            injector._default = saved


class ResultCase(unittest.TestCase):
    def test_result_compares_with_status_strings(self):
        res = InjectResult(COPIED_ONLY, 'accessibility')
        self.assertEqual(res, COPIED_ONLY)
        self.assertEqual(res, 'copied_only')
        self.assertNotEqual(res, PASTED)
        self.assertEqual(res, InjectResult(COPIED_ONLY, 'accessibility'))
        self.assertNotEqual(res, InjectResult(COPIED_ONLY, 'paste_failed'))
        self.assertTrue(res.copied_only and not res.pasted and not res.failed)
        self.assertIn('copied_only', repr(res))

    def test_importable_without_pyobjc(self):
        # Nothing at module level may import AppKit/Quartz.
        import ast
        with open(injector.__file__, encoding='utf-8') as f:
            tree = ast.parse(f.read())
        top = []
        for node in tree.body:
            if isinstance(node, ast.Import):
                top += [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                top.append(node.module)
        for name in top:
            self.assertNotIn(name.split('.')[0],
                             ('AppKit', 'Quartz', 'objc', 'Foundation',
                              'ApplicationServices', 'pyperclip'))


class SpacingCase(unittest.TestCase):
    def test_strips_recogniser_whitespace_and_adds_nothing(self):
        self.assertEqual(smart_spacing('  Hello there. \n'), 'Hello there.')
        self.assertEqual(smart_spacing('Hello'), 'Hello')
        self.assertEqual(smart_spacing(''), '')
        self.assertEqual(smart_spacing(None), '')
        self.assertEqual(smart_spacing('   '), '')

    def test_never_adds_a_trailing_space(self):
        for prev in (None, '', 'Earlier sentence.', 'word'):
            for text in ('Hello.', 'hello', 'ok?', 'a b'):
                self.assertFalse(smart_spacing(text, prev).endswith(' '))

    def test_inner_whitespace_is_untouched(self):
        self.assertEqual(smart_spacing(' line one\nline  two '),
                         'line one\nline  two')

    def test_leading_space_only_with_a_known_previous_paste(self):
        self.assertEqual(smart_spacing('next bit'), 'next bit')
        self.assertEqual(smart_spacing('next bit', ''), 'next bit')
        self.assertEqual(smart_spacing('Next one.', 'First one.'), ' Next one.')
        self.assertEqual(smart_spacing('and more', 'some words'), ' and more')
        self.assertEqual(smart_spacing('more', 'wait,'), ' more')
        self.assertEqual(smart_spacing('more', '(aside)'), ' more')
        self.assertEqual(smart_spacing('more', 'he said "hi"'), ' more')
        self.assertEqual(smart_spacing('2 items', 'take 1'), ' 2 items')

    def test_no_leading_space_when_the_join_does_not_want_one(self):
        self.assertEqual(smart_spacing('more', 'ends with space '), 'more')
        self.assertEqual(smart_spacing('more', 'new line\n'), 'more')
        self.assertEqual(smart_spacing('.', 'sentence'), '.')
        self.assertEqual(smart_spacing(', and then', 'first'), ', and then')
        self.assertEqual(smart_spacing('? really', 'what'), '? really')
        self.assertEqual(smart_spacing(') done', 'aside'), ') done')
        self.assertEqual(smart_spacing('path', 'cd /usr/'), 'path')
        self.assertEqual(smart_spacing('name', 'hello@'), 'name')
        self.assertEqual(smart_spacing('inside', 'call('), 'inside')
        self.assertEqual(smart_spacing('tag', '#'), 'tag')
        self.assertEqual(smart_spacing('part', 'well-'), 'part')

    def test_devanagari(self):
        # A word ending in a vowel sign (a combining mark) still needs a space.
        self.assertEqual(smart_spacing('कैसे हो', 'नमस्ते'), ' कैसे हो')
        self.assertEqual(smart_spacing('फिर', 'ठीक है।'), ' फिर')
        self.assertEqual(smart_spacing('।', 'ठीक है'), '।')

    def test_scripts_without_spaces(self):
        self.assertEqual(smart_spacing('世界', '你好'), '世界')
        self.assertEqual(smart_spacing('世界', '你好。'), '世界')
        self.assertEqual(smart_spacing('です', 'これ'), 'です')
        self.assertEqual(smart_spacing('hello', '你好'), 'hello')

    def test_needs_leading_space_edges(self):
        self.assertFalse(needs_leading_space('', 'x'))
        self.assertFalse(needs_leading_space('x', ''))
        self.assertFalse(needs_leading_space('x', None))
        self.assertFalse(needs_leading_space(' x', 'y'))
        self.assertTrue(needs_leading_space('x', 'y'))


class ContinuityCase(unittest.TestCase):
    def setUp(self):
        self.now = [100.0]
        self.pc = PasteContinuity(max_age=20.0, clock=lambda: self.now[0])

    def test_remembers_only_for_the_same_app_and_a_short_while(self):
        self.assertIsNone(self.pc.previous('com.app.a'))
        self.pc.note('Hello there.', 'com.app.a')
        self.assertEqual(self.pc.previous('com.app.a'), 'Hello there.')
        self.assertIsNone(self.pc.previous('com.app.b'))
        self.assertEqual(self.pc.previous('com.app.a'), 'Hello there.')
        self.now[0] += 21.0
        self.assertIsNone(self.pc.previous('com.app.a'))
        self.now[0] -= 21.0
        self.assertIsNone(self.pc.previous('com.app.a'), 'expiry is final')

    def test_invalidate_forgets(self):
        self.pc.note('Hello', 'a')
        self.pc.invalidate()
        self.assertIsNone(self.pc.previous('a'))
        self.pc.note('', 'a')
        self.assertIsNone(self.pc.previous('a'))

    def test_joins_two_dictations(self):
        first = smart_spacing('First sentence.', self.pc.previous('a'))
        self.pc.note(first, 'a')
        second = smart_spacing('Second one.', self.pc.previous('a'))
        self.assertEqual(first + second, 'First sentence. Second one.')


@unittest.skipUnless(HAVE_PYOBJC, 'PyObjC (AppKit/Quartz) is not installed '
                     'for this interpreter')
class RealPasteboard(unittest.TestCase):
    """The real NSPasteboard code, on a private pasteboard. The general
    (user) clipboard is never read or written."""

    def setUp(self):
        from AppKit import NSPasteboard
        self.board = NSPasteboard.pasteboardWithUniqueName()
        self.pb = injector.NSPasteboardBackend(self.board)

    def tearDown(self):
        self.board.releaseGlobally()

    def _put_rich(self):
        from AppKit import NSPasteboardItem
        from Foundation import NSData
        png = b'\x89PNG\r\n\x1a\n' + bytes(range(256)) * 8
        rtf = b'{\\rtf1\\ansi old \\b rich\\b0  text}'
        one = NSPasteboardItem.alloc().init()
        one.setString_forType_('old text', 'public.utf8-plain-text')
        one.setData_forType_(NSData.dataWithBytes_length_(rtf, len(rtf)),
                             'public.rtf')
        two = NSPasteboardItem.alloc().init()
        two.setData_forType_(NSData.dataWithBytes_length_(png, len(png)),
                             'public.png')
        self.board.clearContents()
        self.assertTrue(self.board.writeObjects_([one, two]))
        return {'public.utf8-plain-text': b'old text', 'public.rtf': rtf,
                'public.png': png}

    def _contents(self):
        out = []
        for item in self.board.pasteboardItems() or ():
            entry = {}
            for ptype in item.types():
                data = item.dataForType_(ptype)
                entry[str(ptype)] = bytes(data) if data is not None else None
            out.append(entry)
        return out

    def test_write_marks_the_text_as_transient(self):
        count = self.pb.write_text('नमस्ते hello \U0001f600', True)
        self.assertEqual(count, self.pb.change_count())
        self.assertEqual(self.pb.read_text(), 'नमस्ते hello \U0001f600')
        types = [str(t) for t in self.board.types()]
        for marker in injector.TRANSIENT_TYPES:
            self.assertIn(marker, types)
        self.assertIn('org.nspasteboard.TransientType', types)
        self.assertIn('org.nspasteboard.ConcealedType', types)

    def test_plain_copy_has_no_markers(self):
        self.pb.write_text('plain', False)
        types = [str(t) for t in self.board.types()]
        for marker in injector.TRANSIENT_TYPES:
            self.assertNotIn(marker, types)

    def test_snapshot_and_restore_round_trip_non_text_content(self):
        expected = self._put_rich()
        before = self._contents()
        self.assertEqual(len(before), 2)
        snap = self.pb.snapshot()
        self.assertEqual(len(snap), 2)
        self.pb.write_text('dictated', True)
        self.assertEqual(self.pb.read_text(), 'dictated')
        self.assertTrue(self.pb.restore(snap))
        after = self._contents()
        self.assertEqual(after, before)
        self.assertEqual(after[0]['public.utf8-plain-text'],
                         expected['public.utf8-plain-text'])
        self.assertEqual(after[0]['public.rtf'], expected['public.rtf'])
        self.assertEqual(after[1]['public.png'], expected['public.png'])
        self.assertEqual(self.pb.read_text(), 'old text')

    def test_change_count_moves_on_every_write(self):
        c0 = self.pb.change_count()
        c1 = self.pb.write_text('a', True)
        c2 = self.pb.write_text('b', True)
        self.assertTrue(c0 < c1 < c2)
        self.pb.restore([])
        self.assertGreater(self.pb.change_count(), c2)

    def test_empty_snapshot_restores_to_empty(self):
        self.assertEqual(self.pb.snapshot(), [])
        self.pb.write_text('dictated', True)
        self.assertTrue(self.pb.restore([]))
        self.assertIsNone(self.pb.read_text())
        self.assertEqual(self._contents(), [])

    def test_snapshot_over_the_size_cap_is_refused(self):
        self._put_rich()
        self.assertIsNone(self.pb.snapshot(max_bytes=100))

    def test_injector_end_to_end_on_a_real_pasteboard(self):
        self._put_rich()
        before = self._contents()
        seen = []

        def fake_paste():
            seen.append((self.pb.read_text(),
                         [str(t) for t in self.board.types()]))
            return True
        inj = Injector(pasteboard=self.pb, post_paste=fake_paste,
                       is_trusted=lambda: True, modifiers_down=lambda: False,
                       restore_delay=0.05)
        res = inj.inject('dictated text')
        self.assertEqual(res.status, PASTED)
        self.assertEqual(seen[0][0], 'dictated text')
        self.assertIn('org.nspasteboard.TransientType', seen[0][1])
        end = time.monotonic() + 2.0
        while time.monotonic() < end and self._contents() != before:
            time.sleep(0.01)
        self.assertEqual(self._contents(), before)

    def test_restore_is_skipped_when_someone_else_wrote(self):
        self._put_rich()
        inj = Injector(pasteboard=self.pb, post_paste=lambda: True,
                       is_trusted=lambda: True, modifiers_down=lambda: False,
                       restore_delay=30.0)
        inj.inject('dictated text')
        self.pb.write_text('user copied this', False)   # "someone else"
        inj.flush()
        self.assertEqual(self.pb.read_text(), 'user copied this')

    def test_paste_events_are_cmd_v_and_tagged(self):
        # Built, inspected, never posted.
        import Quartz
        down, up = injector.make_paste_events()
        for event, is_down in ((down, True), (up, False)):
            self.assertEqual(Quartz.CGEventGetIntegerValueField(
                event, Quartz.kCGKeyboardEventKeycode), injector.PASTE_KEYCODE)
            flags = Quartz.CGEventGetFlags(event)
            self.assertTrue(flags & Quartz.kCGEventFlagMaskCommand)
            for other in (Quartz.kCGEventFlagMaskShift,
                          Quartz.kCGEventFlagMaskAlternate,
                          Quartz.kCGEventFlagMaskControl):
                self.assertFalse(flags & other)
            self.assertEqual(Quartz.CGEventGetType(event),
                             Quartz.kCGEventKeyDown if is_down
                             else Quartz.kCGEventKeyUp)
            self.assertTrue(injector.is_synthetic(event))
        plain = Quartz.CGEventCreateKeyboardEvent(None, 9, True)
        self.assertFalse(injector.is_synthetic(plain))

    def test_paste_events_use_the_key_that_types_v_on_this_layout(self):
        import Quartz
        saved = injector._paste_keycode
        injector._paste_keycode = 47            # where Dvorak has "v"
        try:
            down, up = injector.make_paste_events()
        finally:
            injector._paste_keycode = saved
        for event in (down, up):
            self.assertEqual(Quartz.CGEventGetIntegerValueField(
                event, Quartz.kCGKeyboardEventKeycode), 47)
            self.assertTrue(Quartz.CGEventGetFlags(event)
                            & Quartz.kCGEventFlagMaskCommand)

    def _layout(self, name):
        """A TISInputSourceRef for an installed (not necessarily enabled)
        system keyboard layout, or None. Read-only: nothing is selected."""
        import ctypes
        import objc
        from Foundation import NSDictionary
        libs = injector._carbon()
        if libs is None:
            return None
        carbon, cf = libs
        carbon.TISCreateInputSourceList.restype = ctypes.c_void_p
        carbon.TISCreateInputSourceList.argtypes = [ctypes.c_void_p, ctypes.c_bool]
        cf.CFArrayGetCount.restype = ctypes.c_long
        cf.CFArrayGetCount.argtypes = [ctypes.c_void_p]
        cf.CFArrayGetValueAtIndex.restype = ctypes.c_void_p
        cf.CFArrayGetValueAtIndex.argtypes = [ctypes.c_void_p, ctypes.c_long]
        query = NSDictionary.dictionaryWithObject_forKey_(
            'com.apple.keylayout.' + name, 'TISPropertyInputSourceID')
        found = carbon.TISCreateInputSourceList(objc.pyobjc_id(query), True)
        if not found or cf.CFArrayGetCount(found) == 0:
            return None
        # (the array is deliberately not released: it keeps the source alive)
        return cf.CFArrayGetValueAtIndex(found, 0)

    def test_the_v_key_is_found_from_the_keyboard_layout(self):
        from Foundation import NSThread
        if not NSThread.isMainThread():
            self.skipTest('keyboard layouts can only be read on the main thread')
        expected = {
            'US': 9,
            'Dvorak': 47,               # keycode 9 types "k" there: Cmd+K
            'DVORAK-QWERTYCMD': 9,      # QWERTY while Cmd is held
            'Turkish': 8,               # Turkish F: keycode 9 is Cmd+C
            'Russian': 9,               # non-Latin: Cmd falls back to QWERTY
        }
        checked = 0
        for name, keycode in expected.items():
            source = self._layout(name)
            if source is None:
                continue
            checked += 1
            self.assertEqual(injector.keycode_in_layout(source), keycode, name)
        if not checked:
            self.skipTest('no system keyboard layouts could be read')

    def test_refresh_resolves_the_current_layout(self):
        from Foundation import NSThread
        if not NSThread.isMainThread():
            self.skipTest('main thread only')
        saved = injector._paste_keycode
        try:
            keycode = injector.refresh_paste_keycode()
            self.assertEqual(keycode, injector.paste_keycode())
            self.assertTrue(0 <= keycode <= 50)
            # Off the main thread it must not touch TIS (macOS would kill
            # the process): the remembered value is returned untouched.
            injector._paste_keycode = 33
            got = []
            t = threading.Thread(
                target=lambda: got.append(injector.refresh_paste_keycode()))
            t.start()
            t.join(5.0)
            self.assertEqual(got, [33])
        finally:
            injector._paste_keycode = saved

    def test_read_only_probes_do_not_raise(self):
        self.assertIn(injector.accessibility_trusted(), (True, False))
        self.assertIn(injector.modifiers_down(), (True, False))
        front = injector.frontmost_app_id()
        self.assertTrue(front is None or isinstance(front, str))
        self.assertEqual(injector._MODIFIER_MASK,
                         Quartz.kCGEventFlagMaskShift
                         | Quartz.kCGEventFlagMaskControl
                         | Quartz.kCGEventFlagMaskAlternate
                         | Quartz.kCGEventFlagMaskCommand)


if __name__ == '__main__':
    unittest.main()
