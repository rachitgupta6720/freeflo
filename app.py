import os
import sys
import time
import threading
import subprocess
import rumps
from ApplicationServices import AXIsProcessTrusted, AXIsProcessTrustedWithOptions

import config as cfg
from engine.recorder import Recorder
from engine.transcriber import transcribe
from engine.injector import inject
from engine import history
from hotkey import HotkeyListener

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
        super().__init__('freeflo', title=_ICONS['idle'], quit_button='Quit')

        settings = cfg.load()
        self._enabled = settings.get('enabled', True)

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
            None,
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

    def _send_shortcuts(self):
        settings = cfg.load()
        keys = [{'id': k, 'label': v['label']} for k, v in cfg.HOTKEY_KEYS.items()]
        self._push_ui('shortcuts', {
            'keys': keys,
            'ptt_key': settings.get('ptt_key', 'left_option'),
            'toggle_key': settings.get('toggle_key', 'right_option'),
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
            if mode == 'test':
                self._set_state('idle', 'Ready')
                self._push_ui('test_result', {'text': text or ''})
            elif text:
                inject(text)
                self._set_state('idle', f'"{text[:40]}{"…" if len(text) > 40 else ""}"')
            else:
                self._set_state('idle', 'Nothing heard — try again')
            self._log_history(text, mode, duration)
        except Exception as e:
            self._set_state('idle', f'Error: {e}')
            if mode == 'test':
                self._push_ui('test_result', {'text': '', 'note': f'Error: {e}'})
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
            pass


if __name__ == '__main__':
    FreefloApp().run()
