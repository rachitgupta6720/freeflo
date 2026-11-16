// freeflo-helper: owns the microphone and on-device transcription for freeflo.
//
//   freeflo-helper serve                      JSON-lines protocol on stdin/stdout
//   freeflo-helper transcribe-file <path> [--locale en-US] [--languages en-IN,hi-IN]
//                                 [--context a,b] [--explain]
//   freeflo-helper blend                      two transcripts (JSON on stdin) -> one
//   freeflo-helper diagnose                   what this Mac offers, as JSON
//   freeflo-helper --version
//
// See helper/README.md for the protocol.

import AVFoundation
import Foundation
import Speech

// A closed stdout must surface as a write error, not kill the process mid-teardown.
signal(SIGPIPE, SIG_IGN)

private func usage() -> Never {
    let text = """
    freeflo-helper \(helperVersion)
    usage:
      freeflo-helper serve
      freeflo-helper transcribe-file <path> [--locale en-US] [--languages en-IN,hi-IN]
                                    [--context word,word] [--explain]
      freeflo-helper blend < {"primary": [segments], "secondary": [segments]}
      freeflo-helper diagnose
      freeflo-helper --version

    """
    FileHandle.standardError.write(text.data(using: .utf8)!)
    exit(64)
}

// MARK: - serve

@MainActor
private func serve() -> Never {
    let coordinator = Coordinator()
    coordinator.announceReady()

    // Commands are read on their own thread and handed to the main queue in
    // arrival order. Handlers never block, so `ping`/`cancel` are answered
    // even while a dictation is starting or finalising.
    let reader = Thread {
        while let line = readLine(strippingNewline: true) {
            let received = DispatchTime.now()
            let trimmed = line.trimmingCharacters(in: .whitespacesAndNewlines)
            if trimmed.isEmpty { continue }
            DispatchQueue.main.async {
                MainActor.assumeIsolated { coordinator.handle(line: trimmed, received: received) }
            }
        }
        // stdin closed: the parent is gone (or done with us).
        DispatchQueue.main.async {
            MainActor.assumeIsolated { coordinator.shutdown(reason: "stdin closed", code: 0) }
        }
    }
    reader.name = "freeflo.helper.stdin"
    reader.stackSize = 1 << 20
    reader.start()

    // Release the mic cleanly on SIGTERM / SIGINT too.
    var sources: [DispatchSourceSignal] = []
    for sig in [SIGTERM, SIGINT, SIGHUP] {
        signal(sig, SIG_IGN)
        let source = DispatchSource.makeSignalSource(signal: sig, queue: .main)
        source.setEventHandler {
            MainActor.assumeIsolated { coordinator.shutdown(reason: "signal \(sig)", code: 0) }
        }
        source.resume()
        sources.append(source)
    }
    withExtendedLifetime(sources) { dispatchMain() }
}

// MARK: - transcribe-file

@MainActor
private func transcribeFile(_ arguments: [String]) async -> Int32 {
    var path: String?
    var localeArg = "auto"
    var languages: [String] = []
    var context: [String] = []
    var explain = false
    func list(_ raw: String) -> [String] {
        raw.split(separator: ",").map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }
    }
    var i = 0
    while i < arguments.count {
        let a = arguments[i]
        if a == "--locale", i + 1 < arguments.count {
            localeArg = arguments[i + 1]; i += 2
        } else if a == "--languages", i + 1 < arguments.count {
            languages = list(arguments[i + 1]); i += 2
        } else if a == "--context", i + 1 < arguments.count {
            context = list(arguments[i + 1]); i += 2
        } else if a == "--explain" {
            explain = true; i += 1
        } else if a.hasPrefix("--") {
            usage()
        } else {
            path = a; i += 1
        }
    }
    guard let path else { usage() }

    func fail(_ code: String, _ message: String) -> Int32 {
        print(JSON.object(["error": code, "message": message]))
        // Last stderr line = the reason, for callers that only look there.
        flushLog()
        FileHandle.standardError.write("freeflo-helper: \(code): \(message)\n".data(using: .utf8)!)
        return 1
    }

    let models = ModelStore()
    let file: AVAudioFile
    do {
        file = try AVAudioFile(forReading: URL(fileURLWithPath: path))
    } catch {
        return fail("bad_file", "Cannot read \(path): \(describe(error))")
    }
    let audioSeconds = Double(file.length) / file.fileFormat.sampleRate

    let started = DispatchTime.now()
    let request = ModelRequest(locale: localeArg, languages: languages, partials: false)
    do {
        let resolved = try await models.resolved(request)
        let pipeline = try await models.take(resolved.key)
        let key = pipeline.key
        if !context.isEmpty {
            let analysisContext = AnalysisContext()
            analysisContext.contextualStrings[.general] = context
            try await pipeline.analyzer.setContext(analysisContext)
        }
        let collectors = pipeline.transcribers.map { transcriber in
            Task { () throws -> (String, [SpokenSegment]) in
                var text = ""
                var segments: [SpokenSegment] = []
                for try await result in transcriber.results where result.isFinal {
                    text += String(result.text.characters)
                    if key.blended { segments.append(SpokenSegment(result)) }
                }
                return (text, segments)
            }
        }
        if let last = try await pipeline.analyzer.analyzeSequence(from: file) {
            try await pipeline.analyzer.finalizeAndFinish(through: last)
        } else {
            await pipeline.analyzer.cancelAndFinishNow()
        }
        var tracks: [(String, [SpokenSegment])] = []
        for collector in collectors { tracks.append(try await collector.value) }

        var text = tracks[0].0.trimmingCharacters(in: .whitespacesAndNewlines)
        var extras: [(String, Any?)] = []
        if key.blended {
            let blend = Blender.blend(
                primary: tracks[0].1, secondary: tracks[1].1,
                lexicon: Blender.usesLexicon(secondaryLocale: key.locales[1]))
            log("blend: \(blend.summary)")
            var used: [String] = []
            var dominant = key.locales[0]
            if !blend.text.isEmpty {
                text = blend.text
                if blend.sources.contains("P") { used.append(key.locales[0]) }
                if blend.sources.contains("S") { used.append(key.locales[1]) }
                if blend.secondaryCharacters > blend.primaryCharacters { dominant = key.locales[1] }
            }
            extras = [("languages", key.locales), ("used", used), ("dominant", dominant), ("sources", blend.sources)]
            if explain {
                extras.append(("decisions", JSON.fragment(blend.decisions.map(\.json))))
                extras.append(("primary_text", tracks[0].0.trimmingCharacters(in: .whitespacesAndNewlines)))
                extras.append(("secondary_text", tracks[1].0.trimmingCharacters(in: .whitespacesAndNewlines)))
                // Everything `blend` needs to replay this decision (and to
                // turn the clip into a fixture for blend_test.py).
                extras.append(("primary_segments", JSON.fragment(tracks[0].1.map(\.json))))
                extras.append(("secondary_segments", JSON.fragment(tracks[1].1.map(\.json))))
            }
        }
        if !resolved.missing.isEmpty { extras.append(("missing", resolved.missing)) }
        var pairs: [(String, Any?)] = [
            ("text", text),
            ("audio_sec", audioSeconds),
            ("elapsed_ms", RawJSON(text: JSON.number(millis(since: started), places: 1))),
            ("locale", key.locale),
        ]
        pairs.append(contentsOf: extras)
        print(JSON.object(pairs))
        return 0
    } catch {
        let e = ModelStore.map(error, locale: languages.first ?? localeArg)
        return fail(e.code, e.message)
    }
}

// MARK: - blend

/// `{"primary": [segments], "secondary": [segments]}` on stdin (segments as
/// in `SpokenSegment.init(json:)`) -> the blended transcript. No audio, no
/// model: this is how the thresholds are tested (helper/tests/blend_test.py).
/// Optional `"secondary_locale"`: the Hindi word list is used when it is
/// missing or a locale of the India model.
private func blendCommand() -> Int32 {
    let data = FileHandle.standardInput.readDataToEndOfFile()
    func parse(_ any: Any?) -> [SpokenSegment]? {
        guard let rows = any as? [Any] else { return nil }
        var out: [SpokenSegment] = []
        for row in rows {
            guard let segment = SpokenSegment(json: row) else { return nil }
            out.append(segment)
        }
        return out
    }
    guard let object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
          let primary = parse(object["primary"]), let secondary = parse(object["secondary"]) else {
        print("{\"error\":\"bad_input\"}")
        return 1
    }
    let blend = Blender.blend(
        primary: primary, secondary: secondary,
        lexicon: Blender.usesLexicon(secondaryLocale: object["secondary_locale"] as? String))
    let output: [String: Any] = [
        "text": blend.text,
        "sources": blend.sources,
        "decisions": blend.decisions.map(\.json),
        "primary_chars": blend.primaryCharacters,
        "secondary_chars": blend.secondaryCharacters,
    ]
    guard let encoded = try? JSONSerialization.data(withJSONObject: output, options: [.sortedKeys, .withoutEscapingSlashes]),
          let text = String(data: encoded, encoding: .utf8) else {
        print("{\"error\":\"internal\"}")
        return 1
    }
    print(text)
    return 0
}

// MARK: - diagnose

/// One JSON object describing what this Mac offers the helper. For support
/// and debugging; touches neither the microphone nor any permission prompt.
@MainActor
private func diagnose() async -> Int32 {
    let models = ModelStore()
    let mic: String
    switch AVCaptureDevice.authorizationStatus(for: .audio) {
    case .authorized: mic = "granted"
    case .notDetermined: mic = "undetermined"
    default: mic = "denied"
    }
    let speechAuth: String
    switch SFSpeechRecognizer.authorizationStatus() {
    case .authorized: speechAuth = "granted"
    case .notDetermined: speechAuth = "undetermined"
    default: speechAuth = "denied"
    }
    let v = ProcessInfo.processInfo.operatingSystemVersion
    print(JSON.object([
        "version": helperVersion,
        "os": "\(v.majorVersion).\(v.minorVersion).\(v.patchVersion)",
        "transcriber_available": SpeechTranscriber.isAvailable,
        "mic": mic,
        "speech_recognition_authorization": speechAuth,   // informational: not required
        "input_device": await InputDevice.defaultName(),
        "system_locale": bcp47(Locale.current),
        "default_locale": await models.defaultLocaleID(),
        "supported": await models.supportedIDs(),
        "installed": await models.installedIDs(),
        "groups": await models.groups(),
        "max_languages": maxBlendedLanguages,
        "reserved": await AssetInventory.reservedLocales.map(bcp47).sorted(),
        "max_reserved": AssetInventory.maximumReservedLocales,
    ]))
    return 0
}

// MARK: - entry point

let arguments = Array(CommandLine.arguments.dropFirst())
switch arguments.first {
case "--version", "-v", "version":
    print("freeflo-helper \(helperVersion)")
    exit(0)
case "serve":
    MainActor.assumeIsolated { serve() }
case "transcribe-file":
    let rest = Array(arguments.dropFirst())
    Task { @MainActor in
        let code = await transcribeFile(rest)
        flushLog()
        exit(code)
    }
    dispatchMain()
case "blend":
    exit(blendCommand())
case "diagnose":
    Task { @MainActor in
        let code = await diagnose()
        exit(code)
    }
    dispatchMain()
default:
    usage()
}
