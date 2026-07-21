"""Opt-in, consent-gated telemetry.

Analytics + identity via PostHog; crash reporting via Sentry. Both are
agent-analyzable (each vendor ships an MCP server).

Two hard rules:
  1. Nothing is sent unless the user consented (checked live on every call, so
     turning a toggle off in Settings stops transmission immediately) AND this
     build has keys configured.
  2. Transcription text and audio are NEVER sent — only metadata and the
     profile the user chose to share at onboarding.

Keys come from config.get_telemetry_config() (env vars for dev, or a bundled
telemetry.json baked in at build time). With no keys, every function here is a
silent no-op, so the app ships and runs identically until keys are added.
"""
import atexit
import logging
import platform

import config
from engine import logs

log = logging.getLogger('freeflo.telemetry')

_ph = None          # posthog module once initialised
_sentry_on = False
_distinct_id = None


def _os():
    try:
        return 'macOS ' + platform.mac_ver()[0]
    except Exception:
        return 'macOS'


def _analytics_live():
    """PostHog is ready AND the user currently consents (re-read each call)."""
    return _ph is not None and config.load().get('analytics_enabled', True)


def init():
    """Set up SDKs from configured keys. Safe to call once at startup; never
    raises. SDKs are created whenever keys exist so a later consent-toggle works
    without re-init — actual sending is gated live by consent."""
    global _ph, _sentry_on, _distinct_id
    _distinct_id = config.get_or_create_install_id()
    keys = config.get_telemetry_config()

    if keys.get('posthog_key'):
        try:
            import posthog
            posthog.project_api_key = keys['posthog_key']
            posthog.host = keys.get('posthog_host') or 'https://us.i.posthog.com'
            _ph = posthog
            atexit.register(flush)
        except Exception as e:
            log.warning('PostHog init failed: %s', e)

    if keys.get('sentry_dsn'):
        try:
            import sentry_sdk
            sentry_sdk.init(
                dsn=keys['sentry_dsn'],
                release=f'freeflo@{config.get_version()}',
                traces_sample_rate=0.0,
                send_default_pii=False,
                before_send=_before_send,   # drop everything if consent is off
            )
            sentry_sdk.set_user({'id': _distinct_id})
            _sentry_on = True
            logs.add_exception_listener(_on_exception)
        except Exception as e:
            log.warning('Sentry init failed: %s', e)

    if _ph or _sentry_on:
        log.info('telemetry ready (analytics=%s crashes=%s)', bool(_ph), _sentry_on)
    identify()
    capture('app_launched')


def _before_send(event, hint):
    # Live consent gate for Sentry: no crash payloads leave if the user opted out.
    if not config.load().get('crash_enabled', True):
        return None
    return event


def _on_exception(exc_type, exc_value, exc_tb):
    if not _sentry_on:
        return
    try:
        import sentry_sdk
        sentry_sdk.capture_exception(exc_value)
    except Exception:
        pass


def identify():
    """Attach the anonymous install id to the onboarding profile (name/email/
    role/goal). No transcriptions — this is who the user told us they are."""
    if not _analytics_live():
        return
    s = config.load()
    try:
        _ph.identify(_distinct_id, {
            'app_version': config.get_version(),
            'os': _os(),
            'name': s.get('profile_name'),
            'email': s.get('profile_email'),
            'role': s.get('profile_role'),
            'goal': s.get('profile_goal'),
        })
    except Exception:
        pass


def capture(event, props=None):
    if not _analytics_live():
        return
    try:
        p = {'app_version': config.get_version(), 'os': _os()}
        if props:
            p.update(props)
        _ph.capture(_distinct_id, event, p)
    except Exception:
        pass


def flush():
    try:
        if _ph is not None:
            _ph.flush()
    except Exception:
        pass


# --- small helpers so callers never build raw payloads (keeps text out) ---

def _bucket(n, edges):
    for e in edges:
        if n <= e:
            return f'<={e}'
    return f'>{edges[-1]}'


def dictation_completed(mode, language, duration, char_count):
    capture('dictation_completed', {
        'mode': mode,
        'language': language,
        'duration_bucket': _bucket(duration or 0, [2, 5, 10, 30, 60]),
        'length_bucket': _bucket(char_count or 0, [20, 60, 140, 400]),
    })
