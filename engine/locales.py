"""Dictation languages: what the speech helper offers, under friendly names.

The Whisper build had a fixed list of language codes ('en', 'hi', 'auto'...).
The on-device recogniser instead reports the locales this Mac supports and
which of them are already installed (``SpeechEngine.last_status``), so the
list is built from that at runtime. This module is the pure glue:

* ``display_name('en-IN')`` -> "English (India)"
* ``resolve(setting, status)`` -> the locale to dictate in, migrating the old
  config values ('auto', 'en', 'hi', ...) on the way
* ``choices(status, current)`` -> rows for the settings page / menu
* ``resolve_languages(languages, language, status)`` -> the languages to
  dictate in (one, or a primary plus one more: the helper runs two
  recognisers on the same audio and picks the better one per sentence)
* ``presets(status, current)`` -> the two or three one-click choices a simple
  picker needs ("English (India)", "English (India) + Hindi", "Hindi"), each
  with what still has to be downloaded and how big that is

Two facts about the India model, measured on macOS 27.2: every non-English
locale of India (hi-IN, mul-IN, bn-IN, ta-IN, ...) is the same multilingual
model (``status['groups']``), so choosing two of them gains nothing; and it
writes everything in English letters ("kal raat ko server down ho gaya"),
following English mixed into a sentence. It is downloaded once (about
620 MB) for all of them.

Nothing here talks to the helper or touches the config file.
"""
from engine import speech

# Names the system gets wrong or phrases awkwardly.
_NAME_OVERRIDES = {
    'mul-IN': 'Multilingual (India)',
}

# Used only where Foundation is not importable (tests on a bare interpreter).
_FALLBACK_LANGUAGE_NAMES = {
    'en': 'English', 'hi': 'Hindi', 'es': 'Spanish', 'fr': 'French',
    'de': 'German', 'zh': 'Chinese', 'ja': 'Japanese', 'ar': 'Arabic',
    'pt': 'Portuguese', 'it': 'Italian', 'ko': 'Korean', 'bn': 'Bangla',
    'ta': 'Tamil', 'te': 'Telugu', 'mr': 'Marathi', 'gu': 'Gujarati',
    'kn': 'Kannada', 'ml': 'Malayalam', 'pa': 'Punjabi', 'ur': 'Urdu',
    'yue': 'Cantonese', 'mul': 'Multilingual',
}

_name_cache = {}


def normalize(tag):
    """'en_in' / 'EN-in' -> 'en-IN'. Bare codes and 'auto' come back
    lower-cased and otherwise untouched."""
    if not isinstance(tag, str):
        return ''
    parts = [p for p in tag.strip().replace('_', '-').split('-') if p]
    if not parts:
        return ''
    out = [parts[0].lower()]
    for p in parts[1:]:
        if len(p) == 4 and p.isalpha():
            out.append(p.title())           # script: Hans, Latn
        elif (len(p) == 2 and p.isalpha()) or (len(p) == 3 and p.isdigit()):
            out.append(p.upper())           # region
        else:
            out.append(p)
    return '-'.join(out)


def is_concrete(value):
    """True for a full locale tag ('en-IN'); False for the legacy settings
    ('auto', 'en', 'hi', None) that still need resolving."""
    tag = normalize(value)
    return bool(tag) and tag != 'auto' and '-' in tag


def system_locale():
    """The user's macOS locale as a BCP-47 tag ('en-IN'), or None."""
    try:
        from Foundation import NSLocale
        ident = NSLocale.currentLocale().localeIdentifier()
        if ident:
            # 'en_IN@rg=inzzzz' -> 'en-IN'
            return normalize(str(ident).split('@')[0]) or None
    except Exception:
        pass
    return None


def display_name(tag):
    """A human name for a locale tag, in the user's own language:
    'en-IN' -> 'English (India)'. Falls back to the tag itself."""
    tag = normalize(tag)
    if not tag:
        return ''
    if tag == 'auto':
        return 'System default'
    cached = _name_cache.get(tag)
    if cached:
        return cached
    name = _NAME_OVERRIDES.get(tag)
    if not name:
        try:
            from Foundation import NSLocale
            loc = NSLocale.currentLocale()
            got = loc.localizedStringForLocaleIdentifier_(tag)
            if got:
                name = str(got)
                name = name[:1].upper() + name[1:]
        except Exception:
            name = None
    if not name:
        lang = tag.split('-')[0]
        base = _FALLBACK_LANGUAGE_NAMES.get(lang)
        if base and '-' in tag:
            name = '%s (%s)' % (base, tag.split('-', 1)[1])
        else:
            name = base or tag
    _name_cache[tag] = name
    return name


def _match(tag, tags):
    """The spelling of ``tag`` used in ``tags`` (case/underscore-insensitive),
    or None."""
    want = normalize(tag).lower()
    for t in tags or ():
        if normalize(t).lower() == want:
            return t
    return None


def resolve(setting, status, system=None):
    """Pick the locale to dictate in.

    ``setting`` is the stored ``language`` value: a full tag, or one of the
    old Whisper-era values ('auto', 'en', 'hi', ...). ``status`` is the
    helper's last status dict (or None before it has answered).

    Returns ``(locale, known)``. ``known`` is False when there is no status
    yet: ``locale`` is then just the setting passed through for the helper to
    resolve itself (it accepts bare codes and 'auto'), and nothing should be
    written back to the config.

    With a status:
      * a full tag that is still supported is kept as is;
      * 'auto' / empty becomes the helper's recommended locale for this Mac
        (the system locale when it is supported, e.g. en-IN);
      * a bare code ('hi') becomes the best variant of that language - the
        user's own region first, then an installed one;
      * anything unsupported (a language this recogniser does not have, or a
        locale Apple dropped) falls back to the recommended locale.
    """
    setting = normalize(setting) or 'auto'
    if not status or not status.get('supported'):
        return setting, False
    supported = list(status.get('supported') or ())
    installed = list(status.get('installed') or ())
    system = system or system_locale()
    default = (_match(status.get('default_locale'), supported)
               or speech.pick_locale('auto', supported, installed, system)
               or speech.pick_locale('en', supported, installed, system)
               or supported[0])
    if setting == 'auto':
        return default, True
    if '-' in setting:
        exact = _match(setting, supported)
        if exact:
            return exact, True
    picked = speech.pick_locale(setting, supported, installed, system)
    return (picked or default), True


HINDI = 'hi-IN'
MULTILINGUAL_INDIA = 'mul-IN'
INDIA_GROUP = 'india'


def _parts(tag):
    """'en-IN' -> ('en', 'IN'); region '' when there is none."""
    t = normalize(tag)
    if not t or t == 'auto':
        return '', ''
    pieces = t.split('-')
    region = ''
    for p in pieces[1:]:
        if (len(p) == 2 and p.isalpha()) or (len(p) == 3 and p.isdigit()):
            region = p.upper()
            break
    return pieces[0].lower(), region


def _status_groups(status):
    """The helper's model groups, or None when it does not report them (a
    helper before 1.1, or no status yet)."""
    groups = (status or {}).get('groups')
    if not isinstance(groups, list):
        return None
    if not groups and (status or {}).get('max_languages', 1) < 2:
        return None     # an older helper: [] means "not reported"
    return [[normalize(t) for t in g if isinstance(t, str)]
            for g in groups if isinstance(g, list)]


def group_of(tag, status=None):
    """Which shared speech model serves ``tag``: 'india' for the India
    multilingual model, another name for any other shared model, None for a
    locale with a model of its own. Taken from ``status['groups']`` when the
    helper reports it, else from the rule measured on macOS 27 (every
    non-English locale of India is the India model)."""
    tag = normalize(tag)
    if not tag:
        return None
    groups = _status_groups(status)
    if groups is not None:
        for group in groups:
            if tag.lower() in [t.lower() for t in group]:
                if any(_parts(t)[1] == 'IN' and _parts(t)[0] != 'en'
                       for t in group):
                    return INDIA_GROUP
                return group[0].lower()
        return None
    language, region = _parts(tag)
    if region == 'IN' and language and language != 'en':
        return INDIA_GROUP
    return None


def same_model(a, b, status=None):
    """True when ``a`` and ``b`` would load the same speech model."""
    na, nb = normalize(a).lower(), normalize(b).lower()
    if not na or not nb:
        return False
    if na == nb:
        return True
    group = group_of(a, status)
    return group is not None and group == group_of(b, status)


def short_name(tag, status=None):
    """The language alone when this Mac offers it in one variant only
    ('Hindi', 'French'), else the full name ('English (India)')."""
    tag = normalize(tag)
    if not tag:
        return ''
    if tag in _NAME_OVERRIDES:
        return _NAME_OVERRIDES[tag]
    supported = (status or {}).get('supported') or ()
    language, _ = _parts(tag)
    if language and supported:
        same = [t for t in supported if _parts(t)[0] == language]
        if len(same) == 1:
            name = display_name(language)
            if name and name != language:
                return name
    return display_name(tag)


def describe(tag, status=None):
    """One line about what a language does, for the picker ('' if nothing
    needs saying)."""
    tag = normalize(tag)
    if tag == HINDI:
        return 'Hindi and English, even mixed in one sentence. Written in English letters.'
    if tag == MULTILINGUAL_INDIA:
        return ('Hindi, Bangla, Tamil, Telugu and other Indian languages, '
                'mixed with English. Written in English letters.')
    if group_of(tag, status) == INDIA_GROUP:
        return 'Uses the Multilingual (India) model. Written in English letters.'
    return ''


def _recommended(status, system=None):
    supported = list((status or {}).get('supported') or ())
    if not supported:
        return None
    return (_match((status or {}).get('default_locale'), supported)
            or _match(system or system_locale(), supported))


def choices(status, current=None, system=None):
    """Rows for a language picker: ``{'code', 'name', 'installed', 'short',
    'note', 'group'}``. First the short list (``short`` True): the
    recommended locale for this Mac, Hindi and Multilingual (India), those
    this Mac supports, in that order; then everything else by name.
    ``current`` (a tag or a list of them) is always included, flagged
    installed=False if the helper does not list it."""
    supported = list((status or {}).get('supported') or ())
    installed = set(normalize(t).lower()
                    for t in (status or {}).get('installed') or ())

    def row(tag, short):
        return {'code': tag, 'name': short_name(tag, status),
                'installed': normalize(tag).lower() in installed,
                'short': short, 'note': describe(tag, status),
                'group': group_of(tag, status)}

    rows, seen = [], set()
    for want in (_recommended(status, system), HINDI, MULTILINGUAL_INDIA):
        tag = _match(want, supported) if want else None
        if tag and normalize(tag).lower() not in seen:
            seen.add(normalize(tag).lower())
            rows.append(row(tag, True))
    rest = []
    for tag in supported:
        key = normalize(tag).lower()
        if key in seen:
            continue
        seen.add(key)
        rest.append(row(tag, False))
    rest.sort(key=lambda r: r['name'].lower())
    rows.extend(rest)
    currents = [current] if isinstance(current, str) else list(current or ())
    for tag in currents:
        if isinstance(tag, str) and is_concrete(tag) and normalize(tag).lower() not in seen:
            seen.add(normalize(tag).lower())
            extra = row(normalize(tag), False)
            extra['installed'] = False
            rows.append(extra)
    return rows


def _as_list(languages):
    if isinstance(languages, str):
        return [languages]
    if isinstance(languages, (list, tuple)):
        return [x for x in languages if isinstance(x, str) and x.strip()]
    return []


def max_languages(status):
    value = (status or {}).get('max_languages')
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return 1
    return value


def resolve_languages(languages, language, status, system=None):
    """Pick the languages to dictate in: ``(list, known)``, primary first.

    ``languages`` is the stored list (None for a config from before it
    existed), ``language`` the stored single setting it replaces (which may
    be a legacy value such as 'auto'). The primary is resolved like
    ``resolve()``; each further entry is kept only when it is supported and
    not the same model as an earlier one, up to the helper's
    ``max_languages`` (and never more than two). Without a status
    ``known`` is False and the input comes back normalized, undecided.
    """
    wanted = _as_list(languages) or [language if isinstance(language, str) else 'auto']
    if not status or not status.get('supported'):
        out = []
        for tag in wanted:
            t = normalize(tag) or 'auto'
            if not any(t.lower() == o.lower() for o in out):
                out.append(t)
        return out[:2], False
    primary, _ = resolve(wanted[0], status, system)
    out = [primary]
    cap = min(2, max_languages(status))
    supported = list(status.get('supported') or ())
    installed = list(status.get('installed') or ())
    system = system or system_locale()
    for tag in wanted[1:]:
        if len(out) >= cap:
            break
        t = normalize(tag)
        if not t or t == 'auto':
            continue
        if '-' in t:
            picked = _match(t, supported)
        else:
            picked = speech.pick_locale(t, supported, installed, system)
        if not picked or any(same_model(picked, o, status) for o in out):
            continue
        out.append(picked)
    return out, True


def usable(languages, status):
    """``(ready, missing)``: the languages whose speech model is installed,
    and the ones that still need a download, each in the order given."""
    ready, missing = [], []
    for tag in _as_list(languages):
        (ready if is_installed(tag, status) else missing).append(tag)
    return ready, missing


def label(languages, status=None):
    """'English (India) + Hindi'."""
    return ' + '.join(short_name(t, status) for t in _as_list(languages))


def suggest_addition(languages, status, system=None):
    """'hi-IN' when this person would very likely want Hindi added to their
    one English language: the helper can run two, the Mac (or, when its
    region is unknown, the language) is set to India, and Hindi is offered.
    Else None."""
    langs = _as_list(languages)
    if max_languages(status) < 2 or len(langs) != 1:
        return None
    language, own_region = _parts(langs[0])
    if language != 'en':
        return None
    hindi = _match(HINDI, (status or {}).get('supported') or ())
    if not hindi:
        return None
    system = system or system_locale()
    region = _parts(system)[1] if system else ''
    if (region or own_region) != 'IN':
        return None
    return hindi


def default_languages(primary, status, system=None):
    """The languages a new setup starts with: ``[primary, 'hi-IN']`` for an
    English speaker in India whose Mac already has the India model, else
    ``[primary]``."""
    hindi = suggest_addition([primary], status, system)
    if hindi and is_installed(hindi, status):
        return [primary, hindi]
    return [primary]


def is_installed(locale, status):
    return bool(_match(locale, (status or {}).get('installed') or ()))


# Download sizes, measured on macOS 27.2 (the system does not report them):
# every English locale shares one asset, every non-English locale of India
# another. None = not measured.
_ENGLISH_MODEL_MB = 380
_INDIA_MODEL_MB = 620


def download_mb(languages, status):
    """About how many MB still have to be downloaded before ``languages`` (a
    tag or a list) can be used: 0 when everything is installed, None when it
    includes a model whose size is not known. A model shared by several
    locales is counted once."""
    total, seen, unknown = 0, set(), False
    for tag in _as_list(languages):
        if is_installed(tag, status):
            continue
        language, _ = _parts(tag)
        if group_of(tag, status) == INDIA_GROUP:
            key, size = INDIA_GROUP, _INDIA_MODEL_MB
        elif language == 'en':
            key, size = 'en', _ENGLISH_MODEL_MB
        else:
            key, size = normalize(tag).lower(), None
        if key in seen:
            continue
        seen.add(key)
        if size is None:
            unknown = True
        else:
            total += size
    return None if unknown else total


def download_note(languages, status):
    """'A one-time download of about 620 MB.' for what is still missing, ''
    when nothing is (or before the helper has answered)."""
    if not status or not usable(languages, status)[1]:
        return ''
    size = download_mb(languages, status)
    if size:
        return 'A one-time download of about %d MB.' % size
    return 'A one-time download.'


def _mode_key(languages):
    return tuple(normalize(t).lower() for t in _as_list(languages))


def presets(status, current=None, system=None):
    """The few one-click choices a simple language picker needs, best first:

      1. the language of this Mac alone            "English (India)"
      2. that language plus Hindi (when it is English, two languages are
         possible, and the Mac is in India or already has the India model)
                                                    "English (India) + Hindi"
      3. Hindi alone (same condition)               "Hindi"

    and, in front of them, what is in use now if it is none of these.

    Each row: ``{'id', 'languages', 'name', 'note', 'installed', 'missing',
    'download_mb', 'download_note', 'current'}``. ``id`` is the languages
    joined with '+' ('en-IN+hi-IN'); ``missing`` lists the ones whose model
    still has to be downloaded (``installed`` is True when there is none);
    ``current`` marks the row matching ``current`` (a tag or a list).
    Empty before the helper has answered."""
    supported = list((status or {}).get('supported') or ())
    if not supported:
        return []
    primary = (_recommended(status, system)
               or resolve('auto', status, system)[0])
    modes = [[primary]]
    hindi = _match(HINDI, supported)
    system = system or system_locale()
    region = (_parts(system)[1] if system else '') or _parts(primary)[1]
    if (hindi and not same_model(primary, hindi, status)
            and _parts(primary)[0] == 'en'
            and (region == 'IN' or is_installed(hindi, status))):
        if max_languages(status) >= 2:
            modes.append([primary, hindi])
        modes.append([hindi])
    now = [normalize(t) for t in _as_list(current) if is_concrete(t)]
    if now and _mode_key(now) not in [_mode_key(m) for m in modes]:
        modes.insert(0, now)

    def note(mode):
        india = [t for t in mode if group_of(t, status) == INDIA_GROUP]
        if len(mode) > 1 and india:
            return ('English and %s, even mixed in one sentence. %s is '
                    'written in English letters.'
                    % (short_name(india[0], status), short_name(india[0], status)))
        if len(mode) > 1:
            return 'Either language, sentence by sentence.'
        return describe(mode[0], status)

    rows = []
    for mode in modes:
        missing = usable(mode, status)[1]
        rows.append({
            'id': '+'.join(mode),
            'languages': list(mode),
            'name': label(mode, status),
            'note': note(mode),
            'installed': not missing,
            'missing': missing,
            'download_mb': download_mb(mode, status),
            'download_note': download_note(mode, status),
            'current': bool(now) and _mode_key(mode) == _mode_key(now),
        })
    return rows


def is_supported(locale, status):
    return bool(_match(locale, (status or {}).get('supported') or ()))
