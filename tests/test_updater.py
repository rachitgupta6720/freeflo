"""The self-updater's choice of release (no network: requests.get is faked)."""
import unittest

import support  # noqa: F401  (puts the repo root on sys.path)

try:
    from engine import updater
    HAVE_UPDATER = True
except Exception:                                   # pragma: no cover
    HAVE_UPDATER = False

MARKER = '<!-- freeflo-requires: macos=26.0 arch=arm64 -->'


def release(tag, assets=('freeflo-macos26.zip',), body='notes', **extra):
    data = {
        'tag_name': tag,
        'body': body,
        'html_url': 'https://example.invalid/releases/tag/' + tag,
        'published_at': '2026-11-01T00:00:00Z',
        'assets': [{'name': a,
                    'browser_download_url':
                        'https://example.invalid/dl/%s/%s' % (tag, a)}
                   for a in assets],
    }
    data.update(extra)
    return data


class FakeResponse(object):
    def __init__(self, payload, fail=False):
        self._payload = payload
        self._fail = fail

    def raise_for_status(self):
        if self._fail:
            raise RuntimeError('HTTP 403')

    def json(self):
        return self._payload


@unittest.skipUnless(HAVE_UPDATER, 'needs requests')
class UpdaterCase(unittest.TestCase):

    def setUp(self):
        self._real_get = updater.requests.get
        self.urls = []

    def tearDown(self):
        updater.requests.get = self._real_get

    def serve(self, payload, fail=False):
        def fake_get(url, **kwargs):
            self.urls.append(url)
            return FakeResponse(payload, fail)
        updater.requests.get = fake_get

    def check(self, current='1.5.0', macos=(26, 1), arch='arm64'):
        return updater.check_for_update(current, macos=macos, arch=arch)

    def test_offers_the_newest_release_of_this_line(self):
        self.serve([release('v1.5.1'), release('v1.6.0', body='big\n\n' + MARKER),
                    release('v1.5.2')])
        info = self.check()
        self.assertEqual(info['version'], '1.6.0')
        self.assertEqual(info['download_url'],
                         'https://example.invalid/dl/v1.6.0/freeflo-macos26.zip')
        self.assertEqual(info['notes'], 'big')          # marker not shown
        self.assertIn('/releases?', self.urls[0])       # the list, not /latest

    def test_the_old_whisper_build_is_never_offered_as_an_update(self):
        # A build of this line that was never version-bumped (1.4.2) must not
        # "update" to v1.4.3, which is the previous engine.
        self.serve([release('v1.4.3', assets=('freeflo.zip',))])
        self.assertIsNone(self.check(current='1.4.2'))
        # ...and a later 1.4.x fix that is flagged latest hides nothing.
        self.serve([release('v1.4.4', assets=('freeflo.zip',)),
                    release('v1.5.1')])
        self.assertEqual(self.check()['version'], '1.5.1')

    def test_the_asset_is_not_the_name_old_clients_download(self):
        # 1.4.x clients fetch 'freeflo.zip' from releases/latest with no OS
        # check; sharing that name would put this build on Macs that cannot
        # run it.
        self.assertNotEqual(updater._ASSET_NAME, 'freeflo.zip')
        with open(support.REPO_ROOT + '/scripts/release.sh') as f:
            script = f.read()
        self.assertIn('ASSET="%s"' % updater._ASSET_NAME, script)
        self.assertNotIn('dist/freeflo.zip', script)

    def test_a_release_this_mac_cannot_run_is_skipped(self):
        newer_os = 'x\n<!-- freeflo-requires: macos=27.0 arch=arm64 -->'
        self.serve([release('v2.0.0', body=newer_os), release('v1.5.1', body=MARKER)])
        self.assertEqual(self.check(macos=(26, 3))['version'], '1.5.1')
        self.assertEqual(self.check(macos=(27, 0))['version'], '2.0.0')
        self.assertIsNone(self.check(arch='x86_64'))
        # An OS version we could not establish never blocks an update.
        self.assertEqual(self.check(macos=None)['version'], '2.0.0')

    def test_requirements_marker(self):
        self.assertEqual(updater.parse_requirements('hi\n' + MARKER),
                         {'macos': (26, 0), 'arch': 'arm64'})
        self.assertEqual(updater.parse_requirements('no marker'), {})
        self.assertEqual(updater.parse_requirements(None), {})
        self.assertEqual(
            updater.parse_requirements('<!-- freeflo-requires: macos=27 -->'),
            {'macos': (27,)})
        self.assertTrue(updater.is_compatible({'macos': (27,)}, (27, 0), 'arm64'))
        self.assertFalse(updater.is_compatible({'macos': (27,)}, (26, 9), 'arm64'))

    def test_nothing_newer_drafts_and_prereleases(self):
        self.serve([release('v1.5.0'), release('v1.4.9'),
                    release('v1.9.0', draft=True),
                    release('v1.8.0', prerelease=True)])
        self.assertIsNone(self.check())

    def test_failures_are_silent(self):
        self.serve([], fail=True)
        self.assertIsNone(self.check())
        self.serve({'message': 'rate limited'})
        self.assertIsNone(self.check())
        self.serve([None, 'junk', {'tag_name': 'v9.0.0', 'assets': 'nope'},
                    release('v1.5.3')])
        self.assertEqual(self.check()['version'], '1.5.3')

        def boom(url, **kwargs):
            raise OSError('offline')
        updater.requests.get = boom
        self.assertIsNone(self.check())


if __name__ == '__main__':
    unittest.main()
