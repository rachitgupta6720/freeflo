"""Local logging + crash capture.

Writes rotating logs to ``~/Library/Logs/freeflo/`` and installs excepthooks
(uncaught exceptions on the main thread and on worker threads) plus
``faulthandler`` for native crashes — the segfault class of failure that comes
from PyObjC / whisper.cpp and that used to vanish without a trace.

Zero privacy cost: everything stays on disk. Phase-2 telemetry can subscribe to
uncaught exceptions via :func:`add_exception_listener` without this module ever
importing or depending on any network code.
"""
import os
import sys
import time
import platform
import logging
import logging.handlers
import threading
import faulthandler

import config

_LOG_DIR = os.path.expanduser('~/Library/Logs/freeflo')
_LOG_FILE = os.path.join(_LOG_DIR, 'freeflo.log')
_FAULT_FILE = os.path.join(_LOG_DIR, 'faults.log')

_log = logging.getLogger('freeflo')
_exception_listeners = []
_fault_fp = None  # kept open for the process lifetime so faulthandler can write


def log_dir():
    return _LOG_DIR


def log_file():
    return _LOG_FILE


def add_exception_listener(fn):
    """Register ``fn(exc_type, exc_value, exc_tb)`` to run on any uncaught
    exception (main or worker thread). Listener errors are swallowed so a broken
    listener can never mask the original crash. Used by telemetry later."""
    _exception_listeners.append(fn)


def setup_logging(level=logging.INFO):
    """Idempotent: configure the 'freeflo' logger with a rotating file handler
    (and a console handler when running from source)."""
    os.makedirs(_LOG_DIR, exist_ok=True)
    _log.setLevel(level)
    if _log.handlers:
        return _log
    fmt = logging.Formatter(
        '%(asctime)s %(levelname)s [%(threadName)s] %(name)s: %(message)s'
    )
    # encoding='utf-8' is essential: a frozen py2app bundle runs under a C/ASCII
    # locale, so without it any non-ASCII char (e.g. an em dash) raises
    # UnicodeEncodeError and silently drops the log line.
    fh = logging.handlers.RotatingFileHandler(
        _LOG_FILE, maxBytes=1_000_000, backupCount=3, encoding='utf-8'
    )
    fh.setFormatter(fmt)
    _log.addHandler(fh)
    if not getattr(sys, 'frozen', False):
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        _log.addHandler(sh)
    _log.info('freeflo %s starting — %s, python %s',
              config.get_version(), _os_string(), platform.python_version())
    return _log


def _notify_listeners(exc_type, exc_value, exc_tb):
    for fn in list(_exception_listeners):
        try:
            fn(exc_type, exc_value, exc_tb)
        except Exception:
            pass


def _handle_exception(exc_type, exc_value, exc_tb):
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_tb)
        return
    _log.critical('Uncaught exception', exc_info=(exc_type, exc_value, exc_tb))
    _notify_listeners(exc_type, exc_value, exc_tb)


def _handle_thread_exception(args):
    if issubclass(args.exc_type, SystemExit):
        return
    _log.critical('Uncaught exception in thread %s',
                  getattr(args.thread, 'name', '?'),
                  exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
    _notify_listeners(args.exc_type, args.exc_value, args.exc_traceback)


def install_excepthooks():
    """Route uncaught exceptions (main + threads) to the log and any listeners,
    and dump native fault tracebacks to ``faults.log``."""
    global _fault_fp
    sys.excepthook = _handle_exception
    try:
        threading.excepthook = _handle_thread_exception  # Python 3.8+
    except Exception:
        pass
    try:
        _fault_fp = open(_FAULT_FILE, 'a')
        _fault_fp.write('\n===== session %s (freeflo %s) =====\n' %
                        (time.strftime('%Y-%m-%d %H:%M:%S'), config.get_version()))
        _fault_fp.flush()
        faulthandler.enable(file=_fault_fp, all_threads=True)
    except Exception:
        pass


def _os_string():
    try:
        return 'macOS ' + platform.mac_ver()[0]
    except Exception:
        return sys.platform


def diagnostics(tail_lines=60):
    """A copy-pasteable diagnostics blob for bug reports: environment summary
    plus the tail of the log. No transcription text is ever stored in the log,
    so this is safe to share."""
    lines = [
        'freeflo diagnostics',
        '-------------------',
        f'version : {config.get_version()}',
        f'os      : {_os_string()}',
        f'python  : {platform.python_version()}',
        f'frozen  : {bool(getattr(sys, "frozen", False))}',
        f'whisper : {"found" if os.path.exists(config.get_whisper_cli()) else "MISSING"}',
        '',
        f'--- last {tail_lines} log lines ---',
    ]
    try:
        with open(_LOG_FILE) as f:
            tail = f.readlines()[-tail_lines:]
        lines.extend(line.rstrip() for line in tail)
    except Exception:
        lines.append('(no log file yet)')
    return '\n'.join(lines)
