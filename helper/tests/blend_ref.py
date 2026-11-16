#!/usr/bin/env python3
"""REFERENCE implementation of the two-language blend, ported line for line to
helper/Sources/Blend.swift (helper/tests/blend_test.py --ref checks both).

Input: two transcripts of the SAME audio, each a list of finalised segments
  {"text": str, "start": sec, "end": sec, "words": [[text, confidence, start, end], ...]}
(word text keeps its leading space / trailing punctuation exactly as the recogniser's
AttributedString run gave it; confidence is 0..1 or -1; start/end -1 when the run has no time range).

primary   = the monolingual recogniser (punctuates; e.g. en-IN)
secondary = the other one (e.g. the India multilingual model, hi-IN)

Three signals decide each sentence: the word confidences of both recognisers
("floor", "margin"), the secondary's own words when it is the India model
("lexicon": it wrote Hindi), and whether both heard the same words ("same":
then the sentence is in the primary's language, whatever the confidences).

Usage:  blend_ref.py cases.json            -> prints every case
        blend_ref.py --one < {"primary": [...], "secondary": [...]}   -> one JSON result
        (optional "secondary_locale": the lexicon is used when it is missing or a
        non-English locale of India)
"""
import json, re, sys

PRIMARY_FLOOR = 0.72      # below: the primary did not understand the sentence
PRIMARY_TRUSTED = 0.86    # at or above: the primary is kept
MARGIN = 0.02             # in between: secondary must be at least this much surer ...
MIDCAP_MIN = 0.20         # ... and the primary's sentence must look "foreign" (see midcap)
CUT_PAD = 0.05            # seconds: tolerance when cutting between two sentences
ORPHAN_AFTER = 0.15       # a secondary word ending this long after a sentence ended ...
ORPHAN_BEFORE = 0.05      # ... and before the next one began was not heard by the primary
ORPHAN_MIN_WORDS = 2
LEXICON_MIN_WORDS = 2     # the secondary wrote at least this many Hindi words in the sentence ...
LEXICON_MIN_SHARE = 0.30  # ... and they are at least this share of its words: the sentence is Hindi
PRIMARY_SURE = 0.93       # ... unless the primary is this sure of what it heard
SAME_MIN = 0.80           # both recognisers wrote (this share of) the same words: not a foreign sentence

# Hindi as the India model writes it (Latin letters), most frequent words
# first in spirit: particles, pronouns, auxiliaries, everyday verbs and nouns.
# Words that are also English (to, do, is, us, the, me, main, hum, ho, so, par,
# use, log, din, mat, hi, teen, char, tab, jab) are deliberately left out.
# Keep in step with Blend.swift (blend_test.py --ref compares the two).
HINDI_WORDS = frozenset("""
hai hain hoon hun tha thi hoga hogi honge hota hoti hote hua hui hue nahin nahi kya kyon kyun kyonki kyunki
aur mein mei ko ka ki ke se bhi toh tak liye lie wala wali wale
mujhe mujhko mera meri mere maine hamen hame humein hamara hamari hamare tum tumhe tumhen tumne tumhara
aap aapka aapki aapke aapko aapne yeh ye woh wo voh iska iski iske uska uski uske usne usko unka unki unke
inka ise isi kaun kuch sabhi koi kisi
isliye lekin magar agar phir fir abhi aaj kal kab kahan yahan wahan kaise kaisa kitna kitne bahut thoda thodi
zyada jyada sirf accha acha achha thik theek haan
kar karo karna karni karne karta karti karte karen karke kiya kiye karunga karenge diya diye dena deni dene
dijiye deejie liya lena leni lene lekar gaya gayi gaye jaana jana jaata jaati jaate jaaega jaega jayega
jaenge raha rahi rahe aaya aayi aaye aana chal chalo chalte chalta chalti chahiye chahie sakta sakti sakte
lagta lagti lagte pata dekho dekh dekha milna hona padega padegi paregi rakh rakho bhej bhejo bheja
bhai yaar yar kaam baat raat subah shaam ghar paisa paise samay waqt tarah saath baad pehle pahle upar niche
andar bahar ek poora pura agle agla pichle naya nayi purana bada badi chhota chota sahi galat zaroori zaruri
jaldi der mausam khana pani paani baar gai apna apni apne dono samajh matlab shayad zaroor bilkul sath
""".split())

def uses_lexicon(secondary_locale):
    """The Hindi word list only says something about the India multilingual model."""
    if not secondary_locale: return True
    parts = secondary_locale.replace('_', '-').split('-')
    language = parts[0].lower()
    region = next((p.upper() for p in parts[1:] if len(p) == 2 and p.isalpha()), None)
    return region == 'IN' and language not in ('', 'en')

def hindi_evidence(text):
    """(Hindi words, words) in a stretch of the secondary's text."""
    count = total = 0
    for token in text.split():
        b = bare(token)
        if not b: continue
        total += 1
        if b in HINDI_WORDS: count += 1
    return count, total

def is_hindi(text):
    count, total = hindi_evidence(text)
    return count >= LEXICON_MIN_WORDS and count >= LEXICON_MIN_SHARE * total

def agreement(a_text, b_text):
    """Share of the words the two recognisers have in common (each word counted
    as often as both wrote it), relative to the longer of the two."""
    a = [x for x in (bare(t) for t in a_text.split()) if x]
    b = [x for x in (bare(t) for t in b_text.split()) if x]
    if not a or not b: return 0.0
    left = {}
    for x in b: left[x] = left.get(x, 0) + 1
    common = 0
    for x in a:
        if left.get(x, 0) > 0:
            left[x] -= 1
            common += 1
    return common / float(max(len(a), len(b)))

def tidy(text):
    """The India model capitalises Hindi words at random ("gaya Tha", "Agle
    hafte"): known Hindi words are written in lower case."""
    out = []
    for token in text.split():
        if token[:1].isupper() and token[1:] == token[1:].lower() and bare(token) in HINDI_WORDS:
            token = token.lower()
        out.append(token)
    return ' '.join(out)

def mean(ws):
    # A plain left fold, like Swift's loop (Python 3.12+ sum() compensates
    # rounding error, which would make the two differ in the last bit).
    total, count = 0.0, 0
    for w in ws:
        if w['conf'] >= 0:
            total += w['conf']
            count += 1
    return total / count if count else -1.0

def words_of(segments):
    out = []
    for seg in segments:
        orphan = ''
        first = len(out)
        for w in seg.get('words', []):
            text, conf, start, end = w[0], float(w[1]), float(w[2]), float(w[3])
            if start < 0 or end < 0:                    # no time range: glue to a neighbour
                if len(out) > first: out[-1]['text'] += text
                else: orphan += text
                continue
            out.append({'text': orphan + text, 'conf': conf, 'start': start, 'end': max(start, end)})
            orphan = ''
    return out

def ends_sentence(text):
    t = text.strip()
    return t[-1:] in ('.', '?', '!')

def text_of(ws):
    return ''.join(w['text'] for w in ws).strip()

def units_of(segments):
    """The primary cut into sentences: after every word ending in . ? ! and at every segment end."""
    units = []
    for seg in segments:
        cur = []
        for w in words_of([seg]):
            cur.append(w)
            if ends_sentence(w['text']):
                units.append(cur); cur = []
        if cur: units.append(cur)
    return [u for u in units if text_of(u)]

def bare(word):
    return ''.join(ch for ch in word.lower() if ch.isalnum())

def midcap(unit):
    """Share of the sentence's words, the first excluded, that start with a capital letter
    (the pronoun I excluded). en-* recognisers write words they do not know as Proper Nouns."""
    if len(unit) < 2: return 0.0
    n = 0
    for w in unit[1:]:
        t = w['text'].strip()
        letters = [ch for ch in t if ch.isalpha()]
        if not letters or not letters[0].isupper(): continue
        b = bare(t)
        if b == 'i' or t.startswith("I'") or t.startswith('I’'): continue
        n += 1
    return n / float(len(unit) - 1)

def blend(primary, secondary, lexicon=True):
    units = units_of(primary)
    theirs = words_of(secondary)
    result = {'text': '', 'sources': '', 'decisions': [], 'primary_chars': 0, 'secondary_chars': 0}
    if not units:
        result['text'] = ''.join(s.get('text', '') for s in secondary).strip()
        if lexicon: result['text'] = tidy(result['text'])
        result['sources'] = 'S' if result['text'] else ''
        result['secondary_chars'] = len(result['text'])
        return result
    if not theirs:
        result['text'] = ''.join(s.get('text', '') for s in primary).strip()
        result['sources'] = 'P' if result['text'] else ''
        result['primary_chars'] = len(result['text'])
        return result
    n = len(units)
    cuts = []
    for i in range(n - 1):
        a_end, b_start = units[i][-1]['end'], units[i + 1][0]['start']
        cuts.append(max((a_end + b_start) / 2.0, a_end + CUT_PAD))
    assigned = [[] for _ in range(n)]
    runs = {}                                   # k -> secondary words spoken BEFORE unit k (k == n: after the last)
    for w in theirs:                            # in time order
        e = w['end']
        if e <= units[0][0]['start'] + ORPHAN_BEFORE:
            runs.setdefault(0, []).append(w); continue
        if e > units[-1][-1]['end'] + ORPHAN_AFTER:
            runs.setdefault(n, []).append(w); continue
        k = 0
        while k < n - 1 and e > cuts[k]: k += 1
        if k > 0 and e > units[k - 1][-1]['end'] + ORPHAN_AFTER and e <= units[k][0]['start'] + ORPHAN_BEFORE:
            runs.setdefault(k, []).append(w); continue
        if k < n - 1 and e > units[k][-1]['end'] + ORPHAN_AFTER and e <= units[k + 1][0]['start'] + ORPHAN_BEFORE:
            runs.setdefault(k + 1, []).append(w); continue
        assigned[k].append(w)
    for k in sorted(runs):                      # a lone stray word is boundary jitter, not speech
        if len(runs[k]) >= ORPHAN_MIN_WORDS: continue
        w = runs.pop(k)[0]
        if k == 0: assigned[0].insert(0, w)
        elif k == n: assigned[n - 1].append(w)
        elif w['end'] - units[k - 1][-1]['end'] <= units[k][0]['start'] - w['end']: assigned[k - 1].append(w)
        else: assigned[k].insert(0, w)
    pieces = []                                 # [source, text, closing mark borrowed from the primary]
    for k in range(n + 1):
        if k in runs:
            run = text_of(runs[k])
            pieces.append(['S', tidy(run) if lexicon else run, ''])
        if k == n: break
        unit, mine = units[k], assigned[k]
        cp, cs, mc = mean(unit), mean(mine), midcap(unit)
        rule = 'keep'
        if mine and cp >= 0 and cs >= 0:
            if cp < PRIMARY_FLOOR and cs > cp: rule = 'floor'
            elif cp < PRIMARY_TRUSTED and cs - cp >= MARGIN and mc >= MIDCAP_MIN: rule = 'margin'
        theirs_text = text_of(mine)
        hindi = lexicon and is_hindi(theirs_text)
        agree = agreement(text_of(unit), theirs_text)
        if rule != 'keep':
            # Unsure, but both heard the same words and nothing looks foreign:
            # the sentence is in the primary's language, which punctuates it.
            if not hindi and mc < MIDCAP_MIN and agree >= SAME_MIN: rule = 'same'
        elif hindi and cp < PRIMARY_SURE:
            # The confidences did not say so, but the India model wrote Hindi.
            rule = 'lexicon'
        kept = rule in ('keep', 'same')
        result['decisions'].append({
            'start': round(unit[0]['start'], 2), 'end': round(unit[-1]['end'], 2), 'words': len(unit),
            'secondary_words': len(mine), 'primary': round(cp, 3), 'secondary': round(cs, 3),
            'midcap': round(mc, 3), 'agree': round(agree, 3), 'hindi': bool(hindi),
            'rule': rule, 'used': 'P' if kept else 'S'})
        if kept:
            pieces.append(['P', text_of(unit), ''])
        else:
            mark = text_of(unit)[-1:]
            pieces.append(['S', tidy(theirs_text) if lexicon else theirs_text, mark if mark in ('.', '?', '!') else ''])
    pieces = [p for p in pieces if p[1]]
    # a word both recognisers reported on either side of a seam: keep one
    out = []
    for src, text, mark in pieces:
        if out and out[-1][0] != src:
            before, after = out[-1][1].split(), text.split()
            if before and after and bare(after[0]) and bare(before[-1]) == bare(after[0]):
                if src == 'S':
                    text = ' '.join(after[1:])
                else:
                    rest = ' '.join(before[:-1])
                    if rest: out[-1][1] = rest
                    else: out.pop()
        if text: out.append([src, text, mark])
    # the secondary's stretches read as sentences
    for i, p in enumerate(out):
        if p[0] != 'S': continue
        last = p[1][-1:]
        if last.isalnum():
            if p[2]: p[1] += p[2]
            elif i + 1 < len(out): p[1] += '.'
    for i, p in enumerate(out):
        if p[0] != 'S': continue
        if (i == 0 or ends_sentence(out[i - 1][1])) and p[1][:1].islower():
            p[1] = p[1][:1].upper() + p[1][1:]
    result['text'] = ' '.join(p[1] for p in out)
    srcs = ''
    for p in out:
        if not srcs or srcs[-1] != p[0]: srcs += p[0]
        if p[0] == 'P': result['primary_chars'] += len(p[1])
        else: result['secondary_chars'] += len(p[1])
    result['sources'] = srcs
    return result

if __name__ == '__main__':
    if sys.argv[1:2] == ['--one']:
        d = json.load(sys.stdin)
        print(json.dumps(blend(d['primary'], d['secondary'], uses_lexicon(d.get('secondary_locale'))), ensure_ascii=False))
    else:
        cases = json.load(open(sys.argv[1]))
        verbose = '-v' in sys.argv
        for c in cases:
            r = blend(c['primary'], c['secondary'], uses_lexicon(c.get('secondary_locale')))
            ok = '' if 'expect' not in c else ('  OK' if (r['text'] == c['expect']['text'] and r['sources'] == c['expect']['sources']) else '  MISMATCH')
            print('== %s %s%s' % (c['name'], r['sources'], ok))
            if verbose:
                for dcs in r['decisions']: print('     ', json.dumps(dcs))
            print('   ->', r['text'])
