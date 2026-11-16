// Locale resolution, speech-asset inventory, and warm analyzer pipelines.

import AVFoundation
import Foundation
import Speech

func bcp47(_ locale: Locale) -> String { locale.identifier(.bcp47) }

/// Which transcriber configuration a pipeline was built for.
struct ModelKey: Hashable, CustomStringConvertible {
    /// Resolved BCP-47 locales. One entry: an ordinary dictation. Two: both
    /// recognisers listen to the same audio and their transcripts are blended
    /// (see Blend.swift); the first is the primary language.
    let locales: [String]
    let partials: Bool      // report volatile (in-progress) results
    let fast: Bool          // also ask for `.fastResults` (only meaningful with partials)
    var locale: String { locales[0] }
    var blended: Bool { locales.count > 1 }
    var description: String {
        "\(locales.joined(separator: "+"))\(partials ? (fast ? "+fast-partials" : "+partials") : "")"
    }
}

/// What the app asked for in `start` / `prewarm`.
struct ModelRequest {
    var locale: String
    /// Optional: the languages the user speaks, most used first. When present
    /// it wins over `locale`.
    var languages: [String] = []
    var partials = true
    var fast = false

    /// What to echo as `locale` in answers to this request.
    var label: String { languages.first ?? locale }
}

/// A request checked against what this Mac has.
struct ResolvedModel {
    let key: ModelKey
    /// Requested languages that are left out because they are unsupported or
    /// not installed yet (as the app spelled them).
    let missing: [String]
}

/// At most this many recognisers run on one dictation.
let maxBlendedLanguages = 2

/// One ready-to-use transcriber + analyzer pair. Single use: a SpeechAnalyzer
/// that has been finished cannot be restarted, so every dictation gets its own.
final class Pipeline {
    let key: ModelKey
    /// One per `key.locales`, in the same order, all attached to `analyzer`.
    let transcribers: [SpeechTranscriber]
    let analyzer: SpeechAnalyzer
    let format: AVAudioFormat
    let created = Date()

    var transcriber: SpeechTranscriber { transcribers[0] }

    init(key: ModelKey, transcribers: [SpeechTranscriber], analyzer: SpeechAnalyzer, format: AVAudioFormat) {
        self.key = key
        self.transcribers = transcribers
        self.analyzer = analyzer
        self.format = format
    }

    /// Releases an analyzer that will not be used (or is being abandoned).
    func discard() {
        let analyzer = self.analyzer
        Task.detached { await analyzer.cancelAndFinishNow() }
    }
}

@MainActor
final class ModelStore {
    private var supportedByID: [String: Locale]?          // lowercased BCP-47 -> Locale
    private var standby: (key: ModelKey, task: Task<Pipeline, Error>)?
    private var lastKey: ModelKey?
    private var reserved: Set<String> = []
    private var formats: [ModelKey: AVAudioFormat] = [:]

    /// A prepared analyzer that has sat unused this long is replaced rather
    /// than trusted (sleep/wake, the system reclaiming the model, ...). The
    /// replacement is built while the microphone is opening, so it costs no
    /// start latency.
    private static let standbyMaxAge: TimeInterval = 20 * 60

    // MARK: Inventory

    func supported() async -> [String: Locale] {
        if let supportedByID, !supportedByID.isEmpty { return supportedByID }
        var map: [String: Locale] = [:]
        for locale in await SpeechTranscriber.supportedLocales {
            map[bcp47(locale).lowercased()] = locale
        }
        supportedByID = map
        return map
    }

    func supportedIDs() async -> [String] {
        await supported().values.map(bcp47).sorted()
    }

    /// Always asked fresh (sub-millisecond): assets can be installed or
    /// removed by the system while the helper is running. (Under the
    /// FREEFLO_TEST_INSTALL simulation, fake downloads count as installed.)
    func installedIDs() async -> [String] {
        let real = await SpeechTranscriber.installedLocales.map(bcp47)
        if fakeInstalled.isEmpty { return real.sorted() }
        return Array(Set(real).union(fakeInstalled)).sorted()
    }

    /// The best supported locale for this Mac's region settings, if any.
    func defaultLocaleID() async -> String? {
        (try? await resolve("auto")).map(bcp47)
    }

    /// Maps what the app sent ("en-IN", "en_in", "hi", "auto") onto a locale
    /// the transcriber supports. A bare language picks, in order: an installed
    /// locale in the system's region, the language's default region if
    /// installed, any installed locale, then the same preferences among
    /// not-yet-installed ones. (`supportedLocale(equivalentTo:)` alone is not
    /// used for this because its answer for a bare "en" varies between calls.)
    func resolve(_ requested: String) async throws -> Locale {
        let supported = await supported()
        var wanted = requested.trimmingCharacters(in: .whitespaces).replacingOccurrences(of: "_", with: "-")
        if wanted.isEmpty || ["auto", "system", "default"].contains(wanted.lowercased()) {
            wanted = bcp47(Locale.current)
        }
        if let exact = supported[wanted.lowercased()] { return exact }

        let asked = Locale(identifier: wanted)
        guard let language = asked.language.languageCode?.identifier.lowercased(), !language.isEmpty else {
            throw HelperError(code: "locale_unsupported", message: "'\(requested)' is not a valid locale.")
        }
        let candidates = supported.values.filter { $0.language.languageCode?.identifier.lowercased() == language }
        if !candidates.isEmpty {
            let installed = Set(await installedIDs())
            let systemRegion = Locale.current.region?.identifier
            let askedRegion = asked.region?.identifier
            let defaultRegion = Locale.Language(identifier: language).maximalIdentifier.split(separator: "-").last.map(String.init)
            func score(_ l: Locale) -> Int {
                let region = l.region?.identifier
                var s = 0
                if installed.contains(bcp47(l)) { s += 100 }
                if let askedRegion, region == askedRegion { s += 50 }
                if askedRegion == nil, let systemRegion, region == systemRegion { s += 20 }
                if let defaultRegion, region == defaultRegion { s += 10 }
                return s
            }
            // Stable order: score, then identifier.
            let best = candidates.sorted { (score($0), bcp47($1)) > (score($1), bcp47($0)) }.first!
            return best
        }
        if let equivalent = await SpeechTranscriber.supportedLocale(equivalentTo: asked) { return equivalent }
        throw HelperError(code: "locale_unsupported", message: "On-device dictation does not support '\(requested)'.")
    }

    func requireInstalled(_ locale: Locale, requested: String) async throws {
        let id = bcp47(locale)
        guard await installedIDs().contains(id) else {
            throw HelperError(code: "locale_not_installed", message: "The speech model for \(id) is not installed yet.")
        }
    }

    // MARK: Pipelines

    /// Reporting options, measured on an M4 with an 11.6 s clip:
    ///  - none: final results only.
    ///  - volatileResults: running text, refreshed about every 4 s of speech;
    ///    the final text is identical to the no-partials one.
    ///  - volatileResults + fastResults: running text about every second, at
    ///    the price of slightly different final text (punctuation drifted).
    ///
    /// `attributed` adds a confidence and an audio time range to every word,
    /// which is what blending two languages is decided on. It does not change
    /// the text.
    private func makeTranscriber(_ locale: Locale, partials: Bool, fast: Bool = false, attributed: Bool = false) -> SpeechTranscriber {
        var reporting: Set<SpeechTranscriber.ReportingOption> = []
        if partials {
            reporting.insert(.volatileResults)
            if fast { reporting.insert(.fastResults) }
        }
        let attributes: Set<SpeechTranscriber.ResultAttributeOption> = attributed ? [.audioTimeRange, .transcriptionConfidence] : []
        return SpeechTranscriber(locale: locale, transcriptionOptions: [], reportingOptions: reporting, attributeOptions: attributes)
    }

    private func build(_ key: ModelKey) async throws -> Pipeline {
        let started = DispatchTime.now()
        let supported = await supported()
        var transcribers: [SpeechTranscriber] = []
        for id in key.locales {
            guard let locale = supported[id.lowercased()] else {
                throw HelperError(code: "locale_unsupported", message: "On-device dictation does not support \(id).")
            }
            transcribers.append(makeTranscriber(locale, partials: key.partials, fast: key.fast, attributed: key.blended))
        }
        guard let format = await SpeechAnalyzer.bestAvailableAudioFormat(compatibleWith: transcribers) else {
            throw HelperError(code: "locale_not_installed", message: "No speech model is available for \(key.locales.joined(separator: " + ")).")
        }
        // processLifetime keeps the model loaded between dictations; it is
        // what makes every analyzer after the first one cheap to create.
        let options = SpeechAnalyzer.Options(priority: .userInitiated, modelRetention: .processLifetime)
        let analyzer = SpeechAnalyzer(modules: transcribers, options: options)
        do {
            try await analyzer.prepareToAnalyze(in: format)
        } catch {
            throw Self.map(error, locale: key.locale)
        }
        formats[key] = format
        log("pipeline ready for \(key) in \(JSON.number(millis(since: started), places: 1)) ms (\(Int(format.sampleRate)) Hz)")
        return Pipeline(key: key, transcribers: transcribers, analyzer: analyzer, format: format)
    }

    static func map(_ error: Error, locale: String) -> HelperError {
        if let h = error as? HelperError { return h }
        if let speech = error as? SFSpeechError {
            switch speech.code {
            case .noModel, .assetLocaleNotAllocated:
                return HelperError(code: "locale_not_installed", message: "The speech model for \(locale) is not installed: \(describe(error))")
            case .cannotAllocateUnsupportedLocale:
                return HelperError(code: "locale_unsupported", message: "On-device dictation does not support \(locale).")
            default:
                break
            }
        }
        return .internalError(describe(error))
    }

    // MARK: Model groups

    /// Locales that are served by the same speech model. On macOS 27 every
    /// non-English locale of India (hi-IN, mul-IN, bn-IN, ta-IN, ...) is one
    /// multilingual model that writes in Latin letters (measured: identical
    /// text and word confidences for hi-IN and mul-IN on 23 of 23 clips), so
    /// running two of them on one dictation would only double the work.
    nonisolated static func modelGroup(_ id: String) -> String {
        let parts = id.replacingOccurrences(of: "_", with: "-").split(separator: "-").map(String.init)
        let language = parts.first?.lowercased() ?? ""
        let region = parts.dropFirst().first(where: { $0.count == 2 && $0.allSatisfy(\.isLetter) })?.uppercased()
        if region == "IN" && language != "en" && !language.isEmpty { return "india" }
        return id.lowercased()
    }

    /// Supported locales that share a model with at least one other, each
    /// group sorted (for `status` / `diagnose`).
    func groups() async -> [[String]] {
        var byGroup: [String: [String]] = [:]
        for id in await supportedIDs() {
            byGroup[Self.modelGroup(id), default: []].append(id)
        }
        return byGroup.values.filter { $0.count >= 2 }.map { $0.sorted() }.sorted { $0[0] < $1[0] }
    }

    func single(_ key: ModelKey) -> ModelKey {
        ModelKey(locales: [key.locale], partials: key.partials, fast: key.fast)
    }

    /// Validates the request (supported? installed?) and names the
    /// transcriber configuration it needs.
    ///
    /// With a plain `locale` the answer is that locale or an error. With
    /// `languages` a dictation must never fail over a language that is merely
    /// extra: the ones that are supported and installed are used (the first
    /// `maxBlendedLanguages` of them, in the order given, one per model), the
    /// rest are reported as missing, and it is only an error when none is
    /// usable. When two remain and the first is the India model, the other
    /// one becomes the primary: the monolingual recogniser is the one that
    /// punctuates, so it must be the one whose sentences are kept.
    func resolved(_ request: ModelRequest) async throws -> ResolvedModel {
        let wanted = request.languages.isEmpty ? [request.locale] : request.languages
        let installed = Set(await installedIDs())
        var usable: [String] = []
        var taken: Set<String> = []
        var missing: [String] = []
        var firstError: Error?
        for name in wanted {
            do {
                let id = bcp47(try await resolve(name))
                if usable.contains(id) { continue }
                let group = Self.modelGroup(id)
                if taken.contains(group) {
                    log("\(id) uses the same speech model as \(usable.joined(separator: " + ")); not loading it twice")
                    continue
                }
                guard installed.contains(id) else {
                    throw HelperError(code: "locale_not_installed", message: "The speech model for \(id) is not installed yet.")
                }
                if usable.count < maxBlendedLanguages {
                    usable.append(id)
                    taken.insert(group)
                } else {
                    log("more than \(maxBlendedLanguages) languages requested; ignoring \(id)")
                }
            } catch {
                missing.append(name)
                if firstError == nil { firstError = error }
            }
        }
        guard !usable.isEmpty else {
            throw firstError ?? HelperError(code: "locale_unsupported", message: "No language was given.")
        }
        if usable.count == 2, Self.modelGroup(usable[0]) == "india", Self.modelGroup(usable[1]) != "india" {
            usable.swapAt(0, 1)
        }
        let key = ModelKey(locales: usable, partials: request.partials, fast: request.partials && request.fast)
        return ResolvedModel(key: key, missing: missing)
    }

    func key(for request: ModelRequest) async throws -> ModelKey {
        try await resolved(request).key
    }

    /// The analyzer's input format for this configuration, if a pipeline for
    /// it has been built before in this process.
    func format(for key: ModelKey) -> AVAudioFormat? { formats[key] }

    func forgetFormat(for key: ModelKey) { formats[key] = nil }

    /// Makes sure a prepared pipeline for this configuration is waiting.
    /// Returns what was actually prepared.
    @discardableResult
    func prewarm(_ request: ModelRequest) async throws -> ResolvedModel {
        let resolved = try await resolved(request)
        let key = resolved.key
        lastKey = key
        let task = standbyTask(for: key)
        do {
            _ = try await task.value
        } catch {
            if standby?.key == key { standby = nil }
            throw Self.map(error, locale: key.locale)
        }
        keepReserved(key.locales)
        return resolved
    }

    /// Hands out a prepared pipeline: the standby one when it matches and is
    /// recent (instant), otherwise a freshly built one.
    func take(_ key: ModelKey) async throws -> Pipeline {
        lastKey = key
        if let slot = standby, slot.key == key {
            standby = nil
            if let pipeline = try? await slot.task.value {
                if Date().timeIntervalSince(pipeline.created) <= Self.standbyMaxAge { return pipeline }
                log("standby pipeline for \(key) is \(Int(Date().timeIntervalSince(pipeline.created))) s old; replacing it")
                pipeline.discard()
            } else {
                log("standby pipeline for \(key) had failed; building a fresh one")
            }
        }
        return try await build(key)
    }

    /// Forgets a standby pipeline that is not becoming ready.
    func dropStandby() {
        guard let slot = standby else { return }
        standby = nil
        let task = slot.task
        Task { (try? await task.value)?.discard() }
    }

    /// A fresh pipeline for the same configuration (used to retry once when a
    /// standby analyzer turns out to be unusable).
    func rebuild(_ key: ModelKey) async throws -> Pipeline {
        try await build(key)
    }

    /// Prepares the next pipeline in the background so the next `start` is instant.
    func rearm() {
        guard let key = lastKey else { return }
        if let slot = standby, slot.key == key { return }
        Task {
            let installed = Set(await self.installedIDs())
            guard key.locales.allSatisfy(installed.contains) else { return }
            guard self.lastKey == key else { return }
            _ = self.standbyTask(for: key)
        }
    }

    private func standbyTask(for key: ModelKey) -> Task<Pipeline, Error> {
        if let slot = standby {
            if slot.key == key { return slot.task }
            // A different configuration was waiting: release it.
            let old = slot.task
            Task { (try? await old.value)?.discard() }
        }
        let task = Task { try await self.build(key) }
        standby = (key, task)
        return task
    }

    // MARK: Asset installation

    /// The per-app reservation table (AssetInventory.maximumReservedLocales,
    /// 5 on macOS 27) is shared by every process signed with the helper's
    /// identifier, including an installed copy of the app that may be
    /// running. Tests and fake-mic runs must not touch it.
    private static var reservationsDisabled: Bool {
        if FakeMic.isActive { return true }
        if let v = ProcessInfo.processInfo.environment["FREEFLO_NO_RESERVE"], !v.isEmpty { return true }
        return false
    }

    /// Tells the system this app relies on these locales' assets so it does
    /// not reclaim them. Only ever called for already-installed locales, so it
    /// never triggers a download. Best effort.
    private func keepReserved(_ ids: [String]) {
        if Self.reservationsDisabled { return }
        let fresh = ids.filter { !reserved.contains($0) }
        guard !fresh.isEmpty else { return }
        for id in fresh { reserved.insert(id) }
        let needed = Set(ids)
        Task {
            let supported = await self.supported()
            for id in fresh {
                guard let locale = supported[id.lowercased()] else { continue }
                let current = await AssetInventory.reservedLocales
                if current.contains(where: { bcp47($0) == id }) { continue }
                if current.count >= AssetInventory.maximumReservedLocales {
                    // Make room, but never by giving up a language in use.
                    guard let victim = current.first(where: { !needed.contains(bcp47($0)) }) else { continue }
                    _ = await AssetInventory.release(reservedLocale: victim)
                    self.reserved.remove(bcp47(victim))
                }
                do {
                    try await AssetInventory.reserve(locale: locale)
                } catch {
                    log("could not reserve \(id) (harmless): \(describe(error))")
                }
            }
        }
    }

    /// What an `install` did.
    struct InstallOutcome {
        let resolved: String
        /// True when nothing had to be downloaded.
        let already: Bool
    }

    /// Turns a failed download into something the app can show. Codes:
    /// locale_unsupported, network, no_space, cancelled, install_failed.
    static func mapInstall(_ error: Error, locale: String) -> HelperError {
        if let h = error as? HelperError { return h }
        if error is CancellationError {
            return HelperError(code: "cancelled", message: "The download of \(locale) was cancelled.")
        }
        if let speech = error as? SFSpeechError {
            switch speech.code {
            case .cannotAllocateUnsupportedLocale:
                return HelperError(code: "locale_unsupported", message: "On-device dictation does not support \(locale).")
            case .insufficientResources:
                return HelperError(code: "no_space", message: "This Mac does not have room for the \(locale) speech model: \(describe(error))")
            case .timeout:
                return HelperError(code: "network", message: "The download of \(locale) timed out: \(describe(error))")
            default:
                break
            }
        }
        let ns = error as NSError
        var chain: [NSError] = [ns]
        if let under = ns.userInfo[NSUnderlyingErrorKey] as? NSError { chain.append(under) }
        if chain.contains(where: { $0.domain == NSURLErrorDomain }) {
            return HelperError(code: "network", message: "The speech model for \(locale) could not be downloaded (no internet connection?): \(describe(error))")
        }
        if chain.contains(where: { $0.domain == NSCocoaErrorDomain && $0.code == NSFileWriteOutOfSpaceError }) {
            return HelperError(code: "no_space", message: "Not enough free disk space for the \(locale) speech model.")
        }
        if chain.contains(where: { $0.domain == NSCocoaErrorDomain && $0.code == NSUserCancelledError }) {
            return HelperError(code: "cancelled", message: "The download of \(locale) was cancelled.")
        }
        return HelperError(code: "install_failed", message: describe(error))
    }

    /// FREEFLO_TEST_INSTALL simulation: locales whose fake download finished.
    private var fakeInstalled: Set<String> = []

    /// Whether `id` counts as installed for `install` (the simulation
    /// pretends the requested locale is missing until its fake download
    /// succeeds, whatever the system has).
    private func installedForInstall(_ id: String) async -> Bool {
        if TestInstall.mode != nil { return fakeInstalled.contains(id) }
        return await installedIDs().contains(id)
    }

    /// Downloads and installs the system speech asset for `requested`,
    /// reporting progress (0...1): 0 at once, then whenever it moves, and at
    /// least every 2 s as a heartbeat. Returns once the locale really is
    /// listed as installed. One retry: the asset service fails transiently
    /// when a download is requested the moment the network comes up. A
    /// download that made no progress for `stallSeconds` is cancelled and
    /// reported as `network` (not retried: it already waited minutes).
    func install(requested: String, progress: @escaping @MainActor (Double) -> Void) async throws -> InstallOutcome {
        let locale = try await resolve(requested)
        let id = bcp47(locale)
        if await installedForInstall(id) { return InstallOutcome(resolved: id, already: true) }
        progress(0)

        var lastError: Error?
        for attempt in 1...2 {
            do {
                let downloaded = try await installOnce(locale, id: id, progress: progress)
                supportedByID = nil
                return InstallOutcome(resolved: id, already: !downloaded)
            } catch {
                lastError = error
                let mapped = Self.mapInstall(error, locale: id)
                log("install of \(id) failed (attempt \(attempt)): \(mapped.code): \(mapped.message)")
                if error is StalledDownload || ["locale_unsupported", "no_space", "cancelled"].contains(mapped.code) { break }
                // The download may have landed even though the call threw.
                if await installedForInstall(id) { return InstallOutcome(resolved: id, already: false) }
                if attempt == 1 { try? await Task.sleep(nanoseconds: 1_500_000_000) }
            }
        }
        if let stalled = lastError as? StalledDownload { throw stalled.error }
        throw Self.mapInstall(lastError ?? HelperError.internalError("install failed"), locale: id)
    }

    /// Returns true if something was downloaded, false if the system said
    /// there was nothing to fetch.
    private func installOnce(_ locale: Locale, id: String, progress: @escaping @MainActor (Double) -> Void) async throws -> Bool {
        if let mode = TestInstall.mode {
            try await runGuarded(FakeDownload(mode), id: id, progress: progress)
            fakeInstalled.insert(id)
            return true
        }

        let transcriber = makeTranscriber(locale, partials: true)
        switch await AssetInventory.status(forModules: [transcriber]) {
        case .unsupported:
            throw HelperError(code: "locale_unsupported", message: "On-device dictation does not support \(id).")
        case .installed:
            return false
        default:
            break   // .supported (to download) or .downloading (join it)
        }

        var request: AssetInstallationRequest?
        do {
            request = try await AssetInventory.assetInstallationRequest(supporting: [transcriber])
        } catch let error as SFSpeechError where error.code == .tooManyAssetLocalesAllocated {
            // The per-app reservation table is full: give up exactly one
            // reservation that is neither in use nor the one being installed.
            let inUse = Set(lastKey?.locales ?? [])
            guard let victim = await AssetInventory.reservedLocales.first(where: {
                let other = bcp47($0)
                return other != id && !inUse.contains(other)
            }) else { throw error }
            log("reservation table full; releasing \(bcp47(victim)) to make room for \(id)")
            _ = await AssetInventory.release(reservedLocale: victim)
            reserved.remove(bcp47(victim))
            request = try await AssetInventory.assetInstallationRequest(supporting: [transcriber])
        }
        guard let request else {
            // Nothing to download. Believe it only if the locale is listed.
            if await installedIDs().contains(id) { return false }
            throw HelperError(code: "install_failed", message: "macOS offered no download for \(id).")
        }
        reserved.insert(id)   // an installation request reserves the locale

        try await runGuarded(RealDownload(request), id: id, progress: progress)
        // The inventory can lag the installer by a moment.
        for _ in 0..<20 {
            if await installedIDs().contains(id) { return true }
            try? await Task.sleep(nanoseconds: 250_000_000)
        }
        throw HelperError(code: "install_failed", message: "The download finished but \(id) is still not installed.")
    }

    /// Runs a download while watching it: reports progress (moved >= 0.5 %, or
    /// a 2 s heartbeat with the same value) and cancels it when the fraction
    /// has not changed for the stall limit.
    private func runGuarded(_ download: Download, id: String, progress: @escaping @MainActor (Double) -> Void) async throws {
        let stallSeconds = TestInstall.stallSeconds
        var runner: Task<Void, Never>?
        var watcher: Task<Void, Never>?
        let result = await withCheckedContinuation { (cont: CheckedContinuation<Result<Void, Error>, Never>) in
            let once = ResumeOnce(cont)
            runner = Task.detached {
                do {
                    try await download.run()
                    once.resume(.success(()))
                } catch {
                    once.resume(.failure(error))
                }
            }
            watcher = Task { @MainActor in
                var lastEmitted = 0.0
                var lastEmitAt = Date()
                var lastRaw = download.fraction
                var lastChange = Date()
                while !Task.isCancelled {
                    try? await Task.sleep(nanoseconds: 250_000_000)
                    if Task.isCancelled { return }
                    let now = Date()
                    let raw = download.fraction
                    if raw != lastRaw {
                        lastRaw = raw
                        lastChange = now
                    }
                    let fraction = raw.isFinite ? min(1, max(0, raw)) : 0
                    if abs(fraction - lastEmitted) >= 0.005 || now.timeIntervalSince(lastEmitAt) >= 2.0 {
                        lastEmitted = fraction
                        lastEmitAt = now
                        progress(fraction)
                    }
                    if now.timeIntervalSince(lastChange) >= stallSeconds {
                        download.cancel()
                        let seconds = JSON.number(stallSeconds, places: 0)
                        once.resume(.failure(StalledDownload(error: HelperError(
                            code: "network", message: "The download of \(id) made no progress for \(seconds) s."))))
                        return
                    }
                }
            }
        }
        watcher?.cancel()
        if case .failure = result { runner?.cancel() }
        try result.get()
    }
}

// MARK: - Downloads

/// One asset download, real or simulated.
protocol Download: AnyObject, Sendable {
    /// 0...1 (may be NaN/indeterminate before the download really starts).
    var fraction: Double { get }
    func run() async throws
    func cancel()
}

/// AssetInstallationRequest: progress via its Foundation.Progress.
final class RealDownload: Download, @unchecked Sendable {
    private let request: AssetInstallationRequest
    init(_ request: AssetInstallationRequest) { self.request = request }
    var fraction: Double { request.progress.fractionCompleted }
    func run() async throws { try await request.downloadAndInstall() }
    func cancel() { request.progress.cancel() }
}

/// A download that made no progress for too long (reported as `network`).
struct StalledDownload: Error {
    let error: HelperError
}

/// FREEFLO_TEST_INSTALL (documented in helper/README.md):
///   ok:<sec>     the fraction goes 0 -> 1 linearly over <sec>, then success
///   stall        the fraction never moves (exercises stall detection)
///   fail:<code>  throws HelperError(<code>, "simulated") after 0.3 s
/// FREEFLO_INSTALL_STALL_SEC overrides the 180 s stall limit.
enum TestInstall {
    enum Mode { case ok(Double), stall, fail(String) }

    static let mode: Mode? = {
        guard let v = ProcessInfo.processInfo.environment["FREEFLO_TEST_INSTALL"], !v.isEmpty else { return nil }
        if v == "stall" { return .stall }
        if v.hasPrefix("ok:"), let seconds = Double(v.dropFirst(3)), seconds >= 0 { return .ok(seconds) }
        if v.hasPrefix("fail:") { return .fail(String(v.dropFirst(5))) }
        log("ignoring unknown FREEFLO_TEST_INSTALL=\(v)")
        return nil
    }()

    static let stallSeconds: Double = {
        if let v = Double(ProcessInfo.processInfo.environment["FREEFLO_INSTALL_STALL_SEC"] ?? ""), v > 0 { return v }
        return 180
    }()
}

final class FakeDownload: Download, @unchecked Sendable {
    private let mode: TestInstall.Mode
    private let started = Date()
    private let lock = NSLock()
    private var cancelled = false

    init(_ mode: TestInstall.Mode) { self.mode = mode }

    var fraction: Double {
        switch mode {
        case .ok(let seconds):
            return seconds <= 0 ? 1 : min(1, Date().timeIntervalSince(started) / seconds)
        case .stall, .fail:
            return 0
        }
    }

    private var isCancelled: Bool {
        lock.lock(); defer { lock.unlock() }
        return cancelled
    }

    func cancel() {
        lock.lock(); cancelled = true; lock.unlock()
    }

    func run() async throws {
        switch mode {
        case .ok(let seconds):
            while Date().timeIntervalSince(started) < seconds {
                if isCancelled || Task.isCancelled { throw CancellationError() }
                try? await Task.sleep(nanoseconds: 50_000_000)
            }
        case .stall:
            while !isCancelled && !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 100_000_000)
            }
            throw CancellationError()
        case .fail(let code):
            try? await Task.sleep(nanoseconds: 300_000_000)
            throw HelperError(code: code, message: "simulated")
        }
    }
}
