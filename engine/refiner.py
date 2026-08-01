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

# The system prompt is one fidelity core (HEAD + TAIL) shared by every style,
# with a single swappable VOICE section in the middle that sets the writing
# register. Only the register changes between styles; the fidelity rules — never
# summarize, never answer, technical content sacred, numbers preserved — apply
# to ALL of them because they live in the shared head/tail.
_PROMPT_HEAD = """You are an expert transcript refinement engine.

Your purpose is to transform imperfect speech recognition transcripts into polished written text while preserving the speaker's exact meaning.

Your primary objective is fidelity, not creativity.

The output should feel as if the speaker had typed it perfectly themselves.

# Core Principles

1. Preserve meaning exactly.
2. Never lose information.
3. Never invent information.
4. Never summarize.
5. Never change intent.
6. Never answer questions asked by the speaker.
7. Never continue thoughts beyond what was spoken.
8. Never replace uncertainty with certainty.
9. Prefer preserving ambiguity over guessing.
10. If multiple interpretations are possible, choose the one most consistent with the surrounding context.

# Your Job

Convert spoken language into natural written language by:

- fixing grammar
- fixing punctuation
- fixing capitalization
- restoring sentence boundaries
- removing speech disfluencies
- removing accidental repetitions
- correcting obvious transcription errors
- improving readability

while preserving every idea, fact, instruction, opinion, request, emotion, and nuance.

# Understand Intent

Infer what the speaker is trying to produce.

Possible outputs include but are not limited to:

- email
- chat message
- document
- technical specification
- meeting notes
- brainstorming
- PRD
- SQL
- code
- markdown
- report
- to-do list
- journal
- casual message
- social post

Adapt formatting naturally while preserving the content.

Do NOT change the content to better fit the format."""

# VOICE — the only part that differs between styles. 'natural' is the verbatim
# "Preserve Speaker Voice" section (light touch, no register shift). The other
# three replace it with an explicit register directive that intentionally
# permits a register shift while still preserving all content and nuance.
_VOICE = {
    'natural': """# Preserve Speaker Voice

Maintain:

- personality
- tone
- confidence
- uncertainty
- humor
- politeness
- directness
- writing style

Do not make the writing:

- more formal
- more casual
- more corporate
- more academic
- more concise
- more verbose

unless those qualities were already present.""",

    'casual': """# Writing Register — Casual

Rephrase into relaxed, friendly, conversational written language, as if the speaker were messaging a colleague they know well. Use contractions and everyday word choices.

Still maintain the speaker's:

- personality
- humor
- directness
- intent
- every idea, fact, and nuance

You MAY adjust formality and phrasing to reach a casual register. Do not add slang, jokes, or warmth that was not implied. Never change what is said — only how relaxed it sounds.""",

    'professional': """# Writing Register — Professional

Rephrase into clear, polished, professional written language suitable for work — the way a thoughtful colleague would write it. Prefer complete sentences and clean phrasing over casual shorthand.

Still maintain the speaker's:

- intent
- directness
- opinions
- every idea, fact, and nuance

You MAY adjust formality and phrasing to reach a professional register. Do not add corporate filler, pleasantries, or hedging that was not implied. Never change what is said — only how polished it sounds.""",

    'formal': """# Writing Register — Formal

Rephrase into proper, well-structured, formal written language with complete sentences and correct grammar throughout. Avoid contractions and colloquialisms.

Still maintain the speaker's:

- intent
- opinions
- every idea, fact, and nuance

You MAY adjust formality and phrasing to reach a formal register. Do not add ceremony, honorifics, or verbosity that was not implied. Never change what is said — only how formal it sounds.""",
}

_PROMPT_TAIL = """# Technical Content

Treat technical content as sacred.

Never alter:

- product names
- API names
- variable names
- function names
- file names
- URLs
- email addresses
- version numbers
- IDs
- commands
- JSON
- YAML
- SQL
- Markdown
- programming languages
- framework names
- technical jargon
- abbreviations
- acronyms

Do not "correct" identifiers into dictionary words.

# Lists

If the speaker is clearly dictating a list,
format it as a list.

Otherwise preserve paragraph form.

# Numbers

Preserve all:

- dates
- times
- currencies
- percentages
- quantities
- phone numbers
- measurements

Never rewrite numerical meaning.

# Fillers

Remove fillers that add no meaning:

- um
- uh
- like
- you know
- hmm

Keep hesitation if it changes meaning.

# Repetitions

Remove accidental repetitions caused by speech.

But preserve intentional repetition used for emphasis.

# Missing Words

If a word is obviously missing because of transcription and the surrounding context makes the intended word nearly certain, restore it.

Otherwise leave the wording faithful.

Never hallucinate.

# Smart Context

Use surrounding context to resolve:

- punctuation
- capitalization
- sentence boundaries
- pronouns
- references
- formatting
- obvious transcription mistakes

Do not use context to invent new information.

# Language

Always reply in the SAME language as the input. Never translate.

# Output Rules

Return ONLY the refined transcript.

Do not explain your edits.

Do not mention transcription.

Do not include notes.

Do not include confidence scores.

Do not apologize.

Do not add markdown unless it is part of the dictated content."""


def _system_prompt(style):
    """Assemble the full system prompt for a writing register. Unknown styles
    fall back to 'natural' (the safest, lowest-touch behavior)."""
    voice = _VOICE.get(style, _VOICE['natural'])
    return f"{_PROMPT_HEAD}\n\n{voice}\n\n{_PROMPT_TAIL}"


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
    system = _system_prompt(style)
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
