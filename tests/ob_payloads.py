"""Setup-window payloads exactly as the app builds them: a real
engine.setup_state.SetupFlow driven into a state, decorated by
setup_state.page_state() (the function app._send_ob_state uses).

Used by tests/ui_pages_check.py and tests/ui_snapshots.py, so neither feeds
the page a payload the app can no longer produce.
"""
from engine import setup_state

KEY_LABELS = {'left_option': 'Left Option (⌥)', 'right_option': 'Right Option (⌥)',
              'right_command': 'Right Command (⌘)', 'right_control': 'Right Control (⌃)'}

# name -> what it shows (first run, repair after an update, review from the menu)
SCENARIOS = ('first-start', 'first-mic-waiting', 'first-mic-denied', 'first-ax-needed',
             'first-ax-waiting', 'first-ax-stale', 'first-done', 'first-engine-error',
             'repair-ax', 'repair-legacy', 'repair-ax-waiting', 'repair-both',
             'review', 'restart')


class _Clock(object):
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def view(scen, settings=None):
    """The ob_state payload for scenario ``scen`` (see SCENARIOS).
    ``settings`` overrides the stored settings (analytics_enabled, ...)."""
    clock = _Clock()
    first = scen.startswith('first')
    ledger = None if first or scen == 'repair-legacy' else {
        'v': 1, 'version': '1.5.0', 'ready': '1.5.0',
        'granted': {'mic': True, 'accessibility': True}, 'notices': {}}
    flow = setup_state.SetupFlow(ledger=ledger, version='1.6.0', onboarded=not first,
                                 clock=clock, ptt_label='Left Option')

    def update(mic, ax, tap=False, error=None):
        flow.update(mic=mic, accessibility=ax, engine_error=error, enabled=True,
                    tap_running=tap)

    if scen == 'first-start':
        update('undetermined', False)
    elif scen == 'first-mic-waiting':
        update('undetermined', False)
        flow.action(setup_state.ACT_GRANT_MIC)
    elif scen == 'first-mic-denied':
        update('denied', False)
    elif scen == 'first-ax-needed':
        update('granted', False)
    elif scen == 'first-ax-waiting':
        update('granted', False)
        flow.action(setup_state.ACT_GRANT_ACCESSIBILITY)
    elif scen == 'first-ax-stale':
        update('granted', False)
        flow.action(setup_state.ACT_GRANT_ACCESSIBILITY)
        clock.t += 30
        update('granted', False)
    elif scen == 'first-done':
        update('granted', False)
        update('granted', True)
        clock.t += 1.5
        update('granted', True, tap=True)
    elif scen == 'first-engine-error':
        update(None, False, error='freeflo needs macOS 26 or later')
    elif scen in ('repair-ax', 'repair-legacy'):
        update('granted', False)
    elif scen == 'repair-ax-waiting':
        update('granted', False)
        flow.action(setup_state.ACT_GRANT_ACCESSIBILITY)
    elif scen == 'repair-both':
        update('undetermined', False)
    elif scen == 'review':
        update('granted', True, tap=True)
        clock.t += 1.5
        update('granted', True, tap=True)
        flow.open_window()
    elif scen == 'restart':
        update('granted', True)
        for _ in range(8):
            clock.t += 2.5
            update('granted', True)
        flow.open_window()
    else:
        raise ValueError('unknown setup scenario %r' % scen)
    stored = {'onboarded': not first, 'ptt_key': 'left_option',
              'toggle_key': 'right_option', 'analytics_enabled': True,
              'crash_enabled': True}
    stored.update(settings or {})
    return setup_state.page_state(flow.view(), stored,
                                  lambda k: KEY_LABELS.get(k, 'Left Option (⌥)'), '1.6.0')
