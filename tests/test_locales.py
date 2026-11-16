"""Language list / migration glue (engine/locales.py)."""
import unittest

import support  # noqa: F401
from engine import locales

STATUS = {
    'supported': ['en-AU', 'en-GB', 'en-IN', 'en-US', 'es-ES', 'es-MX', 'fr-FR',
                  'hi-IN', 'mul-IN', 'zh-CN'],
    'installed': ['en-GB', 'en-IN', 'en-US'],
    'default_locale': 'en-IN',
}


class Normalize(unittest.TestCase):
    def test_spellings(self):
        self.assertEqual(locales.normalize('en_in'), 'en-IN')
        self.assertEqual(locales.normalize('EN-us'), 'en-US')
        self.assertEqual(locales.normalize('zh-hans-cn'), 'zh-Hans-CN')
        self.assertEqual(locales.normalize(' hi '), 'hi')
        self.assertEqual(locales.normalize(None), '')
        self.assertEqual(locales.normalize(42), '')

    def test_concrete(self):
        self.assertTrue(locales.is_concrete('en-IN'))
        self.assertTrue(locales.is_concrete('en_US'))
        for legacy in ('auto', 'en', 'hi', '', None):
            self.assertFalse(locales.is_concrete(legacy), legacy)


class Resolve(unittest.TestCase):
    def r(self, setting, status=STATUS, system='en-IN'):
        return locales.resolve(setting, status, system=system)

    def test_before_the_helper_has_answered_nothing_is_decided(self):
        self.assertEqual(locales.resolve('auto', None), ('auto', False))
        self.assertEqual(locales.resolve('hi', None), ('hi', False))
        self.assertEqual(locales.resolve('en-IN', {}), ('en-IN', False))
        self.assertEqual(locales.resolve(None, None), ('auto', False))

    def test_legacy_auto_becomes_the_recommended_locale(self):
        self.assertEqual(self.r('auto'), ('en-IN', True))
        self.assertEqual(self.r(None), ('en-IN', True))
        self.assertEqual(self.r(''), ('en-IN', True))

    def test_legacy_bare_codes(self):
        self.assertEqual(self.r('en'), ('en-IN', True))        # user's region
        self.assertEqual(self.r('hi'), ('hi-IN', True))        # not installed: still theirs
        self.assertEqual(self.r('fr'), ('fr-FR', True))
        self.assertEqual(self.r('zh'), ('zh-CN', True))

    def test_bare_code_prefers_region_then_installed(self):
        self.assertEqual(self.r('en', system='en-GB'), ('en-GB', True))
        self.assertEqual(self.r('es', system='en-MX'), ('es-MX', True))
        status = dict(STATUS, installed=['es-MX'], default_locale='es-MX')
        self.assertEqual(self.r('es', status, system='fr-FR'), ('es-MX', True))

    def test_unsupported_language_falls_back(self):
        self.assertEqual(self.r('ar'), ('en-IN', True))        # was in the Whisper list
        self.assertEqual(self.r('xx-YY'), ('en-IN', True))
        self.assertEqual(self.r('ja'), ('en-IN', True))

    def test_a_concrete_choice_is_kept(self):
        self.assertEqual(self.r('en-US'), ('en-US', True))
        self.assertEqual(self.r('en_us'), ('en-US', True))
        self.assertEqual(self.r('hi-IN'), ('hi-IN', True))

    def test_a_region_that_is_not_offered_keeps_the_language(self):
        self.assertEqual(self.r('en-NZ'), ('en-IN', True))
        self.assertEqual(self.r('fr-CA'), ('fr-FR', True))

    def test_without_a_recommendation_the_system_locale_decides(self):
        status = dict(STATUS)
        del status['default_locale']
        self.assertEqual(self.r('auto', status, system='en-GB'), ('en-GB', True))
        self.assertEqual(self.r('auto', status, system='de-DE'), ('en-US', True))

    def test_resolution_is_stable(self):
        for setting in ('auto', 'en', 'hi', 'ar', 'en-US'):
            first, _ = self.r(setting)
            self.assertEqual(self.r(first), (first, True))


class Choices(unittest.TestCase):
    def test_short_list_first_then_by_name(self):
        rows = locales.choices(STATUS, 'en-IN', system='en-IN')
        codes = [r['code'] for r in rows]
        self.assertEqual(len(rows), 10)
        self.assertEqual(codes[:3], ['en-IN', 'hi-IN', 'mul-IN'])
        self.assertEqual([r['short'] for r in rows], [True] * 3 + [False] * 7)
        rest = [r['name'].lower() for r in rows[3:]]
        self.assertEqual(rest, sorted(rest))
        names = dict((r['code'], r['name']) for r in rows)
        self.assertEqual(names['hi-IN'], 'Hindi')
        self.assertEqual(names['fr-FR'], 'French')
        self.assertEqual(names['mul-IN'], 'Multilingual (India)')
        self.assertIn('(', names['en-GB'])
        self.assertIn('English', names['en-IN'])
        for r in rows:
            self.assertTrue(r['name'])
            self.assertEqual(set(r), {'code', 'name', 'installed', 'short', 'note', 'group'})
        by = dict((r['code'], r) for r in rows)
        self.assertTrue(by['en-IN']['installed'])
        self.assertFalse(by['hi-IN']['installed'])
        self.assertEqual(by['hi-IN']['group'], 'india')
        self.assertIsNone(by['en-IN']['group'])
        self.assertIn('English letters', by['hi-IN']['note'])
        self.assertIn('Tamil', by['mul-IN']['note'])
        self.assertEqual(by['en-GB']['note'], '')

    def test_short_list_follows_the_recommendation(self):
        status = dict(STATUS, default_locale='en-GB')
        codes = [r['code'] for r in locales.choices(status, system='fr-FR')]
        self.assertEqual(codes[:3], ['en-GB', 'hi-IN', 'mul-IN'])
        status = dict(STATUS, supported=['en-US', 'fr-FR'], default_locale=None)
        rows = locales.choices(status, system='fr-FR')
        self.assertEqual([r['code'] for r in rows if r['short']], ['fr-FR'])

    def test_current_list_is_kept(self):
        rows = locales.choices(STATUS, ['en-IN', 'ko-KR'])
        self.assertEqual([r['code'] for r in rows][-1], 'ko-KR')
        self.assertFalse(rows[-1]['installed'])

    def test_current_is_listed_even_if_the_helper_dropped_it(self):
        rows = locales.choices(STATUS, 'ko-KR')
        row = [r for r in rows if r['code'] == 'ko-KR']
        self.assertEqual(len(row), 1)
        self.assertFalse(row[0]['installed'])

    def test_no_status(self):
        self.assertEqual(locales.choices(None), [])

    def test_membership(self):
        self.assertTrue(locales.is_installed('en_in', STATUS))
        self.assertFalse(locales.is_installed('hi-IN', STATUS))
        self.assertTrue(locales.is_supported('hi-IN', STATUS))
        self.assertFalse(locales.is_supported('ko-KR', STATUS))
        self.assertFalse(locales.is_installed('en-IN', None))


class Names(unittest.TestCase):
    def test_names_are_never_empty(self):
        for tag in ('en-IN', 'hi-IN', 'mul-IN', 'zz-ZZ', 'en'):
            self.assertTrue(locales.display_name(tag), tag)
        self.assertEqual(locales.display_name('auto'), 'System default')
        self.assertEqual(locales.display_name('mul-IN'), 'Multilingual (India)')
        self.assertEqual(locales.display_name(None), '')

    def test_english_india(self):
        # Foundation's wording when available, the fallback table otherwise.
        self.assertIn('English', locales.display_name('en-IN'))


STATUS2 = dict(STATUS, installed=['en-GB', 'en-IN', 'en-US', 'hi-IN', 'mul-IN'],
               max_languages=2, groups=[['hi-IN', 'mul-IN']])


class Groups(unittest.TestCase):
    def test_rule_without_groups(self):
        for tag in ('hi-IN', 'mul-IN', 'ta_in', 'bn-IN'):
            self.assertEqual(locales.group_of(tag), 'india', tag)
        for tag in ('en-IN', 'en-US', 'fr-FR', 'hi', '', None):
            self.assertIsNone(locales.group_of(tag), tag)

    def test_groups_from_the_helper_win(self):
        status = {'groups': [['hi-IN', 'mul-IN'], ['zh-CN', 'zh-TW']], 'max_languages': 2}
        self.assertEqual(locales.group_of('hi-IN', status), 'india')
        self.assertEqual(locales.group_of('zh-TW', status), 'zh-cn')
        # The helper says ta-IN is a model of its own (e.g. an older macOS).
        self.assertIsNone(locales.group_of('ta-IN', status))
        self.assertIsNone(locales.group_of('ta-IN', {'groups': [], 'max_languages': 2}))
        # An older helper reports no groups: the rule applies.
        self.assertEqual(locales.group_of('ta-IN', {'groups': [], 'max_languages': 1}), 'india')

    def test_same_model(self):
        self.assertTrue(locales.same_model('hi-IN', 'mul-IN'))
        self.assertTrue(locales.same_model('en_in', 'en-IN'))
        self.assertFalse(locales.same_model('en-IN', 'hi-IN'))
        self.assertFalse(locales.same_model('en-IN', 'en-US'))
        self.assertFalse(locales.same_model('hi-IN', None))


class Languages(unittest.TestCase):
    def rl(self, languages, language=None, status=STATUS2, system='en-IN'):
        return locales.resolve_languages(languages, language, status, system=system)

    def test_two_languages(self):
        self.assertEqual(self.rl(['en-IN', 'hi-IN']), (['en-IN', 'hi-IN'], True))
        self.assertEqual(self.rl(['en_in', 'hi']), (['en-IN', 'hi-IN'], True))

    def test_one_model_counts_once(self):
        self.assertEqual(self.rl(['hi-IN', 'mul-IN']), (['hi-IN'], True))
        self.assertEqual(self.rl(['en-IN', 'hi-IN', 'mul-IN']), (['en-IN', 'hi-IN'], True))
        self.assertEqual(self.rl(['en-IN', 'en-IN']), (['en-IN'], True))

    def test_capped(self):
        self.assertEqual(self.rl(['en-IN', 'fr-FR', 'hi-IN']), (['en-IN', 'fr-FR'], True))
        older = dict(STATUS2, max_languages=1)
        self.assertEqual(self.rl(['en-IN', 'hi-IN'], status=older), (['en-IN'], True))
        self.assertEqual(self.rl(['en-IN', 'hi-IN'], status=dict(STATUS2, max_languages=5))[0],
                         ['en-IN', 'hi-IN'])

    def test_unsupported_extras_are_dropped(self):
        self.assertEqual(self.rl(['en-IN', 'xx-YY', 'ja']), (['en-IN'], True))

    def test_legacy_single_setting(self):
        self.assertEqual(self.rl(None, 'auto'), (['en-IN'], True))
        self.assertEqual(self.rl(None, 'hi'), (['hi-IN'], True))
        self.assertEqual(self.rl([], 'en-US'), (['en-US'], True))
        self.assertEqual(self.rl('fr-FR', None), (['fr-FR'], True))

    def test_without_status_nothing_is_decided(self):
        self.assertEqual(locales.resolve_languages(['en_in', 'hi-IN'], None, None),
                         (['en-IN', 'hi-IN'], False))
        self.assertEqual(locales.resolve_languages(None, 'auto', None), (['auto'], False))

    def test_usable(self):
        self.assertEqual(locales.usable(['en-IN', 'hi-IN'], STATUS), (['en-IN'], ['hi-IN']))
        self.assertEqual(locales.usable(['en-IN', 'hi-IN'], STATUS2), (['en-IN', 'hi-IN'], []))
        self.assertEqual(locales.usable(['en-IN'], None), ([], ['en-IN']))

    def test_label(self):
        label = locales.label(['en-IN', 'hi-IN'], STATUS)
        self.assertTrue(label.endswith(' + Hindi'), label)
        self.assertIn('English', label)
        self.assertEqual(locales.label(['mul-IN']), 'Multilingual (India)')

    def test_suggest_addition(self):
        self.assertEqual(locales.suggest_addition(['en-IN'], STATUS2, system='en-IN'), 'hi-IN')
        self.assertEqual(locales.suggest_addition(['en-US'], STATUS2, system='hi-IN'), 'hi-IN')
        self.assertIsNone(locales.suggest_addition(['en-IN'], STATUS2, system='en-GB'))
        self.assertIsNone(locales.suggest_addition(['en-IN', 'hi-IN'], STATUS2, system='en-IN'))
        self.assertIsNone(locales.suggest_addition(['fr-FR'], STATUS2, system='en-IN'))
        self.assertIsNone(locales.suggest_addition(['en-IN'], STATUS, system='en-IN'))   # older helper
        no_hindi = dict(STATUS2, supported=['en-IN', 'en-US'])
        self.assertIsNone(locales.suggest_addition(['en-IN'], no_hindi, system='en-IN'))

    def test_default_languages(self):
        self.assertEqual(locales.default_languages('en-IN', STATUS2, system='en-IN'), ['en-IN', 'hi-IN'])
        not_yet = dict(STATUS2, installed=['en-IN'])
        self.assertEqual(locales.default_languages('en-IN', not_yet, system='en-IN'), ['en-IN'])
        self.assertEqual(locales.default_languages('en-GB', STATUS2, system='en-GB'), ['en-GB'])

    def test_describe(self):
        self.assertIn('mixed in one sentence', locales.describe('hi-IN'))
        self.assertIn('Bangla', locales.describe('mul-IN'))
        self.assertIn('Multilingual (India) model', locales.describe('ta-IN'))
        self.assertEqual(locales.describe('en-IN'), '')


class Presets(unittest.TestCase):
    """The one-click choices for a simple picker."""
    TWO = dict(STATUS, max_languages=2)

    def ids(self, rows):
        return [r['id'] for r in rows]

    def test_nothing_before_the_helper_answers(self):
        self.assertEqual(locales.presets(None), [])
        self.assertEqual(locales.presets({}), [])

    def test_english_speaker_in_india(self):
        rows = locales.presets(self.TWO, ['en-IN'], system='en-IN')
        self.assertEqual(self.ids(rows), ['en-IN', 'en-IN+hi-IN', 'hi-IN'])
        alone, both, hindi = rows
        self.assertTrue(alone['current'] and alone['installed'])
        self.assertEqual(alone['download_mb'], 0)
        self.assertEqual(alone['download_note'], '')
        self.assertEqual(both['languages'], ['en-IN', 'hi-IN'])
        self.assertEqual(both['name'], 'English (India) + Hindi')
        self.assertFalse(both['installed'])
        self.assertEqual(both['missing'], ['hi-IN'])
        self.assertEqual(both['download_mb'], 620)
        self.assertIn('620 MB', both['download_note'])
        self.assertIn('mixed', both['note'])
        self.assertFalse(both['current'])
        self.assertEqual(hindi['name'], 'Hindi')

    def test_everything_installed(self):
        status = dict(self.TWO, installed=STATUS['installed'] + ['hi-IN', 'mul-IN'])
        rows = locales.presets(status, ['en-IN', 'hi-IN'], system='en-IN')
        both = rows[1]
        self.assertTrue(both['current'] and both['installed'])
        self.assertEqual((both['missing'], both['download_mb'], both['download_note']), ([], 0, ''))
        self.assertEqual([r['current'] for r in rows], [False, True, False])

    def test_current_matches_whatever_the_spelling(self):
        rows = locales.presets(self.TWO, ['en_in', 'HI-in'], system='en-IN')
        self.assertEqual([r['id'] for r in rows if r['current']], ['en-IN+hi-IN'])

    def test_an_unusual_current_choice_comes_first(self):
        rows = locales.presets(self.TWO, ['en-GB', 'fr-FR'], system='en-IN')
        self.assertEqual(self.ids(rows), ['en-GB+fr-FR', 'en-IN', 'en-IN+hi-IN', 'hi-IN'])
        self.assertTrue(rows[0]['current'])
        self.assertEqual(rows[0]['missing'], ['fr-FR'])
        self.assertIsNone(rows[0]['download_mb'])           # size not measured
        self.assertEqual(rows[0]['download_note'], 'A one-time download.')

    def test_no_hindi_outside_india_unless_it_is_installed(self):
        status = dict(self.TWO, default_locale='en-US')
        self.assertEqual(self.ids(locales.presets(status, ['en-US'], system='en-US')), ['en-US'])
        status['installed'] = STATUS['installed'] + ['hi-IN']
        self.assertEqual(self.ids(locales.presets(status, ['en-US'], system='en-US')),
                         ['en-US', 'en-US+hi-IN', 'hi-IN'])

    def test_an_older_helper_offers_no_pair(self):
        rows = locales.presets(STATUS, ['en-IN'], system='en-IN')     # max_languages 1
        self.assertEqual(self.ids(rows), ['en-IN', 'hi-IN'])

    def test_a_non_english_mac_gets_its_own_language_only(self):
        status = dict(self.TWO, default_locale='fr-FR')
        self.assertEqual(self.ids(locales.presets(status, ['fr-FR'], system='fr-FR')), ['fr-FR'])

    def test_hindi_mac(self):
        status = dict(self.TWO, default_locale='hi-IN')
        self.assertEqual(self.ids(locales.presets(status, ['hi-IN'], system='hi-IN')), ['hi-IN'])


class Downloads(unittest.TestCase):
    def test_sizes(self):
        self.assertEqual(locales.download_mb(['en-IN'], STATUS), 0)
        self.assertEqual(locales.download_mb('hi-IN', STATUS), 620)
        # hi-IN and mul-IN are one model: counted once
        self.assertEqual(locales.download_mb(['hi-IN', 'mul-IN'], STATUS), 620)
        self.assertEqual(locales.download_mb(['en-AU'], STATUS), 380)
        self.assertIsNone(locales.download_mb(['fr-FR'], STATUS))
        self.assertIsNone(locales.download_mb(['hi-IN', 'fr-FR'], STATUS))

    def test_note(self):
        self.assertEqual(locales.download_note(['en-IN'], STATUS), '')
        self.assertEqual(locales.download_note(['en-IN', 'hi-IN'], STATUS),
                         'A one-time download of about 620 MB.')
        self.assertEqual(locales.download_note(['fr-FR'], STATUS), 'A one-time download.')
        self.assertEqual(locales.download_note(['hi-IN'], None), '')



if __name__ == '__main__':
    unittest.main()