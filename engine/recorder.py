import os
import tempfile

import numpy as np
import sounddevice as sd
from scipy.io.wavfile import write as wav_write

SAMPLE_RATE = 16000
MIN_DURATION_SEC = 0.3


class Recorder:
    def __init__(self):
        self._frames = []
        self._stream = None

    def start(self):
        self._frames = []
        self._stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype='int16',
            callback=self._callback,
        )
        self._stream.start()

    def _callback(self, indata, frames, time, status):
        self._frames.append(indata.copy())

    def stop_and_save(self):
        """Stop recording, write to a temp WAV file, return its path or None."""
        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None

        if not self._frames:
            return None

        audio = np.concatenate(self._frames, axis=0)

        if len(audio) < SAMPLE_RATE * MIN_DURATION_SEC:
            return None

        tmp = tempfile.NamedTemporaryFile(suffix='.wav', delete=False)
        tmp_path = tmp.name
        tmp.close()  # Close handle before wav_write opens the same path
        wav_write(tmp_path, SAMPLE_RATE, audio)
        return tmp_path
