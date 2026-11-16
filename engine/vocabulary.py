"""The user's custom vocabulary: words to listen for, and corrections to make.

Two small lists, kept in one JSON file under
``~/Library/Application Support/freeflo/vocabulary.json``:

``words``
    Names and jargon the recogniser should expect ("Kiwi", "UPI", "Rachit").
    They are handed to the speech helper as the ``context`` of every
    dictation (see :meth:`Vocabulary.context`), which biases recognition
    towards them. Nothing is replaced in the text.

``replacements``
    "heard -> written" corrections applied to the transcript afterwards
    (:meth:`Vocabulary.apply_replacements`), for the cases biasing cannot
    fix: "kiwi" -> "Kiwi", "you pee eye" -> "UPI", "at the rate" -> "@".

The file is plain, hand-editable JSON::

    {"version": 1,
     "words": ["Kiwi", "UPI"],
     "replacements": [{"heard": "you pee eye", "written": "UPI"}]}

Everything here is local: the lists never leave the Mac. Standard library
only; no AppKit. Safe to use from any thread.
"""
import json
import logging
import os
import re
import tempfile
import threading
import time
import unicodedata

log = logging.getLogger('freeflo.vocabulary')

FILE_NAME = 'vocabulary.json'
FORMAT_VERSION = 1

# Limits. The word list rides along with every "start" command, so it is kept
# small enough to stay well inside what the helper accepts as context
# (SpeechEngine caps context at 200 items / 8000 characters as a backstop).
MAX_WORDS = 200
MAX_WORD_CHARS = 60          # a name or term, not a sentence
MAX_REPLACEMENTS = 200
MAX_HEARD_CHARS = 80
MAX_WRITTEN_CHARS = 200      # may be a snippet: an address, a sign-off


# Everyday words a custom word must not silently re-spell (see
# Vocabulary.apply): a user who stores the acronym "IT" does not want every
# "it" in a sentence upper-cased.
_COMMON_SHORT_WORDS = frozenset((
    'a', 'i', 'am', 'an', 'as', 'at', 'be', 'by', 'do', 'go', 'he', 'hi',
    'if', 'in', 'is', 'it', 'me', 'my', 'no', 'of', 'oh', 'ok', 'on', 'or',
    'so', 'to', 'up', 'us', 'we', 'add', 'aid', 'all', 'and', 'any', 'are',
    'ask', 'but', 'can', 'did', 'for', 'get', 'had', 'has', 'her', 'him',
    'his', 'how', 'its', 'let', 'may', 'new', 'not', 'now', 'one', 'our',
    'out', 'pin', 'ram', 'rest', 'she', 'the', 'too', 'two', 'use', 'was',
    'who', 'why', 'yes', 'yet', 'you', 'this', 'that', 'what', 'when',
    'with', 'will', 'were', 'have', 'from', 'here', 'there', 'they', 'them',
))


class VocabularyError(ValueError):
    """An entry was rejected. ``code`` is one of 'empty', 'too_long', 'full';
    ``str(error)`` is a sentence that can be shown to the user."""

    def __init__(self, code, message):
        ValueError.__init__(self, message)
        self.code = code


def default_path():
    return os.path.join(os.path.expanduser('~/Library/Application Support'),
                        'freeflo', FILE_NAME)


# ----------------------------------------------------------------------
# Normalisation
# ----------------------------------------------------------------------

def _clean(value):
    """One line of text: NFC, control characters dropped, runs of whitespace
    collapsed to a single space, trimmed. Non-strings become ''."""
    if not isinstance(value, str):
        return ''
    value = unicodedata.normalize('NFC', value)
    value = ''.join(' ' if ch.isspace() else ch for ch in value
                    if ch.isspace() or unicodedata.category(ch) not in
                    ('Cc', 'Cs', 'Co', 'Cn'))
    return ' '.join(value.split())


def _key(value):
    """Identity of an entry: two spellings that differ only in case or spacing
    are the same word."""
    return _clean(value).casefold()


def _is_word_char(ch):
    # Letters, digits, underscore, and combining marks. Marks matter: a
    # Devanagari word can end in a vowel sign, which is a mark, not a letter,
    # and regex \b / \w would see a word boundary in the middle of the word.
    return ch.isalnum() or ch == '_' or unicodedata.category(ch)[0] == 'M'


# ----------------------------------------------------------------------
# Replacements (pure)
# ----------------------------------------------------------------------

def _compile(pairs):
    """Build the matcher for a list of (heard, written) pairs. Returns
    (regex, {key: written}) or (None, {}) when there is nothing to match."""
    table, spelled = {}, {}
    for heard, written in pairs:
        heard = _clean(heard)
        key = heard.casefold()
        if key and key not in table and isinstance(written, str):
            table[key] = written
            spelled[key] = heard
    if not table:
        return None, {}
    alts = []
    # Longest first, so "new york city" wins over "new york".
    for key in sorted(table, key=lambda k: (-len(k), k)):
        # Any run of whitespace in the transcript matches a space in "heard".
        alts.append(r'\s+'.join(re.escape(tok)
                                for tok in spelled[key].split(' ')))
    return re.compile('|'.join(alts), re.IGNORECASE | re.UNICODE), table


def _apply(text, regex, table):
    out, pos, last, changed = [], 0, 0, False
    n = len(text)
    while pos <= n:
        m = regex.search(text, pos)
        if m is None:
            break
        start, end = m.span()
        if end == start:
            pos = start + 1
            continue
        whole = True
        # Whole words only: the match must not continue a word on either
        # side. Checked only where the match itself begins/ends with a word
        # character, so a "heard" such as "c++" still works before a space.
        if (start > 0 and _is_word_char(text[start])
                and _is_word_char(text[start - 1])):
            whole = False
        if (end < n and _is_word_char(text[end - 1])
                and _is_word_char(text[end])):
            whole = False
        written = table.get(_key(m.group())) if whole else None
        if written is None:
            # Not on a word boundary (or a case-folding oddity): look again
            # one character further on, so an overlapping match is not lost.
            pos = start + 1
            continue
        out.append(text[last:start])
        out.append(written)
        last = pos = end
        changed = True
    if not changed:
        return None
    out.append(text[last:])
    return ''.join(out)


def _run(text, regex, table):
    if regex is None:
        return text
    # Entries are stored NFC; match against the NFC form of the text so a
    # decomposed transcript still matches. Only adopt it if something changed.
    subject = text
    if not unicodedata.is_normalized('NFC', text):
        subject = unicodedata.normalize('NFC', text)
    result = _apply(subject, regex, table)
    return text if result is None else result


def apply_replacements(text, replacements):
    """Apply "heard -> written" corrections to a transcript.

    ``replacements`` is an iterable of ``(heard, written)`` pairs or of
    ``{'heard': .., 'written': ..}`` dicts. The rule:

    * ``heard`` matches case-insensitively and only as whole words: "kiwi"
      matches "Kiwi" and "KIWI," but not "kiwis" or "akiwi". It may be several
      words; any whitespace between them matches.
    * ``written`` is inserted exactly as given (it is the spelling the user
      asked for); surrounding punctuation is untouched.
    * One pass, longest ``heard`` first. The output of one replacement is
      never fed into another, so "a -> b" plus "b -> a" swaps rather than
      loops, and order of definition does not matter.
    * An empty ``written`` deletes the phrase (and nothing else: tidy the
      spacing in the caller if that matters).

    Text with no match is returned unchanged (the very same string).
    """
    if not text or not isinstance(text, str):
        return text
    pairs = []
    for item in replacements or ():
        if isinstance(item, dict):
            pairs.append((item.get('heard'), item.get('written')))
        else:
            try:
                heard, written = item
            except (TypeError, ValueError):
                continue
            pairs.append((heard, written))
    regex, table = _compile(pairs)
    return _run(text, regex, table)


# ----------------------------------------------------------------------
# The store
# ----------------------------------------------------------------------

class Vocabulary(object):
    """Custom words + replacements, persisted as JSON.

    ``path``      where to keep the file (default: Application Support).
    ``autosave``  write the file after every change (default True).

    Every mutating method saves; reads never touch the disk after the first
    load. A missing file is an empty vocabulary. A corrupt file is moved
    aside (``vocabulary.json.corrupt-<timestamp>``) rather than overwritten,
    so a hand-edit gone wrong is never silently destroyed.
    """

    def __init__(self, path=None, autosave=True):
        self.path = path or default_path()
        self.autosave = autosave
        self._lock = threading.RLock()
        self._words = []             # display spellings, in insertion order
        self._replacements = []      # (heard, written), in insertion order
        self._loaded = False
        self._matcher = None         # cached (regex, table)
        self._full_matcher = None    # cached: replacements + word spellings

    # -- loading / saving ----------------------------------------------

    def load(self):
        """(Re)read the file. Returns self. Never raises for a bad file."""
        with self._lock:
            self._words, self._replacements = [], []
            self._matcher = None
            self._full_matcher = None
            self._loaded = True
            try:
                with open(self.path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
            except FileNotFoundError:
                return self
            except (OSError, ValueError) as e:
                log.warning('Vocabulary file unreadable (%s) - starting empty',
                            type(e).__name__)
                self._quarantine()
                return self
            if not isinstance(data, dict):
                log.warning('Vocabulary file has the wrong shape - starting empty')
                self._quarantine()
                return self
            self._adopt(data)
            return self

    def _adopt(self, data):
        """Take whatever is valid from parsed JSON; skip the rest quietly."""
        seen = set()
        for item in data.get('words') or ():
            word = _clean(item)
            key = word.casefold()
            if (not word or key in seen or len(word) > MAX_WORD_CHARS
                    or len(self._words) >= MAX_WORDS):
                continue
            seen.add(key)
            self._words.append(word)
        seen = set()
        raw = data.get('replacements') or ()
        if isinstance(raw, dict):               # {"heard": "written"} also ok
            raw = [{'heard': k, 'written': v} for k, v in raw.items()]
        for item in raw:
            if isinstance(item, dict):
                heard, written = item.get('heard'), item.get('written')
            elif isinstance(item, (list, tuple)) and len(item) == 2:
                heard, written = item
            else:
                continue
            heard, written = _clean(heard), _clean(written)
            key = heard.casefold()
            if (not heard or key in seen or len(heard) > MAX_HEARD_CHARS
                    or len(written) > MAX_WRITTEN_CHARS
                    or len(self._replacements) >= MAX_REPLACEMENTS):
                continue
            seen.add(key)
            self._replacements.append((heard, written))

    def _quarantine(self):
        try:
            os.replace(self.path, '%s.corrupt-%d' % (self.path, int(time.time())))
        except OSError:
            pass

    def _ensure_loaded(self):
        if not self._loaded:
            self.load()

    def save(self):
        """Write the file atomically (temp file + rename), so a crash or a
        full disk mid-write can never leave a half-written vocabulary.
        Returns True on success; failures are logged, not raised."""
        with self._lock:
            self._ensure_loaded()
            data = {
                'version': FORMAT_VERSION,
                'words': list(self._words),
                'replacements': [{'heard': h, 'written': w}
                                 for h, w in self._replacements],
            }
            folder = os.path.dirname(self.path) or '.'
            tmp = None
            try:
                os.makedirs(folder, exist_ok=True)
                fd, tmp = tempfile.mkstemp(prefix='.vocabulary-', suffix='.tmp',
                                           dir=folder)
                # ensure_ascii=False keeps Devanagari etc. readable in the
                # file; the explicit utf-8 matters in a frozen app, whose
                # default encoding is ASCII.
                with os.fdopen(fd, 'w', encoding='utf-8') as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                    f.write('\n')
                os.replace(tmp, self.path)
                return True
            except (OSError, ValueError):
                log.exception('Could not save the vocabulary')
                if tmp:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
                return False

    def _changed(self):
        self._matcher = None
        self._full_matcher = None
        if self.autosave:
            self.save()

    # -- words ---------------------------------------------------------

    def list(self):
        """The custom words, in the order they were added."""
        with self._lock:
            self._ensure_loaded()
            return list(self._words)

    words = list

    def __len__(self):
        with self._lock:
            self._ensure_loaded()
            return len(self._words)

    def __contains__(self, word):
        key = _key(word)
        with self._lock:
            self._ensure_loaded()
            return any(w.casefold() == key for w in self._words)

    def add(self, word):
        """Add a word. Returns the spelling now stored.

        Adding a word that is already there (ignoring case and spacing) is
        not an error: the stored spelling is updated to the new one, so
        re-adding "kiwi" as "Kiwi" fixes the capitalisation.
        Raises :class:`VocabularyError` if it is empty, too long, or the
        list is full."""
        word = _clean(word)
        if not word:
            raise VocabularyError('empty', 'Type a word to add.')
        if len(word) > MAX_WORD_CHARS:
            raise VocabularyError(
                'too_long', 'That is too long - keep it under %d characters.'
                % MAX_WORD_CHARS)
        key = word.casefold()
        with self._lock:
            self._ensure_loaded()
            for i, existing in enumerate(self._words):
                if existing.casefold() == key:
                    if existing != word:
                        self._words[i] = word
                        self._changed()
                    return word
            if len(self._words) >= MAX_WORDS:
                raise VocabularyError(
                    'full', 'The vocabulary is full (%d words) - remove one '
                    'first.' % MAX_WORDS)
            self._words.append(word)
            self._changed()
            return word

    def add_many(self, words):
        """Add several words (an import / paste of a list). Invalid entries
        are skipped. Returns ``(added, skipped)`` counts; saves once."""
        added = skipped = 0
        with self._lock:
            self._ensure_loaded()
            before_all = list(self._words)
            autosave, self.autosave = self.autosave, False
            try:
                for word in words or ():
                    before = len(self._words)
                    try:
                        self.add(word)
                    except VocabularyError:
                        skipped += 1
                        continue
                    if len(self._words) > before:
                        added += 1
                    else:
                        skipped += 1
            finally:
                self.autosave = autosave
            if self._words != before_all:
                self._changed()
        return added, skipped

    def remove(self, word):
        """Remove a word (case-insensitive). Returns whether it was there."""
        key = _key(word)
        with self._lock:
            self._ensure_loaded()
            for i, existing in enumerate(self._words):
                if existing.casefold() == key:
                    del self._words[i]
                    self._changed()
                    return True
            return False

    def clear(self):
        """Forget every word and replacement."""
        with self._lock:
            self._ensure_loaded()
            if self._words or self._replacements:
                self._words, self._replacements = [], []
                self._changed()

    # -- replacements --------------------------------------------------

    def replacements(self):
        """``[{'heard': .., 'written': ..}, ...]`` in the order added."""
        with self._lock:
            self._ensure_loaded()
            return [{'heard': h, 'written': w} for h, w in self._replacements]

    def add_replacement(self, heard, written):
        """Add or update a correction. Returns ``(heard, written)`` as stored.
        A second rule for the same ``heard`` replaces the first."""
        heard, written = _clean(heard), _clean(written)
        if not heard:
            raise VocabularyError('empty', 'Type the phrase to listen for.')
        if len(heard) > MAX_HEARD_CHARS:
            raise VocabularyError(
                'too_long', 'That phrase is too long - keep it under %d '
                'characters.' % MAX_HEARD_CHARS)
        if len(written) > MAX_WRITTEN_CHARS:
            raise VocabularyError(
                'too_long', 'That replacement is too long - keep it under %d '
                'characters.' % MAX_WRITTEN_CHARS)
        key = heard.casefold()
        with self._lock:
            self._ensure_loaded()
            for i, (h, w) in enumerate(self._replacements):
                if h.casefold() == key:
                    if (h, w) != (heard, written):
                        self._replacements[i] = (heard, written)
                        self._changed()
                    return heard, written
            if len(self._replacements) >= MAX_REPLACEMENTS:
                raise VocabularyError(
                    'full', 'There are already %d replacements - remove one '
                    'first.' % MAX_REPLACEMENTS)
            self._replacements.append((heard, written))
            self._changed()
            return heard, written

    def remove_replacement(self, heard):
        """Remove the correction for ``heard``. Returns whether it existed."""
        key = _key(heard)
        with self._lock:
            self._ensure_loaded()
            for i, (h, _w) in enumerate(self._replacements):
                if h.casefold() == key:
                    del self._replacements[i]
                    self._changed()
                    return True
            return False

    def apply_replacements(self, text):
        """Run the user's corrections over a transcript. See the module-level
        :func:`apply_replacements` for the exact rule. Never raises: a
        transcript is worth more than a correction."""
        if not text or not isinstance(text, str):
            return text
        try:
            with self._lock:
                self._ensure_loaded()
                if not self._replacements:
                    return text
                if self._matcher is None:
                    self._matcher = _compile(self._replacements)
                regex, table = self._matcher
            return _run(text, regex, table)
        except Exception:
            log.exception('Applying replacements failed - text left as is')
            return text

    def apply(self, text):
        """Everything the vocabulary does to a finished transcript, in one
        pass: the "heard -> written" replacements, plus the custom words
        written with the spelling the user stored ("kiwi" -> "Kiwi").

        The word list is applied here, app-side, because the on-device
        recogniser currently ignores vocabulary hints (measured on macOS 27:
        output is identical with and without them). A stored word is matched
        case-insensitively as a whole word, exactly like a replacement whose
        ``heard`` is the word itself.

        One guard: a stored word that is spelled like an everyday short word
        ("IT", "US", "AM") is left alone, or every "it" in a sentence would
        be upper-cased. Use an explicit replacement for those. Never raises.
        """
        if not text or not isinstance(text, str):
            return text
        try:
            with self._lock:
                self._ensure_loaded()
                if not self._replacements and not self._words:
                    return text
                if self._full_matcher is None:
                    pairs = list(self._replacements)
                    pairs.extend((w, w) for w in self._words
                                 if w.casefold() not in _COMMON_SHORT_WORDS)
                    self._full_matcher = _compile(pairs)
                regex, table = self._full_matcher
            return _run(text, regex, table)
        except Exception:
            log.exception('Applying the vocabulary failed - text left as is')
            return text

    def is_ambiguous(self, word):
        """True if ``word`` is one :meth:`apply` will not re-spell because it
        collides with an everyday word (so a settings page can say so)."""
        return _key(word) in _COMMON_SHORT_WORDS

    # -- for the speech engine -----------------------------------------

    def context(self):
        """The hint list for ``SpeechEngine.start(context=...)``: the custom
        words, then the ``written`` side of each replacement that is a short
        term in its own right (so the recogniser may get it right first
        time). Deduplicated, at most MAX_WORDS entries."""
        with self._lock:
            self._ensure_loaded()
            out = list(self._words)
            seen = set(w.casefold() for w in out)
            for _h, written in self._replacements:
                key = written.casefold()
                if (written and key not in seen
                        and len(written) <= MAX_WORD_CHARS
                        and any(_is_word_char(ch) for ch in written)):
                    seen.add(key)
                    out.append(written)
            return out[:MAX_WORDS]

    def to_dict(self):
        """Everything, for a settings page."""
        with self._lock:
            self._ensure_loaded()
            return {
                'words': list(self._words),
                'replacements': self.replacements(),
                'limits': {
                    'words': MAX_WORDS, 'word_chars': MAX_WORD_CHARS,
                    'replacements': MAX_REPLACEMENTS,
                    'heard_chars': MAX_HEARD_CHARS,
                    'written_chars': MAX_WRITTEN_CHARS,
                },
            }


# ----------------------------------------------------------------------
# Process-wide default
# ----------------------------------------------------------------------

_default = None
_default_lock = threading.Lock()


def get_default():
    """The app's shared vocabulary (created and loaded on first use)."""
    global _default
    with _default_lock:
        if _default is None:
            _default = Vocabulary().load()
        return _default
