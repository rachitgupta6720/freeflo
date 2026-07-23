"""Turbo mode: a local llama-server that polishes whisper transcripts.

Lifecycle: start(tier) when Turbo is enabled -> server holds the model in RAM ->
refine(text, style) per dictation -> stop() when Turbo is disabled or the app quits.

GOLDEN RULE: refine() must never raise and never block forever. On ANY problem it
returns the input text unchanged, so dictation still works."""
import subprocess
import time

import requests

import config
from engine import models

_HOST = '127.0.0.1'
_PORT = 8791                # loopback only — never exposed off the machine
_BASE = f'http://{_HOST}:{_PORT}'
_REQUEST_TIMEOUT = 8.0      # seconds; refine gives up and falls back after this
_MAX_TOKENS = 400           # cap output length so latency stays bounded

_proc = None                # the llama-server subprocess

PROMPTS = {
    'clean':   "You clean up dictated speech. Fix punctuation, capitalization, and remove "
               "filler words (um, uh, like, you know). Keep EVERY point the speaker made — "
               "do not summarize or add anything. Reply with ONLY the cleaned text.",
    'bullets': "Reorganize the dictated speech into a clear bullet-point list. Keep all the "
               "information. Reply with ONLY the bullet list.",
    'summary': "Summarize the dictated speech into its key points, concisely. "
               "Reply with ONLY the summary.",
    'email':   "Rewrite the dictated speech as a polished, professional short message. "
               "Reply with ONLY the message body.",
}
# Appended to every prompt so the model never switches languages.
_LANG_RULE = " Always reply in the SAME language as the input. Never translate."


def start(tier):
    """Launch llama-server for the given model tier. Returns True once healthy."""
    global _proc
    stop()  # ensure no old server is running
    model_path = config.get_turbo_model_path(tier)
    ctx = models.get(tier)['ctx']
    _proc = subprocess.Popen(
        [config.get_llama_server(),
         '-m', model_path,
         '--host', _HOST, '--port', str(_PORT),
         '-c', str(ctx),
         '-ngl', '999'],          # offload all layers to the Apple GPU (Metal) for speed
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    if not _wait_healthy(timeout=60):
        stop()
        return False
    _warmup()                     # first request is slow; prime it now
    return True


def stop():
    """Terminate the server and free its RAM. Safe to call anytime."""
    global _proc
    if _proc is not None:
        try:
            _proc.terminate()
            _proc.wait(timeout=5)
        except Exception:
            try:
                _proc.kill()
            except Exception:
                pass
        _proc = None


def is_ready():
    if _proc is None or _proc.poll() is not None:
        return False
    try:
        return requests.get(f'{_BASE}/health', timeout=1).status_code == 200
    except Exception:
        return False


def refine(text, style):
    """Polish `text`. Returns polished text, or the ORIGINAL text on any failure."""
    if not text or not is_ready():
        return text
    system = PROMPTS.get(style, PROMPTS['clean']) + _LANG_RULE
    try:
        resp = requests.post(
            f'{_BASE}/v1/chat/completions',
            json={
                'messages': [
                    {'role': 'system', 'content': system},
                    {'role': 'user', 'content': text},
                ],
                'temperature': 0.3,
                'max_tokens': _MAX_TOKENS,
            },
            timeout=_REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        out = resp.json()['choices'][0]['message']['content'].strip()
        out = _strip_wrappers(out)
        return out or text          # never return empty
    except Exception:
        return text                 # GOLDEN RULE: fall back to raw transcript


def _wait_healthy(timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _proc is not None and _proc.poll() is not None:
            return False            # process died while loading
        try:
            if requests.get(f'{_BASE}/health', timeout=1).status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def _warmup():
    try:
        requests.post(f'{_BASE}/v1/chat/completions',
                      json={'messages': [{'role': 'user', 'content': 'hi'}],
                            'max_tokens': 1}, timeout=_REQUEST_TIMEOUT)
    except Exception:
        pass


def _strip_wrappers(s):
    """Models sometimes wrap output in quotes or ```fences``` or add 'Sure, here...'.
    Strip the obvious ones."""
    s = s.strip().strip('`').strip()
    if s.lower().startswith(('sure,', 'here is', "here's", 'okay,')):
        # drop everything up to the first newline if it looks like a preamble
        parts = s.split('\n', 1)
        if len(parts) == 2:
            s = parts[1].strip()
    return s.strip('"').strip()
