"""Microphone capture.

The input stream is opened once and left running across dictations; capture is
gated by swapping a buffer in and out of ``_buf``. That is not a latency
optimisation — it is a correctness fix. Tearing a PortAudio stream down per
dictation can deadlock against the CoreAudio IO thread (``Pa_StopStream`` waits
for the IO thread inside ``AudioOutputUnitStop`` while PortAudio's
``startStopCallback`` on that thread waits for the AudioUnit lock the stopping
thread holds). When that happened the app wedged permanently: menu-bar icon
stuck on ⏳, hotkey dead, nothing in the log because it is a hang and not an
exception. Keeping the stream open removes that teardown from the dictation
path entirely — ``stop_and_save()`` now makes no PortAudio call at all.

THREADING CONTRACT: ``start()``, ``close()`` and ``stop_and_save()`` must only
ever be called from app.py's single 'audio-ctl' thread. Never call them from
the hotkey event-tap thread or the main thread — a blocking CoreAudio call on
either of those wedges the whole app.
"""
import logging
import os
import tempfile
import threading

import numpy as np
import sounddevice as sd
from scipy.io.wavfile import write as wav_write

log = logging.getLogger('freeflo.recorder')

SAMPLE_RATE = 16000
MIN_DURATION_SEC = 0.3
# Safety cap: a missed key-release event must not grow the buffer without bound.
# We keep what was captured up to the cap rather than discarding the recording.
MAX_DURATION_SEC = 300
_MAX_SAMPLES = int(SAMPLE_RATE * MAX_DURATION_SEC)

# A stream we couldn't tear down cleanly is abandoned: its CoreAudio resources
# leak and the mic indicator may stay lit. Opening yet another stream on top of a
# poisoned audio stack tends to fail or block, so after this many *consecutive*
# strikes we stop trying and the app asks the user to restart. The count resets
# as soon as a recording actually captures audio — that proves the stack is
# healthy again, and dropping a stream that died during sleep/wake is routine
# recovery, not evidence of a broken audio stack.
MAX_ABANDONS = 3

# Belt-and-braces only: every caller is supposed to be the same thread, so this
# lock should never actually contend. Bounded so a caller can never block long.
_LOCK_TIMEOUT = 2.0


class AudioUnavailable(Exception):
    """The microphone could not be opened, or the audio stack needs a restart."""


class Recorder:
    def __init__(self):
        # _buf is both the capture gate and the buffer: a list while capturing,
        # None otherwise. The audio callback only ever reads it into a local,
        # and a single attribute read/write is atomic under the GIL — so the
        # callback needs no lock. That matters: taking a lock in a realtime
        # audio callback invites priority inversion and dropouts.
        self._buf = None
        self._samples = 0
        self._truncated = False
        self._stream = None
        self._lock = threading.RLock()
        self._abandons = 0

    # ------------------------------------------------------------------
    # audio-ctl thread only
    # ------------------------------------------------------------------

    def start(self):
        """Open the stream if it isn't already live, then open the capture gate.
        Raises AudioUnavailable if the mic can't be opened."""
        if self._abandons >= MAX_ABANDONS:
            raise AudioUnavailable('audio stack needs an app restart')
        if not self._lock.acquire(timeout=_LOCK_TIMEOUT):
            raise AudioUnavailable('audio busy')
        try:
            self._ensure_stream()
            self._samples = 0
            self._truncated = False
            self._buf = []          # opens the gate — must be the last step
        finally:
            self._lock.release()

    def stop_and_save(self):
        """Close the capture gate and write the captured audio to a temp WAV.
        Returns its path, or None if there was nothing usable.

        Deliberately makes no PortAudio call, so this stays safe to run even if
        the audio stack is misbehaving."""
        buf, self._buf = self._buf, None      # closes the gate
        truncated, self._truncated = self._truncated, False
        self._samples = 0

        if not buf:
            return None
        # We captured audio, so the stream is demonstrably working: clear any
        # abandonment strikes from earlier device churn.
        self._abandons = 0
        if truncated:
            log.warning('Recording hit the %ds cap — saving what was captured',
                        MAX_DURATION_SEC)
        try:
            audio = np.concatenate(buf, axis=0)
        except ValueError:
            log.exception('Could not assemble the captured audio')
            return None

        if len(audio) < SAMPLE_RATE * MIN_DURATION_SEC:
            return None

        tmp = tempfile.NamedTemporaryFile(suffix='.wav', delete=False)
        tmp_path = tmp.name
        tmp.close()  # Close handle before wav_write opens the same path
        try:
            wav_write(tmp_path, SAMPLE_RATE, audio)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        return tmp_path

    def close(self):
        """Stop and close the stream, releasing the mic (and clearing the macOS
        mic indicator). This is the one remaining call that can block inside
        CoreAudio, which is why only audio-ctl may call it."""
        if not self._lock.acquire(timeout=_LOCK_TIMEOUT):
            log.warning('Skipping mic close — recorder lock is busy')
            return
        try:
            self._buf = None
            stream, self._stream = self._stream, None
            if stream is None:
                return
            try:
                stream.stop()
                stream.close()
            except Exception:
                self._abandons += 1
                log.exception('Closing the mic stream failed (abandoned %d/%d)',
                              self._abandons, MAX_ABANDONS)
            else:
                self._abandons = 0
        finally:
            self._lock.release()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _ensure_stream(self):
        stream = self._stream
        if stream is not None:
            try:
                if stream.active:
                    return
            except Exception:
                pass
            # The stream died under us (device change, sleep/wake). Let go of it
            # without touching PortAudio — a dead stream's stop() is exactly the
            # call that can hang — and build a fresh one. Routine after a wake,
            # so this only counts a strike; the count clears on the next capture.
            log.warning('Mic stream is no longer active — replacing it')
            self._stream = None
            self._abandons += 1

        try:
            stream = sd.InputStream(
                samplerate=SAMPLE_RATE,
                channels=1,
                dtype='int16',
                callback=self._callback,
            )
            stream.start()
        except Exception as e:
            log.exception('Could not open the microphone')
            raise AudioUnavailable(str(e) or 'microphone unavailable')
        self._stream = stream

    def _callback(self, indata, frames, time, status):
        # Realtime audio thread: no locks, no logging, no allocation beyond the
        # frame copy. Returns immediately when the gate is closed, which is what
        # guarantees nothing is retained between dictations.
        buf = self._buf
        if buf is None:
            return
        if self._samples >= _MAX_SAMPLES:
            self._truncated = True
            return
        self._samples += frames
        buf.append(indata.copy())
