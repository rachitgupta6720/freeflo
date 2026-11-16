// Audio capture: the microphone (AVAudioEngine) and a file-backed stand-in for tests.

import AVFoundation
import CoreAudio
import Foundation

enum CaptureError: DescribedError {
    case noInputDevice(String)
    case engineFailed(String)

    var code: String {
        switch self {
        case .noInputDevice: return "no_input_device"
        case .engineFailed: return "engine_failed"
        }
    }

    var description: String {
        switch self {
        case .noInputDevice(let m), .engineFailed(let m): return m
        }
    }
}

/// Thrown when a CoreAudio call did not return in time. The thread stuck in
/// it cannot be recovered, so the helper finishes what it can and exits.
struct AudioWedged: DescribedError {
    let operation: String
    let seconds: Double
    var description: String {
        "audio system did not respond to \(operation) within \(JSON.number(seconds, places: 1))s"
    }
}

/// Every call that can touch CoreAudio runs on this one serial queue, with a
/// deadline. That keeps blocking HAL calls off the main actor (commands such
/// as `ping`/`cancel` stay responsive) and turns a hung call - the failure
/// that used to wedge the whole app - into an error the helper can act on.
enum AudioQueue {
    static let queue = DispatchQueue(label: "freeflo.helper.audio", qos: .userInteractive)

    static func run(_ operation: String, timeout: Double, _ body: @escaping () throws -> Void) async -> Result<Void, Error> {
        await withCheckedContinuation { (cont: CheckedContinuation<Result<Void, Error>, Never>) in
            let once = ResumeOnce(cont)
            queue.async {
                do {
                    try body()
                    once.resume(.success(()))
                } catch {
                    once.resume(.failure(error))
                }
            }
            DispatchQueue.global().asyncAfter(deadline: .now() + timeout) {
                once.resume(.failure(AudioWedged(operation: operation, seconds: timeout)))
            }
        }
    }
}

/// Runs `body`, turning an Objective-C exception (which AVAudioEngine raises
/// for e.g. a missing input device or a stale tap format, and which Swift
/// cannot catch) into a thrown error instead of a crash.
func objcTry(_ body: () -> Void) throws {
    var error: NSError?
    if !FFTryObjC(body, &error) {
        throw error ?? NSError(domain: FFObjCExceptionDomain, code: 1, userInfo: [NSLocalizedDescriptionKey: "unknown Objective-C exception"])
    }
}

/// A source of audio buffers. `start`/`stop` are only ever called on `AudioQueue.queue`.
protocol Capture: AnyObject {
    func start() throws
    func stop()
}

// MARK: - CoreAudio queries

enum InputDevice {
    /// The system default input device, or nil when the Mac has none
    /// (e.g. a Mac mini / Studio with nothing plugged in).
    static func defaultID() -> AudioDeviceID? {
        var id = AudioDeviceID(kAudioObjectUnknown)
        var size = UInt32(MemoryLayout<AudioDeviceID>.size)
        var address = AudioObjectPropertyAddress(
            mSelector: kAudioHardwarePropertyDefaultInputDevice,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain)
        let status = AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &address, 0, nil, &size, &id)
        guard status == noErr, id != kAudioObjectUnknown else { return nil }
        return id
    }

    static func name(_ id: AudioDeviceID) -> String? {
        var address = AudioObjectPropertyAddress(
            mSelector: kAudioObjectPropertyName,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain)
        var size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
        var value: Unmanaged<CFString>?
        let status = withUnsafeMutablePointer(to: &value) { p in
            AudioObjectGetPropertyData(id, &address, 0, nil, &size, p)
        }
        guard status == noErr, let value else { return nil }
        return value.takeRetainedValue() as String
    }

    /// Name of the default input device; nil if there is none. Bounded: a
    /// CoreAudio daemon that is not answering yields nil rather than a hang.
    static func defaultName(timeout: Double = 1.0) async -> String? {
        let result = await withDeadline(timeout, "input device query") { () -> String? in
            await withCheckedContinuation { (cont: CheckedContinuation<String?, Never>) in
                DispatchQueue.global(qos: .userInitiated).async {
                    cont.resume(returning: defaultID().flatMap { name($0) ?? "Unknown input" })
                }
            }
        }
        if case .success(let name) = result { return name }
        return nil
    }
}

// MARK: - Microphone

/// Captures the default input device with a fresh AVAudioEngine per session.
///
/// A new engine each time means a device change between dictations needs no
/// handling at all, and stopping the engine fully releases the microphone
/// (the orange indicator goes out). During a session it survives:
///  - `AVAudioEngineConfigurationChange` (device unplugged, default input
///    switched, sample rate changed): the engine is rebuilt on the new device.
///  - a stalled device (no buffers for 2 s): same rebuild.
/// Rebuilds are retried with backoff; if they keep failing `onLost` fires once
/// and the session carries on with whatever audio it already has.
final class MicCapture: Capture, @unchecked Sendable {
    private let feeder: AudioFeeder
    private let onLost: (CaptureError) -> Void

    private var engine: AVAudioEngine?
    private var observer: NSObjectProtocol?
    private var wanted = false
    private var generation = 0
    private var burstAttempts = 0
    private var totalRestarts = 0
    private var stallTimer: DispatchSourceTimer?
    private var lastBufferCount: Int64 = -1
    private var stalledTicks = 0

    // Recovery has to be over (one way or the other) well inside the app's
    // "no level events for 5 s" watchdog: 5 attempts span about 1.6 s.
    private static let maxBurstAttempts = 5
    private static let maxTotalRestarts = 10
    private static let stallTick = 0.5
    private static let stallTicksBeforeRestart = 4   // 2 s without a buffer

    init(feeder: AudioFeeder, onLost: @escaping (CaptureError) -> Void) {
        self.feeder = feeder
        self.onLost = onLost
    }

    func start() throws {
        wanted = true
        do {
            try bringUp()
        } catch {
            wanted = false
            throw error
        }
        startStallTimer()
    }

    func stop() {
        wanted = false
        stallTimer?.cancel()
        stallTimer = nil
        tearDown()
    }

    // MARK: Engine lifecycle (audio queue)

    private func bringUp() throws {
        guard let device = InputDevice.defaultID() else {
            throw CaptureError.noInputDevice("No microphone found. Connect or select an input device in System Settings > Sound.")
        }

        let engine = AVAudioEngine()
        var format: AVAudioFormat?
        var startError: Error?
        let feeder = self.feeder
        do {
            try objcTry {
                let input = engine.inputNode
                let hardware = input.outputFormat(forBus: 0)
                format = hardware
                guard hardware.sampleRate > 0, hardware.channelCount > 0 else { return }
                // Ask for ~40 ms buffers. macOS treats the size as a hint and
                // in practice delivers 100 ms (4800 frames at 48 kHz, measured
                // on macOS 27.2 for every requested size); the feeder copes
                // with whatever arrives.
                let frames = AVAudioFrameCount(max(256, hardware.sampleRate * 0.04))
                input.installTap(onBus: 0, bufferSize: frames, format: hardware) { buffer, _ in
                    feeder.feed(buffer)
                }
                engine.prepare()
                do { try engine.start() } catch { startError = error }
            }
        } catch {
            Self.dispose(engine)
            throw CaptureError.engineFailed("Audio engine raised an exception: \(error.localizedDescription)")
        }
        guard let format, format.sampleRate > 0, format.channelCount > 0 else {
            Self.dispose(engine)
            throw CaptureError.noInputDevice("The input device reports no usable audio format.")
        }
        if let startError {
            Self.dispose(engine)
            throw CaptureError.engineFailed("Could not start the audio engine: \(describe(startError))")
        }

        self.engine = engine
        generation += 1
        let gen = generation
        observer = NotificationCenter.default.addObserver(
            forName: .AVAudioEngineConfigurationChange, object: engine, queue: nil
        ) { [weak self] _ in
            AudioQueue.queue.async { self?.configurationChanged(generation: gen) }
        }
        lastBufferCount = feeder.bufferCount
        stalledTicks = 0
        if let after = TestHooks.configChangeAfter, totalRestarts == 0 {
            // Test hook: pretend the input device changed mid-session.
            AudioQueue.queue.asyncAfter(deadline: .now() + after) { [weak engine] in
                guard let engine else { return }
                log("TEST: posting AVAudioEngineConfigurationChange")
                NotificationCenter.default.post(name: .AVAudioEngineConfigurationChange, object: engine)
            }
        }
        log("mic live: \(InputDevice.name(device) ?? "device \(device)") @ \(Int(format.sampleRate)) Hz, \(format.channelCount) ch")
    }

    private func tearDown() {
        if let observer {
            NotificationCenter.default.removeObserver(observer)
            self.observer = nil
        }
        guard let engine else { return }
        self.engine = nil
        Self.dispose(engine)
    }

    /// Stops the engine, which is what releases the microphone (~5-10 ms).
    /// Destroying the engine object costs another ~45 ms (measured), so that
    /// is pushed to a later turn of the audio queue: the caller gets on with
    /// finalising the transcript, and anything that touches CoreAudio next
    /// still runs strictly after the old engine is gone.
    private static func dispose(_ engine: AVAudioEngine) {
        do {
            try objcTry {
                engine.inputNode.removeTap(onBus: 0)
                engine.stop()
            }
        } catch {
            log("ignoring exception while stopping audio engine: \(error.localizedDescription)")
        }
        AudioQueue.queue.async { withExtendedLifetime(engine) {} }
    }

    // MARK: Recovery (audio queue)

    private func configurationChanged(generation gen: Int) {
        guard wanted, gen == generation else { return }
        log("audio configuration changed mid-session; rebuilding capture")
        tearDown()
        scheduleRestart(after: 0.05)
    }

    private func scheduleRestart(after delay: Double) {
        AudioQueue.queue.asyncAfter(deadline: .now() + delay) { [weak self] in
            guard let self, self.wanted, self.engine == nil else { return }
            self.burstAttempts += 1
            self.totalRestarts += 1
            do {
                try self.bringUp()
                self.burstAttempts = 0
                log("capture recovered")
            } catch {
                let captureError = (error as? CaptureError) ?? .engineFailed(describe(error))
                if self.burstAttempts >= Self.maxBurstAttempts || self.totalRestarts >= Self.maxTotalRestarts {
                    log("capture lost: \(captureError)")
                    self.wanted = false
                    self.stallTimer?.cancel()
                    self.stallTimer = nil
                    self.onLost(captureError)
                    return
                }
                // 0.1, 0.2, 0.4, 0.8 s: devices take a moment to settle.
                let next = min(1.0, 0.1 * pow(2.0, Double(self.burstAttempts - 1)))
                log("capture restart attempt \(self.burstAttempts) failed (\(captureError)); retrying in \(JSON.number(next, places: 2))s")
                self.scheduleRestart(after: next)
            }
        }
    }

    private func startStallTimer() {
        let timer = DispatchSource.makeTimerSource(queue: AudioQueue.queue)
        timer.schedule(deadline: .now() + Self.stallTick, repeating: Self.stallTick)
        timer.setEventHandler { [weak self] in
            guard let self, self.wanted, self.engine != nil else { return }
            let count = self.feeder.bufferCount
            if count == self.lastBufferCount {
                self.stalledTicks += 1
            } else {
                self.stalledTicks = 0
                self.lastBufferCount = count
            }
            guard self.stalledTicks >= Self.stallTicksBeforeRestart else { return }
            self.stalledTicks = 0
            guard self.totalRestarts < Self.maxTotalRestarts else {
                log("capture lost: the input device keeps stalling")
                self.wanted = false
                self.stallTimer?.cancel()
                self.stallTimer = nil
                self.tearDown()
                self.onLost(.engineFailed("The input device stopped delivering audio."))
                return
            }
            log("no audio from the input device for \(JSON.number(Double(Self.stallTicksBeforeRestart) * Self.stallTick, places: 1)) s; rebuilding capture")
            self.tearDown()
            self.scheduleRestart(after: 0.05)
        }
        timer.resume()
        stallTimer = timer
    }
}

// MARK: - File-backed capture (tests)

/// Plays audio files into the feeder at real-time pace (or faster), then
/// silence. Selected with FREEFLO_FAKE_MIC=<path>[:<path>...]; lets the whole
/// streaming path be exercised without microphone permission or a quiet room.
/// Several files play back to back, each in its own native format - which is
/// exactly what a mid-session input device switch looks like to the feeder.
final class FileCapture: Capture, @unchecked Sendable {
    private let paths: [String]
    private let speed: Double
    private let feeder: AudioFeeder
    private let queue = DispatchQueue(label: "freeflo.helper.fake-mic", qos: .userInteractive)
    private var timer: DispatchSourceTimer?
    private var files: [AVAudioFile] = []
    private var index = 0
    private let onLost: (CaptureError) -> Void

    init(path: String, speed: Double, feeder: AudioFeeder, onLost: @escaping (CaptureError) -> Void = { _ in }) {
        self.paths = path.split(separator: ":").map(String.init)
        self.speed = max(0.1, speed)
        self.feeder = feeder
        self.onLost = onLost
    }

    func start() throws {
        do {
            files = try paths.map { try AVAudioFile(forReading: URL(fileURLWithPath: $0)) }
        } catch {
            throw CaptureError.engineFailed("fake mic: cannot open \(paths.joined(separator: ", ")): \(describe(error))")
        }
        guard !files.isEmpty else { throw CaptureError.engineFailed("fake mic: no file given") }
        index = 0
        let timer = DispatchSource.makeTimerSource(queue: queue)
        timer.schedule(deadline: .now(), repeating: 0.04 / speed, leeway: .milliseconds(1))
        timer.setEventHandler { [weak self] in self?.tick() }
        timer.resume()
        self.timer = timer
        if let after = TestHooks.loseMicAfter {
            // Test hook: the device disappears for good mid-session.
            queue.asyncAfter(deadline: .now() + after) { [weak self] in
                guard let self, self.timer != nil else { return }
                self.timer?.cancel()
                self.timer = nil
                log("TEST: fake mic lost")
                self.onLost(.noInputDevice("The microphone was disconnected (simulated)."))
            }
        }
        log("fake mic live: \(paths.joined(separator: ", ")) x\(JSON.number(speed, places: 2))")
    }

    private func tick() {
        guard !files.isEmpty else { return }
        while index < files.count - 1, files[index].framePosition >= files[index].length { index += 1 }
        let file = files[index]
        let format = file.processingFormat
        let chunk = AVAudioFrameCount(max(64, format.sampleRate * 0.04))
        guard let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: chunk) else { return }
        if file.framePosition < file.length {
            try? file.read(into: buffer, frameCount: chunk)
        }
        if buffer.frameLength == 0 {
            // Past the end of the last file: keep "recording" silence.
            buffer.frameLength = chunk
            if let channels = buffer.floatChannelData {
                for c in 0..<Int(format.channelCount) {
                    memset(channels[c], 0, Int(chunk) * MemoryLayout<Float>.size)
                }
            }
        }
        feeder.feed(buffer)
    }

    func stop() {
        timer?.cancel()
        timer = nil
        queue.sync {}  // let an in-flight tick finish before the caller flushes
        files = []
    }
}

// MARK: - Test hooks

/// FREEFLO_FAKE_MIC replaces the microphone (documented in helper/README.md):
///   <path>      play this audio file as if it were the mic
///   @none       behave as if no input device exists
///   @denied     behave as if microphone permission is denied
///   @hang       capture start never returns (exercises the wedge watchdog)
///   @hang-stop  capture works, but stopping it never returns
enum FakeMic {
    static let setting: String? = {
        guard let v = ProcessInfo.processInfo.environment["FREEFLO_FAKE_MIC"], !v.isEmpty else { return nil }
        return v
    }()

    static let speed: Double = {
        Double(ProcessInfo.processInfo.environment["FREEFLO_FAKE_MIC_SPEED"] ?? "") ?? 1.0
    }()

    static var isActive: Bool { setting != nil }

    /// What `status` reports as the input device while the mic is faked.
    static var deviceName: String? {
        guard let setting else { return nil }
        return setting == "@none" ? nil : "Fake microphone (\(setting))"
    }
}

enum TestHooks {
    /// FREEFLO_TEST_CONFIG_CHANGE=<seconds>: that long after the real mic goes
    /// live, post AVAudioEngineConfigurationChange as a device switch would.
    static let configChangeAfter: Double? = {
        Double(ProcessInfo.processInfo.environment["FREEFLO_TEST_CONFIG_CHANGE"] ?? "")
    }()

    /// FREEFLO_TEST_LOSE_MIC=<seconds>: that long after the fake mic goes
    /// live it disappears for good (as when the only input is unplugged).
    static let loseMicAfter: Double? = {
        Double(ProcessInfo.processInfo.environment["FREEFLO_TEST_LOSE_MIC"] ?? "")
    }()
}

/// A capture source that misbehaves on purpose.
final class FaultyCapture: Capture {
    enum Fault { case none, hangOnStart, hangOnStop }
    private let fault: Fault
    private let inner: Capture?

    init(fault: Fault, inner: Capture? = nil) {
        self.fault = fault
        self.inner = inner
    }

    func start() throws {
        switch fault {
        case .none:
            throw CaptureError.noInputDevice("No microphone found (simulated).")
        case .hangOnStart:
            while true { sleep(1000) }
        case .hangOnStop:
            try inner?.start()
        }
    }

    func stop() {
        if fault == .hangOnStop { while true { sleep(1000) } }
        inner?.stop()
    }
}

func makeCapture(feeder: AudioFeeder, onLost: @escaping (CaptureError) -> Void) -> Capture {
    guard let setting = FakeMic.setting else {
        return MicCapture(feeder: feeder, onLost: onLost)
    }
    switch setting {
    case "@none", "@denied":
        return FaultyCapture(fault: .none)
    case "@hang":
        return FaultyCapture(fault: .hangOnStart)
    case "@hang-stop":
        let path = ProcessInfo.processInfo.environment["FREEFLO_FAKE_MIC_FILE"] ?? ""
        return FaultyCapture(fault: .hangOnStop, inner: FileCapture(path: path, speed: FakeMic.speed, feeder: feeder))
    default:
        return FileCapture(path: setting, speed: FakeMic.speed, feeder: feeder, onLost: onLost)
    }
}
