#!/usr/bin/env python3
"""Two-language blending, checked case by case.

    scripts/build_helper.sh && python3 helper/tests/blend_test.py [-v]

Every case in fixtures/blend_cases.json (transcripts recorded from en-IN +
the India multilingual model on synthesised clips, plus synthetic edge cases)
goes through `freeflo-helper blend`; text, sources and the per-sentence rules
must equal the expectations, which come from blend_ref.py (the reference
implementation the thresholds were tuned with). No audio, no speech model.
A case may name its "secondary_locale" (default: the India model, for which
the Hindi word list is used). With --ref the two implementations must also
agree on every number of every decision.

A new recorded case: `freeflo-helper transcribe-file clip.wav --languages
en-IN,hi-IN --explain` prints primary_segments / secondary_segments.

Options:
    --helper PATH   binary to test (default: build/freeflo-helper, or $FREEFLO_HELPER)
    --ref           also check that blend_ref.py itself still gives the expectations
    -v              print every result

Python 3.9+, standard library only. Exit code 0 when everything passed; also 0
(with a note) when the binary has not been built.
"""
import argparse
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
CASES = os.path.join(HERE, 'fixtures', 'blend_cases.json')


def run_helper(binary, case):
    body = {'primary': case['primary'], 'secondary': case['secondary']}
    if case.get('secondary_locale'):
        body['secondary_locale'] = case['secondary_locale']
    payload = json.dumps(body)
    out = subprocess.run([binary, 'blend'], input=payload.encode('utf-8'),
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20)
    if out.returncode != 0:
        raise RuntimeError('exit %d: %s' % (out.returncode, out.stdout.decode()[:200]))
    return json.loads(out.stdout.decode('utf-8'))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--helper', default=os.environ.get('FREEFLO_HELPER', os.path.join(ROOT, 'build', 'freeflo-helper')))
    parser.add_argument('--ref', action='store_true')
    parser.add_argument('-v', dest='verbose', action='store_true')
    args = parser.parse_args()

    if not os.access(args.helper, os.X_OK):
        print('note: helper binary not found at %s (run scripts/build_helper.sh); blend checks skipped' % args.helper)
        return 0
    with open(CASES, encoding='utf-8') as f:
        cases = json.load(f)

    failures = []
    if args.ref:
        sys.path.insert(0, HERE)
        import blend_ref
        # The Hindi word list exists twice (Blend.swift and blend_ref.py).
        try:
            with open(os.path.join(ROOT, 'helper', 'Sources', 'Blend.swift'), encoding='utf-8') as f:
                swift = f.read()
            block = swift[swift.index('static let hindiWords'):]
            block = block[block.index('"""') + 3:]
            listed = set(block[:block.index('"""')].split())
        except (OSError, ValueError):
            listed = None
        same = listed == set(blend_ref.HINDI_WORDS)
        print('  %s  the Hindi word list is the same in Blend.swift and blend_ref.py (%d words)'
              % ('ok  ' if same else 'FAIL', len(blend_ref.HINDI_WORDS)))
        if not same:
            failures.append('ref:word-list')
        for case in cases:
            r = blend_ref.blend(case['primary'], case['secondary'],
                                blend_ref.uses_lexicon(case.get('secondary_locale')))
            got = (r['text'], r['sources'], [d['rule'] for d in r['decisions']])
            want = (case['expect']['text'], case['expect']['sources'], case['expect']['rules'])
            if got != want:
                failures.append('ref:' + case['name'])
                print('  FAIL  reference %s' % case['name'])

    for case in cases:
        want = case['expect']
        try:
            r = run_helper(args.helper, case)
        except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as e:
            failures.append(case['name'])
            print('  FAIL  %s -- %s' % (case['name'], e))
            continue
        rules = [d.get('rule') for d in r.get('decisions', [])]
        problems = []
        if r.get('text') != want['text']:
            problems.append('text %r != %r' % (r.get('text'), want['text']))
        if r.get('sources') != want['sources']:
            problems.append('sources %r != %r' % (r.get('sources'), want['sources']))
        if rules != want['rules']:
            problems.append('rules %r != %r' % (rules, want['rules']))
        if args.ref:
            ref = blend_ref.blend(case['primary'], case['secondary'],
                                  blend_ref.uses_lexicon(case.get('secondary_locale')))
            if r.get('decisions') != ref['decisions']:
                problems.append('decisions differ from the reference')
            for key in ('primary_chars', 'secondary_chars'):
                if r.get(key) != ref[key]:
                    problems.append('%s %r != reference %r' % (key, r.get(key), ref[key]))
        if problems:
            failures.append(case['name'])
            print('  FAIL  %s -- %s' % (case['name'], '; '.join(problems)))
        else:
            print('  ok    %s (%s)' % (case['name'], r['sources'] or '-'))
        if args.verbose:
            print('        -> %s' % r.get('text'))
            for d in r.get('decisions', []):
                print('           %s' % json.dumps(d, sort_keys=True))

    # Bad input is rejected, not guessed at.
    out = subprocess.run([args.helper, 'blend'], input=b'{"primary": 3}', stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, timeout=20)
    bad_ok = out.returncode == 1 and json.loads(out.stdout.decode() or '{}').get('error') == 'bad_input'
    print('  %s  bad input -> {"error":"bad_input"}, exit 1' % ('ok  ' if bad_ok else 'FAIL'))
    if not bad_ok:
        failures.append('bad_input')

    total = len(cases) + 1 + (1 if args.ref else 0)
    print('')
    if failures:
        print('%d of %d blend checks FAILED: %s' % (len(failures), total, ', '.join(failures)))
        return 1
    print('all %d blend checks passed (%d fixture cases)' % (total, len(cases)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
