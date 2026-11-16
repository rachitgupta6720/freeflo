// Two languages in one dictation: merging the transcripts of two recognisers
// that listened to the same audio.
//
// Why: a SpeechTranscriber is built for one locale. `en-IN` writes excellent,
// punctuated English but turns Hindi into "Subhe, Das, Baji, Mujhe, Daftar";
// the multilingual India model (hi-IN, mul-IN and every other Indic locale are
// the same model on macOS 27, and it writes in Latin letters) understands
// Hindi, English and a mix of the two, but punctuates and capitalises less.
// Both run on the same audio inside one SpeechAnalyzer and the transcript is
// chosen sentence by sentence.
//
// How (measured, see helper/README.md "Two languages"): every word of a final
// result carries a confidence and an audio time range. The primary's
// transcript (the monolingual recogniser, e.g. en-IN) is cut into sentences.
// Every secondary word is assigned to the sentence it was spoken in, by the
// END time of the word (both recognisers attach the pause before a word to
// that word, so start times are loose and end times tight), or to a "run"
// between two sentences when the primary did not hear that stretch at all.
// A sentence is handed to the secondary when the primary did not understand
// it ("floor"), or when the primary is unsure, the secondary surer, and the
// sentence looks like the primary's way of writing foreign words: Many Words
// Capitalised ("margin").
//
// Confidences alone are a thin signal (they were measured on synthesised
// speech), so two checks that do not depend on them sit on top:
//  - "lexicon": the secondary is the India model and what it wrote for the
//    sentence is Hindi (two or more words of a fixed list, 30 % of the
//    sentence). Then it is a Hindi sentence even when the primary sounded
//    sure of its guess.
//  - "same": both recognisers wrote the same words and nothing looks
//    foreign. Then it is a sentence in the primary's language, however unsure
//    the primary was, and the primary's punctuated version is kept.
//
// This file is pure: no audio, no Speech sessions, nothing asynchronous. It
// is a line-for-line port of the reference implementation the thresholds were
// tuned with; helper/tests/blend_test.py runs the same fixtures through the
// `blend` subcommand (main.swift). Keep the two in step.

import CoreMedia
import Foundation
import Speech

/// One recognised token with the white space / punctuation attached to it.
struct SpokenWord {
    var text: String
    let confidence: Double   // 0...1, or -1 when the recogniser gave none
    let start: Double        // seconds from the start of the audio; -1: no time range
    let end: Double
}

/// One finalised result of a transcriber (roughly a sentence or a breath).
struct SpokenSegment {
    let text: String
    let start: Double
    let end: Double
    let words: [SpokenWord]
}

extension SpokenSegment {
    /// Needs a transcriber created with `.audioTimeRange` and
    /// `.transcriptionConfidence`. A run without a time range is kept with
    /// start/end -1; `Blender` glues it to a neighbour.
    init(_ result: SpeechTranscriber.Result) {
        var words: [SpokenWord] = []
        for run in result.text.runs {
            let piece = String(result.text[run.range].characters)
            if piece.isEmpty { continue }
            if let range = run.audioTimeRange, range.start.seconds.isFinite, range.end.seconds.isFinite {
                words.append(SpokenWord(
                    text: piece,
                    confidence: run.transcriptionConfidence ?? -1,
                    start: range.start.seconds,
                    end: range.end.seconds))
            } else {
                words.append(SpokenWord(text: piece, confidence: run.transcriptionConfidence ?? -1, start: -1, end: -1))
            }
        }
        let start = result.range.start.seconds
        let end = result.range.end.seconds
        self.init(
            text: String(result.text.characters),
            start: start.isFinite ? start : -1,
            end: end.isFinite ? end : -1,
            words: words)
    }

    /// Mean confidence of the words that have one (-1 when none has).
    var meanConfidence: (mean: Double, count: Int) {
        var sum = 0.0, count = 0
        for word in words where word.confidence >= 0 {
            sum += word.confidence
            count += 1
        }
        return (count > 0 ? sum / Double(count) : -1, count)
    }
}

enum Blender {
    // Thresholds. Measured on an M4 / macOS 27.2 with en-IN as the primary
    // and the India multilingual model as the secondary, over 52 English,
    // Hindi, Hinglish, Tamil, Bangla and Telugu clips (clean and 10 dB SNR)
    // and one 80 s mixed clip.
    /// Below this the primary did not understand the sentence.
    static let primaryFloor = 0.72
    /// At or above this the primary is kept.
    static let primaryTrusted = 0.86
    /// In between, the secondary must be at least this much surer ...
    static let margin = 0.02
    /// ... and the primary's sentence must look "foreign" (see `midcap`).
    static let midcapMin = 0.20
    /// Seconds: tolerance when cutting between two sentences.
    static let cutPad = 0.05
    /// A secondary word ending this long after a sentence ended ...
    static let orphanAfter = 0.15
    /// ... and before the next one began was not heard by the primary.
    static let orphanBefore = 0.05
    /// Fewer words than this between two sentences is boundary jitter.
    static let orphanMinWords = 2
    /// The secondary wrote at least this many Hindi words in the sentence ...
    static let lexiconMinWords = 2
    /// ... and they are at least this share of its words: it is Hindi ...
    static let lexiconMinShare = 0.30
    /// ... unless the primary is this sure of what it heard.
    static let primarySure = 0.93
    /// Both wrote (this share of) the same words: not a foreign sentence.
    static let sameMin = 0.80

    /// Hindi as the India model writes it (Latin letters): particles,
    /// pronouns, auxiliaries, everyday verbs and nouns. Words that are also
    /// English (to, do, is, us, the, me, main, hum, ho, so, par, use, log,
    /// din, mat, hi, teen, char, tab, jab) are deliberately left out. Keep in
    /// step with helper/tests/blend_ref.py.
    static let hindiWords: Set<String> = Set("""
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
        """.split(whereSeparator: { $0.isWhitespace }).map(String.init))

    struct Decision {
        let start: Double
        let end: Double
        let words: Int
        let secondaryWords: Int
        let primary: Double      // mean confidence of the sentence's words (-1: none)
        let secondary: Double    // same for the secondary words assigned to it
        let midcap: Double
        let agree: Double        // share of words both recognisers wrote
        let hindi: Bool          // the secondary's words for it are Hindi (lexicon)
        let rule: String         // "keep" / "same" (primary), "floor" / "margin" / "lexicon" (secondary)
        var kept: Bool { rule == "keep" || rule == "same" }
        var used: String { kept ? "P" : "S" }
    }

    struct Output {
        var text = ""
        var decisions: [Decision] = []
        /// "P" / "S" per stretch of the result, in order ("PSP").
        var sources = ""
        var primaryCharacters = 0
        var secondaryCharacters = 0
        /// Stretches only the secondary heard (between or around sentences).
        var runs = 0
    }

    // MARK: The algorithm

    /// `lexicon`: the secondary is the India multilingual model, so the
    /// Hindi word list says something about what it wrote (see `usesLexicon`).
    static func blend(primary: [SpokenSegment], secondary: [SpokenSegment], lexicon: Bool = true) -> Output {
        let units = sentences(primary)
        let theirs = words(secondary)
        var result = Output()
        if units.isEmpty {
            result.text = trim(secondary.map(\.text).joined())
            if lexicon { result.text = tidy(result.text) }
            result.sources = result.text.isEmpty ? "" : "S"
            result.secondaryCharacters = length(result.text)
            return result
        }
        if theirs.isEmpty {
            result.text = trim(primary.map(\.text).joined())
            result.sources = result.text.isEmpty ? "" : "P"
            result.primaryCharacters = length(result.text)
            return result
        }
        let n = units.count
        var cuts: [Double] = []
        for i in 0..<(n - 1) {
            let aEnd = units[i].last!.end, bStart = units[i + 1].first!.start
            cuts.append(max((aEnd + bStart) / 2.0, aEnd + cutPad))
        }
        var assigned = [[SpokenWord]](repeating: [], count: n)
        var runs: [Int: [SpokenWord]] = [:]   // k -> secondary words spoken BEFORE unit k (k == n: after the last)
        for w in theirs {                     // in time order
            let e = w.end
            if e <= units[0].first!.start + orphanBefore {
                runs[0, default: []].append(w); continue
            }
            if e > units[n - 1].last!.end + orphanAfter {
                runs[n, default: []].append(w); continue
            }
            var k = 0
            while k < n - 1 && e > cuts[k] { k += 1 }
            if k > 0 && e > units[k - 1].last!.end + orphanAfter && e <= units[k].first!.start + orphanBefore {
                runs[k, default: []].append(w); continue
            }
            if k < n - 1 && e > units[k].last!.end + orphanAfter && e <= units[k + 1].first!.start + orphanBefore {
                runs[k + 1, default: []].append(w); continue
            }
            assigned[k].append(w)
        }
        for k in runs.keys.sorted() {         // a lone stray word is boundary jitter, not speech
            guard let run = runs[k], run.count < orphanMinWords else { continue }
            runs[k] = nil
            let w = run[0]
            if k == 0 {
                assigned[0].insert(w, at: 0)
            } else if k == n {
                assigned[n - 1].append(w)
            } else if w.end - units[k - 1].last!.end <= units[k].first!.start - w.end {
                assigned[k - 1].append(w)
            } else {
                assigned[k].insert(w, at: 0)
            }
        }
        result.runs = runs.count

        struct Piece { var source: Character; var text: String; var mark: String }
        var pieces: [Piece] = []
        for k in 0...n {
            if let run = runs[k] {
                let text = textOf(run)
                pieces.append(Piece(source: "S", text: lexicon ? tidy(text) : text, mark: ""))
            }
            if k == n { break }
            let unit = units[k], mine = assigned[k]
            let cp = mean(unit), cs = mean(mine), mc = midcap(unit)
            var rule = "keep"
            if !mine.isEmpty && cp >= 0 && cs >= 0 {
                if cp < primaryFloor && cs > cp {
                    rule = "floor"
                } else if cp < primaryTrusted && cs - cp >= margin && mc >= midcapMin {
                    rule = "margin"
                }
            }
            let theirText = textOf(mine)
            let hindi = lexicon && isHindi(theirText)
            let agree = agreement(textOf(unit), theirText)
            if rule != "keep" {
                // Unsure, but both heard the same words and nothing looks
                // foreign: the sentence is in the primary's language, which
                // punctuates it.
                if !hindi && mc < midcapMin && agree >= sameMin { rule = "same" }
            } else if hindi && cp < primarySure {
                // The confidences did not say so, but the India model wrote Hindi.
                rule = "lexicon"
            }
            let decision = Decision(
                start: unit.first!.start, end: unit.last!.end, words: unit.count,
                secondaryWords: mine.count, primary: cp, secondary: cs, midcap: mc,
                agree: agree, hindi: hindi, rule: rule)
            result.decisions.append(decision)
            if decision.kept {
                pieces.append(Piece(source: "P", text: textOf(unit), mark: ""))
            } else {
                let mark = textOf(unit).last.map(String.init) ?? ""
                pieces.append(Piece(source: "S", text: lexicon ? tidy(theirText) : theirText, mark: [".", "?", "!"].contains(mark) ? mark : ""))
            }
        }
        pieces = pieces.filter { !$0.text.isEmpty }

        // A word both recognisers reported on either side of a seam: keep one.
        var out: [Piece] = []
        for piece in pieces {
            var text = piece.text
            if let previous = out.last, previous.source != piece.source {
                let before = tokens(previous.text), after = tokens(text)
                if let a = before.last, let b = after.first, !bare(b).isEmpty, bare(a) == bare(b) {
                    if piece.source == "S" {
                        text = after.dropFirst().joined(separator: " ")
                    } else {
                        let rest = before.dropLast().joined(separator: " ")
                        if !rest.isEmpty { out[out.count - 1].text = rest } else { out.removeLast() }
                    }
                }
            }
            if !text.isEmpty { out.append(Piece(source: piece.source, text: text, mark: piece.mark)) }
        }

        // The secondary's stretches read as sentences.
        for i in out.indices where out[i].source == "S" {
            guard let last = out[i].text.unicodeScalars.last, isAlnum(last) else { continue }
            if !out[i].mark.isEmpty {
                out[i].text += out[i].mark
            } else if i + 1 < out.count {
                out[i].text += "."
            }
        }
        for i in out.indices where out[i].source == "S" {
            guard i == 0 || endsSentence(out[i - 1].text) else { continue }
            guard let first = out[i].text.unicodeScalars.first, first.properties.isLowercase else { continue }
            let rest = String(String.UnicodeScalarView(out[i].text.unicodeScalars.dropFirst()))
            out[i].text = String(first).uppercased() + rest
        }

        result.text = out.map(\.text).joined(separator: " ")
        var sources = ""
        for p in out {
            if sources.last != p.source { sources.append(p.source) }
            if p.source == "P" { result.primaryCharacters += length(p.text) } else { result.secondaryCharacters += length(p.text) }
        }
        result.sources = sources
        return result
    }

    // MARK: Pieces

    /// All words in order. A run without a time range is glued to the
    /// previous word of the same segment (or put in front of the next one).
    static func words(_ segments: [SpokenSegment]) -> [SpokenWord] {
        var out: [SpokenWord] = []
        for segment in segments {
            var orphan = ""
            let first = out.count
            for w in segment.words {
                if w.start < 0 || w.end < 0 {
                    if out.count > first { out[out.count - 1].text += w.text } else { orphan += w.text }
                    continue
                }
                out.append(SpokenWord(text: orphan + w.text, confidence: w.confidence, start: w.start, end: max(w.start, w.end)))
                orphan = ""
            }
        }
        return out
    }

    /// The primary cut into sentences: after every word ending in . ? ! and
    /// at every segment end.
    private static func sentences(_ segments: [SpokenSegment]) -> [[SpokenWord]] {
        var units: [[SpokenWord]] = []
        for segment in segments {
            var current: [SpokenWord] = []
            for w in words([segment]) {
                current.append(w)
                if endsSentence(w.text) {
                    units.append(current)
                    current = []
                }
            }
            if !current.isEmpty { units.append(current) }
        }
        return units.filter { !textOf($0).isEmpty }
    }

    /// Sum in word order, then divide (keeps results bit-identical with the
    /// reference implementation).
    static func mean(_ words: [SpokenWord]) -> Double {
        var sum = 0.0, count = 0
        for w in words where w.confidence >= 0 {
            sum += w.confidence
            count += 1
        }
        return count > 0 ? sum / Double(count) : -1
    }

    /// Share of the sentence's words, the first excluded, that start with a
    /// capital letter (the pronoun I excluded). en-* recognisers write words
    /// they do not know as Proper Nouns.
    static func midcap(_ unit: [SpokenWord]) -> Double {
        guard unit.count >= 2 else { return 0 }
        var n = 0
        for w in unit.dropFirst() {
            let t = trim(w.text)
            guard let letter = t.unicodeScalars.first(where: isAlpha), letter.properties.isUppercase else { continue }
            if bare(t) == "i" || t.hasPrefix("I'") || t.hasPrefix("I\u{2019}") { continue }
            n += 1
        }
        return Double(n) / Double(unit.count - 1)
    }

    // MARK: Signals that do not depend on confidences

    /// The Hindi word list only says something about the India multilingual
    /// model (on macOS 27: every non-English locale of India).
    static func usesLexicon(secondaryLocale: String?) -> Bool {
        guard let secondaryLocale, !secondaryLocale.isEmpty else { return true }
        return ModelStore.modelGroup(secondaryLocale) == "india"
    }

    /// (Hindi words, words) in a stretch of the secondary's text.
    static func hindiEvidence(_ text: String) -> (count: Int, total: Int) {
        var count = 0, total = 0
        for token in tokens(text) {
            let b = bare(token)
            if b.isEmpty { continue }
            total += 1
            if hindiWords.contains(b) { count += 1 }
        }
        return (count, total)
    }

    static func isHindi(_ text: String) -> Bool {
        let (count, total) = hindiEvidence(text)
        return count >= lexiconMinWords && Double(count) >= lexiconMinShare * Double(total)
    }

    /// Share of the words the two recognisers have in common (each word
    /// counted as often as both wrote it), relative to the longer of the two.
    static func agreement(_ a: String, _ b: String) -> Double {
        let mine = tokens(a).map(bare).filter { !$0.isEmpty }
        let theirs = tokens(b).map(bare).filter { !$0.isEmpty }
        if mine.isEmpty || theirs.isEmpty { return 0 }
        var left: [String: Int] = [:]
        for word in theirs { left[word, default: 0] += 1 }
        var common = 0
        for word in mine where (left[word] ?? 0) > 0 {
            left[word]! -= 1
            common += 1
        }
        return Double(common) / Double(max(mine.count, theirs.count))
    }

    /// The India model capitalises Hindi words at random ("gaya Tha", "Agle
    /// hafte"): known Hindi words are written in lower case.
    static func tidy(_ text: String) -> String {
        tokens(text).map { token -> String in
            guard let first = token.unicodeScalars.first, first.properties.isUppercase else { return token }
            let rest = String(String.UnicodeScalarView(token.unicodeScalars.dropFirst()))
            guard rest == rest.lowercased(), hindiWords.contains(bare(token)) else { return token }
            return token.lowercased()
        }.joined(separator: " ")
    }

    /// First letter in upper case (for the running text; the blend does its
    /// own sentence casing).
    static func sentenceCased(_ text: String) -> String {
        guard let first = text.unicodeScalars.first, first.properties.isLowercase else { return text }
        return String(first).uppercased() + String(String.UnicodeScalarView(text.unicodeScalars.dropFirst()))
    }

    private static func textOf(_ words: [SpokenWord]) -> String {
        trim(words.map(\.text).joined())
    }

    private static func trim(_ s: String) -> String {
        s.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    static func tokens(_ s: String) -> [String] {
        s.split(whereSeparator: { $0.isWhitespace }).map(String.init)
    }

    private static func endsSentence(_ text: String) -> Bool {
        guard let last = trim(text).unicodeScalars.last else { return false }
        return last == "." || last == "?" || last == "!"
    }

    /// Code points, as Python's len() counts them.
    private static func length(_ s: String) -> Int { s.unicodeScalars.count }

    private static func isAlpha(_ u: Unicode.Scalar) -> Bool {
        switch u.properties.generalCategory {
        case .uppercaseLetter, .lowercaseLetter, .titlecaseLetter, .modifierLetter, .otherLetter: return true
        default: return false
        }
    }

    private static func isAlnum(_ u: Unicode.Scalar) -> Bool {
        isAlpha(u) || u.properties.numericType != nil
    }

    /// Letters and digits only, lowercased: for comparing two spellings of a word.
    static func bare(_ word: String) -> String {
        String(String.UnicodeScalarView(word.lowercased().unicodeScalars.filter(isAlnum)))
    }
}

// MARK: - JSON (the `blend` subcommand, transcribe-file --explain, tests)

/// A JSON number as the nearest Double. JSONSerialization can hand out a
/// decimal number whose `doubleValue` is one ulp off; going through its
/// decimal text is exact, which keeps the fixture means bit-identical with
/// the reference implementation.
private func number(_ any: Any?) -> Double? {
    guard let n = any as? NSNumber, CFGetTypeID(n) != CFBooleanGetTypeID() else { return nil }
    return Double(n.stringValue) ?? n.doubleValue
}

extension SpokenSegment {
    /// `{"text": "...", "start": 0.0, "end": 1.2, "words": [["word ", 0.93, 0.0, 0.4], ...]}`
    /// (`words` may be absent; times -1 mean "no time range").
    init?(json: Any) {
        guard let object = json as? [String: Any] else { return nil }
        var words: [SpokenWord] = []
        if let raw = object["words"] {
            guard let rows = raw as? [Any] else { return nil }
            for item in rows {
                guard let row = item as? [Any], row.count >= 4, let text = row[0] as? String,
                      let confidence = number(row[1]), let start = number(row[2]),
                      let end = number(row[3]) else { return nil }
                words.append(SpokenWord(text: text, confidence: confidence, start: start, end: end))
            }
        }
        if let text = object["text"], !(text is String) { return nil }
        self.init(
            text: (object["text"] as? String) ?? words.map(\.text).joined(),
            start: number(object["start"]) ?? words.first?.start ?? 0,
            end: number(object["end"]) ?? words.last?.end ?? 0,
            words: words)
    }

    var json: [String: Any] {
        [
            "text": text, "start": start, "end": end,
            "words": words.map { [$0.text, $0.confidence, $0.start, $0.end] as [Any] },
        ]
    }
}

/// Rounded on the exact binary value, as Python's round() does (so the
/// numbers match the reference implementation's).
private func rounded(_ value: Double, _ places: Int) -> Double {
    guard value.isFinite else { return value }
    return Double(String(format: "%.\(places)f", value)) ?? value
}

extension Blender.Decision {
    var json: [String: Any] {
        [
            "start": rounded(start, 2), "end": rounded(end, 2), "words": words,
            "secondary_words": secondaryWords, "primary": rounded(primary, 3),
            "secondary": rounded(secondary, 3), "midcap": rounded(midcap, 3),
            "agree": rounded(agree, 3), "hindi": hindi,
            "rule": rule, "used": used,
        ]
    }
}

extension Blender.Output {
    /// One text-free line for the log, for tuning the thresholds from real use.
    var summary: String {
        var floor = 0, margin = 0, lexicon = 0, same = 0
        var confidences: [Double] = []
        for d in decisions {
            switch d.rule {
            case "floor": floor += 1
            case "margin": margin += 1
            case "lexicon": lexicon += 1
            case "same": same += 1
            default: break
            }
            if d.primary >= 0 { confidences.append(d.primary) }
        }
        let kept = decisions.count - floor - margin - lexicon
        let low = confidences.min().map { JSON.number($0, places: 3) } ?? "-"
        let avg = confidences.isEmpty ? "-" : JSON.number(confidences.reduce(0, +) / Double(confidences.count), places: 3)
        return "\(decisions.count) sentences, kept \(kept) (\(same) by same words), handed over \(floor) by floor + \(margin) by margin + \(lexicon) by lexicon, \(runs) runs; primary confidence min \(low) mean \(avg); sources \(sources.isEmpty ? "-" : sources)"
    }
}
