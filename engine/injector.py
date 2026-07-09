import subprocess
import time
import pyperclip

# Timing. Pasting via the clipboard is inherently a small race: we put text on
# the clipboard, tell the frontmost app to Cmd+V, then restore the user's
# previous clipboard. If we restore too soon the app pastes the *old* value (or
# nothing) — the "nothing gets filled" bug. These waits are deliberately
# generous; injection latency is dwarfed by transcription time anyway.
_CLIP_SETTLE = 0.12    # let the clipboard settle before we paste
_PASTE_SETTLE = 0.45   # let Cmd+V complete before we restore the old clipboard
_COPY_RETRIES = 5


def _copy_and_confirm(text):
    """Put text on the clipboard and confirm it actually landed. Returns True
    on success. Some apps/races drop a single copy, so we retry."""
    for _ in range(_COPY_RETRIES):
        try:
            pyperclip.copy(text)
        except Exception:
            time.sleep(0.02)
            continue
        try:
            if pyperclip.paste() == text:
                return True
        except Exception:
            pass
        time.sleep(0.02)
    return False


def inject(text):
    """Paste text at the cursor, then restore the user's original clipboard.

    We paste via the clipboard because it works in any app (and handles
    Unicode/Devanagari that keystroke injection cannot). We must not leave the
    dictated text on the clipboard afterwards — the user never copied it, so a
    later Cmd+V should paste whatever they had before.
    """
    if not text:
        return

    try:
        previous = pyperclip.paste()
    except Exception:
        previous = None

    # Make sure the dictated text is really on the clipboard before we paste,
    # otherwise Cmd+V may fire against a stale/empty clipboard → nothing fills.
    if not _copy_and_confirm(text):
        # Last resort: try a plain copy and paste anyway.
        try:
            pyperclip.copy(text)
        except Exception:
            return

    time.sleep(_CLIP_SETTLE)
    subprocess.run(
        ['osascript', '-e',
         'tell application "System Events" to keystroke "v" using {command down}'],
        capture_output=True,
    )

    # Wait for the paste to complete before restoring. Only restore if the
    # clipboard still holds *our* text — if the user copied something else in
    # the meantime, leave their new clipboard alone.
    time.sleep(_PASTE_SETTLE)
    if previous is None:
        return
    try:
        if pyperclip.paste() == text:
            pyperclip.copy(previous)
    except Exception:
        pass
