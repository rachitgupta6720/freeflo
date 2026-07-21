"""Microphone permission helpers for the onboarding flow.

Preferred path uses AVFoundation to *query* and *request* the mic authorization
cleanly. If the AVFoundation PyObjC framework isn't present, we degrade: status
becomes 'unknown' and a request is triggered by briefly opening the input device
(macOS shows its TCC prompt on first mic access), which is exactly how the app
already obtains the mic today.

Accessibility permission is handled directly in app.py via AXIsProcessTrusted*.
"""

_STATUS = {0: 'undetermined', 1: 'restricted', 2: 'denied', 3: 'authorized'}


def _av():
    from AVFoundation import AVCaptureDevice, AVMediaTypeAudio  # may not be installed
    return AVCaptureDevice, AVMediaTypeAudio


def mic_status():
    """'authorized' | 'denied' | 'restricted' | 'undetermined' | 'unknown'."""
    try:
        AVCaptureDevice, AVMediaTypeAudio = _av()
        return _STATUS.get(
            AVCaptureDevice.authorizationStatusForMediaType_(AVMediaTypeAudio),
            'unknown',
        )
    except Exception:
        return 'unknown'


def request_mic():
    """Trigger the macOS microphone prompt. Non-blocking; check mic_status()
    afterwards (it flips once the user responds)."""
    try:
        AVCaptureDevice, AVMediaTypeAudio = _av()
        AVCaptureDevice.requestAccessForMediaType_completionHandler_(
            AVMediaTypeAudio, lambda granted: None
        )
        return True
    except Exception:
        # Fallback: opening the input device forces the TCC prompt on first use.
        try:
            import sounddevice as sd
            with sd.InputStream(channels=1):
                pass
        except Exception:
            pass
        return False
