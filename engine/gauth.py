"""Google sign-in for the optional Drive backup feature.

Uses the RFC 8252 loopback flow (InstalledAppFlow.run_local_server): a system
browser opens Google's consent screen and a short-lived local HTTP server
catches the redirect. Only the refresh token is persisted, in the macOS
Keychain — never in a plain file — and only the narrow `drive.appdata` scope
is requested for storage, which grants access to nothing in the user's Drive
except a hidden, app-private folder freeflo creates itself.

FREEFLO_GOOGLE_CLIENT_ID / FREEFLO_GOOGLE_CLIENT_SECRET come from a Google
Cloud OAuth "Desktop app" client that the freeflo maintainer registers once;
each user still signs in individually and only ever grants access to their
own account. See README.md for how to create one.
"""
import os

import keyring
import keyring.errors
import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    'https://www.googleapis.com/auth/drive.appdata',
    'openid',
    'https://www.googleapis.com/auth/userinfo.email',
]

_KEYRING_SERVICE = 'freeflo-google-backup'
_KEYRING_ACCOUNT = 'refresh_token'

_AUTH_URI = 'https://accounts.google.com/o/oauth2/auth'
_TOKEN_URI = 'https://oauth2.googleapis.com/token'
_USERINFO_URI = 'https://www.googleapis.com/oauth2/v3/userinfo'


class NotConfigured(Exception):
    """Raised when this build has no Google OAuth client baked in."""


def _client_id():
    return os.environ.get('FREEFLO_GOOGLE_CLIENT_ID', '')


def _client_secret():
    return os.environ.get('FREEFLO_GOOGLE_CLIENT_SECRET', '')


def is_configured():
    return bool(_client_id() and _client_secret())


def is_connected():
    return keyring.get_password(_KEYRING_SERVICE, _KEYRING_ACCOUNT) is not None


def connect():
    """Run the sign-in flow. Blocks the calling thread until the browser
    redirect lands (or the user closes the tab / it times out). Returns the
    signed-in account's email on success."""
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
    creds = flow.run_local_server(port=0, open_browser=True)
    keyring.set_password(_KEYRING_SERVICE, _KEYRING_ACCOUNT, creds.refresh_token)
    return _fetch_email(creds)


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


def _fetch_email(creds):
    resp = requests.get(
        _USERINFO_URI,
        headers={'Authorization': f'Bearer {creds.token}'},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json().get('email', '')
