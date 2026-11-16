// The serve loop's brain: command handling and the dictation session state machine.

import AVFoundation
import Foundation
import Speech

/// One recogniser's running transcript within a dictation.
@MainActor
final class Track {
    var finalized = ""
    var volatile = ""
    /// Final results with word confidences and times (two-language mode only).
    var segments: [SpokenSegment] = []

    var text: String {
        (finalized + volatile).trimmingCharacters(in: .whitespacesAndNewlines)
    }
}

/// One dictation, from `start` to its single terminal event
/// (`final`, `cancelled` or `error`).
@MainActor
final class Session {
    enum Phase { case starting, recording, finishing, ended }

    let id: Int
    let request: ModelRequest
    let context: [String]
    var localeRequested: String { request.label }
    var partials: Bool { request.partials }

    var phase = Phase.starting
    var cancelled = false
    var stopReceived: DispatchTime?
    var tailSeconds = 0.0
    /// True from the moment the capture start has been issued until its
    /// result is known. While set, stop/cancel are deferred to `runStart`.
    var captureInFlight = false

    var capture: Capture?
    var feeder: AudioFeeder?
    var pipeline: Pipeline?
    let input: AsyncStream<AnalyzerInput>
    let inputContinuation: AsyncStream<AnalyzerInput>.Continuation

    /// One per transcriber of the pipeline, in `key.locales` order.
    var tracks: [Track] = [Track()]
    var resultsTasks: [Task<Void, Never>] = []
    /// The track the running (partial) text is taken from: the primary
    /// unless its last confident sentence looked like it did not understand.
    var lead = 0
    /// Two languages and the second is the India model: its Hindi word list
    /// helps decide which track the running text follows.
    var lexicon = false
    /// The configuration actually running, once known.
    var key: ModelKey?
    /// Requested languages left out of this dictation (as the app spelled them).
    var missing: [String] = []
    var lastPartial = ""
    var analysisError: Error?
    var captureLost: CaptureError?
    var startReceived = DispatchTime.now()

    /// Fires once this session no longer holds (and never again will hold)
    /// the microphone. The next session waits on it before opening the mic.
    let micReleased = Latch()

    init(id: Int, request: ModelRequest, context: [String]) {
        self.id = id
        self.request = request
        self.context = context
        // The analyzer consumes far faster than real time, so this buffer is
        // normally empty and a dictation of any length holds no audio here.
        // The cap (6000 buffers: 10 minutes of 100 ms tap buffers, ~19 MB of
        // 16 kHz Int16) only matters if the analyzer stalls: memory stays
        // bounded, at the cost of dropping the oldest unprocessed audio.
        let (stream, continuation) = AsyncStream<AnalyzerInput>.makeStream(bufferingPolicy: .bufferingNewest(6000))
        self.input = stream
        self.inputContinuation = continuation
    }

    /// The lead track's text, or another track's when the lead has none.
    var text: String {
        let order = [lead] + tracks.indices.filter { $0 != lead }
        for index in order where index < tracks.count {
            let t = tracks[index].text
            if !t.isEmpty { return t }
        }
        return ""
    }

    func cancelResults() {
        for task in resultsTasks { task.cancel() }
    }
}

enum Outcome {
    case final(text: String, audioSeconds: Double, silent: Bool = false, micLost: Bool = false, extras: [(String, Any?)] = [])
    case cancelled
    case error(code: String, message: String)
}

@MainActor
final class Coordinator {
    private let out = Emitter.shared
    private let models = ModelStore()

    private var sessions: [Int: Session] = [:]   // not yet ended
    private var active: Session?                 // starting or recording
    private var lastSession: Session?
    /// Running downloads: resolved locale -> every spelling the app asked
    /// for it with (each one gets the events, so no request goes unanswered).
    private var installing: [String: [String]] = [:]
    private var permissionRequestPending = false
    private var shuttingDown = false

    // Timeouts (seconds). See README "Timeouts".
    private static let captureStartTimeout = 4.0   // opening the mic (Bluetooth can take 1-2 s)
    private static let captureStopTimeout = 4.0    // closing the mic, before declaring CoreAudio wedged
    private static let captureStopGrace = 1.0      // ...but the transcript does not wait that long
    private static let prewarmTimeout = 30.0       // prewarm may load a cold model from disk
    private static let pipelineTimeout = 8.0       // loading the speech model / preparing an analyzer
    private static let analyzerStartTimeout = 5.0  // attaching the audio stream to the analyzer
    private static let finalizeTimeout = 4.0       // analyzer finalisation (normally 50-250 ms)
    private static let drainTimeout = 0.13         // one tap buffer (100 ms) plus jitter

    // MARK: Startup / shutdown

    func announceReady() {
        let v = ProcessInfo.processInfo.operatingSystemVersion
        let os = v.patchVersion == 0 ? "\(v.majorVersion).\(v.minorVersion)" : "\(v.majorVersion).\(v.minorVersion).\(v.patchVersion)"
        out.send(["event": "ready", "version": helperVersion, "os": os, "pid": Int(ProcessInfo.processInfo.processIdentifier)])
        log("serving (version \(helperVersion), macOS \(os)\(FakeMic.isActive ? ", FAKE MIC \(FakeMic.setting!)" : ""))")
    }

    /// Releases the microphone and exits. Bounded: exits within ~1.5 s even
    /// if CoreAudio is not answering.
    func shutdown(reason: String, code: Int32) {
        guard !shuttingDown else { return }
        shuttingDown = true
        log("shutting down: \(reason)")
        DispatchQueue.global().asyncAfter(deadline: .now() + 1.5) {
            Emitter.shared.flush(timeout: 0.2)
            _exit(code)  // no atexit handlers: something is already stuck
        }
        let live = Array(sessions.values)
        Task {
            for s in live {
                if let capture = s.capture {
                    s.capture = nil
                    _ = await AudioQueue.run("stop", timeout: 1.0) { capture.stop() }
                }
                if s.phase != .ended { self.end(s, .cancelled, rearm: false) }
            }
            self.out.flush()
            flushLog()
            exit(code)
        }
    }

    private func die(code: Int32, _ why: String) -> Never {
        log("exiting (\(code)): \(why)")
        out.flush()
        flushLog()
        _exit(code)  // skip atexit handlers: a thread is stuck inside CoreAudio
    }

    // MARK: Command dispatch

    func handle(line: String, received: DispatchTime) {
        guard !shuttingDown else { return }
        guard let data = line.data(using: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data),
              let command = object as? [String: Any] else {
            // The line itself is not echoed: errors end up in the app's log,
            // and a mangled command could carry the user's vocabulary.
            sendError(id: nil, code: "bad_command", message: "Not a JSON object (\(line.utf8.count) bytes).")
            return
        }
        guard let name = command["cmd"] as? String else {
            sendError(id: JSON.int(command["id"]), code: "bad_command", message: "Missing \"cmd\".")
            return
        }
        switch name {
        case "ping":
            out.send(["event": "pong", "seq": JSON.echoNumber(command["seq"])])
        case "status":
            Task { await self.sendStatus() }
        case "prewarm":
            cmdPrewarm(command)
        case "start":
            cmdStart(command, received: received)
        case "stop":
            cmdStop(command, received: received)
        case "cancel":
            cmdCancel(command)
        case "install":
            cmdInstall(command)
        case "request_permissions":
            cmdRequestPermissions()
        case "quit":
            shutdown(reason: "quit command", code: 0)
        default:
            sendError(id: JSON.int(command["id"]), code: "bad_command", message: "Unknown command \"\(name)\".", extra: name)
        }
    }

    /// `locale` and `cmd` are optional extras: prewarm/install failures carry
    /// both, so the client can tell which request (and which language) failed.
    private func sendError(id: Int?, code: String, message: String, extra cmd: String? = nil, locale: String? = nil) {
        log("error \(code)\(id.map { " (id \($0))" } ?? ""): \(message)")
        var event: [(String, Any?)] = [("event", "error"), ("id", id), ("code", code), ("message", message)]
        if let locale { event.append(("locale", locale)) }
        if let cmd { event.append(("cmd", cmd)) }
        out.send(pairs: event)
    }

    // MARK: status / permissions

    private func micStatus() -> String {
        if let fake = FakeMic.setting { return fake == "@denied" ? "denied" : "granted" }
        switch AVCaptureDevice.authorizationStatus(for: .audio) {
        case .authorized: return "granted"
        case .notDetermined: return "undetermined"
        default: return "denied"   // denied or restricted
        }
    }

    private func sendStatus() async {
        async let supported = models.supportedIDs()
        async let installed = models.installedIDs()
        async let preferred = models.defaultLocaleID()
        async let device: String? = FakeMic.isActive ? FakeMic.deviceName : InputDevice.defaultName()
        async let groups = models.groups()
        // "speech": SpeechAnalyzer/SpeechTranscriber run entirely on device
        // and do not consult Speech Recognition authorisation (verified: they
        // transcribe while SFSpeechRecognizer.authorizationStatus() is
        // .notDetermined), so only the microphone needs consent.
        out.send([
            "event": "status",
            "mic": micStatus(),
            "speech": "not_required",
            "supported": await supported,
            "installed": await installed,
            "default_locale": await preferred,
            "input_device": await device,
            // Locales served by one shared model (choose one per dictation),
            // and how many languages one dictation can listen for.
            "groups": await groups,
            "max_languages": maxBlendedLanguages,
        ])
    }

    private func cmdRequestPermissions() {
        guard micStatus() == "undetermined", !permissionRequestPending else {
            Task { await self.sendStatus() }
            return
        }
        permissionRequestPending = true
        Task {
            // Report the current state right away (the prompt can sit there
            // for as long as the user likes), then again once it is answered.
            await self.sendStatus()
            let granted = await AVCaptureDevice.requestAccess(for: .audio)
            self.permissionRequestPending = false
            log("microphone permission \(granted ? "granted" : "denied")")
            await self.sendStatus()
        }
    }

    // MARK: prewarm / install

    /// `locale` (default: the system's), `partials` (default true) and the
    /// optional `fast_partials` (default false) select the transcriber. The
    /// optional `languages` (most used first) asks for two recognisers on
    /// the same audio; it wins over `locale`.
    private static func modelRequest(_ command: [String: Any]) -> ModelRequest {
        let languages = ((command["languages"] as? [Any]) ?? []).compactMap { $0 as? String }
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }.filter { !$0.isEmpty }
        return ModelRequest(
            locale: (command["locale"] as? String) ?? "auto",
            languages: Array(languages.prefix(4)),
            partials: (command["partials"] as? Bool) ?? true,
            fast: (command["fast_partials"] as? Bool) ?? false)
    }

    private static func isLocaleError(_ error: Error) -> Bool {
        let code = ModelStore.map(error, locale: "").code
        return code == "locale_unsupported" || code == "locale_not_installed"
    }

    private func cmdPrewarm(_ command: [String: Any]) {
        let request = Self.modelRequest(command)
        let locale = request.label
        let models = self.models
        Task {
            let result = await withDeadline(Self.prewarmTimeout, "loading the speech model") {
                try await models.prewarm(request)
            }
            switch result {
            case .success(let resolved):
                let key = resolved.key
                var event: [(String, Any?)] = [("event", "prewarmed"), ("locale", locale), ("resolved", key.locale)]
                if key.blended { event.append(("languages", key.locales)) }
                if !resolved.missing.isEmpty { event.append(("missing", resolved.missing)) }
                self.out.send(pairs: event)
            case .failure(let error):
                if error is TimeoutError { models.dropStandby() }
                let e = ModelStore.map(error, locale: locale)
                self.sendError(id: nil, code: e.code, message: e.message, extra: "prewarm", locale: locale)
            }
        }
    }

    private func cmdInstall(_ command: [String: Any]) {
        guard let locale = command["locale"] as? String, !locale.isEmpty else {
            sendError(id: nil, code: "bad_command", message: "install requires \"locale\".", extra: "install")
            return
        }
        let models = self.models
        Task {
            let id: String
            do {
                id = bcp47(try await models.resolve(locale))
            } catch {
                let e = ModelStore.map(error, locale: locale)
                self.sendError(id: nil, code: "locale_unsupported", message: "Could not install \(locale): \(e.message)", extra: "install", locale: locale)
                return
            }
            // Already running: this spelling joins it and gets its events.
            if let spellings = self.installing[id] {
                if !spellings.contains(locale) { self.installing[id] = spellings + [locale] }
                return
            }
            self.installing[id] = [locale]
            do {
                let outcome = try await models.install(requested: locale) { fraction in
                    for name in self.installing[id] ?? [locale] {
                        self.out.send(["event": "install_progress", "locale": name, "fraction": fraction])
                    }
                }
                let installed = await models.installedIDs()
                for name in self.installing.removeValue(forKey: id) ?? [locale] {
                    self.out.send(pairs: [
                        ("event", "installed"), ("locale", name), ("resolved", outcome.resolved),
                        ("already", outcome.already), ("installed", installed),
                    ])
                }
            } catch {
                for name in self.installing.removeValue(forKey: id) ?? [locale] {
                    let e = ModelStore.mapInstall(error, locale: name)
                    self.sendError(id: nil, code: e.code, message: "Could not install \(name): \(e.message)", extra: "install", locale: name)
                }
            }
        }
    }

    // MARK: start

    private func cmdStart(_ command: [String: Any], received: DispatchTime) {
        guard let id = JSON.int(command["id"]) else {
            sendError(id: nil, code: "bad_command", message: "start requires an integer \"id\".", extra: "start")
            return
        }
        let context = ((command["context"] as? [Any]) ?? []).compactMap { $0 as? String }
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }.filter { !$0.isEmpty }

        // A new start while a session is active implicitly cancels the old one.
        if let current = active { cancel(current) }
        if let stale = sessions[id], stale.phase != .ended { cancel(stale) }

        let previous = lastSession
        let session = Session(id: id, request: Self.modelRequest(command), context: context)
        session.startReceived = received
        sessions[id] = session
        active = session
        lastSession = session
        Task { await self.runStart(session, after: previous) }
    }

    private func runStart(_ s: Session, after previous: Session?) async {
        // Never open the mic while the previous session still holds it.
        if let previous, !previous.micReleased.isFired {
            await previous.micReleased.wait(timeout: Self.captureStopTimeout + 2)
        }
        if s.phase == .ended { return }

        // 1. Microphone permission.
        switch micStatus() {
        case "denied":
            end(s, .error(code: "mic_denied", message: "Microphone access is denied. Allow it in System Settings > Privacy & Security > Microphone."))
            return
        case "undetermined":
            log("microphone permission undetermined; asking")
            let granted = await AVCaptureDevice.requestAccess(for: .audio)
            if s.phase == .ended { return }
            if !granted {
                end(s, .error(code: "mic_denied", message: "Microphone access was not granted."))
                return
            }
        default:
            break
        }

        // 2. Locale(s): supported and installed? (1-2 ms; decided before the
        //    mic opens.) Extra languages that are not ready are left out and
        //    reported in `started.missing`.
        var key: ModelKey
        do {
            let resolved = try await models.resolved(s.request)
            key = resolved.key
            s.missing = resolved.missing
        } catch {
            if s.phase != .ended {
                let e = ModelStore.map(error, locale: s.localeRequested)
                end(s, .error(code: e.code, message: e.message))
            }
            return
        }
        if s.phase == .ended { return }

        // 3. The analyzer's audio format. Known from any earlier prewarm or
        //    dictation; otherwise the pipeline has to be built first.
        var pipeline: Pipeline?
        var pipelineError: Error?
        var knownFormat = models.format(for: key)
        if knownFormat == nil {
            var acquired = await acquirePipeline(key, context: s.context)
            if case .failure(let error) = acquired, let single = fallback(s, from: key, after: error) {
                key = single
                acquired = await acquirePipeline(key, context: s.context)
            }
            switch acquired {
            case .success(let p):
                pipeline = p
                knownFormat = p.format
            case .failure(let error):
                if s.phase != .ended {
                    let e = ModelStore.map(error, locale: s.localeRequested)
                    end(s, .error(code: e.code, message: e.message))
                }
                return
            }
            if s.phase == .ended {
                pipeline?.discard()
                models.rearm()
                return
            }
        }
        guard let format = knownFormat else { return }

        // 4. Open the microphone. From here on audio is buffered into the
        //    session's stream, so nothing is lost while step 5 completes.
        let id = s.id
        let continuation = s.inputContinuation
        let feeder = AudioFeeder(
            target: format,
            sink: { buffer in continuation.yield(AnalyzerInput(buffer: buffer)) },
            level: { value, delay in Emitter.shared.level(id: id, rms: value, after: delay) })
        let capture = makeCapture(feeder: feeder) { [weak self, weak s] error in
            DispatchQueue.main.async {
                MainActor.assumeIsolated {
                    guard let self, let s else { return }
                    s.captureLost = error
                    self.captureWasLost(s)
                }
            }
        }
        s.feeder = feeder
        s.captureInFlight = true
        async let captureResult = AudioQueue.run("start", timeout: Self.captureStartTimeout) { try capture.start() }

        // 5. Meanwhile, attach an analyzer: the prewarmed one when there is
        //    one (instant), else a new one. One retry with a fresh pipeline,
        //    because a long-idle standby analyzer may have gone stale.
        for attempt in 1...2 {
            if pipeline == nil {
                let acquired = attempt == 1
                    ? await acquirePipeline(key, context: s.context)
                    : await acquirePipeline(key, context: s.context, fresh: true)
                switch acquired {
                case .success(let p): pipeline = p
                case .failure(let error): pipelineError = error
                }
            }
            guard let p = pipeline else { break }
            guard p.format.isEqual(format) else {
                pipelineError = HelperError.internalError("analyzer audio format changed unexpectedly")
                p.discard()
                pipeline = nil
                models.forgetFormat(for: key)
                break
            }
            if let error = await attach(s, to: p) {
                log("session \(s.id): analyzer failed to start (attempt \(attempt)): \(describe(error))")
                p.discard()
                pipeline = nil
                pipelineError = error
                continue
            }
            pipelineError = nil
            break
        }
        // Two languages that will not load together: the primary alone is
        // far better than no dictation (only if it takes the same audio
        // format, since the microphone is already being opened in it).
        if pipeline == nil, let error = pipelineError, let single = fallback(s, from: key, after: error) {
            switch await acquirePipeline(single, context: s.context) {
            case .success(let p) where p.format.isEqual(format):
                if let failure = await attach(s, to: p) {
                    p.discard()
                    pipelineError = failure
                } else {
                    key = single
                    pipeline = p
                    pipelineError = nil
                }
            case .success(let p):
                p.discard()
            case .failure(let failure):
                pipelineError = failure
            }
        }
        s.pipeline = pipeline
        s.key = key

        // 6. Both halves are in; settle the outcome.
        let started = await captureResult
        s.captureInFlight = false
        switch started {
        case .failure(let wedged as AudioWedged):
            abandonAnalysis(s)
            end(s, .error(code: "engine_failed", message: "\(wedged.description). The helper is restarting."), rearm: false)
            die(code: exitCodeAudioWedged, wedged.description)
        case .failure(let error):
            abandonAnalysis(s)
            let c = (error as? CaptureError) ?? .engineFailed(describe(error))
            end(s, .error(code: c.code, message: c.description))
            return
        case .success:
            s.capture = capture
        }

        if pipeline == nil {
            let wedged = await releaseCapture(s)
            abandonAnalysis(s)
            let e = ModelStore.map(pipelineError ?? HelperError.internalError("no analyzer"), locale: s.localeRequested)
            end(s, .error(code: e.code, message: "Could not start transcription: \(e.message)"))
            if wedged { dieWedged("stop") }
            return
        }
        if s.cancelled {
            s.phase = .finishing
            if active === s { active = nil }
            await runCancel(s)
            return
        }

        s.phase = .recording
        var startedEvent: [(String, Any?)] = [("event", "started"), ("id", s.id), ("locale", key.locale)]
        if key.blended { startedEvent.append(("languages", key.locales)) }
        if !s.missing.isEmpty { startedEvent.append(("missing", s.missing)) }
        out.send(pairs: startedEvent)
        out.openLevels(for: s.id)
        feeder.setLevelsEnabled(true)
        log("session \(s.id) started in \(JSON.number(millis(since: s.startReceived), places: 1)) ms (\(key))")

        if s.stopReceived != nil {
            // Released before the mic was even live.
            s.phase = .finishing
            if active === s { active = nil }
            await runStop(s)
        } else if s.captureLost != nil {
            captureWasLost(s)
        }
    }

    /// The input device went away mid-dictation and could not be reopened.
    /// Nothing more can be recorded, so the session is wrapped up right now
    /// instead of leaving the user talking to a dead microphone: whatever was
    /// already said comes back as `final`; if nothing was, as an `error`.
    private func captureWasLost(_ s: Session) {
        guard s.phase == .recording else { return }  // starting: runStart checks; finishing: already wrapping up
        log("session \(s.id): microphone lost (\(s.captureLost?.description ?? "?")); finishing with the audio captured so far")
        s.stopReceived = DispatchTime.now()
        s.phase = .finishing
        if active === s { active = nil }
        Task { await self.runStop(s) }
    }

    /// For a two-language key whose pipeline failed for a reason other than
    /// the languages themselves (which `resolved` already vetted): the
    /// primary alone. Records the dropped language as missing. nil: no fallback.
    private func fallback(_ s: Session, from key: ModelKey, after error: Error) -> ModelKey? {
        guard key.blended, !Self.isLocaleError(error), s.phase != .ended else { return nil }
        let single = models.single(key)
        log("session \(s.id): could not load \(key) (\(describe(error))); continuing with \(single.locale) alone")
        s.missing.append(contentsOf: key.locales.dropFirst())
        return single
    }

    /// Starts collecting results and attaches the session's audio stream to
    /// the pipeline's analyzer. Returns the error when that failed.
    private func attach(_ s: Session, to p: Pipeline) async -> Error? {
        collectResults(for: s, from: p)
        let analyzer = p.analyzer
        let input = s.input
        let startResult = await withDeadline(Self.analyzerStartTimeout, "analyzer start") {
            try await analyzer.start(inputSequence: input)
        }
        if case .failure(let error) = startResult {
            s.cancelResults()
            return error
        }
        return nil
    }

    /// A ready analyzer with the vocabulary applied. Bounded in time: a
    /// speech model that will not load must not leave a session hanging.
    private func acquirePipeline(_ key: ModelKey, context: [String], fresh: Bool = false) async -> Result<Pipeline, Error> {
        let models = self.models
        let result = await withDeadline(Self.pipelineTimeout, "loading the speech model") {
            fresh ? try await models.rebuild(key) : try await models.take(key)
        }
        switch result {
        case .success(let pipeline):
            await applyContext(context, to: pipeline)
            return .success(pipeline)
        case .failure(let error):
            if error is TimeoutError { models.dropStandby() }
            return .failure(error)
        }
    }

    /// Vocabulary hints. SpeechAnalyzer accepts them as
    /// AnalysisContext.contextualStrings[.general].
    private func applyContext(_ strings: [String], to pipeline: Pipeline) async {
        guard !strings.isEmpty else { return }
        let context = AnalysisContext()
        context.contextualStrings[.general] = Array(strings.prefix(500))
        let analyzer = pipeline.analyzer
        let result = await withDeadline(2.0, "setContext") { try await analyzer.setContext(context) }
        if case .failure(let error) = result {
            log("could not apply contextual strings (continuing without): \(describe(error))")
        }
    }

    /// One results task per transcriber (one track each).
    private func collectResults(for s: Session, from pipeline: Pipeline) {
        s.cancelResults()
        let blended = pipeline.key.blended
        s.tracks = pipeline.transcribers.map { _ in Track() }
        s.lead = 0
        s.lexicon = blended && Blender.usesLexicon(secondaryLocale: pipeline.key.locales[1])
        s.resultsTasks = pipeline.transcribers.enumerated().map { index, transcriber in
            Task { @MainActor [weak self] in
                do {
                    for try await result in transcriber.results {
                        if Task.isCancelled { return }
                        self?.apply(result, track: index, blended: blended, to: s)
                    }
                } catch {
                    if Task.isCancelled { return }
                    if s.analysisError == nil { s.analysisError = error }
                    log("session \(s.id): transcription stream \(index) failed: \(describe(error))")
                }
            }
        }
    }

    private func apply(_ result: SpeechTranscriber.Result, track index: Int, blended: Bool, to s: Session) {
        guard index < s.tracks.count else { return }
        let track = s.tracks[index]
        let text = String(result.text.characters)
        if result.isFinal {
            track.finalized += text
            // Keep any part of the running guess that lies beyond what was
            // just finalised, so the partial text does not flicker shorter.
            if track.volatile.hasPrefix(text) {
                track.volatile.removeFirst(text.count)
            } else {
                track.volatile = ""
            }
            if blended {
                let segment = SpokenSegment(result)
                track.segments.append(segment)
                if index == 0 {
                    // The running text follows the primary while it is sure
                    // of itself, the other language once it clearly is not.
                    // (Partial results carry no confidences; finals do.)
                    let (mean, count) = segment.meanConfidence
                    if count >= 3 { s.lead = mean < Blender.primaryFloor ? 1 : 0 }
                }
            }
        } else {
            track.volatile = text
        }
        if blended, index == 1, s.lexicon {
            // The India model's own words say which language is being spoken
            // well before the primary finalises anything: its running guess
            // (or, right after a final, the sentence it just finished).
            let recent = track.volatile.isEmpty ? text : track.volatile
            let (count, total) = Blender.hindiEvidence(recent)
            if total >= 3 {
                if Blender.isHindi(recent) { s.lead = 1 } else if count == 0 { s.lead = 0 }
            }
        }
        guard s.partials, s.phase == .recording else { return }
        var partial = s.text
        if s.lexicon, s.lead == 1, s.tracks.count > 1, !s.tracks[1].text.isEmpty {
            // Shown the way the final text will write it.
            partial = Blender.sentenceCased(Blender.tidy(partial))
        }
        if partial != s.lastPartial {
            s.lastPartial = partial
            out.send(["event": "partial", "id": s.id, "text": partial])
        }
    }

    // MARK: stop

    private func cmdStop(_ command: [String: Any], received: DispatchTime) {
        guard let id = JSON.int(command["id"]) else {
            sendError(id: nil, code: "bad_command", message: "stop requires an integer \"id\".", extra: "stop")
            return
        }
        // Unknown, finished, cancelled or already-stopping ids are ignored.
        guard let s = sessions[id], s.phase != .ended, !s.cancelled, s.stopReceived == nil else { return }
        s.stopReceived = received
        if let tail = command["tail_ms"] as? NSNumber {
            s.tailSeconds = min(1.0, max(0, tail.doubleValue / 1000))
        }
        switch s.phase {
        case .starting:
            if !s.captureInFlight {
                // Nothing was captured (e.g. still waiting on a permission
                // prompt): answer now rather than when the prompt is closed.
                end(s, .final(text: "", audioSeconds: 0))
            }
            // else: runStart finishes opening the mic, then stops it.
        case .recording:
            s.phase = .finishing
            if active === s { active = nil }
            Task { await self.runStop(s) }
        case .finishing, .ended:
            break
        }
    }

    private func runStop(_ s: Session) async {
        let stopReceived = s.stopReceived ?? DispatchTime.now()
        guard let feeder = s.feeder, let pipeline = s.pipeline else {
            end(s, .final(text: "", audioSeconds: 0))
            return
        }
        feeder.setLevelsEnabled(false)
        out.closeLevels(for: s.id)

        // Audio arrives in 100 ms tap buffers; wait for the one in flight so
        // the last syllable before the key was released is not cut off.
        if s.captureLost == nil {
            await feeder.waitForNextBuffer(timeout: Self.drainTimeout)
        }
        if s.tailSeconds > 0 {
            try? await Task.sleep(nanoseconds: UInt64(s.tailSeconds * 1_000_000_000))
        }
        let drained = millis(since: stopReceived)

        // Release the microphone. Normally ~10 ms; if CoreAudio does not
        // answer, stop waiting after a short grace so the user still gets
        // their text, and settle the wedge verdict afterwards.
        let release = Task { await self.releaseCapture(s) }
        let prompt = await withDeadline(Self.captureStopGrace, "mic release") { await release.value }
        var releasePending = false
        if case .failure = prompt { releasePending = true }
        if !releasePending { s.micReleased.fire() }
        feeder.finish()
        s.inputContinuation.finish()
        let audioSeconds = feeder.seconds
        let released = millis(since: stopReceived)

        // Everything spoken so far was analysed while it was being spoken;
        // only the last stretch remains to be finalised.
        let analyzer = pipeline.analyzer
        let finalize = await withDeadline(Self.finalizeTimeout, "finalize") {
            try await analyzer.finalizeAndFinishThroughEndOfInput()
        }
        var clean = false
        switch finalize {
        case .success:
            clean = true
            // The results streams end once the analyzer has finished.
            let tasks = s.resultsTasks
            if !tasks.isEmpty {
                _ = await withDeadline(0.5, "results") {
                    for task in tasks { await task.value }
                }
            }
        case .failure(let error):
            log("session \(s.id): finalize failed (\(describe(error))); returning the text recognised so far")
            s.cancelResults()
            pipeline.discard()
        }

        var text = s.text
        var extras: [(String, Any?)] = []
        if pipeline.key.blended, s.tracks.count >= 2 {
            let locales = pipeline.key.locales
            var used: [String] = []
            var dominant = locales[0]
            if clean {
                let blend = Blender.blend(
                    primary: s.tracks[0].segments, secondary: s.tracks[1].segments,
                    lexicon: Blender.usesLexicon(secondaryLocale: locales[1]))
                log("session \(s.id) blend: \(blend.summary)")
                if !blend.text.isEmpty {
                    text = blend.text
                    if blend.sources.contains("P") { used.append(locales[0]) }
                    if blend.sources.contains("S") { used.append(locales[1]) }
                    if blend.secondaryCharacters > blend.primaryCharacters { dominant = locales[1] }
                }
            }
            if used.isEmpty, !text.isEmpty {
                // Not blended (finalisation cut short): the running text.
                let source = ([s.lead] + [1 - s.lead]).first { $0 < s.tracks.count && !s.tracks[$0].text.isEmpty } ?? 0
                used = [locales[source]]
                dominant = locales[source]
            }
            extras = [("languages", locales), ("used", used), ("dominant", dominant)]
        }
        let silent = text.isEmpty && feeder.wasSilent
        let elapsed = millis(since: stopReceived)
        log("session \(s.id) stop: \(JSON.number(audioSeconds, places: 2)) s audio; drain \(JSON.number(drained, places: 1)) ms, mic released at \(JSON.number(released, places: 1)) ms, final at \(JSON.number(elapsed, places: 1)) ms; \(text.count) chars\(silent ? "; input was digital silence" : "")")

        if s.cancelled {
            end(s, .cancelled)
        } else if text.isEmpty, let lost = s.captureLost {
            end(s, .error(code: lost.code, message: lost.description))
        } else if text.isEmpty, !clean || s.analysisError != nil {
            let why = s.analysisError.map(describe) ?? "finalisation did not complete"
            end(s, .error(code: "internal", message: "Transcription failed: \(why)"))
        } else {
            end(s, .final(text: text, audioSeconds: audioSeconds, silent: silent, micLost: s.captureLost != nil, extras: extras), stopReceived: stopReceived)
        }

        if releasePending, await release.value { dieWedged("stop") }
        s.micReleased.fire()
    }

    // MARK: cancel

    private func cmdCancel(_ command: [String: Any]) {
        guard let id = JSON.int(command["id"]) else {
            sendError(id: nil, code: "bad_command", message: "cancel requires an integer \"id\".", extra: "cancel")
            return
        }
        guard let s = sessions[id] else { return }  // unknown ids are ignored
        cancel(s)
    }

    private func cancel(_ s: Session) {
        guard s.phase != .ended, !s.cancelled else { return }
        s.cancelled = true
        switch s.phase {
        case .starting:
            // Not holding the mic yet: answer immediately. If the mic is
            // being opened right now, runStart closes it and then answers.
            if !s.captureInFlight { end(s, .cancelled) }
        case .recording:
            s.phase = .finishing
            if active === s { active = nil }
            Task { await self.runCancel(s) }
        case .finishing, .ended:
            break  // a stop in progress reports `cancelled` instead of `final`
        }
    }

    private func runCancel(_ s: Session) async {
        s.feeder?.setLevelsEnabled(false)
        out.closeLevels(for: s.id)
        let wedged = await releaseCapture(s)
        abandonAnalysis(s)
        end(s, .cancelled)
        if wedged { dieWedged("stop") }
    }

    // MARK: Teardown helpers

    /// Stops the capture source. Returns true if CoreAudio did not answer.
    private func releaseCapture(_ s: Session) async -> Bool {
        guard let capture = s.capture else { return false }
        s.capture = nil
        let result = await AudioQueue.run("stop", timeout: Self.captureStopTimeout) { capture.stop() }
        if case .failure(let error) = result {
            log("session \(s.id): \(describe(error))")
            return true
        }
        return false
    }

    private func abandonAnalysis(_ s: Session) {
        s.feeder?.finish()
        s.inputContinuation.finish()
        s.cancelResults()
        s.pipeline?.discard()
    }

    private func dieWedged(_ operation: String) -> Never {
        // The thread stuck inside CoreAudio cannot be recovered, and the mic
        // may still be held. Exiting releases it; the app respawns the helper.
        let message = "The audio system stopped responding (\(operation)). The helper is restarting."
        out.send(["event": "error", "id": nil, "code": "engine_failed", "message": message])
        die(code: exitCodeAudioWedged, message)
    }

    /// Emits the session's one terminal event and retires it.
    private func end(_ s: Session, _ outcome: Outcome, stopReceived: DispatchTime? = nil, rearm: Bool = true) {
        guard s.phase != .ended else { return }
        s.phase = .ended
        if active === s { active = nil }
        if sessions[s.id] === s { sessions[s.id] = nil }
        switch outcome {
        case .final(let text, let audioSeconds, let silent, let micLost, let extras):
            let elapsed = RawJSON(text: JSON.number((stopReceived ?? s.stopReceived).map { millis(since: $0) } ?? 0, places: 1))
            var event: [(String, Any?)] = [
                ("event", "final"), ("id", s.id), ("text", text), ("audio_sec", audioSeconds), ("elapsed_ms", elapsed),
            ]
            // Optional hints, present only when true:
            //  silent   - audio was captured but it was pure digital silence
            //             (muted input, or a MacBook mic with the lid closed)
            //  mic_lost - the helper ended the session itself because the
            //             input device disappeared; text is what came before
            if silent { event.append(("silent", true)) }
            if micLost { event.append(("mic_lost", true)) }
            // Two-language dictations: languages, used, dominant.
            event.append(contentsOf: extras)
            out.send(pairs: event)
        case .cancelled:
            out.send(["event": "cancelled", "id": s.id])
        case .error(let code, let message):
            sendError(id: s.id, code: code, message: message)
        }
        // If a capture is somehow still attached, its owner fires the latch
        // once the microphone has really been released.
        if s.capture == nil { s.micReleased.fire() }
        if rearm { models.rearm() }
    }
}
