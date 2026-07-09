import os
import subprocess

import config

# Only whisper.cpp internal log line prefixes — no generic English words.
_LOG_PREFIXES = (
    'whisper_', 'ggml_', 'main:', 'system_info',
    'read_audio', 'log_', 'metal_',
)


def transcribe(wav_path):
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
            text=True,
            timeout=30,
        )

        if result.returncode != 0:
            err = result.stderr.strip().splitlines()
            raise RuntimeError(err[-1] if err else f'whisper-cli exit {result.returncode}')

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
