#!/usr/bin/env python3
"""A scriptable stand-in for ``freeflo-helper`` used by the test suite.

It speaks the real JSON-lines protocol on stdin/stdout, needs no microphone,
and can be told to misbehave so the tests can prove the supervisor recovers.

Control comes from the directory named by ``$FAKE_HELPER_DIR``:

  scenario.json   {"spawns": [ {...}, {...} ], "default": {...}}
                  The Nth process started uses ``spawns[N]``; later ones use
                  ``default``. Read once at startup, so a test can rewrite it
                  to change how the *next* spawn behaves.
  spawns.log      one JSON line per process started: {"spawn": N, "pid": P}
  commands.log    one JSON line per command received

Behaviour keys (all optional):

  ready_delay   seconds to wait before printing "ready"
  no_ready      never print "ready" (hung at launch)
  exit_at_start exit with this code before "ready" ("kill" = SIGKILL itself)
  hang          [cmd, ...]  swallow these commands and never answer them
  freeze        [cmd, ...]  on this command stop reading stdin for good
  crash         {cmd: code} die on this command ("kill" = SIGKILL itself)
  garbage       [cmd, ...]  answer this command with endless non-JSON
  flood         [cmd, ...]  answer with one endless line (never a newline)
  stray         [cmd, ...]  print ONE junk line, then answer normally
  delay         {cmd: sec}  block the command loop before answering
  final_delay   seconds between stop and final (command loop keeps running)
  text          transcript returned by "final"
  final_extra   dict merged into the "final" event (e.g. {"silent": true})
  partials      [text, ...] partials emitted right after "started"
  levels        false to emit no level events
  level_count   emit only this many level events (then the "audio" stalls)
  start_error   {"code": .., "message": ..} answer start with an error
  stop_error    {"code": .., "message": ..} answer stop with an error
  status        dict merged into the status event
  late_after_cancel  after "cancelled", keep emitting events for that id
  ignore_quit / ignore_eof   refuse to exit (so shutdown must SIGKILL)
  stderr_bytes  write this many bytes to stderr at startup
  split_writes  deliver every output line in two separate writes
  prewarm_error {"code": .., "message": ..} answer prewarm with an id-less error
  install_error {"code": .., "message": ..} answer install with an id-less error
                (either may add "tagged": true to also send the real helper's
                "locale" and "cmd" extras)
  async_start   seconds a session takes to exist: until then the helper does
                not know its id, so a stop/cancel for it is dropped as unknown
  started_extra dict merged into every "started" event (e.g. {"missing": [..]})
  install_steps    fractions reported by install_progress (default .25 .5 1)
  install_delay    seconds between those progress events (and before installed)
  install_already  answer install with installed (already: true), no progress
  installed_list   value of "installed" in the installed event (the real
                   helper's fresh list); absent by default, like helper 1.0

"start" and "prewarm" echo the command's "languages" (when it has two or
more) the way the real helper does when both are usable.

``transcribe-file <path> [--locale L] [--languages a,b] [--context w,w]
[--explain]`` prints one result line like the real helper's; with two
languages and --explain it adds decisions, primary_text / secondary_text and
primary_segments / secondary_segments.
"""
import json
import os
import signal
import sys
import threading
import time

_out_lock = threading.Lock()
_split = False


def emit(obj):
    data = (json.dumps(obj, ensure_ascii=False) + '\n').encode('utf-8')
    with _out_lock:
        try:
            if _split and len(data) > 4:
                mid = len(data) // 2
                sys.stdout.buffer.write(data[:mid])
                sys.stdout.buffer.flush()
                time.sleep(0.01)
                sys.stdout.buffer.write(data[mid:])
            else:
                sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()
        except (BrokenPipeError, ValueError):
            os._exit(0)


def _idless_error(spec, cmd):
    event = {'event': 'error', 'id': None, 'code': spec.get('code'),
             'message': spec.get('message', '')}
    if spec.get('tagged'):      # what the real helper adds
        event['locale'] = cmd.get('locale')
        event['cmd'] = cmd.get('cmd')
    return event


def raw(data):
    with _out_lock:
        try:
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()
        except (BrokenPipeError, ValueError):
            os._exit(0)


def die(code):
    if code == 'kill':
        os.kill(os.getpid(), signal.SIGKILL)
        time.sleep(5)
    os._exit(int(code))


def sleep_forever():
    while True:
        time.sleep(3600)


class Fake(object):
    def __init__(self, cfg, log_cmd):
        self.cfg = cfg
        self.log_cmd = log_cmd
        self.active = None              # id of the session with a "live mic"
        self.languages = None           # "languages" of the last start
        self.downloaded = []            # locales "installed" by this process
        self.lock = threading.Lock()

    # -- recording simulation ------------------------------------------
    def _level_loop(self, sid):
        left = self.cfg.get('level_count')
        while True:
            with self.lock:
                if self.active != sid:
                    return
            if left is not None:
                if left <= 0:
                    return
                left -= 1
            emit({'event': 'level', 'id': sid, 'rms': 0.25})
            time.sleep(0.02)

    def _late_loop(self, sid):
        for _ in range(5):
            time.sleep(0.03)
            emit({'event': 'level', 'id': sid, 'rms': 0.9})
            emit({'event': 'partial', 'id': sid, 'text': 'STALE'})
        emit({'event': 'final', 'id': sid, 'text': 'STALE FINAL',
              'audio_sec': 1.0, 'elapsed_ms': 1.0})

    def _final(self, sid, t0):
        delay = self.cfg.get('final_delay')
        if delay:
            time.sleep(delay)
        final = {'event': 'final', 'id': sid,
                 'text': self.cfg.get('text', 'hello world'),
                 'audio_sec': 1.5, 'elapsed_ms': (time.time() - t0) * 1000.0}
        final.update(self.cfg.get('final_extra', {}))
        emit(final)

    def _begin(self, sid, delay):
        if delay:
            time.sleep(delay)
        with self.lock:
            self.active = sid
            languages = self.languages
        started = {'event': 'started', 'id': sid}
        if languages:
            started['locale'] = languages[0]
            started['languages'] = languages
        started.update(self.cfg.get('started_extra', {}))
        emit(started)
        for text in self.cfg.get('partials', ()):
            emit({'event': 'partial', 'id': sid, 'text': text})
        if self.cfg.get('levels', True):
            threading.Thread(target=self._level_loop, args=(sid,),
                             daemon=True).start()

    def _install(self, cmd):
        cfg = self.cfg
        locale = cmd.get('locale')
        done = {'event': 'installed', 'locale': locale, 'resolved': locale,
                'already': bool(cfg.get('install_already'))}
        if 'installed_list' in cfg:
            done['installed'] = cfg['installed_list']
        if not cfg.get('install_already'):
            delay = cfg.get('install_delay', 0)
            for frac in cfg.get('install_steps', (0.25, 0.5, 1.0)):
                if delay:
                    time.sleep(delay)
                emit({'event': 'install_progress', 'locale': locale,
                      'fraction': frac})
            if delay:
                time.sleep(delay)
        if locale and locale not in self.downloaded:
            self.downloaded.append(locale)  # later statuses list it, as for real
        emit(done)

    # -- commands ------------------------------------------------------
    def handle(self, cmd):
        cfg = self.cfg
        name = cmd.get('cmd')
        self.log_cmd(cmd)

        if name in cfg.get('freeze', ()):
            sleep_forever()
        if name in cfg.get('crash', {}):
            die(cfg['crash'][name])
        if name in cfg.get('garbage', ()):
            while True:
                raw(b'\x00\xff this is not json {{{\n')
                time.sleep(0.005)
        if name in cfg.get('flood', ()):
            while True:
                raw(b'A' * 4096)            # one endless line, no newline
        if name in cfg.get('hang', ()):
            return
        if name in cfg.get('stray', ()):
            raw(b'some framework printed this\n')
        if name in cfg.get('delay', {}):
            time.sleep(cfg['delay'][name])

        if name == 'ping':
            emit({'event': 'pong', 'seq': cmd.get('seq')})
        elif name == 'status':
            status = {'event': 'status', 'mic': 'granted',
                      'speech': 'not_required',
                      'supported': ['en-US', 'en-IN', 'hi-IN'],
                      'installed': ['en-US', 'en-IN']}
            status.update(cfg.get('status', {}))
            extra = [t for t in self.downloaded if t not in status['installed']]
            if extra:
                status['installed'] = list(status['installed']) + extra
            emit(status)
        elif name == 'prewarm':
            if cfg.get('prewarm_error'):
                emit(_idless_error(cfg['prewarm_error'], cmd))
            else:
                event = {'event': 'prewarmed', 'locale': cmd.get('locale'),
                         'resolved': cmd.get('locale')}
                if len(cmd.get('languages') or ()) >= 2:
                    event['languages'] = cmd['languages'][:2]
                emit(event)
        elif name == 'install' and cfg.get('install_error'):
            emit(_idless_error(cfg['install_error'], cmd))
        elif name == 'install' and cfg.get('install_delay'):
            threading.Thread(target=self._install, args=(cmd,),
                             daemon=True).start()
        elif name == 'install':
            self._install(cmd)
        elif name == 'request_permissions':
            emit({'event': 'status', 'mic': 'granted',
                  'speech': 'not_required', 'supported': [], 'installed': []})
        elif name == 'start':
            sid = cmd.get('id')
            with self.lock:
                old, self.active = self.active, None
                langs = cmd.get('languages') or ()
                self.languages = list(langs[:2]) if len(langs) >= 2 else None
            if old is not None:             # protocol: implicit cancel
                emit({'event': 'cancelled', 'id': old})
            if cfg.get('start_error'):
                err = cfg['start_error']
                emit({'event': 'error', 'id': sid, 'code': err.get('code'),
                      'message': err.get('message', '')})
                return
            if cfg.get('async_start'):
                threading.Thread(target=self._begin, daemon=True,
                                 args=(sid, cfg['async_start'])).start()
            else:
                self._begin(sid, 0)
        elif name == 'stop':
            sid = cmd.get('id')
            with self.lock:
                if self.active != sid:
                    return                  # unknown id: ignored
                self.active = None
            if cfg.get('stop_error'):
                err = cfg['stop_error']
                emit({'event': 'error', 'id': sid, 'code': err.get('code'),
                      'message': err.get('message', '')})
                return
            threading.Thread(target=self._final, args=(sid, time.time()),
                             daemon=True).start()
        elif name == 'cancel':
            sid = cmd.get('id')
            with self.lock:
                if self.active != sid:
                    return
                self.active = None
            emit({'event': 'cancelled', 'id': sid})
            if cfg.get('late_after_cancel'):
                threading.Thread(target=self._late_loop, args=(sid,),
                                 daemon=True).start()
        elif name == 'quit':
            if not cfg.get('ignore_quit'):
                os._exit(0)
        else:
            emit({'event': 'error', 'id': None, 'code': 'bad_command',
                  'message': 'unknown command'})


def serve():
    global _split
    ctl = os.environ.get('FAKE_HELPER_DIR')
    cfg, index = {}, 0
    if ctl:
        spawn_log = os.path.join(ctl, 'spawns.log')
        try:
            with open(spawn_log) as f:
                index = sum(1 for _ in f)
        except OSError:
            index = 0
        with open(spawn_log, 'a') as f:
            f.write(json.dumps({'spawn': index, 'pid': os.getpid()}) + '\n')
        try:
            with open(os.path.join(ctl, 'scenario.json')) as f:
                scenario = json.load(f)
        except (OSError, ValueError):
            scenario = {}
        spawns = scenario.get('spawns') or []
        cfg = spawns[index] if index < len(spawns) else scenario.get('default', {})

    def log_cmd(cmd):
        if not ctl:
            return
        with open(os.path.join(ctl, 'commands.log'), 'a') as f:
            f.write(json.dumps({'spawn': index, 'cmd': cmd}) + '\n')

    _split = bool(cfg.get('split_writes'))

    if cfg.get('stderr_bytes'):
        chunk = b'fake helper diagnostic line\n'
        n = 0
        while n < cfg['stderr_bytes']:
            sys.stderr.buffer.write(chunk)
            n += len(chunk)
        sys.stderr.buffer.flush()
    if 'exit_at_start' in cfg:
        sys.stderr.write('fake helper: dying at start\n')
        sys.stderr.flush()
        die(cfg['exit_at_start'])
    if cfg.get('no_ready'):
        sleep_forever()
    if cfg.get('ready_delay'):
        time.sleep(cfg['ready_delay'])

    emit({'event': 'ready', 'version': 'fake-1.0', 'os': 'test'})
    fake = Fake(cfg, log_cmd)

    stdin = sys.stdin.buffer
    while True:
        line = stdin.readline()
        if not line:                        # parent closed the pipe / died
            if cfg.get('ignore_eof'):
                sleep_forever()
            os._exit(0)
        line = line.strip()
        if not line:
            continue
        try:
            cmd = json.loads(line.decode('utf-8'))
        except ValueError:
            emit({'event': 'error', 'id': None, 'code': 'bad_command',
                  'message': 'not json'})
            continue
        fake.handle(cmd)


def main(argv):
    if argv[:1] == ['--version']:
        print('fake-1.0')
        return 0
    if argv[:1] == ['transcribe-file']:
        if len(argv) < 2 or not os.path.exists(argv[1]):
            sys.stderr.write('no such file\n')
            return 1
        locale = 'en-US'
        context = []
        languages = None
        explain = False
        rest = argv[2:]
        while rest:
            if rest[0] == '--locale' and len(rest) > 1:
                locale = rest[1]
                rest = rest[2:]
            elif rest[0] == '--languages' and len(rest) > 1:
                languages = rest[1].split(',')
                rest = rest[2:]
            elif rest[0] == '--context' and len(rest) > 1:
                context = rest[1].split(',')
                rest = rest[2:]
            elif rest[0] == '--explain':
                explain = True
                rest = rest[1:]
            else:
                rest = rest[1:]
        result = {'text': 'fake transcript', 'audio_sec': 1.0,
                  'elapsed_ms': 2.0, 'locale': locale, 'context': context}
        if languages:
            result.update(locale=languages[0], languages=languages[:2],
                          used=languages[:1], dominant=languages[0],
                          sources='P')
            if explain and len(languages) >= 2:
                # What the real helper adds with --explain (helper 1.2).
                words = [['fake', 0.9, 0.0, 0.4], [' transcript', 0.9, 0.4, 1.0]]
                segment = {'text': 'fake transcript', 'start': 0.0, 'end': 1.0,
                           'words': words}
                result.update(
                    decisions=[{'start': 0.0, 'end': 1.0, 'words': 2,
                                'secondary_words': 2, 'primary': 0.9,
                                'secondary': 0.9, 'midcap': 0.0, 'agree': 1.0,
                                'hindi': False, 'rule': 'keep', 'used': 'P'}],
                    primary_text='fake transcript',
                    secondary_text='fake transcript',
                    primary_segments=[segment], secondary_segments=[segment])
        print(json.dumps(result))
        return 0
    if argv[:1] == ['serve']:
        serve()
        return 0
    sys.stderr.write('usage: fake_helper.py serve\n')
    return 2


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
