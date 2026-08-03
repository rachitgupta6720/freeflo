"""Google sign-in for the optional Drive backup feature.

Uses the RFC 8252 loopback flow (InstalledAppFlow.run_local_server): a system
browser opens Google's consent screen and a short-lived local HTTP server
catches the redirect. Only the refresh token is persisted, in the macOS
Keychain — never in a plain file — and only the narrow `drive.appdata` scope
is requested for storage, which grants access to nothing in the user's Drive
except a hidden, app-private folder freeflo creates itself.

The OAuth "Desktop app" client comes from config.get_google_client() — either
the FREEFLO_GOOGLE_CLIENT_ID / FREEFLO_GOOGLE_CLIENT_SECRET environment
variables (run-from-source) or a google_client.json bundled into the .app at
build time. The freeflo maintainer registers the client once; each user still
signs in individually and only ever grants access to their own account. See
README.md for how to create one.
"""
import config

import keyring
import keyring.errors
import requests
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

# In a packaged .app, keyring can't discover its backend through entry-point
# metadata (py2app doesn't bundle it), so it would fail to find the Keychain
# and raise NoKeyringError at runtime. Pin the macOS backend explicitly; this
# is a no-op when running from source.
try:
    import keyring.backends.macOS
    keyring.set_keyring(keyring.backends.macOS.Keyring())
except Exception:
    pass

SCOPES = [
    'https://www.googleapis.com/auth/drive.appdata',
    'openid',
    'https://www.googleapis.com/auth/userinfo.email',
    # profile → the signed-in user's display name, captured at onboarding for
    # identity. No extra access to Drive/content; just who they are.
    'https://www.googleapis.com/auth/userinfo.profile',
]

_KEYRING_SERVICE = 'freeflo-google-backup'
_KEYRING_ACCOUNT = 'refresh_token'

_AUTH_URI = 'https://accounts.google.com/o/oauth2/auth'
_TOKEN_URI = 'https://oauth2.googleapis.com/token'
_USERINFO_URI = 'https://www.googleapis.com/oauth2/v3/userinfo'

# OAuth error codes that no amount of retrying can fix — the stored refresh
# token (or the client itself) is dead and only a fresh sign-in will help.
# Everything else, notably network errors and 5xx, is transient and must stay
# retryable: misclassifying one of those would silently stop backing up.
_TERMINAL_AUTH_ERRORS = frozenset({
    'invalid_grant',        # refresh token expired or revoked by the user
    'invalid_client',       # client credentials no longer valid
    'unauthorized_client',
    'invalid_request',      # malformed token request (e.g. no client id baked in)
})


class NotConfigured(Exception):
    """Raised when this build has no Google OAuth client baked in."""


def _client_id():
    return config.get_google_client()[0]


def _client_secret():
    return config.get_google_client()[1]


def is_configured():
    return bool(_client_id() and _client_secret())


def is_connected():
    return keyring.get_password(_KEYRING_SERVICE, _KEYRING_ACCOUNT) is not None


def is_auth_expired(exc):
    """True if `exc` means the stored credentials are dead and only a fresh
    sign-in can fix it. Callers use this to stop retrying — so it must stay
    strict: a transient failure classified as terminal would quietly disable
    backup until the user noticed."""
    if not isinstance(exc, RefreshError):
        return False
    # google-auth raises RefreshError(message, error_dict); prefer the structured
    # code over the message, which is not a stable contract.
    for arg in getattr(exc, 'args', ()):
        if isinstance(arg, dict) and 'error' in arg:
            return arg['error'] in _TERMINAL_AUTH_ERRORS
    text = str(exc)
    return any(code in text for code in _TERMINAL_AUTH_ERRORS)


def connect():
    """Run the sign-in flow. Returns the signed-in account's email (used by the
    backup feature, whose callers expect a plain email string)."""
    return connect_full().get('email', '')


def connect_full():
    """Run the sign-in flow and return the account identity as a dict
    ``{email, name, picture}``. Blocks the calling thread until the browser
    redirect lands (or the user closes the tab / it times out)."""
    if not is_configured():
        raise NotConfigured(
            'This build of freeflo has no Google Backup credentials configured.'
        )
    client_config = {
        'installed': {
            'client_id': _client_id(),
            'client_secret': _client_secret(),
            'auth_uri': _AUTH_URI,
            'token_uri': _TOKEN_URI,
        }
    }
    flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
    # timeout_seconds bounds the wait: if the user abandons the browser tab the
    # local server stops instead of leaving the thread and socket alive forever.
    creds = flow.run_local_server(port=0, open_browser=True, timeout_seconds=300)
    keyring.set_password(_KEYRING_SERVICE, _KEYRING_ACCOUNT, creds.refresh_token)
    return _fetch_userinfo(creds)


def get_identity():
    """Best-effort {email, name, picture} for the already-connected account, or
    None. Used to rehydrate the profile without a fresh sign-in."""
    creds = get_credentials()
    if creds is None:
        return None
    try:
        return _fetch_userinfo(creds)
    except Exception:
        return None


def disconnect():
    """Revoke local access to this account. Does not touch the remote backup
    file — see engine.backup.delete_remote() for that."""
    try:
        keyring.delete_password(_KEYRING_SERVICE, _KEYRING_ACCOUNT)
    except keyring.errors.PasswordDeleteError:
        pass


def get_credentials():
    """Fresh, refreshed Credentials built from the stored refresh token, or
    None if not connected. Always refreshes rather than also caching an
    access token — simpler, and cheap given how infrequently sync runs."""
    refresh_token = keyring.get_password(_KEYRING_SERVICE, _KEYRING_ACCOUNT)
    if not refresh_token:
        return None
    creds = Credentials(
        None,
        refresh_token=refresh_token,
        token_uri=_TOKEN_URI,
        client_id=_client_id(),
        client_secret=_client_secret(),
        scopes=SCOPES,
    )
    creds.refresh(Request())
    return creds


def _fetch_userinfo(creds):
    resp = requests.get(
        _USERINFO_URI,
        headers={'Authorization': f'Bearer {creds.token}'},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    return {
        'email': data.get('email', ''),
        'name': data.get('name', ''),
        'picture': data.get('picture', ''),
    }
