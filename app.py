import os
import sys
import time
import logging
import threading
import subprocess
import rumps
from ApplicationServices import AXIsProcessTrusted, AXIsProcessTrustedWithOptions

import config as cfg
from engine.recorder import Recorder
from engine.transcriber import transcribe
from engine.injector import inject
from engine import history, gauth, backup, logs, updater, telemetry
from hotkey import HotkeyListener

log = logging.getLogger('freeflo.app')

_ICONS = {
    'idle':       '🎙',
    'recording':  '🔴',
    'processing': '⏳',
    'disabled':   '○',
}

_ACCESSIBILITY_URL = (
    'x-apple.systempreferences:'
    'com.apple.preference.security?Privacy_Accessibility'
)

# (name shown in menu, whisper language code)
# 'auto' removes the -l flag — whisper detects the language from audio.
# Hinglish (Hindi+English code-switching) has no dedicated whisper code;
# auto-detect gives the best results for mixed-language speech.
_LANGUAGES = [
    ('English',     'en'),
    ('Hindi',       'hi'),
    ('Hinglish',    'auto'),
    ('Spanish',     'es'),
    ('French',      'fr'),
    ('German',      'de'),
    ('Chinese',     'zh'),
    ('Japanese',    'ja'),
    ('Arabic',      'ar'),
    ('Portuguese',  'pt'),
    (None,          None),   # separator
    ('Auto-detect', 'auto'),
]


def _bundle_path():
    """Return the .app bundle path when frozen, else None."""
    if getattr(sys, 'frozen', False):
        # sys.executable = .../freeflo.app/Contents/MacOS/python
        return os.path.normpath(
            os.path.join(os.path.dirname(sys.executable), '..', '..')
        )
    return None


class FreefloApp(rumps.App):
    def __init__(self):
        # quit_button=None — we install a custom Quit that flushes a final
        # snapshot + backup sync before exiting (see _quit).
        super().__init__('freeflo', title=_ICONS['idle'], quit_button=None)

        settings = cfg.load()
        self._enabled = settings.get('enabled', True)

        # Restore history from the local snapshot if the DB came up empty
        # (corruption / reset) — before anything reads or writes history.
        try:
            restored = history.restore_from_snapshot_if_empty()
            if restored:
                log.warning('Restored %d history entries from local snapshot', restored)
        except Exception:
            log.exception('Snapshot restore check failed')

        # Shared state — written from any thread, read by timer on main thread
        self._state = 'idle' if self._enabled else 'disabled'
        self._state_lock = threading.Lock()
        self._last_status = 'Ready'

        # _busy_lock serialises a full record→transcribe cycle. It is acquired
        # when a toggle starts recording and released when transcription ends
        # (or the recording is aborted). _toggle_lock makes the read-modify of
        # the recording flag atomic, since the hotkey fires from the tap thread.
        self._busy_lock = threading.Lock()
        self._toggle_lock = threading.Lock()
        self._is_recording = False    # guarded by _toggle_lock
        self._record_mode = None      # 'ptt' | 'toggle' | None, guarded by _toggle_lock

        # Guards against overlapping backup syncs (e.g. rapid-fire dictation).
        # _backup_pending coalesces a sync requested while one is already
        # running, so the last utterance before quit is never left unsynced.
        self._backup_lock = threading.Lock()
        self._backup_pending = False
        self._backup_retry_scheduled = False
        # Local snapshot writer — same coalescing pattern, off the dictation path.
        self._snapshot_lock = threading.Lock()
        self._snapshot_pending = False

        ptt = cfg.resolve_key(settings.get('ptt_key', 'left_option'))
        tog = cfg.resolve_key(settings.get('toggle_key', 'right_option'))
        self._recorder = Recorder()
        self._hotkey = HotkeyListener(
            on_ptt_start=self._on_ptt_start,     # push-to-talk key pressed
            on_ptt_stop=self._on_ptt_stop,       # push-to-talk key released
            on_ptt_cancel=self._on_ptt_cancel,   # push-to-talk key used as a modifier
            on_toggle=self._on_toggle,           # toggle key tapped
            ptt_keycode=ptt['keycode'], ptt_mask=ptt['mask'],
            toggle_keycode=tog['keycode'], toggle_mask=tog['mask'],
        )

        # --- Menu items ---
        self._toggle_item = rumps.MenuItem(
            'Disable Dictation' if self._enabled else 'Enable Dictation',
            callback=self._toggle,
        )
        self._open_item = rumps.MenuItem('Open freeflo…', callback=self._open_window)
        self._ui = None   # lazily-created WindowController (holds ObjC objects)
        self._status_item = rumps.MenuItem('● Ready')
        self._access_item = rumps.MenuItem(
            '⚠️  Grant Accessibility Permission',
            callback=self._open_accessibility,
        )
        self._restart_item = rumps.MenuItem(
            '↺  Restart to Activate',
            callback=self._restart_app,
        )

        # Self-update. The title flips to a download prompt once a newer release
        # is found. Result is passed from the worker thread to the main-thread
        # poll timer via _pending_update / _update_dirty (no cross-thread UI).
        self._update_item = rumps.MenuItem('Check for Updates…', callback=self._on_update_menu)
        self._pending_update = None
        self._update_status = None
        self._update_dirty = False
        self._updating = False

        # Troubleshooting — local, zero-network diagnostics for bug reports.
        self._help_menu = rumps.MenuItem('Troubleshooting')
        self._help_menu.add(rumps.MenuItem('Reveal Logs', callback=self._reveal_logs))
        self._help_menu.add(rumps.MenuItem('Copy Diagnostics', callback=self._copy_diagnostics))

        # First-run onboarding.
        self._setup_item = rumps.MenuItem('Rerun Setup…', callback=self._open_onboarding)
        self._ob_ui = None            # onboarding WindowController (lazily created)
        self._ob_mic_requested = False  # user tapped Grant on the mic step
        # Cache the Keychain-backed connection state so the onboarding status
        # poll doesn't read the Keychain every tick (each read triggers a macOS
        # Keychain authorization prompt on an unsigned build). None = not read yet.
        self._ob_connected = None

        # Language submenu — checkmark tracks the active language
        current_lang = settings.get('language', 'en')
        self._lang_menu_items = {}   # name -> (MenuItem, code)
        lang_menu = rumps.MenuItem('Language')
        for name, code in _LANGUAGES:
            if name is None:
                lang_menu.add(None)
                continue
            # First item whose code matches gets the checkmark
            already_checked = any(
                c == current_lang for _, c in self._lang_menu_items.values()
            )
            prefix = '✓  ' if (code == current_lang and not already_checked) else '    '
            item = rumps.MenuItem(
                prefix + name,
                callback=lambda sender, c=code, n=name: self._set_language(c, n),
            )
            lang_menu.add(item)
            self._lang_menu_items[name] = (item, code)

        # Build base menu; conditional permission items are added below.
        self.menu = [
            self._open_item,
            None,
            self._toggle_item,
            None,
            self._status_item,
            None,
            lang_menu,
            self._update_item,
            self._setup_item,
            self._help_menu,
            None,
            rumps.MenuItem('Quit freeflo', callback=self._quit),
        ]

        # The hotkeys use an *active* CGEventTap, which is gated by
        # Accessibility, so that is the only permission we require.
        self._has_accessibility = AXIsProcessTrusted()

        if not self._has_accessibility:
            self.menu.add(self._access_item)
            self.menu.add(self._restart_item)

        # Start the hotkey only when Accessibility is granted.
        if self._enabled and self._has_accessibility:
            self._hotkey.start()

        self._access_check_counter = 0
        rumps.Timer(self._poll_state, 0.15).start()

        # Delay the permission prompt 1 s so the NSApp run loop is active.
        if not self._has_accessibility:
            rumps.Timer(self._prompt_accessibility_once, 1.0).start()

        # Catch up on any backup that happened on another Mac since we last ran.
        self._maybe_sync_backup()

        # First launch (or after a reset): open onboarding once the run loop is up.
        if not settings.get('onboarded'):
            rumps.Timer(self._launch_onboarding, 0.8).start()

        # Check for a newer release shortly after launch, then every 6 hours.
        rumps.Timer(self._launch_update_check, 6.0).start()
        rumps.Timer(self._periodic_update_check, 6 * 3600).start()

    # ------------------------------------------------------------------
    # Thread-safe state helpers
    # ------------------------------------------------------------------

    def _set_state(self, state, status=None):
        with self._state_lock:
            self._state = state
            if status:
                self._last_status = status

    def _get_state(self):
        with self._state_lock:
            return self._state, self._last_status

    # ------------------------------------------------------------------
    # Timer — runs on main thread, safe to update UI
    # ------------------------------------------------------------------

    def _poll_state(self, _):
        state, status = self._get_state()
        self.title = _ICONS.get(state, _ICONS['idle'])
        self._status_item.title = f'● {status}'

        # Reflect any update-check result produced on a worker thread. Touching
        # the menu / posting notifications must happen here on the main thread.
        if self._update_dirty:
            self._update_dirty = False
            if self._pending_update:
                v = self._pending_update['version']
                self._update_item.title = f'⬇  Download update (v{v})'
                rumps.notification(
                    'freeflo', f'Update available — v{v}',
                    (self._pending_update.get('notes') or '')[:140]
                    or 'Open the menu to download.',
                )
            elif not self._updating:
                self._update_item.title = 'Check for Updates…'
            if self._update_status:
                rumps.notification('freeflo', self._update_status, '')
                self._update_status = None

        # Re-check Accessibility every ~3 s. This is a cheap, crash-safe call
        # (unlike the old CGEventTap probe, which crashed natively — see the
        # git history / crash reports). It keeps the "granted while running"
        # onboarding working.
        self._access_check_counter += 1
        if self._access_check_counter < 20:   # 20 × 150 ms = 3 s
            return
        self._access_check_counter = 0

        trusted = AXIsProcessTrusted()
        has_access_warning = '⚠️  Grant Accessibility Permission' in self.menu
        has_restart        = '↺  Restart to Activate' in self.menu

        if not trusted and not has_access_warning:
            self.menu.add(self._access_item)
            if not has_restart:
                self.menu.add(self._restart_item)
        elif trusted and has_access_warning:
            # Accessibility just granted. macOS does not update the accessibility
            # context for a running process — a restart is required for the tap
            # to see the new permission. Keep ↺ visible and notify.
            del self.menu['⚠️  Grant Accessibility Permission']
            if not has_restart:
                self.menu.add(self._restart_item)
            rumps.notification(
                title='freeflo',
                subtitle='Accessibility granted',
                message="Now click '↺ Restart to Activate' in the menu.",
            )

    # ------------------------------------------------------------------
    # Menu callbacks
    # ------------------------------------------------------------------

    def _toggle(self, sender):
        self._enabled = not self._enabled
        if self._enabled:
            self._hotkey.start()
            sender.title = 'Disable Dictation'
            self._set_state('idle', 'Ready')
        else:
            self._hotkey.stop()
            # Clean up any in-flight recording so _busy_lock is never stranded.
            with self._toggle_lock:
                was_recording = self._is_recording
                self._is_recording = False
                self._record_mode = None
            if was_recording:
                self._recorder.stop_and_save()   # discard audio
                try:
                    self._busy_lock.release()
                except RuntimeError:
                    pass  # transcription thread already released it — fine
            sender.title = 'Enable Dictation'
            self._set_state('disabled', 'Dictation disabled')

        settings = cfg.load()
        settings['enabled'] = self._enabled
        cfg.save(settings)

    def _open_window(self, _):
        """Open the freeflo window. Imported lazily and guarded so a failure in
        the window/WebKit subsystem can never take down core dictation."""
        try:
            if self._ui is None:
                from ui.window import WindowController
                self._ui = WindowController(on_message=self._on_ui_message)
            self._ui.show()
        except Exception as e:
            log.exception('Could not open the window')
            rumps.alert('freeflo', f'Could not open the window:\n{e}')

    def _push_ui(self, name, payload):
        """Send an event to the window UI if it is open (never raises)."""
        if self._ui is None:
            return
        try:
            self._ui.send(name, payload)
        except Exception:
            pass

    def _on_ui_message(self, body):
        """Handle a JS->Python message. Runs on the main thread (WebKit
        delivers script messages there)."""
        try:
            action = body.get('action') if hasattr(body, 'get') else None
        except Exception:
            action = None
        action = str(action) if action is not None else ''

        if action == 'test_start':
            if self._begin_recording('test'):
                self._push_ui('status', {'state': 'recording'})
            else:
                self._push_ui('status', {'state': 'busy',
                                         'message': 'Busy — finishing last one…'})
        elif action == 'test_stop':
            self._push_ui('status', {'state': 'processing'})
            self._end_recording('test', transcribe=True)
        elif action == 'get_shortcuts':
            self._send_shortcuts()
        elif action == 'set_shortcuts':
            self._set_shortcuts(body)
        elif action == 'get_history':
            q = body.get('query') if hasattr(body, 'get') else None
            self._send_history(str(q) if q else None)
        elif action == 'clear_history':
            try:
                history.clear()
            except Exception:
                pass
            self._send_history(None)
        elif action == 'set_save_history':
            try:
                on = bool(body.get('on'))
            except Exception:
                on = True
            settings = cfg.load()
            settings['save_history'] = on
            cfg.save(settings)
            self._push_ui('save_history_state', {'on': on})
        elif action == 'get_privacy':
            self._send_privacy()
        elif action == 'set_privacy':
            settings = cfg.load()
            try:
                if body.get('analytics') is not None:
                    settings['analytics_enabled'] = bool(body.get('analytics'))
                if body.get('crash') is not None:
                    settings['crash_enabled'] = bool(body.get('crash'))
            except Exception:
                pass
            cfg.save(settings)
            telemetry.identify()   # re-attach identity if analytics was just turned on
            self._send_privacy()
        elif action == 'get_backup_status':
            self._send_backup_status()
        elif action == 'set_backup_enabled':
            try:
                on = bool(body.get('on'))
            except Exception:
                on = False
            settings = cfg.load()
            settings['backup_enabled'] = on
            cfg.save(settings)
            self._send_backup_status()
            if on:
                self._maybe_sync_backup(manual=True)
        elif action == 'google_connect':
            threading.Thread(target=self._connect_google, daemon=True).start()
        elif action == 'google_disconnect':
            try:
                delete_remote = bool(body.get('delete_remote'))
            except Exception:
                delete_remote = False
            threading.Thread(
                target=self._disconnect_google, args=(delete_remote,), daemon=True
            ).start()
        elif action == 'backup_sync_now':
            self._maybe_sync_backup(manual=True)
        elif action == 'get_home':
            self._send_home()
        elif action == 'set_language':
            self._set_language_from_ui(body)
        elif action == 'get_theme':
            self._send_theme()
        elif action == 'set_theme':
            self._set_theme(body)
        elif action == 'feature_request':
            self._handle_feature_request(body)

    def _send_privacy(self):
        s = cfg.load()
        self._push_ui('privacy', {
            'analytics': s.get('analytics_enabled', True),
            'crash': s.get('crash_enabled', True),
        })

    def _send_history(self, query):
        try:
            entries = history.list_entries(query=query, limit=300)
        except Exception:
            entries = []
        settings = cfg.load()
        self._push_ui('history', {
            'entries': entries,
            'save_history': settings.get('save_history', True),
        })

    # ------------------------------------------------------------------
    # Google backup
    # ------------------------------------------------------------------

    def _send_backup_status(self, extra=None):
        settings = cfg.load()
        payload = {
            'configured': gauth.is_configured(),
            'connected': gauth.is_connected(),
            'enabled': settings.get('backup_enabled', False),
            'email': settings.get('backup_account_email'),
            'last_synced': settings.get('backup_last_synced'),
            'syncing': self._backup_lock.locked(),
        }
        if extra:
            payload.update(extra)
        self._push_ui('backup_status', payload)

    def _connect_google(self):
        """Runs the OAuth loopback flow on a background thread — it blocks
        until the browser redirect lands, which would freeze the UI on the
        main thread."""
        try:
            email = gauth.connect()
        except gauth.NotConfigured as e:
            self._send_backup_status({'error': str(e)})
            return
        except Exception as e:
            self._send_backup_status({'error': f'Sign-in failed: {e}'})
            return
        settings = cfg.load()
        settings['backup_account_email'] = email
        settings['backup_enabled'] = True
        cfg.save(settings)
        telemetry.capture('backup_connected', {'source': 'settings'})
        self._send_backup_status()
        self._maybe_sync_backup(manual=True)

    def _disconnect_google(self, delete_remote):
        if delete_remote:
            try:
                backup.delete_remote()
            except Exception as e:
                # Stay connected so the user can retry — disconnecting now would
                # orphan a Drive backup they believe was just deleted.
                self._send_backup_status(
                    {'error': f'Could not delete Drive backup: {e}. Still connected — try again.'}
                )
                return
        gauth.disconnect()
        settings = cfg.load()
        settings['backup_enabled'] = False
        settings['backup_account_email'] = None
        settings['backup_last_synced'] = None
        cfg.save(settings)
        self._send_backup_status()

    def _maybe_sync_backup(self, manual=False):
        """Kick off a sync in the background if backup is on and connected.
        `manual` only affects whether we report back "not connected" — the
        automatic post-dictation trigger should stay silent."""
        settings = cfg.load()
        if not (settings.get('backup_enabled') and gauth.is_connected()):
            if manual:
                self._send_backup_status({'error': 'Connect Google Backup first.'})
            return
        if not self._backup_lock.acquire(blocking=False):
            # A sync is already running; ask it to run once more when it
            # finishes so a dictation that landed mid-sync isn't left behind.
            self._backup_pending = True
            return
        self._backup_pending = False
        threading.Thread(target=self._sync_backup_worker, daemon=True).start()

    def _sync_backup_worker(self):
        self._send_backup_status()  # reflects syncing=True
        try:
            result = None
            while True:
                result = backup.sync()
                # Drain any request that arrived while we were syncing.
                if not self._backup_pending:
                    break
                self._backup_pending = False
        except Exception as e:
            log.warning('Backup sync failed: %s', e)
            self._backup_lock.release()
            self._send_backup_status({'error': f'Sync failed: {e}'})
            self._schedule_backup_retry()
            return
        settings = cfg.load()
        settings['backup_last_synced'] = result['synced_at']
        cfg.save(settings)
        self._backup_lock.release()
        self._send_backup_status()

    def _schedule_backup_retry(self, delay=120.0):
        """After a failed sync, retry once after a delay so unsynced entries
        aren't stranded until the next dictation or launch. Coalesced so
        repeated failures don't stack timers."""
        if self._backup_retry_scheduled:
            return
        self._backup_retry_scheduled = True

        def _retry():
            self._backup_retry_scheduled = False
            self._maybe_sync_backup()
        threading.Timer(delay, _retry).start()

    # ------------------------------------------------------------------
    # Local snapshot safety net + clean-quit flush
    # ------------------------------------------------------------------

    def _write_snapshot_async(self):
        """Write a local history snapshot off the dictation path, coalescing
        overlapping requests (same pattern as backup sync)."""
        if not self._snapshot_lock.acquire(blocking=False):
            self._snapshot_pending = True
            return
        self._snapshot_pending = False
        threading.Thread(target=self._snapshot_worker, daemon=True).start()

    def _snapshot_worker(self):
        try:
            while True:
                history.write_snapshot()
                if not self._snapshot_pending:
                    break
                self._snapshot_pending = False
        except Exception as e:
            log.warning('History snapshot failed: %s', e)
        finally:
            self._snapshot_lock.release()

    def _flush_backup_blocking(self):
        try:
            if (cfg.load().get('backup_enabled') and history.dirty_entries()
                    and gauth.is_connected()):
                backup.sync()
        except Exception as e:
            log.warning('Final backup flush failed: %s', e)

    def _quit(self, _):
        """Flush a final local snapshot + best-effort backup sync, then quit.
        The backup flush is bounded so a hung network can't block quitting."""
        try:
            history.write_snapshot()
        except Exception:
            log.exception('Snapshot on quit failed')
        t = threading.Thread(target=self._flush_backup_blocking, daemon=True)
        t.start()
        t.join(timeout=6.0)
        try:
            telemetry.flush()
        except Exception:
            pass
        rumps.quit_application()

    def _send_shortcuts(self):
        settings = cfg.load()
        keys = [{'id': k, 'label': v['label']} for k, v in cfg.HOTKEY_KEYS.items()]
        languages = [{'code': code, 'name': name} for name, code in _LANGUAGES if name]
        self._push_ui('shortcuts', {
            'keys': keys,
            'ptt_key': settings.get('ptt_key', 'left_option'),
            'toggle_key': settings.get('toggle_key', 'right_option'),
            'language': settings.get('language', 'en'),
            'languages': languages,
        })

    def _set_shortcuts(self, body):
        try:
            ptt = str(body.get('ptt_key'))
            tog = str(body.get('toggle_key'))
        except Exception:
            return
        if ptt not in cfg.HOTKEY_KEYS or tog not in cfg.HOTKEY_KEYS:
            self._push_ui('shortcuts_saved', {'ok': False, 'error': 'Unknown key.'})
            return
        if ptt == tog:
            self._push_ui('shortcuts_saved',
                          {'ok': False, 'error': 'Pick two different keys.'})
            return

        settings = cfg.load()
        settings['ptt_key'] = ptt
        settings['toggle_key'] = tog
        cfg.save(settings)

        # Apply live — no tap restart, just swap the bindings.
        p, t = cfg.resolve_key(ptt), cfg.resolve_key(tog)
        self._hotkey.set_bindings(p['keycode'], p['mask'], t['keycode'], t['mask'])
        self._send_shortcuts()
        self._push_ui('shortcuts_saved', {'ok': True})

    def _send_home(self):
        """Dashboard payload: usage stats + recent activity for the Home view."""
        try:
            st = history.stats()
        except Exception:
            st = {'total': 0, 'today': 0, 'words_week': 0}
        try:
            recent = history.list_entries(limit=6)
        except Exception:
            recent = []
        settings = cfg.load()
        self._push_ui('home', {
            'stats': st,
            'language': settings.get('language', 'en'),
            'ptt_key': settings.get('ptt_key', 'left_option'),
            'enabled': self._enabled,
            'recent': recent,
        })

    def _set_language_from_ui(self, body):
        try:
            code = str(body.get('code'))
        except Exception:
            return
        name = next((n for n, c in _LANGUAGES if n and c == code), code)
        self._set_language(code, name)   # persists + updates the menu checkmark
        self._send_shortcuts()           # echo new selection back to the window

    def _send_theme(self):
        s = cfg.load()
        self._push_ui('theme', {
            'theme': s.get('theme', 'system'),
            'glass': bool(s.get('glass')),
        })

    def _set_theme(self, body):
        try:
            t = str(body.get('theme'))
        except Exception:
            return
        if t not in ('system', 'light', 'dark', 'glass'):
            return
        s = cfg.load()
        s['theme'] = t
        s['glass'] = (t == 'glass')
        cfg.save(s)
        self._send_theme()

    def _handle_feature_request(self, body):
        """Capture a feature request. For now (no backend) it goes to the log
        and — if analytics consent is on — a PostHog event. The text here is a
        user-authored feature idea, not a transcription."""
        try:
            text = str(body.get('text') or '').strip()
        except Exception:
            text = ''
        if not text:
            self._push_ui('feature_ack', {'ok': False})
            return
        settings = cfg.load()
        log.info('Feature request submitted (%d chars)', len(text))
        telemetry.capture('feature_requested', {
            'text': text[:1000],
            'role': settings.get('profile_role'),
        })
        self._push_ui('feature_ack', {'ok': True})

    def _prompt_accessibility_once(self, timer):
        """One-shot timer — fires 1 s after run() so the NSApp run loop is
        active when we ask macOS to highlight this app in the Accessibility list."""
        timer.stop()
        if not AXIsProcessTrusted():
            AXIsProcessTrustedWithOptions({'AXTrustedCheckOptionPrompt': True})

    def _open_accessibility(self, _):
        AXIsProcessTrustedWithOptions({'AXTrustedCheckOptionPrompt': True})
        subprocess.run(['open', _ACCESSIBILITY_URL], capture_output=True)

    def _restart_app(self, _):
        """Quit and relaunch from /Applications so the fresh process picks up
        any permissions that require a restart (e.g. Accessibility)."""
        app = '/Applications/freeflo.app'
        if not os.path.isdir(app):
            app = _bundle_path() or app
        subprocess.Popen(
            ['bash', '-c', f'sleep 1.5 && open "{app}"'],
            close_fds=True,
        )
        rumps.quit_application()

    # ------------------------------------------------------------------
    # Self-update (GitHub Releases) + local diagnostics
    # ------------------------------------------------------------------

    def _launch_update_check(self, timer):
        timer.stop()   # one-shot
        threading.Thread(target=self._check_updates_worker, args=(True,),
                         daemon=True).start()

    def _periodic_update_check(self, _):
        threading.Thread(target=self._check_updates_worker, args=(True,),
                         daemon=True).start()

    def _check_updates_worker(self, auto):
        """Query GitHub for a newer release. Runs off the main thread; hands the
        result back via _pending_update / _update_dirty for _poll_state."""
        info = updater.check_for_update(cfg.get_version())
        self._pending_update = info
        if info:
            log.info('Update available: v%s', info['version'])
        elif not auto:
            self._update_status = "You're up to date"
        self._update_dirty = True

    def _on_update_menu(self, _):
        if self._updating:
            return
        if self._pending_update:
            self._updating = True
            self._update_item.title = 'Downloading update…'
            threading.Thread(target=self._download_update_worker, daemon=True).start()
        else:
            self._update_item.title = 'Checking…'
            threading.Thread(target=self._check_updates_worker, args=(False,),
                             daemon=True).start()

    def _download_update_worker(self):
        info = self._pending_update or {}
        try:
            path = updater.download_update(info.get('download_url'), info.get('version'))
            updater.reveal(path)
            telemetry.capture('update_downloaded', {'to_version': info.get('version')})
            self._update_status = 'Update downloaded — open it, then drag to Applications'
        except Exception as e:
            log.warning('Update download failed (%s); opening release page', e)
            updater.open_url(info.get('url'))
            self._update_status = 'Opened the download page in your browser'
        finally:
            self._updating = False
            self._update_dirty = True

    # ------------------------------------------------------------------
    # Onboarding (first-run flow in ui/onboarding.html)
    # ------------------------------------------------------------------

    def _launch_onboarding(self, timer):
        timer.stop()   # one-shot
        self._open_onboarding()

    def _open_onboarding(self, _=None):
        try:
            from ui.window import WindowController
            if self._ob_ui is None:
                self._ob_ui = WindowController(
                    on_message=self._on_onboarding_message,
                    html_file='onboarding.html',
                    title='Welcome to freeflo',
                    size=(940, 660),
                )
            self._ob_ui.show()
        except Exception:
            log.exception('Could not open onboarding')

    def _push_ob(self, name, payload):
        if self._ob_ui is None:
            return
        try:
            self._ob_ui.send(name, payload)
        except Exception:
            pass

    def _on_onboarding_message(self, body):
        """JS->Python for the onboarding window (runs on the main thread)."""
        try:
            action = body.get('action') if hasattr(body, 'get') else None
        except Exception:
            action = None
        action = str(action) if action is not None else ''

        if action == 'ob_get_state':
            self._send_ob_state()
        elif action == 'ob_grant_mic':
            self._ob_mic_requested = True
            from engine import permissions
            permissions.request_mic()
            self._send_ob_state()
        elif action == 'ob_grant_accessibility':
            self._open_accessibility(None)
            self._send_ob_state()
        elif action == 'ob_set_consent':
            self._save_consent(body)
        elif action == 'ob_set_profile':
            self._save_profile(body)
        elif action == 'ob_google_signin':
            threading.Thread(target=self._ob_signin_worker, daemon=True).start()
        elif action == 'ob_set_backup':
            try:
                on = bool(body.get('on'))
            except Exception:
                on = False
            settings = cfg.load()
            settings['backup_enabled'] = on
            cfg.save(settings)
            if on:
                self._maybe_sync_backup(manual=True)
        elif action == 'ob_test_start':
            if self._begin_recording('ob_test'):
                self._push_ob('ob_test_state', {'recording': True})
            else:
                self._push_ob('ob_test_result', {'text': '', 'note': 'Busy — try again'})
        elif action == 'ob_test_stop':
            self._end_recording('ob_test', transcribe=True)
        elif action == 'ob_complete':
            self._complete_onboarding(body)

    def _ob_is_connected(self):
        """Cached gauth.is_connected() — read the Keychain at most once per
        onboarding session (each read prompts on an unsigned build)."""
        if self._ob_connected is None:
            try:
                self._ob_connected = gauth.is_connected()
            except Exception:
                self._ob_connected = False
        return self._ob_connected

    def _send_ob_state(self):
        from engine import permissions
        settings = cfg.load()
        mic = permissions.mic_status()
        # When AVFoundation can't report status ('unknown'), treat a grant tap as
        # success so users on such setups aren't blocked; the mic test confirms.
        mic_granted = (mic == 'authorized') or (mic == 'unknown' and self._ob_mic_requested)
        self._push_ob('ob_state', {
            'mic': mic,
            'mic_granted': bool(mic_granted),
            'accessibility': bool(AXIsProcessTrusted()),
            'google_configured': gauth.is_configured(),
            'google_connected': self._ob_is_connected(),
            'analytics': settings.get('analytics_enabled', True),
            'crash': settings.get('crash_enabled', True),
            'profile': {
                'name': settings.get('profile_name') or '',
                'email': settings.get('profile_email') or '',
                'role': settings.get('profile_role') or '',
                'goal': settings.get('profile_goal') or '',
            },
        })

    def _save_consent(self, body):
        if not hasattr(body, 'get'):
            return
        settings = cfg.load()
        try:
            if body.get('analytics') is not None:
                settings['analytics_enabled'] = bool(body.get('analytics'))
            if body.get('crash') is not None:
                settings['crash_enabled'] = bool(body.get('crash'))
        except Exception:
            return
        cfg.save(settings)

    def _save_profile(self, body):
        if not hasattr(body, 'get'):
            return
        settings = cfg.load()
        name, role, goal = body.get('name'), body.get('role'), body.get('goal')
        if name:
            settings['profile_name'] = str(name)
        if role:
            settings['profile_role'] = str(role)
        if goal:
            settings['profile_goal'] = str(goal)
        cfg.save(settings)

    def _ob_signin_worker(self):
        """Google sign-in for identity (name + email). Blocks on the browser
        redirect, so it runs off the main thread."""
        try:
            info = gauth.connect_full()
        except gauth.NotConfigured as e:
            self._push_ob('ob_signin_result', {'ok': False, 'error': str(e)})
            return
        except Exception as e:
            log.warning('Onboarding Google sign-in failed: %s', e)
            self._push_ob('ob_signin_result', {'ok': False, 'error': f'Sign-in failed: {e}'})
            return
        settings = cfg.load()
        if info.get('email'):
            settings['profile_email'] = info['email']
            settings['backup_account_email'] = info['email']
        if info.get('name'):
            settings['profile_name'] = info['name']
        cfg.save(settings)
        self._ob_connected = True   # refresh the cache after a successful sign-in
        telemetry.capture('backup_connected', {'source': 'onboarding'})
        self._push_ob('ob_signin_result',
                      {'ok': True, 'name': info.get('name', ''), 'email': info.get('email', '')})
        self._send_ob_state()

    def _complete_onboarding(self, body):
        self._save_consent(body)
        self._save_profile(body)
        settings = cfg.load()
        settings['onboarded'] = True
        settings['consent_version'] = 1
        cfg.save(settings)
        cfg.get_or_create_install_id()
        log.info('Onboarding complete (analytics=%s crash=%s role=%s)',
                 settings.get('analytics_enabled'), settings.get('crash_enabled'),
                 settings.get('profile_role'))
        telemetry.identify()
        telemetry.capture('onboarding_completed', {
            'mic': bool(AXIsProcessTrusted()),   # best-effort snapshot
            'signed_in': gauth.is_connected(),
            'role': settings.get('profile_role'),
            'analytics': settings.get('analytics_enabled'),
            'crash': settings.get('crash_enabled'),
        })
        if self._ob_ui is not None:
            self._ob_ui.close()

    def _reveal_logs(self, _):
        subprocess.run(['open', logs.log_dir()], capture_output=True)

    def _copy_diagnostics(self, _):
        try:
            subprocess.run(['pbcopy'], input=logs.diagnostics().encode('utf-8'),
                           capture_output=True)
            rumps.notification('freeflo', 'Diagnostics copied',
                               'Paste it into your bug report.')
        except Exception as e:
            log.warning('Copy diagnostics failed: %s', e)

    def _set_language(self, code, name):
        """Update checkmark and persist the language choice."""
        for item_name, (item, _) in self._lang_menu_items.items():
            item.title = ('✓  ' if item_name == name else '    ') + item_name
        settings = cfg.load()
        settings['language'] = code
        cfg.save(settings)
        self._set_state(self._state, f'Language: {name}')

    # ------------------------------------------------------------------
    # Recording engine — shared by both hotkeys. Called from the event-tap
    # thread (callbacks never overlap each other); _toggle_lock guards against
    # the menu Disable path racing these.
    # ------------------------------------------------------------------

    def _begin_recording(self, mode):
        """Acquire the busy lock and start recording. Returns True if started."""
        with self._toggle_lock:
            if self._is_recording:
                return False
            if not self._busy_lock.acquire(blocking=False):
                self._set_state(self._state, 'Busy — finishing last one…')
                return False
            self._is_recording = True
            self._record_mode = mode
        self._record_start = time.monotonic()
        self._set_state('recording', 'Recording…')
        self._recorder.start()
        return True

    def _end_recording(self, mode, transcribe):
        """Stop recording iff the active session belongs to `mode`."""
        with self._toggle_lock:
            if not self._is_recording or self._record_mode != mode:
                return
            self._is_recording = False
            self._record_mode = None

        if not transcribe:
            self._recorder.stop_and_save()   # discard
            self._set_state('idle', 'Cancelled')
            try:
                self._busy_lock.release()
            except RuntimeError:
                pass
            return

        duration = max(0.0, time.monotonic() - getattr(self, '_record_start', time.monotonic()))
        self._set_state('processing', 'Transcribing…')
        wav_path = self._recorder.stop_and_save()
        if wav_path:
            threading.Thread(
                target=self._transcribe_worker,
                args=(wav_path, mode, duration),
                daemon=True,
            ).start()
        else:
            self._set_state('idle', 'Too short — try again')
            if mode == 'test':
                self._push_ui('test_result', {'text': '', 'note': 'Too short — try again'})
            elif mode == 'ob_test':
                self._push_ob('ob_test_result', {'text': '', 'note': 'Too short — try again'})
            self._busy_lock.release()

    # ------------------------------------------------------------------
    # Hotkey callbacks — called from the event-tap thread
    # ------------------------------------------------------------------

    def _on_ptt_start(self):
        """Left Option pressed — push-to-talk begins."""
        if not self._enabled:
            return
        self._begin_recording('ptt')

    def _on_ptt_stop(self):
        """Left Option released — stop and transcribe."""
        if not self._enabled:
            return
        self._end_recording('ptt', transcribe=True)

    def _on_ptt_cancel(self):
        """Left Option was used as a shortcut modifier — discard the recording."""
        if not self._enabled:
            return
        self._end_recording('ptt', transcribe=False)

    def _on_toggle(self):
        """Right Option tapped — start, or stop if a toggle session is active."""
        if not self._enabled:
            return
        with self._toggle_lock:
            recording = self._is_recording
            mode = self._record_mode
        if not recording:
            self._begin_recording('toggle')
        elif mode == 'toggle':
            self._end_recording('toggle', transcribe=True)
        # else: a push-to-talk recording owns the session — ignore the toggle.

    # ------------------------------------------------------------------
    # Transcription — runs in background thread
    # ------------------------------------------------------------------

    def _transcribe_worker(self, wav_path, mode, duration):
        """Transcribe on a background thread. In 'test' mode the result is shown
        in the window (no paste); otherwise it is injected at the cursor. Either
        way a successful transcription is logged to history."""
        try:
            text = transcribe(wav_path)
            if mode in ('test', 'ob_test'):
                self._set_state('idle', 'Ready')
                payload = {'text': text or ''}
                if mode == 'ob_test':
                    self._push_ob('ob_test_result', payload)
                else:
                    self._push_ui('test_result', payload)
            elif text:
                inject(text)
                self._set_state('idle', f'"{text[:40]}{"…" if len(text) > 40 else ""}"')
                telemetry.dictation_completed(
                    mode, cfg.load().get('language'), duration, len(text))
            else:
                self._set_state('idle', 'Nothing heard — try again')
            if mode != 'ob_test':   # onboarding tests aren't saved to history
                self._log_history(text, mode, duration)
        except Exception as e:
            log.exception('Transcription failed (mode=%s)', mode)
            self._set_state('idle', f'Error: {e}')
            if mode == 'test':
                self._push_ui('test_result', {'text': '', 'note': f'Error: {e}'})
            elif mode == 'ob_test':
                self._push_ob('ob_test_result', {'text': '', 'note': f'Error: {e}'})
        finally:
            self._busy_lock.release()

    def _log_history(self, text, mode, duration):
        """Record a transcription to the local DB, honoring the save toggle.
        Guarded so a DB problem can never break dictation."""
        if not text:
            return
        try:
            settings = cfg.load()
            if not settings.get('save_history', True):
                return
            history.add(text, language=settings.get('language'),
                        mode=mode, duration=duration)
        except Exception:
            log.exception('Failed to log transcription to history')
        self._write_snapshot_async()   # local safety net, every utterance
        self._maybe_sync_backup()      # cloud mirror if connected


if __name__ == '__main__':
    import atexit
    logs.setup_logging()
    logs.install_excepthooks()
    telemetry.init()   # consent-gated + inert without keys; never sends text

    def _snapshot_on_exit():
        try:
            history.write_snapshot()
        except Exception:
            pass
    atexit.register(_snapshot_on_exit)   # backstop for exits that skip the Quit menu

    try:
        FreefloApp().run()
    except Exception:
        log.critical('Fatal error during startup/run', exc_info=True)
        raise
