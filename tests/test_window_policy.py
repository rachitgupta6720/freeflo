"""ui/window.py keeps the Dock icon while any freeflo window is still open:
closing the setup window must not strand the main window (and the reverse).
Pure bookkeeping, checked with stand-in windows (nothing on screen)."""
import unittest

import support  # noqa: F401  (puts the repo on sys.path)

try:
    from ui import window as W
    HAVE_WINDOW = True
except Exception:                                   # pragma: no cover
    HAVE_WINDOW = False


class FakeWindow(object):
    def __init__(self, visible=True):
        self.visible = visible
        self.fronted = 0

    def isVisible(self):
        return self.visible

    def makeKeyAndOrderFront_(self, sender):
        self.fronted += 1


@unittest.skipUnless(HAVE_WINDOW, 'needs PyObjC')
class WindowPolicy(unittest.TestCase):

    def setUp(self):
        self._saved = list(W._PRESENTED)
        del W._PRESENTED[:]

    def tearDown(self):
        W._PRESENTED[:] = self._saved

    def test_closing_one_of_two_windows_leaves_the_other(self):
        main, setup = FakeWindow(), FakeWindow()
        W._note_presented(main)
        W._note_presented(setup)
        self.assertEqual(W._remaining_windows(setup), [main])
        self.assertEqual(W._PRESENTED, [main])
        self.assertEqual(W._remaining_windows(main), [])

    def test_a_window_shown_again_is_counted_once(self):
        main = FakeWindow()
        W._note_presented(main)
        W._note_presented(main)
        self.assertEqual(W._PRESENTED, [main])

    def test_hidden_windows_do_not_keep_the_dock_icon(self):
        main, setup = FakeWindow(visible=False), FakeWindow()
        W._note_presented(main)
        W._note_presented(setup)
        self.assertEqual(W._remaining_windows(setup), [])

    def test_the_delegate_fronts_the_other_window_and_keeps_regular(self):
        calls = []

        class FakeApp(object):
            def setActivationPolicy_(self, policy):
                calls.append(policy)

        main, setup = FakeWindow(), FakeWindow()
        W._note_presented(main)
        W._note_presented(setup)

        class Note(object):
            def object(self):
                return setup

        real = W.NSApplication
        W.NSApplication = type('A', (), {'sharedApplication': staticmethod(lambda: FakeApp())})
        try:
            closed = []
            d = W._WindowDelegate.alloc().initWithCallback_(lambda: closed.append(1))
            d.windowWillClose_(Note())
            self.assertEqual(calls, [])                 # stays Regular
            self.assertEqual(main.fronted, 1)
            self.assertEqual(closed, [1])
            d.windowWillClose_(type('N', (), {'object': lambda self: main})())
            self.assertEqual(calls, [W.NSApplicationActivationPolicyAccessory])
        finally:
            W.NSApplication = real


if __name__ == '__main__':
    unittest.main()
