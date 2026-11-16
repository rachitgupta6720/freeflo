#!/usr/bin/env python3
"""Protocol smoke test for freeflo-helper.

    scripts/build_helper.sh && python3 helper/tests/smoke_test.py

Drives `freeflo-helper serve` over its JSON-lines protocol and checks the
behaviour the app relies on. It never touches the real microphone: audio comes
from a clip synthesised with macOS `say` and played through the helper's
FREEFLO_FAKE_MIC test hook, so it needs no permission and no quiet room.
Every helper runs with FREEFLO_NO_RESERVE=1: the per-app table of reserved
speech assets is shared with any installed copy of freeflo, and tests must
not change it. Downloads are simulated (FREEFLO_TEST_INSTALL); nothing is
ever downloaded.

The two-language checks need the India speech model (hi-IN) installed and
the `say` voices Rishi and Lekha; they are skipped with a note otherwise.

Options:
    --helper PATH   binary to test (default: build/freeflo-helper, or $FREEFLO_HELPER)
    --clip PATH     use this audio file instead of synthesising one
    --slow          also run the "CoreAudio hangs" scenarios (adds ~12 s)
    --mic           also try one real-microphone dictation (may show a
                    permission prompt; denial or a pending prompt is reported,
                    not failed)
    -v              print every event

Python 3.9+, standard library only. Exit code 0 when everything passed.
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import wave

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SPOKEN = 'Please move the meeting to Thursday afternoon and remind the team to update the dashboard.'
EXPECT_WORDS = ('meeting', 'thursday', 'afternoon', 'dashboard')
TERMINAL = ('final', 'cancelled', 'error')

VERBOSE = False
FAILURES = []
CHECKS = 0


def check(ok, what, detail=''):
    global CHECKS
    CHECKS += 1
    if ok:
        print('  ok    %s' % what)
    else:
        FAILURES.append(what)
        print('  FAIL  %s%s' % (what, (' -- ' + str(detail)) if detail != '' else ''))
    return ok


class Helper(object):
    """A running `freeflo-helper serve` with a background event reader."""

    def __init__(self, binary, env=None):
        full_env = dict(os.environ)
        full_env['FREEFLO_NO_RESERVE'] = '1'
        for key in ('FREEFLO_TEST_INSTALL', 'FREEFLO_INSTALL_STALL_SEC'):
            full_env.pop(key, None)
        full_env.update(env or {})
        self.t0 = time.time()
        self.proc = subprocess.Popen(
            [binary, 'serve'], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, env=full_env, bufsize=0)
        self.events = []
        self.garbage = []
        self.cond = threading.Condition()
        self.reader = threading.Thread(target=self._read)
        self.reader.daemon = True
        self.reader.start()

    def _read(self):
        for raw in self.proc.stdout:
            line = raw.decode('utf-8', 'replace').rstrip('\n')
            try:
                event = json.loads(line)
                if not isinstance(event, dict) or 'event' not in event:
                    raise ValueError('no event field')
            except ValueError:
                self.garbage.append(line)
                continue
            event['_t'] = time.time() - self.t0
            if VERBOSE and event['event'] != 'level':
                print('        <- %s' % line[:200])
            with self.cond:
                self.events.append(event)
                self.cond.notify_all()
        with self.cond:
            self.events.append({'event': '_eof', '_t': time.time() - self.t0})
            self.cond.notify_all()

    def send(self, command):
        line = command if isinstance(command, str) else json.dumps(command)
        if VERBOSE:
            print('        -> %s' % line)
        self.proc.stdin.write((line + '\n').encode('utf-8'))
        self.proc.stdin.flush()
        return time.time() - self.t0

    def wait(self, predicate, timeout=15.0, start=0):
        """First event at index >= start matching predicate, or None on timeout."""
        deadline = time.time() + timeout
        index = start
        with self.cond:
            while True:
                while index < len(self.events):
                    if predicate(self.events[index]):
                        return self.events[index]
                    index += 1
                remaining = deadline - time.time()
                if remaining <= 0:
                    return None
                self.cond.wait(remaining)

    def event(self, name, sid=None, timeout=15.0, start=0):
        return self.wait(lambda e: e['event'] == name and (sid is None or e.get('id') == sid), timeout, start)

    def terminal(self, sid, timeout=20.0):
        return self.wait(lambda e: e['event'] in TERMINAL and e.get('id') == sid, timeout)

    def mark(self):
        with self.cond:
            return len(self.events)

    def of(self, sid):
        with self.cond:
            return [e for e in self.events if e.get('id') == sid]

    def close_stdin_and_wait(self, timeout=5.0):
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        return self.wait_exit(timeout)

    def wait_exit(self, timeout=5.0):
        try:
            return self.proc.wait(timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            return None


def make_clip(directory):
    path = os.path.join(directory, 'clip.aiff')
    try:
        subprocess.run(['say', '-o', path, SPOKEN], check=True, timeout=60,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    return path if os.path.exists(path) and os.path.getsize(path) > 1000 else None


def clip_seconds(path):
    try:
        out = subprocess.run(['afinfo', path], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=20).stdout.decode()
        for line in out.splitlines():
            if 'estimated duration' in line:
                return float(line.split(':')[1].split()[0])
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        pass
    return 6.0


def has_words(text):
    low = text.lower()
    return all(w in low for w in EXPECT_WORDS)


# ---------------------------------------------------------------------------

def test_cli(binary, clip, locale):
    print('command line')
    out = subprocess.run([binary, '--version'], stdout=subprocess.PIPE, timeout=10)
    check(out.returncode == 0 and out.stdout.decode().startswith('freeflo-helper '), '--version prints the version')
    out = subprocess.run([binary, 'nonsense'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
    check(out.returncode == 64, 'unknown subcommand exits 64', out.returncode)
    if clip:
        out = subprocess.run([binary, 'transcribe-file', clip, '--locale', locale, '--context', 'dashboard,Thursday'],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=60)
        try:
            result = json.loads(out.stdout.decode())
        except ValueError:
            result = {}
        check(out.returncode == 0 and has_words(result.get('text', '')), 'transcribe-file returns the spoken text', out.stdout[:200])
        check(result.get('audio_sec', 0) > 1 and result.get('elapsed_ms', 0) > 0, 'transcribe-file reports audio_sec and elapsed_ms', result)
        print('        (%.1f s of audio transcribed in %.0f ms)' % (result.get('audio_sec', 0), result.get('elapsed_ms', 0)))
    out = subprocess.run([binary, 'transcribe-file', '/nonexistent/file.wav'], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30)
    try:
        result = json.loads(out.stdout.decode())
    except ValueError:
        result = {}
    check(out.returncode == 1 and 'error' in result, 'transcribe-file on a missing file prints an error object and exits 1', out.stdout[:200])


def test_basics(binary):
    """ready / ping / status / prewarm / bad commands / unknown ids / EOF. Returns the locale to use."""
    print('protocol basics')
    h = Helper(binary, {'FREEFLO_FAKE_MIC': '@none'})
    ready = h.event('ready', timeout=10)
    check(ready is not None and ready.get('version') and ready.get('os'), 'ready event with version and os', ready)

    h.send({'cmd': 'ping', 'seq': 41})
    pong = h.event('pong', timeout=5)
    check(pong is not None and pong.get('seq') == 41, 'ping -> pong echoes seq', pong)

    h.send({'cmd': 'status'})
    status = h.event('status', timeout=10) or {}
    check(status.get('mic') in ('granted', 'denied', 'undetermined'), 'status.mic is one of granted|denied|undetermined', status.get('mic'))
    check(status.get('speech') in ('granted', 'denied', 'undetermined', 'not_required'), 'status.speech is valid', status.get('speech'))
    supported = status.get('supported') or []
    installed = status.get('installed') or []
    check(len(supported) > 0 and all('-' in x for x in supported), 'status.supported lists BCP-47 locales (%d)' % len(supported))
    check(set(installed) <= set(supported), 'status.installed is a subset of supported (%d installed)' % len(installed))
    groups = status.get('groups')
    check(isinstance(groups, list) and all(isinstance(g, list) and len(g) >= 2 for g in groups),
          'status.groups lists locales that share a model', groups)
    check(status.get('max_languages') == 2, 'status.max_languages is 2', status.get('max_languages'))
    if any('hi-IN' in g for g in groups or ()):
        india = [g for g in groups if 'hi-IN' in g][0]
        check('mul-IN' in india and not any(x.startswith('en-') for x in india),
              'hi-IN and mul-IN are one model, without English (%d locales)' % len(india), india)
    locale = status.get('default_locale')
    if locale not in installed:
        locale = 'en-US' if 'en-US' in installed else (installed[0] if installed else None)
    if not check(locale is not None, 'at least one speech locale is installed'):
        h.close_stdin_and_wait()
        return None, status
    print('        (using locale %s)' % locale)

    mark = h.mark()
    h.send({'cmd': 'prewarm', 'locale': locale})
    check(h.event('prewarmed', timeout=40, start=mark) is not None, 'prewarm -> prewarmed')

    mark = h.mark()
    h.send({'cmd': 'prewarm', 'locale': 'zz-ZZ'})
    err = h.wait(lambda e: e['event'] == 'error', 10, mark)
    check(err is not None and err.get('code') == 'locale_unsupported', 'prewarm of an unknown locale -> locale_unsupported', err)

    missing = [x for x in supported if x not in installed]
    if missing:
        mark = h.mark()
        h.send({'cmd': 'start', 'id': 900, 'locale': missing[0]})
        err = h.terminal(900, 10)
        check(err is not None and err.get('code') == 'locale_not_installed',
              'start on supported-but-not-installed %s -> locale_not_installed' % missing[0], err)

    for bad, why in (('this is not json', 'garbage'), ('[1, 2]', 'a JSON array'),
                     ('{"cmd": "frobnicate"}', 'an unknown command'), ('{"id": 3}', 'a missing cmd'),
                     ('{"cmd": "start"}', 'start without id')):
        mark = h.mark()
        h.send(bad)
        err = h.wait(lambda e: e['event'] == 'error', 5, mark)
        check(err is not None and err.get('code') == 'bad_command', '%s -> bad_command' % why, err)

    mark = h.mark()
    h.send({'cmd': 'stop', 'id': 12345})
    h.send({'cmd': 'cancel', 'id': 12345})
    h.send({'cmd': 'ping', 'seq': 42})
    h.event('pong', timeout=5, start=mark)
    with h.cond:
        between = [e['event'] for e in h.events[mark:]]
    check(between == ['pong'], 'stop/cancel of unknown ids are ignored silently', between)

    mark = h.mark()
    h.send({'cmd': 'start', 'id': 901, 'locale': locale})
    err = h.terminal(901, 10)
    check(err is not None and err.get('code') == 'no_input_device', 'start with no input device -> no_input_device', err)

    mark = h.mark()
    h.send({'cmd': 'prewarm', 'locale': locale})
    pre = h.event('prewarmed', timeout=40, start=mark) or {}
    check(pre.get('resolved') == locale and pre.get('locale') == locale and 'languages' not in pre,
          'prewarmed.resolved is the plain locale string', pre)

    code = h.close_stdin_and_wait()
    check(code == 0, 'helper exits 0 when stdin closes', code)
    check(not h.garbage, 'every stdout line was a JSON event', h.garbage[:2])
    return locale, status


def test_dictation(binary, clip, locale):
    print('dictation (fake microphone)')
    seconds = clip_seconds(clip)
    h = Helper(binary, {'FREEFLO_FAKE_MIC': clip})
    h.event('ready', timeout=10)
    h.send({'cmd': 'prewarm', 'locale': locale})
    h.event('prewarmed', timeout=40)

    # A full dictation.
    sent = h.send({'cmd': 'start', 'id': 1, 'locale': locale, 'context': ['dashboard'], 'partials': True})
    started = h.event('started', 1, timeout=10)
    check(started is not None, 'start -> started')
    if started:
        print('        (start -> started: %.0f ms)' % ((started['_t'] - sent) * 1000))
    time.sleep(seconds + 0.6)
    sent = h.send({'cmd': 'stop', 'id': 1})
    final = h.terminal(1, 20) or {}
    check(final.get('event') == 'final' and has_words(final.get('text', '')), 'stop -> final with the spoken text', final)
    check(abs(final.get('audio_sec', 0) - (seconds + 0.6)) < 0.6, 'final.audio_sec matches the recording length', final.get('audio_sec'))
    check(0 < final.get('elapsed_ms', -1) < 3000, 'final.elapsed_ms is sane', final.get('elapsed_ms'))
    if final.get('event') == 'final':
        print('        (stop -> final: %.0f ms for %.1f s of audio)' % (final['elapsed_ms'], final['audio_sec']))
    events = h.of(1)
    names = [e['event'] for e in events]
    levels = [e for e in events if e['event'] == 'level']
    span = (levels[-1]['_t'] - levels[0]['_t']) if len(levels) > 1 else 0
    rate = (len(levels) - 1) / span if span > 0 else 0
    check(12 <= rate <= 25, 'level events arrive at about 15-20 per second (%.1f/s)' % rate)
    check(all(0.0 <= e['rms'] <= 1.0 for e in levels) and max([e['rms'] for e in levels] or [0]) > 0.2,
          'level rms stays within 0..1 and rises with speech')
    check('partial' in names, 'partial events were sent')
    check(names and names[0] == 'started' and names[-1] == 'final', 'started comes first and final last for the session', names[:1] + names[-1:])

    # Released before anything was said.
    h.send({'cmd': 'start', 'id': 2, 'locale': locale})
    h.send({'cmd': 'stop', 'id': 2})
    final = h.terminal(2, 10) or {}
    check(final.get('event') == 'final' and final.get('text') == '', 'immediate stop -> final with empty text', final)

    # Start while active cancels the old session.
    h.send({'cmd': 'start', 'id': 3, 'locale': locale})
    h.event('started', 3, timeout=10)
    h.send({'cmd': 'start', 'id': 4, 'locale': locale})
    old = h.terminal(3, 10) or {}
    check(old.get('event') == 'cancelled', 'start while active -> cancelled for the old id', old)
    check(h.event('started', 4, timeout=10) is not None, '...and started for the new id')

    # Cancel.
    sent = h.send({'cmd': 'cancel', 'id': 4})
    cancelled = h.terminal(4, 10) or {}
    check(cancelled.get('event') == 'cancelled', 'cancel -> cancelled', cancelled)

    # Stop, then start again at once: the first transcript must still arrive.
    h.send({'cmd': 'start', 'id': 5, 'locale': locale})
    h.event('started', 5, timeout=10)
    time.sleep(1.5)
    h.send({'cmd': 'stop', 'id': 5})
    h.send({'cmd': 'start', 'id': 6, 'locale': locale})
    first = h.terminal(5, 20) or {}
    check(first.get('event') == 'final', 'stop then immediate start: the first session still gets its final', first)
    check(h.event('started', 6, timeout=10) is not None, '...and the second session starts')
    time.sleep(0.5)
    h.send({'cmd': 'stop', 'id': 6})
    check((h.terminal(6, 20) or {}).get('event') == 'final', '...and finishes')

    # No stray events for finished sessions.
    time.sleep(0.3)
    stray = False
    for sid in (1, 2, 3, 4, 5, 6):
        evs = [e['event'] for e in h.of(sid)]
        ends = [i for i, n in enumerate(evs) if n in TERMINAL]
        if len(ends) != 1 or ends[0] != len(evs) - 1:
            stray = True
    check(not stray, 'each session has exactly one terminal event and nothing after it')

    # quit while recording.
    h.send({'cmd': 'start', 'id': 7, 'locale': locale})
    h.event('started', 7, timeout=10)
    h.send({'cmd': 'quit'})
    code = h.wait_exit(5)
    check(code == 0, 'quit while recording exits 0', code)
    check(not h.garbage, 'every stdout line was a JSON event', h.garbage[:2])


def test_mic_lost(binary, clip, locale):
    print('microphone lost mid-dictation (simulated)')
    lose_after = max(1.5, clip_seconds(clip) - 0.5)
    h = Helper(binary, {'FREEFLO_FAKE_MIC': clip, 'FREEFLO_TEST_LOSE_MIC': '%.1f' % lose_after})
    h.event('ready', timeout=10)
    h.send({'cmd': 'start', 'id': 1, 'locale': locale})
    h.event('started', 1, timeout=10)
    final = h.terminal(1, lose_after + 8) or {}
    check(final.get('event') == 'final' and final.get('mic_lost') is True and len(final.get('text', '')) > 5,
          'the session ends by itself with the text captured so far (mic_lost)', final)
    mark = h.mark()
    h.send({'cmd': 'stop', 'id': 1})
    h.send({'cmd': 'ping', 'seq': 9})
    h.event('pong', timeout=5, start=mark)
    with h.cond:
        after = [e['event'] for e in h.events[mark:]]
    check(after == ['pong'], 'a late stop for that session is ignored', after)
    check(h.close_stdin_and_wait() == 0, 'exits 0 on EOF')


def test_denied(binary, locale):
    print('microphone denied (simulated)')
    h = Helper(binary, {'FREEFLO_FAKE_MIC': '@denied'})
    h.event('ready', timeout=10)
    h.send({'cmd': 'status'})
    status = h.event('status', timeout=10) or {}
    check(status.get('mic') == 'denied', 'status.mic reports denied', status.get('mic'))
    h.send({'cmd': 'start', 'id': 1, 'locale': locale})
    err = h.terminal(1, 10) or {}
    check(err.get('code') == 'mic_denied', 'start -> mic_denied', err)
    h.send({'cmd': 'ping', 'seq': 1})
    check(h.event('pong', timeout=5) is not None, 'helper keeps serving afterwards')
    check(h.close_stdin_and_wait() == 0, 'exits 0 on EOF')


def test_wedged(binary, clip, locale):
    print('CoreAudio not answering (simulated, slow)')
    h = Helper(binary, {'FREEFLO_FAKE_MIC': '@hang'})
    h.event('ready', timeout=10)
    h.send({'cmd': 'start', 'id': 1, 'locale': locale})
    h.send({'cmd': 'ping', 'seq': 7})
    check(h.event('pong', timeout=3) is not None, 'ping is answered while the mic open is stuck')
    err = h.terminal(1, 15) or {}
    check(err.get('code') == 'engine_failed', 'stuck mic open -> engine_failed', err)
    code = h.wait_exit(5)
    check(code == 75, 'helper exits 75 so the app respawns it', code)

    h = Helper(binary, {'FREEFLO_FAKE_MIC': '@hang-stop', 'FREEFLO_FAKE_MIC_FILE': clip})
    h.event('ready', timeout=10)
    h.send({'cmd': 'start', 'id': 1, 'locale': locale})
    h.event('started', 1, timeout=10)
    time.sleep(clip_seconds(clip) + 0.5)
    sent = h.send({'cmd': 'stop', 'id': 1})
    final = h.terminal(1, 15) or {}
    check(final.get('event') == 'final' and has_words(final.get('text', '')), 'stuck mic close: the transcript is still delivered', final)
    if final:
        check(final['_t'] - sent < 2.5, '...within about a second (%.0f ms)' % ((final['_t'] - sent) * 1000))
    code = h.wait_exit(10)
    check(code == 75, '...and then the helper exits 75', code)


def test_install_events(binary, locale, supported):
    """Simulated downloads (FREEFLO_TEST_INSTALL): event order, heartbeat,
    errors, stall detection. Plus the real already-installed fast path."""
    print('language downloads (simulated)')
    target = 'fr-FR' if 'fr-FR' in supported else supported[0]

    h = Helper(binary, {'FREEFLO_FAKE_MIC': '@none', 'FREEFLO_TEST_INSTALL': 'ok:2'})
    h.event('ready', timeout=10)
    sent = h.send({'cmd': 'install', 'locale': target})
    h.send({'cmd': 'install', 'locale': target})       # a duplicate joins the first
    other = target.replace('-', '_').lower()
    h.send({'cmd': 'install', 'locale': other})        # so does another spelling
    first = h.event('install_progress', timeout=5)
    check(first is not None and first.get('fraction') == 0 and first['_t'] - sent < 0.5,
          'first install_progress (fraction 0) arrives at once (%.0f ms)' % (((first or {}).get('_t', 99) - sent) * 1000), first)
    # Both spellings finish at the same moment and their order varies, so
    # wait for the answer to the canonical spelling, not the first one.
    done = h.wait(lambda e: e['event'] == 'error' or (e['event'] == 'installed' and e.get('locale') == target), 10) or {}
    with h.cond:
        progress = [e for e in h.events if e['event'] == 'install_progress']
    fractions = [e.get('fraction') for e in progress]
    # The simulated fraction is sampled about every 0.25 s of a 2 s ramp, so
    # the last sample before 'installed' can be anywhere from ~0.87 up.
    check(len(progress) >= 2 and fractions == sorted(fractions) and fractions[-1] > 0.8,
          'install_progress rises to ~1 (%d events)' % len(progress), fractions[:3] + fractions[-2:])
    check(done.get('event') == 'installed' and done.get('already') is False and done.get('locale') == target
          and done.get('resolved') == target and target in (done.get('installed') or []),
          'installed: already false, resolved, and the fresh installed list includes it', {k: done.get(k) for k in ('event', 'already', 'resolved', 'code')})
    time.sleep(0.5)
    with h.cond:
        done_for = [e.get('locale') for e in h.events if e['event'] == 'installed']
        spelled = [e for e in h.events if e['event'] == 'install_progress' and e.get('locale') == other]
    check(sorted(done_for) == sorted([target, other]),
          'duplicate requests join the running download; every spelling gets installed once', done_for)
    check(len(spelled) >= 1, '...and progress under its own spelling', len(spelled))
    mark = h.mark()
    h.send({'cmd': 'install', 'locale': 'zz-ZZ'})
    err = h.wait(lambda e: e['event'] == 'error', 10, mark) or {}
    check(err.get('code') == 'locale_unsupported' and err.get('cmd') == 'install' and err.get('locale') == 'zz-ZZ',
          'install of an unknown locale -> locale_unsupported (cmd, locale echoed)', err)
    h.close_stdin_and_wait()

    h = Helper(binary, {'FREEFLO_FAKE_MIC': '@none', 'FREEFLO_TEST_INSTALL': 'fail:network'})
    h.event('ready', timeout=10)
    h.send({'cmd': 'install', 'locale': target})
    err = h.wait(lambda e: e['event'] in ('error', 'installed'), 10) or {}
    check(err.get('event') == 'error' and err.get('code') == 'network' and err.get('cmd') == 'install'
          and err.get('locale') == target, 'a failed download -> error network (cmd install, locale echoed)', err)
    h.close_stdin_and_wait()

    h = Helper(binary, {'FREEFLO_FAKE_MIC': '@none', 'FREEFLO_TEST_INSTALL': 'stall',
                        'FREEFLO_INSTALL_STALL_SEC': '2'})
    h.event('ready', timeout=10)
    sent = h.send({'cmd': 'install', 'locale': target})
    err = h.wait(lambda e: e['event'] in ('error', 'installed'), 10) or {}
    took = err.get('_t', 99) - sent
    with h.cond:
        beats = len([e for e in h.events if e['event'] == 'install_progress'])
    check(err.get('code') == 'network' and took < 5.0,
          'a download with no progress is cancelled -> network within 5 s (%.1f s)' % took, err)
    check(beats >= 2, 'a stalled download still heartbeats (%d progress events)' % beats)
    h.close_stdin_and_wait()

    h = Helper(binary, {'FREEFLO_FAKE_MIC': '@none'})
    h.event('ready', timeout=10)
    sent = h.send({'cmd': 'install', 'locale': locale})
    done = h.wait(lambda e: e['event'] in ('installed', 'error'), 15) or {}
    with h.cond:
        progress = [e for e in h.events if e['event'] == 'install_progress']
    check(done.get('event') == 'installed' and done.get('already') is True and not progress
          and done.get('resolved') == locale and locale in (done.get('installed') or []),
          'install of an already installed locale -> installed, already true, no progress (%.0f ms)'
          % ((done.get('_t', 0) - sent) * 1000), done)

    # The same for the India model, when this Mac has it: one asset serves
    # hi-IN and mul-IN, so both must answer at once and list each other.
    h.send({'cmd': 'status'})
    have = (h.event('status', timeout=10) or {}).get('installed') or []
    for india in ('hi-IN', 'mul-IN'):
        if india not in have:
            print('        (%s is not installed here: its fast path is not checked)' % india)
            continue
        mark = h.mark()
        sent = h.send({'cmd': 'install', 'locale': india})
        done = h.wait(lambda e: e['event'] in ('installed', 'error') and e.get('locale') == india, 15, mark) or {}
        listed = done.get('installed') or []
        check(done.get('event') == 'installed' and done.get('already') is True
              and 'hi-IN' in listed and 'mul-IN' in listed,
              'install %s (already here) -> installed at once, list has hi-IN and mul-IN (%.0f ms)'
              % (india, (done.get('_t', 0) - sent) * 1000), done)
    h.close_stdin_and_wait()


LANG_EN = 'Please send me the quarterly report by Friday evening, and remind the design team about the review at three.'
LANG_HI = 'आज शाम को मैं बाज़ार जाऊँगा और सब्ज़ी लेकर आऊँगा। क्या तुम्हें कुछ चाहिए?'
LANG_MIX = ('Okay team, quick update on the release.',
            'कल रात को server डाउन हो गया था इसलिए deployment रुक गया',
            'We will retry tonight after the database migration.')


def say_wav(directory, name, voice, text):
    path = os.path.join(directory, name + '.wav')
    try:
        subprocess.run(['say', '-v', voice, '--data-format=LEI16@16000', '-o', path, text], check=True,
                       timeout=60, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    return path if os.path.exists(path) and os.path.getsize(path) > 1000 else None


def silence_wav(directory, seconds):
    path = os.path.join(directory, 'silence.wav')
    w = wave.open(path, 'wb')
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(16000)
    w.writeframes(b'\x00\x00' * int(16000 * seconds))
    w.close()
    return path


def wav_seconds(paths):
    total = 0.0
    for p in paths:
        w = wave.open(p)
        total += w.getnframes() / float(w.getframerate())
        w.close()
    return total


def dictate(binary, clip_paths, start_cmd, hold=0.6, partials=None):
    """One fake-mic dictation; returns (started-or-error event, terminal event).
    ``partials`` (a list) collects the running texts in order."""
    h = Helper(binary, {'FREEFLO_FAKE_MIC': ':'.join(clip_paths)})
    h.event('ready', timeout=10)
    prewarm = dict(start_cmd)
    prewarm.pop('id', None)
    prewarm['cmd'] = 'prewarm'
    h.send(prewarm)
    h.wait(lambda e: e['event'] in ('prewarmed', 'error'), 40)
    h.send(start_cmd)
    first = h.wait(lambda e: e.get('id') == start_cmd['id'] and e['event'] in ('started', 'error'), 15) or {}
    final = first
    if first.get('event') == 'started':
        time.sleep(wav_seconds(clip_paths) + hold)
        h.send({'cmd': 'stop', 'id': start_cmd['id']})
        final = h.terminal(start_cmd['id'], 20) or {}
    if partials is not None:
        partials.extend(e.get('text', '') for e in h.of(start_cmd['id']) if e['event'] == 'partial')
    h.close_stdin_and_wait()
    return first, final


def test_languages(binary, installed, tmp):
    print('two languages (en-IN + the India model)')
    if 'hi-IN' not in installed or 'en-IN' not in installed:
        print('note: skipped - needs en-IN and hi-IN installed (installed: %s)' % ', '.join(installed))
        return
    en = say_wav(tmp, 'lang_en', 'Rishi', LANG_EN)
    hi = say_wav(tmp, 'lang_hi', 'Lekha', LANG_HI)
    m1 = say_wav(tmp, 'mix1', 'Rishi', LANG_MIX[0])
    m2 = say_wav(tmp, 'mix2', 'Lekha', LANG_MIX[1])
    m3 = say_wav(tmp, 'mix3', 'Rishi', LANG_MIX[2])
    if not all((en, hi, m1, m2, m3)):
        print('note: skipped - the say voices Rishi and Lekha are needed')
        return
    gap = silence_wav(tmp, 0.35)
    mix = [m1, gap, m2, gap, m3]
    both = ['en-IN', 'hi-IN']

    _, solo = dictate(binary, [en], {'cmd': 'start', 'id': 1, 'locale': 'en-IN'})
    started, final = dictate(binary, [en], {'cmd': 'start', 'id': 1, 'locale': 'en-IN', 'languages': both})
    en_final = final
    check(started.get('languages') == both and started.get('locale') == 'en-IN', 'started.languages lists both', started)
    check(final.get('event') == 'final' and final.get('text') == solo.get('text') and final.get('used') == ['en-IN'],
          'English: same text as en-IN alone, used == [en-IN]', (final.get('text'), solo.get('text'), final.get('used')))
    check(0 < final.get('elapsed_ms', 9999) < 1000, 'English: stop -> final %.0f ms (< 1000)' % final.get('elapsed_ms', -1))

    running = []
    _, final = dictate(binary, [hi], {'cmd': 'start', 'id': 2, 'locale': 'en-IN', 'languages': both}, partials=running)
    hi_final = final
    text = final.get('text', '')
    check(final.get('event') == 'final' and text and all(ord(c) < 128 for c in text) and 'bazar' in text.lower()
          and final.get('dominant') == 'hi-IN', 'Hindi: written in English letters, dominant hi-IN', (text, final.get('dominant')))
    # en-IN writes Hindi as "Aaj, Shamku make Bazar, Janga"; the India model
    # writes "aaj Shaam ko mei bazar": lower case, no comma after every word.
    check(text[:1].isupper() and text[-1:] in '.?!' and ' Shaam' not in text and ' shaam' in text.lower(),
          'Hindi: sentence case, closing mark, no stray capital on a known Hindi word', text)
    last = running[-1] if running else ''
    check(bool(running) and 'bazar' in last and ', ' not in last,
          'Hindi: the running text follows the India model (%d partials)' % len(running), last)
    check(0 < final.get('elapsed_ms', 9999) < 1000, 'Hindi: stop -> final %.0f ms (< 1000)' % final.get('elapsed_ms', -1))

    # --explain hands out everything `blend` needs to replay the decision.
    env = dict(os.environ, FREEFLO_NO_RESERVE='1')
    out = subprocess.run([binary, 'transcribe-file', hi, '--languages', 'en-IN,hi-IN', '--explain'],
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=60, env=env)
    try:
        explained = json.loads(out.stdout.decode())
    except ValueError:
        explained = {}
    replay = {}
    if explained.get('primary_segments') is not None and explained.get('secondary_segments') is not None:
        body = {'primary': explained['primary_segments'], 'secondary': explained['secondary_segments'],
                'secondary_locale': 'hi-IN'}
        out = subprocess.run([binary, 'blend'], input=json.dumps(body).encode('utf-8'),
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=20)
        try:
            replay = json.loads(out.stdout.decode())
        except ValueError:
            replay = {}
    check(bool(explained.get('decisions')) and replay.get('text') == explained.get('text')
          and replay.get('decisions') == explained.get('decisions'),
          'transcribe-file --explain: segments replayed through blend give the same text and decisions',
          (explained.get('text'), replay.get('text')))

    _, final = dictate(binary, mix, {'cmd': 'start', 'id': 3, 'locale': 'en-IN', 'languages': both})
    text = final.get('text', '')
    check(final.get('event') == 'final' and 'release.' in text and 'server down' in text.lower()
          and 'database migration.' in text and final.get('used') == both,
          'mixed: English sentences punctuated, the Hindi one in between, used both', (text, final.get('used')))
    check(0 < final.get('elapsed_ms', 9999) < 1000, 'mixed: stop -> final %.0f ms (< 1000)' % final.get('elapsed_ms', -1))
    for name, event in (('EN', en_final), ('HI', hi_final), ('MIX', final)):
        print('        (%s, %.0f ms: %r)' % (name, event.get('elapsed_ms', -1), event.get('text')))

    started, final = dictate(binary, [hi], {'cmd': 'start', 'id': 4, 'locale': 'hi-IN', 'languages': ['hi-IN', 'mul-IN']})
    check(started.get('event') == 'started' and 'languages' not in started and 'missing' not in started,
          'hi-IN + mul-IN is one model: one recogniser, nothing missing', started)

    started, final = dictate(binary, [en], {'cmd': 'start', 'id': 5, 'locale': 'en-IN', 'languages': ['en-IN', 'zz-ZZ']})
    check(started.get('event') == 'started' and started.get('missing') == ['zz-ZZ'] and 'languages' not in started,
          'an unusable extra language is reported missing; the dictation still starts', started)

    started, _ = dictate(binary, [en], {'cmd': 'start', 'id': 6, 'locale': 'zz-ZZ', 'languages': ['zz-ZZ']})
    check(started.get('event') == 'error' and started.get('code') == 'locale_unsupported',
          'no usable language at all -> locale_unsupported', started)

    started, final = dictate(binary, mix, {'cmd': 'start', 'id': 7, 'locale': 'hi-IN', 'languages': ['hi-IN', 'en-IN']})
    check(started.get('languages') == both and final.get('used') == both,
          'India model listed first: en-IN still becomes the primary', (started.get('languages'), final.get('used')))


def test_real_mic(binary, locale):
    print('real microphone (informational)')
    h = Helper(binary)
    h.event('ready', timeout=10)
    h.send({'cmd': 'status'})
    status = h.event('status', timeout=10) or {}
    print('        mic permission: %s, input device: %s' % (status.get('mic'), status.get('input_device')))
    sent = h.send({'cmd': 'start', 'id': 1, 'locale': locale})
    first = h.wait(lambda e: e.get('id') == 1 and e['event'] in ('started', 'error'), 12)
    if first is None:
        print('        no answer in 12 s: a permission prompt is probably waiting; cancelling')
        h.send({'cmd': 'cancel', 'id': 1})
        check((h.terminal(1, 5) or {}).get('event') == 'cancelled', 'cancel is answered while the prompt is pending')
    elif first['event'] == 'error':
        print('        start failed: %s (%s)' % (first.get('code'), first.get('message')))
        check(first.get('code') in ('mic_denied', 'no_input_device', 'engine_failed'), 'failure is reported with a protocol error code', first)
    else:
        print('        start -> started: %.0f ms' % ((first['_t'] - sent) * 1000))
        time.sleep(2.0)
        sent = h.send({'cmd': 'stop', 'id': 1})
        final = h.terminal(1, 15) or {}
        check(final.get('event') == 'final', 'real mic: stop -> final', final)
        if final.get('event') == 'final':
            print('        stop -> final: %.0f ms, text=%r%s' % ((final['_t'] - sent) * 1000, final.get('text'),
                                                               ' (input was digital silence)' if final.get('silent') else ''))
    check(h.close_stdin_and_wait() == 0, 'exits 0 on EOF')


def main():
    global VERBOSE
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--helper', default=os.environ.get('FREEFLO_HELPER', os.path.join(ROOT, 'build', 'freeflo-helper')))
    parser.add_argument('--clip')
    parser.add_argument('--slow', action='store_true')
    parser.add_argument('--mic', action='store_true')
    parser.add_argument('-v', dest='verbose', action='store_true')
    args = parser.parse_args()
    VERBOSE = args.verbose

    if not os.access(args.helper, os.X_OK):
        print('helper binary not found at %s (run scripts/build_helper.sh first)' % args.helper)
        return 2

    with tempfile.TemporaryDirectory(prefix='freeflo-helper-test-') as tmp:
        clip = args.clip or make_clip(tmp)
        if clip is None:
            print('note: could not synthesise a test clip with `say`; audio checks are skipped (pass --clip)')

        locale, status = test_basics(args.helper)
        if locale is None:
            print('\nno installed speech locale: cannot run the audio checks')
        else:
            # `say` speaks English; only check transcripts against an English model.
            if not locale.lower().startswith('en'):
                print('note: default locale %s is not English; transcript checks may fail' % locale)
            test_cli(args.helper, clip, locale)
            if clip:
                test_dictation(args.helper, clip, locale)
                test_mic_lost(args.helper, clip, locale)
            test_denied(args.helper, locale)
            test_install_events(args.helper, locale, status.get('supported') or [])
            test_languages(args.helper, status.get('installed') or [], tmp)
            if args.slow and clip:
                test_wedged(args.helper, clip, locale)
            if args.mic:
                test_real_mic(args.helper, locale)

    print('')
    if FAILURES:
        print('%d of %d checks FAILED:' % (len(FAILURES), CHECKS))
        for failure in FAILURES:
            print('  - ' + failure)
        return 1
    print('all %d checks passed' % CHECKS)
    return 0


if __name__ == '__main__':
    sys.exit(main())
