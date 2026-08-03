import logging
import os
import subprocess

import config

log = logging.getLogger('freeflo.transcriber')

# Only whisper.cpp internal log line prefixes — no generic English words.
_LOG_PREFIXES = (
    'whisper_', 'ggml_', 'main:', 'system_info',
    'read_audio', 'log_', 'metal_',
)

# Transcription cost scales with recording length, so a flat timeout silently
# loses long dictations. Scale it, with a floor covering the fixed model-load
# cost and a ceiling so a bogus duration can't effectively disable the timeout.
_TIMEOUT_FLOOR = 30.0
_TIMEOUT_CEILING = 600.0
_TIMEOUT_PER_SECOND = 4.0


def timeout_for(duration=None):
    """Whisper timeout, in seconds, for a recording of `duration` seconds."""
    if not duration or duration <= 0:
        return _TIMEOUT_FLOOR
    return min(_TIMEOUT_CEILING,
               max(_TIMEOUT_FLOOR, _TIMEOUT_PER_SECOND * duration + 15.0))


def transcribe(wav_path, duration=None):
    """Run whisper-cli on wav_path and return the transcript string."""
    try:
        lang = config.load().get('language', 'en')
        cmd = [
            config.get_whisper_cli(),
            '-m', config.get_model_path(lang),
            '-f', wav_path,
            '--no-timestamps', '-nt',
        ]
        if lang != 'auto':
            cmd += ['-l', lang]

        result = subprocess.run(
            cmd,
            capture_output=True,
            # An explicit encoding is essential, not cosmetic: a frozen py2app
            # bundle runs under a C/ASCII locale, so bare text mode decodes
            # whisper's output as ASCII and raises UnicodeDecodeError on the
            # first non-ASCII byte — losing the entire dictation for any
            # non-Latin script (Devanagari starts 0xe0). engine/logs.py pins
            # utf-8 on its file handler for the same reason.
            encoding='utf-8',
            errors='replace',
            timeout=timeout_for(duration),
        )

        if result.returncode != 0:
            err = result.stderr.strip().splitlines()
            raise RuntimeError(err[-1] if err else f'whisper-cli exit {result.returncode}')

        # errors='replace' keeps a partly-undecodable transcript instead of
        # losing it outright, but whisper-cli emits utf-8, so this should never
        # trigger. Say so loudly rather than quietly pasting mojibake.
        if '�' in result.stdout:
            log.warning('Transcript contained undecodable bytes (replaced)')

        lines = result.stdout.splitlines()
        text_lines = [
            l.strip() for l in lines
            if l.strip()
            and not any(l.lstrip().startswith(p) for p in _LOG_PREFIXES)
            and not l.strip().startswith('[')
        ]
        return ' '.join(text_lines).strip()

    except subprocess.TimeoutExpired:
        raise RuntimeError('Transcription timed out')
    finally:
        try:
            os.unlink(wav_path)
        except OSError:
            pass
