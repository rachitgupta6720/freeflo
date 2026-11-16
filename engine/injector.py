"""Put dictated text at the cursor.

How: write the text to the system pasteboard (NSPasteboard), synthesise
Cmd+V with a Quartz CGEvent, then put the user's previous clipboard back.

Why the clipboard at all: it is the one insertion route that works in every
app and for every script (Devanagari, emoji, CJK) -- per-character keystroke
injection does neither.

What changed from the pyperclip + osascript version, and why:

* No subprocesses. ``pbcopy``/``pbpaste`` and ``osascript`` each cost tens of
  milliseconds and the latter needed the separate "control System Events"
  Automation permission; a missing grant made paste silently do nothing.
  NSPasteboard and CGEventPost are in-process and need only the Accessibility
  permission the hotkey already requires.
* The previous clipboard is restored *completely* -- every item and every
  type (images, files, rich text), not just its plain-text flavour.
* Restore is guarded by the pasteboard ``changeCount``: if anything else wrote
  to the clipboard after us (the user copied something), it is left alone.
* The temporary write carries the nspasteboard.org marker types, so clipboard
  managers do not record every dictation in their history.
* It never pastes into the void. Without Accessibility the keystroke would be
  dropped silently, so the text is left on the clipboard as a normal copy and
  the caller is told (``copied_only``) so it can say "Copied - press Cmd+V".
* ``inject()`` returns as soon as the keystroke is posted; the clipboard is
  restored a moment later on a timer instead of making the caller wait.

AppKit / Quartz are imported inside functions so this module (and its pure
helpers) imports cleanly where PyObjC is not installed.
"""
import logging
import threading
import time
import unicodedata

log = logging.getLogger('freeflo.injector')

# --- result ------------------------------------------------------------

PASTED = 'pasted'            # Cmd+V was posted to the frontmost app
COPIED_ONLY = 'copied_only'  # text is on the clipboard; user must paste
FAILED = 'failed'            # nothing happened; text is NOT on the clipboard


class InjectResult(object):
    """Outcome of an injection.

    ``status``        PASTED | COPIED_ONLY | FAILED
    ``reason``        short machine-readable cause when not PASTED:
                      'accessibility', 'paste_failed', 'empty',
                      'clipboard_unavailable', 'clipboard_write_failed'
    ``will_restore``  True if the previous clipboard is scheduled to come back
    """
    __slots__ = ('status', 'reason', 'will_restore')

    def __init__(self, status, reason='', will_restore=False):
        self.status = status
        self.reason = reason
        self.will_restore = will_restore

    @property
    def pasted(self):
        return self.status == PASTED

    @property
    def copied_only(self):
        return self.status == COPIED_ONLY

    @property
    def failed(self):
        return self.status == FAILED

    def __eq__(self, other):
        if isinstance(other, InjectResult):
            return (self.status, self.reason) == (other.status, other.reason)
        if isinstance(other, str):
            return self.status == other
        return NotImplemented

    def __ne__(self, other):
        eq = self.__eq__(other)
        return eq if eq is NotImplemented else not eq

    __hash__ = None

    def __repr__(self):
        return 'InjectResult(%r, reason=%r, will_restore=%r)' % (
            self.status, self.reason, self.will_restore)


# --- timing ------------------------------------------------------------
# Every wait is listed here with the reason it exists.

# After writing the pasteboard, before Cmd+V. The write itself is synchronous
# (the data is with the pasteboard server when the call returns), so this is
# only a small cushion for apps that watch the clipboard through a proxy
# (remote-desktop / VM clipboard sync). The old code needed 120 ms here
# because it wrote through a ``pbcopy`` subprocess.
PRE_PASTE_SETTLE = 0.02

# If the user is still physically holding a modifier (the push-to-talk key is
# one), wait for it to come up before pasting: a few apps read the *hardware*
# modifier state rather than the event's, and would see Option+Cmd+V. Polled,
# so the usual cost is zero; bounded, so a held key cannot block the paste.
MODIFIER_RELEASE_WAIT = 0.30
MODIFIER_POLL = 0.01

# Between key-down and key-up. Some apps ignore a zero-length key press.
KEY_HOLD = 0.01

# After Cmd+V, before the old clipboard is put back. The target app reads the
# pasteboard when it gets round to handling the key event; restoring sooner
# makes it paste the OLD clipboard (the historical "nothing gets filled" /
# "pasted the wrong thing" bug). This runs on a timer thread, so it costs the
# dictation path nothing -- which is why it can afford to be generous (the
# old, blocking code could only afford 0.45 s and still lost to slow apps).
RESTORE_DELAY = 0.75

# A failed pasteboard write is retried this many times, this far apart.
WRITE_RETRIES = 3
WRITE_RETRY_GAP = 0.01

# Above this we do not copy the previous clipboard into our memory (a huge
# image or file set); the dictation is still pasted, but the clipboard is not
# restored and the result says so via ``will_restore=False``.
MAX_SNAPSHOT_BYTES = 128 * 1024 * 1024

# nspasteboard.org conventions: "this content is temporary / not user-copied,
# do not record it". Honoured by Maccy, Paste, Raycast, Alfred, 1Password...
TRANSIENT_TYPES = (
    'org.nspasteboard.TransientType',
    'org.nspasteboard.ConcealedType',
    'org.nspasteboard.AutoGeneratedType',
)

# Stamped on our synthetic key events (kCGEventSourceUserData) so freeflo's
# own hotkey tap can recognise and ignore them -- otherwise the injected "V"
# looks like "another key was pressed while the dictation key is held" and
# cancels a dictation the user has just started. ASCII 'ffLO'.
SYNTHETIC_EVENT_TAG = 0x66664C4F

# Virtual keycode of the key labelled V on ANSI/ISO QWERTY-family layouts.
# Only the fallback: apps match Cmd shortcuts on the CHARACTER the active
# layout produces, so on a layout that moves V (Dvorak, Turkish F, Workman...)
# keycode 9 is some other shortcut entirely (Cmd+K, Cmd+C). The key that
# really types "v" is looked up from the layout by refresh_paste_keycode().
PASTE_KEYCODE = 9

# Above this we give up waiting for the clipboard's current owner to hand
# over its contents (lazily rendered spreadsheet ranges, images, an app that
# is not responding) and paste without restoring. Keeping the old clipboard
# is a courtesy; it must never hold the dictated text back.
SNAPSHOT_TIMEOUT = 0.25

_CMD_FLAGS = 0x100000 | 0x000008     # kCGEventFlagMaskCommand | left-Cmd bit
_MODIFIER_MASK = 0x020000 | 0x040000 | 0x080000 | 0x100000   # shift ctrl opt cmd


class _NoPool(object):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _pool():
    """An autorelease pool: injection runs on worker/timer threads, which do
    not have one, and Cocoa would otherwise leak every temporary object."""
    try:
        import objc
        return objc.autorelease_pool()
    except Exception:
        return _NoPool()


# --- system access (each piece is replaceable for tests) ---------------

class NSPasteboardBackend(object):
    """The handful of pasteboard operations injection needs.

    A snapshot is an opaque list of items, each a list of (type, NSData)
    pairs; ``None`` means "could not be captured" and ``[]`` "was empty".
    """

    def __init__(self, pasteboard=None):
        # None = the general pasteboard, looked up per call. Tests pass a
        # private NSPasteboard so they never touch the user's clipboard.
        self._pb = pasteboard

    def _board(self):
        if self._pb is not None:
            return self._pb
        from AppKit import NSPasteboard
        return NSPasteboard.generalPasteboard()

    def change_count(self):
        with _pool():
            return int(self._board().changeCount())

    def snapshot(self, max_bytes=MAX_SNAPSHOT_BYTES):
        with _pool():
            out, total = [], 0
            for item in (self._board().pasteboardItems() or ()):
                entry = []
                for ptype in (item.types() or ()):
                    data = item.dataForType_(ptype)
                    if data is None:        # promised but never provided
                        continue
                    total += int(data.length())
                    if total > max_bytes:
                        return None
                    entry.append((str(ptype), data))
                if entry:
                    out.append(entry)
            return out

    def write_text(self, text, transient):
        """Replace the clipboard with ``text``. Returns the new changeCount,
        or None if the write was refused."""
        from AppKit import NSPasteboardItem, NSPasteboardTypeString
        with _pool():
            item = NSPasteboardItem.alloc().init()
            if not item.setString_forType_(text, NSPasteboardTypeString):
                return None
            if transient:
                for marker in TRANSIENT_TYPES:
                    item.setString_forType_('', marker)
            pb = self._board()
            pb.clearContents()
            if not pb.writeObjects_([item]):
                return None
            return int(pb.changeCount())

    def read_text(self):
        from AppKit import NSPasteboardTypeString
        with _pool():
            value = self._board().stringForType_(NSPasteboardTypeString)
            return None if value is None else str(value)

    def restore(self, snapshot):
        from AppKit import NSPasteboardItem
        with _pool():
            pb = self._board()
            pb.clearContents()
            if not snapshot:
                return True                 # it was empty before: empty again
            items = []
            for entry in snapshot:
                item = NSPasteboardItem.alloc().init()
                for ptype, data in entry:
                    item.setData_forType_(data, ptype)
                items.append(item)
            return bool(pb.writeObjects_(items))


def accessibility_trusted():
    """Can this process post keystrokes to other apps? Checked on every
    injection (the user can revoke it at any time). Never prompts."""
    try:
        from ApplicationServices import AXIsProcessTrusted
        if AXIsProcessTrusted():
            return True
    except Exception:
        pass
    try:
        import Quartz
        preflight = getattr(Quartz, 'CGPreflightPostEventAccess', None)
        if preflight is not None and preflight():
            return True
    except Exception:
        pass
    return False


def modifiers_down():
    """True while Shift/Control/Option/Command is physically held."""
    try:
        import Quartz
        flags = Quartz.CGEventSourceFlagsState(
            Quartz.kCGEventSourceStateHIDSystemState)
        return bool(int(flags) & _MODIFIER_MASK)
    except Exception:
        return False


# --- which key is "V" on this keyboard layout ---------------------------
#
# Carbon's Text Input Sources + UCKeyTranslate, through ctypes (PyObjC does
# not wrap them). TIS may only be called on the MAIN thread - macOS asserts
# that and kills the process otherwise - so the lookup is done there, ahead of
# time (at launch and whenever the input source changes), and the paste path
# on its worker thread only reads the cached answer.

_CMD_KEY_STATE = 0x0100 >> 8            # Carbon cmdKey, as UCKeyTranslate wants it
_KEYCODE_SCAN = (PASTE_KEYCODE,) + tuple(
    k for k in range(0, 51) if k != PASTE_KEYCODE)     # the alphanumeric block

_paste_keycode = PASTE_KEYCODE
_carbon_cache = None


def _carbon():
    """(Carbon, CoreFoundation) ctypes handles with prototypes set, or None."""
    global _carbon_cache
    if _carbon_cache is None:
        try:
            import ctypes
            carbon = ctypes.CDLL(
                '/System/Library/Frameworks/Carbon.framework/Carbon')
            cf = ctypes.CDLL('/System/Library/Frameworks/CoreFoundation.framework'
                             '/CoreFoundation')
            vp = ctypes.c_void_p
            for name in ('TISCopyCurrentKeyboardLayoutInputSource',
                         'TISCopyCurrentASCIICapableKeyboardLayoutInputSource'):
                fn = getattr(carbon, name)
                fn.restype, fn.argtypes = vp, []
            carbon.TISGetInputSourceProperty.restype = vp
            carbon.TISGetInputSourceProperty.argtypes = [vp, vp]
            carbon.LMGetKbdType.restype = ctypes.c_uint8
            carbon.LMGetKbdType.argtypes = []
            carbon.UCKeyTranslate.restype = ctypes.c_int32
            carbon.UCKeyTranslate.argtypes = [
                vp, ctypes.c_uint16, ctypes.c_uint16, ctypes.c_uint32,
                ctypes.c_uint32, ctypes.c_uint32,
                ctypes.POINTER(ctypes.c_uint32), ctypes.c_ulong,
                ctypes.POINTER(ctypes.c_ulong),
                ctypes.POINTER(ctypes.c_uint16)]
            cf.CFDataGetBytePtr.restype = vp
            cf.CFDataGetBytePtr.argtypes = [vp]
            cf.CFRelease.restype = None
            cf.CFRelease.argtypes = [vp]
            _carbon_cache = (carbon, cf)
        except Exception:
            log.exception('Carbon keyboard-layout API unavailable')
            _carbon_cache = False
    return _carbon_cache or None


def keycode_in_layout(source, char='v', with_cmd=True):
    """The virtual keycode that types ``char`` (while Cmd is held, by default)
    on the keyboard layout of ``source`` (a TISInputSourceRef, as an int
    address), or None if no key does / the source has no layout data (an
    input method). MAIN THREAD ONLY."""
    libs = _carbon()
    if libs is None or not source:
        return None
    import ctypes
    carbon, cf = libs
    key = ctypes.c_void_p.in_dll(carbon, 'kTISPropertyUnicodeKeyLayoutData')
    data = carbon.TISGetInputSourceProperty(source, key)
    if not data:
        return None
    layout = cf.CFDataGetBytePtr(data)
    if not layout:
        return None
    kbd_type = int(carbon.LMGetKbdType())
    state = _CMD_KEY_STATE if with_cmd else 0
    dead = ctypes.c_uint32(0)
    length = ctypes.c_ulong(0)
    chars = (ctypes.c_uint16 * 4)()
    for keycode in _KEYCODE_SCAN:
        dead.value = 0
        length.value = 0
        err = carbon.UCKeyTranslate(
            layout, keycode, 0, state, kbd_type, 1,     # key down, no dead keys
            ctypes.byref(dead), 4, ctypes.byref(length), chars)
        if err == 0 and length.value == 1 and chr(chars[0]).lower() == char:
            return keycode
    return None


def refresh_paste_keycode():
    """Work out which key pastes on the keyboard layout in use right now and
    remember it for ``make_paste_events``. Returns the keycode.

    MAIN THREAD ONLY (a call from any other thread is ignored and returns the
    remembered value). Call it at launch and whenever the input source
    changes. Order: the key that gives "v" with Cmd on the current layout
    (this is what makes "Dvorak - QWERTY Cmd" come out as the QWERTY key);
    else the same on the ASCII-capable layout macOS itself falls back to for
    shortcuts (non-Latin layouts, input methods); else the ANSI V key."""
    global _paste_keycode
    try:
        from Foundation import NSThread
        if not NSThread.isMainThread():
            return _paste_keycode
        libs = _carbon()
        if libs is None:
            return _paste_keycode
        carbon, cf = libs
        found = None
        for getter in (carbon.TISCopyCurrentKeyboardLayoutInputSource,
                       carbon.TISCopyCurrentASCIICapableKeyboardLayoutInputSource):
            source = getter()
            if not source:
                continue
            try:
                found = keycode_in_layout(source)
            finally:
                cf.CFRelease(source)
            if found is not None:
                break
        keycode = PASTE_KEYCODE if found is None else found
        if keycode != _paste_keycode:
            log.info('Paste key for the current keyboard layout: keycode %d',
                     keycode)
        _paste_keycode = keycode
    except Exception:
        log.exception('Could not read the keyboard layout')
    return _paste_keycode


def paste_keycode():
    """The keycode Cmd+V is posted with (see refresh_paste_keycode)."""
    return _paste_keycode


def make_paste_events():
    """Build the Cmd+V key-down / key-up pair (not posted). Returns
    ``(down, up)``, or None if Quartz refused to create them."""
    import Quartz
    source = Quartz.CGEventSourceCreate(
        Quartz.kCGEventSourceStateCombinedSessionState)
    keycode = _paste_keycode
    down = Quartz.CGEventCreateKeyboardEvent(source, keycode, True)
    up = Quartz.CGEventCreateKeyboardEvent(source, keycode, False)
    if down is None or up is None:
        return None
    for event in (down, up):
        # Explicit flags: exactly Cmd, whatever is physically held.
        Quartz.CGEventSetFlags(event, _CMD_FLAGS)
        Quartz.CGEventSetIntegerValueField(
            event, Quartz.kCGEventSourceUserData, SYNTHETIC_EVENT_TAG)
    return down, up


def is_synthetic(event):
    """True if ``event`` (a CGEvent seen by an event tap) is one of the
    keystrokes this module posted. freeflo's hotkey tap must skip these:
    otherwise the injected "V" counts as "another key was pressed while the
    dictation key is held" and cancels a dictation the user has already
    started while the previous one was being pasted."""
    try:
        import Quartz
        return Quartz.CGEventGetIntegerValueField(
            event, Quartz.kCGEventSourceUserData) == SYNTHETIC_EVENT_TAG
    except Exception:
        return False


def post_paste_keystroke(sleep=time.sleep):
    """Synthesise Cmd+V. Returns False if the events could not be created or
    posted. (CGEventPost itself reports nothing: a process without
    Accessibility has its events dropped silently, which is why the caller
    checks ``accessibility_trusted()`` first.)

    Posted at the session tap -- the level remote-desktop and VM clients
    listen at, so the paste also reaches a guest/remote machine."""
    try:
        import Quartz
        with _pool():
            events = make_paste_events()
            if events is None:
                return False
            down, up = events
            Quartz.CGEventPost(Quartz.kCGSessionEventTap, down)
            sleep(KEY_HOLD)
            Quartz.CGEventPost(Quartz.kCGSessionEventTap, up)
        return True
    except Exception:
        log.exception('Posting the paste keystroke failed')
        return False


def frontmost_app_id():
    """Bundle id (or pid as a string) of the app that will receive the paste,
    or None. Identifies the app only; never reads anything inside it."""
    try:
        from AppKit import NSWorkspace
        with _pool():
            app = NSWorkspace.sharedWorkspace().frontmostApplication()
            if app is None:
                return None
            bundle = app.bundleIdentifier()
            return str(bundle) if bundle else str(app.processIdentifier())
    except Exception:
        return None


# --- the injector ------------------------------------------------------

class _Pending(object):
    __slots__ = ('snapshot', 'count', 'timer')

    def __init__(self, snapshot, count):
        self.snapshot = snapshot
        self.count = count
        self.timer = None


class Injector(object):
    """Stateful so that two dictations in quick succession restore the
    clipboard the user actually had, not the first dictation's text."""

    def __init__(self, pasteboard=None, post_paste=None, is_trusted=None,
                 modifiers_down=None, sleep=time.sleep,
                 restore_delay=RESTORE_DELAY, defer_restore=True,
                 snapshot_timeout=SNAPSHOT_TIMEOUT):
        self._pb = pasteboard if pasteboard is not None else NSPasteboardBackend()
        self._post_paste = post_paste or post_paste_keystroke
        self._is_trusted = is_trusted or accessibility_trusted
        self._modifiers_down = modifiers_down or globals()['modifiers_down']
        self._sleep = sleep
        self._restore_delay = restore_delay
        self._defer_restore = defer_restore
        self._snapshot_timeout = snapshot_timeout
        self._lock = threading.RLock()
        self._pending = None

    # -- public --------------------------------------------------------

    def inject(self, text, restore_clipboard=True):
        """Paste ``text`` at the cursor. Returns an InjectResult.

        Takes ~30 ms (plus up to MODIFIER_RELEASE_WAIT if a modifier is still
        held). Call it from a worker thread, not the hotkey tap."""
        if not text:
            return InjectResult(FAILED, 'empty')
        with self._lock:
            try:
                return self._inject_locked(text, restore_clipboard)
            except Exception:
                log.exception('Injection failed')
                return InjectResult(FAILED, 'clipboard_unavailable')

    def copy(self, text):
        """Leave ``text`` on the clipboard as an ordinary copy (no paste, no
        restore). Returns COPIED_ONLY or FAILED."""
        if not text:
            return InjectResult(FAILED, 'empty')
        with self._lock:
            try:
                self._drop_pending()
                if self._write(text, transient=False) is None:
                    return InjectResult(FAILED, 'clipboard_write_failed')
                return InjectResult(COPIED_ONLY, 'requested')
            except Exception:
                log.exception('Copy failed')
                return InjectResult(FAILED, 'clipboard_unavailable')

    def flush(self):
        """Run a scheduled clipboard restore right now (call before quitting,
        so the dictated text is not left on the clipboard)."""
        with self._lock:
            pending = self._take_pending()
            if pending is not None:
                self._restore(pending)

    # -- internals -----------------------------------------------------

    def _inject_locked(self, text, restore_clipboard):
        pb = self._pb

        # 1. What should the clipboard go back to afterwards?
        snapshot = None
        pending = self._take_pending()
        if pending is not None and pb.change_count() == pending.count:
            # The clipboard still holds OUR previous dictation (its restore
            # had not fired yet). The user's real clipboard is the one saved
            # back then -- snapshotting now would "restore" our own text.
            snapshot = pending.snapshot
        elif restore_clipboard:
            snapshot = self._snapshot_bounded()
            if snapshot is None:
                log.warning('Clipboard not captured (too large, too slow or '
                            'unreadable) - it will not be restored after '
                            'this paste')

        # 2. No Accessibility -> Cmd+V would vanish. Hand the text over as a
        #    normal copy instead of pretending.
        if not self._is_trusted():
            if self._write(text, transient=False) is None:
                self._put_back(snapshot)
                return InjectResult(FAILED, 'clipboard_write_failed')
            return InjectResult(COPIED_ONLY, 'accessibility')

        # 3. Stage the text and make sure it really landed before any
        #    keystroke is sent. It is marked temporary only if the previous
        #    clipboard is coming back; otherwise it stays, as a real copy.
        will_restore = restore_clipboard and snapshot is not None
        count = self._write(text, transient=will_restore)
        if count is None:
            self._put_back(snapshot)
            return InjectResult(FAILED, 'clipboard_write_failed')

        self._sleep(PRE_PASTE_SETTLE)
        self._wait_for_modifiers()

        # 4. Paste.
        if not self._post_paste():
            # Re-write without the "temporary" markers: this is now a real
            # copy the user will paste by hand.
            self._write(text, transient=False)
            return InjectResult(COPIED_ONLY, 'paste_failed')

        # 5. Give the clipboard back, later, if it is still ours by then.
        if will_restore:
            self._schedule_restore(_Pending(snapshot, count))
            return InjectResult(PASTED, '', True)
        return InjectResult(PASTED, '', False)

    def _snapshot_bounded(self):
        """Capture the current clipboard, but never wait long for it.

        Reading every type of every item makes the app that owns the
        clipboard render lazily-provided data on demand (a large spreadsheet
        range, an image, file promises), and an owner that is not responding
        blocks the read outright. That happens on a helper thread; if it has
        not finished within ``snapshot_timeout`` the paste goes ahead without
        a snapshot (so without a restore) instead of sitting on "processing".
        Returns the snapshot, or None."""
        box = []

        def work():
            try:
                box.append(self._pb.snapshot())
            except Exception:
                log.exception('Could not read the current clipboard')
                box.append(None)

        reader = threading.Thread(target=work, name='clipboard-snapshot',
                                  daemon=True)
        reader.start()
        reader.join(self._snapshot_timeout)
        if reader.is_alive():
            log.warning('The clipboard owner did not hand over its contents '
                        'within %.2fs', self._snapshot_timeout)
            return None
        return box[0] if box else None

    def _write(self, text, transient):
        """Write and read back. Returns the changeCount, or None."""
        pb = self._pb
        for attempt in range(WRITE_RETRIES):
            try:
                count = pb.write_text(text, transient)
                if count is not None and pb.read_text() == text:
                    return count
            except Exception:
                log.exception('Clipboard write failed (attempt %d)', attempt + 1)
            self._sleep(WRITE_RETRY_GAP)
        return None

    def _put_back(self, snapshot):
        if snapshot is None:
            return
        try:
            self._pb.restore(snapshot)
        except Exception:
            log.exception('Could not put the clipboard back')

    def _wait_for_modifiers(self):
        waited = 0.0
        try:
            while waited < MODIFIER_RELEASE_WAIT and self._modifiers_down():
                self._sleep(MODIFIER_POLL)
                waited += MODIFIER_POLL
        except Exception:
            pass

    def _schedule_restore(self, pending):
        self._pending = pending
        if not self._defer_restore:
            self._sleep(self._restore_delay)
            self._pending = None
            self._restore(pending)
            return
        timer = threading.Timer(self._restore_delay, self._restore_due,
                                args=(pending,))
        timer.name = 'clipboard-restore'
        timer.daemon = True
        pending.timer = timer
        timer.start()

    def _restore_due(self, pending):
        with self._lock:
            if self._pending is not pending:
                return                      # superseded by a newer injection
            self._pending = None
            self._restore(pending)

    def _take_pending(self):
        pending, self._pending = self._pending, None
        if pending is not None and pending.timer is not None:
            pending.timer.cancel()
        return pending

    def _drop_pending(self):
        self._take_pending()

    def _restore(self, pending):
        try:
            if self._pb.change_count() != pending.count:
                # Someone else wrote to the clipboard after us (the user
                # copied something). Theirs wins.
                return False
            return bool(self._pb.restore(pending.snapshot))
        except Exception:
            log.exception('Restoring the clipboard failed')
            return False


_default = None
_default_lock = threading.Lock()


def _injector():
    global _default
    with _default_lock:
        if _default is None:
            _default = Injector()
        return _default


def inject(text, restore_clipboard=True):
    """Paste ``text`` at the cursor and restore the previous clipboard.

    Returns an :class:`InjectResult` (it used to return None):

      PASTED       Cmd+V was sent. The old clipboard returns ~0.75 s later.
      COPIED_ONLY  Could not paste (no Accessibility, or posting failed). The
                   text is on the clipboard: tell the user to press Cmd+V.
      FAILED       Nothing happened (empty text / clipboard unusable).
    """
    return _injector().inject(text, restore_clipboard)


def copy(text):
    """Put ``text`` on the clipboard as a normal copy, without pasting."""
    return _injector().copy(text)


def flush():
    """Restore the clipboard now if a restore is still pending (app quit)."""
    if _default is not None:
        _default.flush()


# --- spacing (pure) ----------------------------------------------------

# Punctuation that hugs the word before it: never put a space in front.
_NO_SPACE_BEFORE = frozenset(
    '.,;:!?%)]}…»”’'    # ... >> closing curly quotes
    '।॥'                           # Devanagari danda, double danda
    '、。，！？：；）】」』')

# What our previous paste may end with for a following word to need a space:
# sentence/clause punctuation and closers. Anything else that is not a letter
# or digit (an opening bracket, a slash, a hyphen, "@", "#", "$"...) is left
# alone -- joining is at least as likely to be what the user wants there.
_SPACE_AFTER = frozenset('.,;:!?%)]}"\'…»”’।॥')


def _spaceless_script(ch):
    """Scripts written without spaces between words (and their punctuation)."""
    cp = ord(ch)
    return (0x3040 <= cp <= 0x30FF or      # Hiragana, Katakana
            0x3400 <= cp <= 0x4DBF or      # CJK extension A
            0x4E00 <= cp <= 0x9FFF or      # CJK unified ideographs
            0x3000 <= cp <= 0x303F or      # CJK punctuation
            0xFF00 <= cp <= 0xFFEF or      # full-width forms
            0x0E00 <= cp <= 0x0EFF or      # Thai, Lao
            0x1000 <= cp <= 0x109F or      # Myanmar
            0x1780 <= cp <= 0x17FF)        # Khmer


def _wordish(ch):
    # Letters, digits, and combining marks (a Devanagari word can end in a
    # vowel sign, which is a mark, not a letter).
    return ch.isalnum() or unicodedata.category(ch).startswith('M')


def needs_leading_space(text, previous):
    """Should a space go between ``previous`` (what sits right before the
    cursor) and ``text``? See :func:`smart_spacing` for the rule."""
    if not text or not previous:
        return False
    first, last = text[0], previous[-1]
    if first.isspace() or last.isspace():
        return False
    if first in _NO_SPACE_BEFORE:
        return False
    if _spaceless_script(first) or _spaceless_script(last):
        return False
    return _wordish(last) or last in _SPACE_AFTER


def smart_spacing(text, previous=None):
    """Tidy the whitespace around a transcript before it is pasted.

    The rule is deliberately minimal, because freeflo cannot (and must not)
    read the text field it is pasting into:

    1. Leading and trailing whitespace from the recogniser is removed.
       Whitespace inside the text is untouched.
    2. A trailing space is NEVER added. We cannot know what follows the
       cursor, and a stray space is wrong in search boxes, terminals, code,
       chat fields (sent with the message) and before existing punctuation.
    3. A single leading space is added only when the caller passes
       ``previous`` -- the text freeflo itself pasted immediately before this,
       at the same place -- and the join needs one: ``previous`` ends in a
       letter, digit, or closing punctuation (``. , ; : ! ? ) ] } " '`` and
       the Devanagari danda), and ``text`` does not start with punctuation
       that attaches to the preceding word. Scripts that do not use spaces
       (Chinese, Japanese, Thai...) never get one.

    With ``previous=None`` (the default, and the right choice whenever the
    caller is not sure the cursor is still where the last paste left it) only
    rule 1 applies.
    """
    if not text:
        return ''
    out = text.strip()
    if out and needs_leading_space(out, previous):
        out = ' ' + out
    return out


class PasteContinuity(object):
    """Remembers freeflo's own last paste, so the next dictation can be
    joined to it with :func:`smart_spacing`.

    It knows only what freeflo did -- it never inspects another app. That
    makes it a guess, so it is discarded aggressively: ``previous()`` returns
    None unless the same app is frontmost, the paste was recent, and nobody
    called ``invalidate()`` in between. The caller must call ``invalidate()``
    for every real key press or mouse click it observes (the cursor may have
    moved); if it cannot observe them, do not use this class and pass
    ``previous=None``.
    """

    def __init__(self, max_age=20.0, clock=time.monotonic):
        self._max_age = max_age
        self._clock = clock
        self._lock = threading.Lock()
        self._text = None
        self._app = None
        self._at = 0.0

    def note(self, text, app=None):
        """Record a successful paste of ``text`` into ``app``."""
        with self._lock:
            self._text = text or None
            self._app = app
            self._at = self._clock()

    def invalidate(self):
        with self._lock:
            self._text = None

    def previous(self, app=None):
        with self._lock:
            if self._text is None:
                return None
            if self._clock() - self._at > self._max_age:
                self._text = None
                return None
            if app != self._app:
                return None
            return self._text
