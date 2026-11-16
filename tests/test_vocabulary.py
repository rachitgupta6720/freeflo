"""Vocabulary store + replacement tests. Uses a temp directory; the user's
real ~/Library/Application Support/freeflo is never touched."""
import json
import os
import shutil
import tempfile
import threading
import unittest

import support  # noqa: F401  (puts the repo root on sys.path)

from engine import vocabulary
from engine.vocabulary import Vocabulary, VocabularyError, apply_replacements


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='freeflo-vocab-')
        self.path = os.path.join(self.dir, 'sub', 'vocabulary.json')

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def vocab(self, **kw):
        return Vocabulary(self.path, **kw)

    def on_disk(self):
        with open(self.path, encoding='utf-8') as f:
            return json.load(f)

    def test_missing_file_is_an_empty_vocabulary(self):
        v = self.vocab().load()
        self.assertEqual(v.list(), [])
        self.assertEqual(v.replacements(), [])
        self.assertEqual(len(v), 0)
        self.assertFalse(os.path.exists(self.path), 'load must not create it')

    def test_add_list_remove_and_persist(self):
        v = self.vocab()
        self.assertEqual(v.add('Kiwi'), 'Kiwi')
        self.assertEqual(v.add('UPI'), 'UPI')
        self.assertEqual(v.list(), ['Kiwi', 'UPI'])
        self.assertIn('kiwi', v)
        self.assertNotIn('mango', v)
        self.assertEqual(self.on_disk()['words'], ['Kiwi', 'UPI'])
        self.assertEqual(self.on_disk()['version'], 1)

        again = self.vocab().load()             # a fresh process
        self.assertEqual(again.list(), ['Kiwi', 'UPI'])
        self.assertTrue(again.remove('kiwi'))   # case-insensitive
        self.assertFalse(again.remove('kiwi'))
        self.assertEqual(self.vocab().list(), ['UPI'])

    def test_lazy_load_on_first_use(self):
        self.vocab().add('Kiwi')
        self.assertEqual(self.vocab().list(), ['Kiwi'])     # no load() call
        self.assertEqual(self.vocab().context(), ['Kiwi'])

    def test_dedupe_ignores_case_and_spacing_and_updates_the_spelling(self):
        v = self.vocab()
        v.add('kiwi')
        self.assertEqual(v.add('  Kiwi '), 'Kiwi')
        self.assertEqual(v.list(), ['Kiwi'], 'spelling should be corrected')
        v.add('Rachit   Gupta')
        v.add('rachit gupta')
        self.assertEqual(v.list(), ['Kiwi', 'rachit gupta'])
        self.assertEqual(len(v), 2)

    def test_whitespace_and_control_characters_are_normalised(self):
        v = self.vocab()
        self.assertEqual(v.add('  two\twords\n'), 'two words')
        self.assertEqual(v.add('bell\x07char'), 'bellchar')
        decomposed = 'café'                # e + combining acute
        self.assertEqual(v.add(decomposed), 'café')
        self.assertIn('café', v)

    def test_length_and_count_limits(self):
        v = self.vocab(autosave=False)
        for bad in ('', '   ', None, 42):
            with self.assertRaises(VocabularyError) as cm:
                v.add(bad)
            self.assertEqual(cm.exception.code, 'empty')
        with self.assertRaises(VocabularyError) as cm:
            v.add('x' * (vocabulary.MAX_WORD_CHARS + 1))
        self.assertEqual(cm.exception.code, 'too_long')
        self.assertTrue(str(cm.exception))
        v.add('x' * vocabulary.MAX_WORD_CHARS)   # exactly at the limit is ok
        for i in range(vocabulary.MAX_WORDS - 1):
            v.add('word%d' % i)
        self.assertEqual(len(v), vocabulary.MAX_WORDS)
        with self.assertRaises(VocabularyError) as cm:
            v.add('one too many')
        self.assertEqual(cm.exception.code, 'full')
        self.assertEqual(v.add('WORD3'), 'WORD3')   # updating is still allowed
        self.assertIsInstance(cm.exception, ValueError)

    def test_add_many(self):
        v = self.vocab()
        added, skipped = v.add_many(['Kiwi', 'kiwi', '', 'UPI', 'x' * 500, None])
        self.assertEqual((added, skipped), (2, 4))
        self.assertEqual(v.list(), ['kiwi', 'UPI'])
        self.assertEqual(self.on_disk()['words'], ['kiwi', 'UPI'])
        self.assertEqual(v.add_many(None), (0, 0))

    def test_clear(self):
        v = self.vocab()
        v.add('Kiwi')
        v.add_replacement('you pee eye', 'UPI')
        v.clear()
        self.assertEqual((v.list(), v.replacements()), ([], []))
        self.assertEqual(self.on_disk()['words'], [])

    def test_unicode_is_stored_readably(self):
        v = self.vocab()
        v.add('कीवी')
        v.add_replacement('यूपीआई', 'UPI')
        with open(self.path, 'rb') as f:
            raw = f.read()
        self.assertIn('कीवी'.encode('utf-8'), raw)
        self.assertEqual(self.vocab().list(), ['कीवी'])

    def test_corrupt_file_is_set_aside_not_destroyed(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, 'w') as f:
            f.write('{"words": ["Kiwi", ')      # a hand-edit gone wrong
        v = self.vocab().load()
        self.assertEqual(v.list(), [])
        kept = [n for n in os.listdir(os.path.dirname(self.path))
                if '.corrupt-' in n]
        self.assertEqual(len(kept), 1)
        v.add('Mango')                          # and it is usable again
        self.assertEqual(self.vocab().list(), ['Mango'])

    def test_wrong_shape_file_is_set_aside(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, 'w') as f:
            json.dump(['just', 'a', 'list'], f)
        self.assertEqual(self.vocab().list(), [])
        self.assertFalse(os.path.exists(self.path))

    def test_load_sanitises_a_hand_edited_file(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, 'w', encoding='utf-8') as f:
            json.dump({
                'words': ['Kiwi', 'kiwi', '', 7, None, 'x' * 999, ' UPI '],
                'replacements': [
                    {'heard': 'you pee eye', 'written': 'UPI'},
                    {'heard': 'YOU PEE EYE', 'written': 'dup'},
                    {'heard': '', 'written': 'x'},
                    {'written': 'no heard'},
                    ['at the rate', '@'],
                    'nonsense', 5,
                ],
                'future_field': {'ignored': True},
            }, f)
        v = self.vocab().load()
        self.assertEqual(v.list(), ['Kiwi', 'UPI'])
        self.assertEqual(v.replacements(),
                         [{'heard': 'you pee eye', 'written': 'UPI'},
                          {'heard': 'at the rate', 'written': '@'}])

    def test_mapping_style_replacements_are_accepted(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, 'w') as f:
            json.dump({'replacements': {'kiwi': 'Kiwi'}}, f)
        self.assertEqual(self.vocab().apply_replacements('a kiwi'), 'a Kiwi')

    def test_save_is_atomic_and_leaves_no_temp_files(self):
        v = self.vocab()
        for i in range(20):
            v.add('word%d' % i)
        names = os.listdir(os.path.dirname(self.path))
        self.assertEqual(names, ['vocabulary.json'])

    def test_unwritable_location_does_not_raise(self):
        blocker = os.path.join(self.dir, 'file')
        with open(blocker, 'w') as f:
            f.write('x')
        v = Vocabulary(os.path.join(blocker, 'vocabulary.json'))
        self.assertEqual(v.add('Kiwi'), 'Kiwi')         # kept in memory
        self.assertFalse(v.save())
        self.assertEqual(v.list(), ['Kiwi'])

    def test_autosave_off(self):
        v = self.vocab(autosave=False)
        v.add('Kiwi')
        self.assertFalse(os.path.exists(self.path))
        self.assertTrue(v.save())
        self.assertEqual(self.on_disk()['words'], ['Kiwi'])

    def test_replacement_crud(self):
        v = self.vocab()
        self.assertEqual(v.add_replacement(' You Pee  Eye ', 'UPI'),
                         ('You Pee Eye', 'UPI'))
        v.add_replacement('you pee eye', 'U.P.I.')       # same rule, updated
        self.assertEqual(v.replacements(),
                         [{'heard': 'you pee eye', 'written': 'U.P.I.'}])
        v.add_replacement('umm', '')                     # deletion rule
        self.assertTrue(v.remove_replacement('YOU PEE EYE'))
        self.assertFalse(v.remove_replacement('you pee eye'))
        self.assertEqual(self.vocab().replacements(),
                         [{'heard': 'umm', 'written': ''}])

    def test_replacement_limits(self):
        v = self.vocab(autosave=False)
        with self.assertRaises(VocabularyError) as cm:
            v.add_replacement('', 'x')
        self.assertEqual(cm.exception.code, 'empty')
        with self.assertRaises(VocabularyError) as cm:
            v.add_replacement('h' * (vocabulary.MAX_HEARD_CHARS + 1), 'x')
        self.assertEqual(cm.exception.code, 'too_long')
        with self.assertRaises(VocabularyError) as cm:
            v.add_replacement('h', 'w' * (vocabulary.MAX_WRITTEN_CHARS + 1))
        self.assertEqual(cm.exception.code, 'too_long')
        for i in range(vocabulary.MAX_REPLACEMENTS):
            v.add_replacement('heard %d' % i, 'w')
        with self.assertRaises(VocabularyError) as cm:
            v.add_replacement('one more', 'w')
        self.assertEqual(cm.exception.code, 'full')

    def test_method_apply_uses_current_rules(self):
        v = self.vocab()
        self.assertEqual(v.apply_replacements('pay by you pee eye'),
                         'pay by you pee eye')
        v.add_replacement('you pee eye', 'UPI')
        self.assertEqual(v.apply_replacements('pay by you pee eye'),
                         'pay by UPI')
        v.add_replacement('you pee eye', 'U.P.I.')       # cache invalidated
        self.assertEqual(v.apply_replacements('pay by You Pee Eye.'),
                         'pay by U.P.I..')
        v.remove_replacement('you pee eye')
        self.assertEqual(v.apply_replacements('pay by you pee eye'),
                         'pay by you pee eye')
        self.assertEqual(v.apply_replacements(''), '')
        self.assertIsNone(v.apply_replacements(None))

    def test_context_for_the_speech_engine(self):
        v = self.vocab()
        v.add('Kiwi')
        v.add_replacement('you pee eye', 'UPI')
        v.add_replacement('kiwi', 'Kiwi')                # already a word
        v.add_replacement('at the rate', '@')            # not a term
        v.add_replacement('my address', 'x' * 100)       # a snippet, too long
        v.add_replacement('umm', '')
        self.assertEqual(v.context(), ['Kiwi', 'UPI'])

    def test_to_dict(self):
        v = self.vocab()
        v.add('Kiwi')
        d = v.to_dict()
        self.assertEqual(d['words'], ['Kiwi'])
        self.assertEqual(d['limits']['words'], vocabulary.MAX_WORDS)
        json.dumps(d)                                    # serialisable

    def test_concurrent_edits_do_not_corrupt_the_file(self):
        v = self.vocab()

        def worker(n):
            for i in range(15):
                v.add('w%d-%d' % (n, i))
                v.add_replacement('h%d-%d' % (n, i), 'x')
                v.apply_replacements('some h0-1 text')
        threads = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(v), 90)
        fresh = self.vocab().load()
        self.assertEqual(sorted(fresh.list()), sorted(v.list()))
        self.assertEqual(len(fresh.replacements()), 90)

    def test_default_path_is_under_application_support(self):
        self.assertTrue(vocabulary.default_path().endswith(
            os.path.join('Library', 'Application Support', 'freeflo',
                         'vocabulary.json')))


class ReplacementCase(unittest.TestCase):
    def test_case_insensitive_whole_word(self):
        rules = [('kiwi', 'Kiwi')]
        self.assertEqual(apply_replacements('i use kiwi daily', rules),
                         'i use Kiwi daily')
        self.assertEqual(apply_replacements('KIWI and kIwI', rules),
                         'Kiwi and Kiwi')
        self.assertEqual(apply_replacements('kiwis akiwi kiwi2 my_kiwi', rules),
                         'kiwis akiwi kiwi2 my_kiwi')

    def test_punctuation_around_a_match_is_kept(self):
        rules = [('kiwi', 'Kiwi')]
        self.assertEqual(apply_replacements('(kiwi), "kiwi". kiwi!', rules),
                         '(Kiwi), "Kiwi". Kiwi!')
        self.assertEqual(apply_replacements("kiwi's app, kiwi-pay", rules),
                         "Kiwi's app, Kiwi-pay")
        self.assertEqual(apply_replacements('kiwi', rules), 'Kiwi')

    def test_multi_word_phrases_and_flexible_whitespace(self):
        rules = [('you pee eye', 'UPI')]
        self.assertEqual(apply_replacements('pay by You Pee Eye now', rules),
                         'pay by UPI now')
        self.assertEqual(apply_replacements('you  pee\neye', rules), 'UPI')
        self.assertEqual(apply_replacements('you pee eyes', rules),
                         'you pee eyes')

    def test_longest_rule_wins_and_order_does_not_matter(self):
        a = [('new york', 'NY'), ('new york city', 'NYC')]
        b = list(reversed(a))
        for rules in (a, b):
            self.assertEqual(
                apply_replacements('new york city, then new york', rules),
                'NYC, then NY')

    def test_single_pass_no_chaining(self):
        rules = [('a', 'b'), ('b', 'a')]
        self.assertEqual(apply_replacements('a b a', rules), 'b a b')
        self.assertEqual(apply_replacements('x', [('x', 'x x')]), 'x x')

    def test_symbols_in_heard_and_written(self):
        self.assertEqual(apply_replacements('i like c++ a lot', [('c++', 'C++')]),
                         'i like C++ a lot')
        self.assertEqual(apply_replacements('mail me at the rate home',
                                            [('at the rate', '@')]),
                         'mail me @ home')
        self.assertEqual(apply_replacements('cost is 5 dollars (approx)',
                                            [('dollars', '$'), ('approx', '~')]),
                         'cost is 5 $ (~)')
        self.assertEqual(apply_replacements('a.b', [('a.b', 'X')]), 'X')
        self.assertEqual(apply_replacements('axb', [('a.b', 'X')]), 'axb',
                         'heard is literal text, never a regex')

    def test_written_is_inserted_literally(self):
        self.assertEqual(apply_replacements('sign off', [('sign off', r'\1 \g<0> $1')]),
                         r'\1 \g<0> $1')

    def test_deletion_rule(self):
        self.assertEqual(apply_replacements('so umm yes', [('umm', '')]),
                         'so  yes')

    def test_devanagari_whole_words(self):
        # "क" must not match inside "कि" / "किताब" (the vowel sign is a
        # combining mark, which a naive \b treats as a word boundary).
        rules = [('क', 'X')]
        self.assertEqual(apply_replacements('क कि किताब', rules), 'X कि किताब')
        self.assertEqual(apply_replacements('मैं कीवी से भुगतान करूँगा',
                                            [('कीवी', 'Kiwi')]),
                         'मैं Kiwi से भुगतान करूँगा')
        self.assertEqual(apply_replacements('कीवीज़', [('कीवी', 'Kiwi')]), 'कीवीज़')

    def test_unicode_case_and_normalisation(self):
        self.assertEqual(apply_replacements('ÉCOLE and école', [('école', 'School')]),
                         'School and School')
        decomposed = 'café time'
        self.assertEqual(apply_replacements(decomposed, [('café', 'Coffee')]),
                         'Coffee time')
        untouched = 'café time'
        self.assertIs(apply_replacements(untouched, [('tea', 'Tea')]), untouched)

    def test_no_rules_or_no_match_returns_the_same_string(self):
        text = 'nothing to see'
        self.assertIs(apply_replacements(text, []), text)
        self.assertIs(apply_replacements(text, None), text)
        self.assertIs(apply_replacements(text, [('kiwi', 'Kiwi')]), text)
        self.assertEqual(apply_replacements('', [('a', 'b')]), '')
        self.assertIsNone(apply_replacements(None, [('a', 'b')]))

    def test_accepts_dicts_and_skips_junk_rules(self):
        rules = [{'heard': 'kiwi', 'written': 'Kiwi'}, {'heard': 'x'},
                 ('only-one',), None, 5, ('', 'y'), (None, 'z'), ('upi', None)]
        self.assertEqual(apply_replacements('kiwi upi x', rules), 'Kiwi upi x')

    def test_first_rule_wins_for_duplicate_heard(self):
        self.assertEqual(apply_replacements('kiwi', [('kiwi', 'A'), ('KIWI', 'B')]),
                         'A')

    def test_overlapping_candidates(self):
        # "ab" is rejected at position 0 (inside "abc"), but the scan must
        # still find the standalone one later.
        self.assertEqual(apply_replacements('abc ab', [('ab', 'X')]), 'abc X')
        self.assertEqual(apply_replacements('aa a', [('a', 'X')]), 'aa X')

    def test_large_rule_set_is_fast(self):
        import time
        rules = [('heard phrase %d' % i, 'W%d' % i) for i in range(200)]
        text = ' '.join('some filler and heard phrase %d here' % (i % 200)
                        for i in range(300))
        t0 = time.monotonic()
        out = apply_replacements(text, rules)
        self.assertLess(time.monotonic() - t0, 1.0)
        self.assertIn('W199', out)
        self.assertNotIn('heard phrase', out)


class ApplyWithWords(unittest.TestCase):
    """Vocabulary.apply(): replacements + stored spellings, in one pass."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='freeflo-vocab-')
        self.v = Vocabulary(path=os.path.join(self.dir, 'vocabulary.json'))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_words_are_written_as_stored(self):
        self.v.add('Kiwi')
        self.v.add('UPI')
        self.v.add('freeflo')
        self.assertEqual(self.v.apply('pay by upi on kiwi with FreeFlo.'),
                         'pay by UPI on Kiwi with freeflo.')

    def test_whole_words_only(self):
        self.v.add('Kiwi')
        self.assertEqual(self.v.apply('kiwis and akiwi'), 'kiwis and akiwi')

    def test_everyday_words_are_not_respelled(self):
        self.v.add('IT')
        self.v.add('US')
        self.assertEqual(self.v.apply('it is up to us'), 'it is up to us')
        self.assertTrue(self.v.is_ambiguous('IT'))
        self.assertTrue(self.v.is_ambiguous(' us '))
        self.assertFalse(self.v.is_ambiguous('Kiwi'))

    def test_a_replacement_wins_over_a_word(self):
        self.v.add('Kiwi')
        self.v.add_replacement('kiwi', 'Kiwi Card')
        self.assertEqual(self.v.apply('my kiwi'), 'my Kiwi Card')

    def test_replacements_still_apply_and_are_single_pass(self):
        self.v.add('UPI')
        self.v.add_replacement('you pee eye', 'UPI')
        self.v.add_replacement('a', 'b')
        self.v.add_replacement('b', 'a')
        self.assertEqual(self.v.apply('you pee eye: a then b'), 'UPI: b then a')

    def test_multi_word_terms(self):
        self.v.add('Wispr Flow')
        self.assertEqual(self.v.apply('like wispr   flow does'), 'like Wispr Flow does')

    def test_edits_invalidate_the_cache(self):
        self.v.add('Kiwi')
        self.assertEqual(self.v.apply('kiwi'), 'Kiwi')
        self.v.remove('Kiwi')
        self.assertEqual(self.v.apply('kiwi'), 'kiwi')
        self.v.add('KIWI')
        self.assertEqual(self.v.apply('kiwi'), 'KIWI')
        self.v.clear()
        self.assertEqual(self.v.apply('kiwi'), 'kiwi')

    def test_empty_and_odd_input(self):
        self.assertEqual(self.v.apply(''), '')
        self.assertIsNone(self.v.apply(None))
        self.v.add('Kiwi')
        self.assertEqual(self.v.apply('nothing to fix'), 'nothing to fix')

    def test_devanagari_word(self):
        self.v.add('कीवी')
        self.assertEqual(self.v.apply('यह कीवी है'), 'यह कीवी है')


if __name__ == '__main__':
    unittest.main()
