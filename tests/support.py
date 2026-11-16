"""Shared plumbing for the test suite (not a test module itself)."""
import json
import logging
import os
import shutil
import sys
import tempfile
import threading
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

FAKE_HELPER = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           'fake_helper.py')

from engine.speech import SpeechEngine  # noqa: E402

TERMINAL = ('final', 'cancelled', 'error')

# The suite provokes dozens of helper failures on purpose; keep their log
# lines out of the test output unless asked (FREEFLO_TEST_LOG=1).
if os.environ.get('FREEFLO_TEST_LOG'):
    logging.basicConfig(level=logging.DEBUG,
                        format='%(asctime)s %(threadName)s %(name)s: %(message)s')
else:
    logging.getLogger('freeflo').addHandler(logging.NullHandler())


class Events(object):
    """Collects every engine callback, with the thread it arrived on."""

    NAMES = ('started', 'level', 'partial', 'final', 'cancelled', 'error',
             'status', 'prewarmed', 'install_progress', 'installed', 'state',
             'final_info', 'install_failed')

    def __init__(self):
        self.items = []          # (name, args)
        self.threads = set()
        self._cond = threading.Condition()

    def attach(self, engine):
        for name in self.NAMES:
            setattr(engine, 'on_' + name, self._make(name))

    def _make(self, name):
        def cb(*args):
            with self._cond:
                self.items.append((name, args))
                self.threads.add(threading.current_thread().name)
                self._cond.notify_all()
        return cb

    def of(self, name):
        with self._cond:
            return [args for n, args in self.items if n == name]

    def for_session(self, sid, names=None):
        with self._cond:
            return [(n, args) for n, args in self.items
                    if args and args[0] == sid and n != 'state'
                    and n not in ('status', 'prewarmed', 'installed',
                                  'install_progress')
                    and (names is None or n in names)]

    def terminals(self, sid):
        return self.for_session(sid, TERMINAL)

    def wait(self, predicate, timeout=6.0):
        """Block until predicate(self) is true. Returns whether it became so."""
        end = time.monotonic() + timeout
        with self._cond:
            while True:
                if predicate(self):
                    return True
                left = end - time.monotonic()
                if left <= 0:
                    return False
                self._cond.wait(left)

    def wait_for(self, name, sid=None, timeout=6.0, count=1):
        def pred(ev):
            n = 0
            for item_name, args in ev.items:
                if item_name == name and (sid is None or (args and args[0] == sid)):
                    n += 1
            return n >= count
        return self.wait(pred, timeout)

    def wait_terminal(self, sid, timeout=6.0):
        return self.wait(
            lambda ev: any(n in TERMINAL and a and a[0] == sid
                           for n, a in ev.items), timeout)


class Harness(object):
    """A temp control directory + a fake helper + an engine wired to it."""

    FAST = dict(
        ready_timeout=3.0,
        start_timeout=1.0,
        stop_timeout_base=1.0,
        stop_timeout_per_audio_sec=0.0,
        cancel_timeout=0.7,
        ping_interval=None,
        ping_timeout=0.5,
        prewarm_timeout=1.0,
        level_stall_timeout=None,
        write_timeout=1.0,
        max_session_sec=None,
        backoff=(0.0, 0.1, 0.2, 0.4),
        min_respawn_interval=0.02,
        stable_after=30.0,
        quit_grace=0.5,
    )

    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix='freeflo-test-')
        self.engines = []
        self.scenario()

    def scenario(self, default=None, spawns=None):
        tmp = os.path.join(self.dir, 'scenario.json.tmp')
        with open(tmp, 'w') as f:
            json.dump({'default': default or {}, 'spawns': spawns or []}, f)
        os.replace(tmp, os.path.join(self.dir, 'scenario.json'))

    def engine(self, helper=None, **overrides):
        opts = dict(self.FAST)
        opts.update(overrides)
        env = dict(os.environ)
        env['FAKE_HELPER_DIR'] = self.dir
        if helper is None:
            helper = [sys.executable, FAKE_HELPER]
        eng = SpeechEngine(helper, env=env, **opts)
        events = Events()
        events.attach(eng)
        self.engines.append(eng)
        return eng, events

    def _lines(self, name):
        try:
            with open(os.path.join(self.dir, name)) as f:
                return [json.loads(line) for line in f if line.strip()]
        except OSError:
            return []

    def spawns(self):
        return self._lines('spawns.log')

    def pids(self):
        return [s['pid'] for s in self.spawns()]

    def commands(self, name=None):
        cmds = [c['cmd'] for c in self._lines('commands.log')]
        if name is not None:
            cmds = [c for c in cmds if c.get('cmd') == name]
        return cmds

    def wait_spawns(self, n, timeout=6.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if len(self.spawns()) >= n:
                return True
            time.sleep(0.01)
        return False

    def close(self):
        for eng in self.engines:
            eng.shutdown(timeout=3.0)
        dead = wait_all_dead(self.pids(), 3.0)
        shutil.rmtree(self.dir, ignore_errors=True)
        return dead


def pid_alive(pid):
    """True while ``pid`` exists (a reaped process no longer does)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def wait_dead(pid, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if not pid_alive(pid):
            return True
        time.sleep(0.01)
    return not pid_alive(pid)


def wait_all_dead(pids, timeout=3.0):
    end = time.monotonic() + timeout
    for pid in pids:
        if not wait_dead(pid, max(0.0, end - time.monotonic())):
            return False
    return True
