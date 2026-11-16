// Converts captured audio to the analyzer's format, meters it, and hands it on.

import AVFoundation
import Accelerate
import Foundation

/// Sits between a capture source (microphone tap or test file) and the speech
/// analyzer's input stream.
///
/// - Converts whatever the device delivers (48 kHz float, 44.1 kHz stereo, a
///   Bluetooth headset's 16/24 kHz ...) into the analyzer's preferred format.
///   The converter is rebuilt automatically whenever the incoming format
///   changes, which is what makes mid-session device switches transparent.
/// - Computes a cheap level meter (about 20 values per second of audio).
/// - Keeps nothing: each converted buffer is handed to `sink` and forgotten.
///
/// `feed` is called from the audio tap's thread; everything else from other
/// threads. A single uncontended lock guards the state; the work under it is
/// a sample-rate conversion of one small buffer.
final class AudioFeeder: @unchecked Sendable {
    let target: AVAudioFormat

    private let sink: (AVAudioPCMBuffer) -> Void
    private let levelSink: (_ level: Float, _ delay: Double) -> Void

    private let lock = NSLock()
    private var converter: AVAudioConverter?
    private var sourceFormat: AVAudioFormat?
    private var closed = false
    private var levelsEnabled = false
    private var framesOut: Int64 = 0
    private var buffersIn: Int64 = 0
    private var feedWaiters: [ResumeOnce<Void>] = []
    private var conversionErrorLogged = false

    // Level meter window (in source frames).
    private var windowSumSquares: Float = 0
    private var windowFrames = 0
    private var peakMeanSquare: Float = 0
    private var meteredWindows = 0
    private var scratch: [Float] = []

    /// Level windows per second of audio.
    static let levelRate = 20.0

    init(
        target: AVAudioFormat,
        sink: @escaping (AVAudioPCMBuffer) -> Void,
        level: @escaping (_ level: Float, _ delay: Double) -> Void
    ) {
        self.target = target
        self.sink = sink
        self.levelSink = level
    }

    /// Seconds of audio delivered to the analyzer so far.
    var seconds: Double {
        lock.lock(); defer { lock.unlock() }
        return Double(framesOut) / target.sampleRate
    }

    /// True when audio was captured but every sample was (digital) silence:
    /// a muted input, or a MacBook's built-in mic with the lid closed.
    var wasSilent: Bool {
        lock.lock(); defer { lock.unlock() }
        return meteredWindows >= 4 && peakMeanSquare < 1e-10
    }

    /// Number of buffers received from the capture source (for stall detection).
    var bufferCount: Int64 {
        lock.lock(); defer { lock.unlock() }
        return buffersIn
    }

    func setLevelsEnabled(_ on: Bool) {
        lock.lock()
        levelsEnabled = on
        if !on { windowSumSquares = 0; windowFrames = 0 }
        lock.unlock()
    }

    /// Accepts one captured buffer. Never blocks beyond the conversion itself.
    func feed(_ buffer: AVAudioPCMBuffer) {
        guard buffer.frameLength > 0 else { return }
        var levels: [(Float, Double)] = []
        lock.lock()
        if closed { lock.unlock(); return }
        buffersIn += 1
        let waiters = feedWaiters
        feedWaiters.removeAll(keepingCapacity: true)
        if levelsEnabled { levels = meter(buffer) }
        if let out = convert(buffer) {
            framesOut += Int64(out.frameLength)
            sink(out)  // non-blocking yield into the analyzer's stream; kept under the lock for ordering
        }
        lock.unlock()
        for (value, delay) in levels { levelSink(value, delay) }
        for w in waiters { w.resume(()) }
    }

    /// Flushes the converter's tail and refuses further audio.
    func finish() {
        lock.lock()
        if closed { lock.unlock(); return }
        closed = true
        if let converter, let out = AVAudioPCMBuffer(pcmFormat: target, frameCapacity: 8192) {
            var error: NSError?
            let status = converter.convert(to: out, error: &error) { _, inputStatus in
                inputStatus.pointee = .endOfStream
                return nil
            }
            if status != .error, out.frameLength > 0 {
                framesOut += Int64(out.frameLength)
                sink(out)
            }
        }
        converter = nil
        let waiters = feedWaiters
        feedWaiters.removeAll()
        lock.unlock()
        for w in waiters { w.resume(()) }
    }

    /// Returns after the next buffer arrives (so audio up to "now" has been
    /// delivered), or after `timeout` seconds, whichever comes first.
    func waitForNextBuffer(timeout: Double) async {
        await withCheckedContinuation { (cont: CheckedContinuation<Void, Never>) in
            let once = ResumeOnce(cont)
            lock.lock()
            if closed {
                lock.unlock()
                once.resume(())
                return
            }
            feedWaiters.append(once)
            lock.unlock()
            DispatchQueue.global().asyncAfter(deadline: .now() + timeout) { once.resume(()) }
        }
    }

    // MARK: - Conversion (lock held)

    private func convert(_ buffer: AVAudioPCMBuffer) -> AVAudioPCMBuffer? {
        let format = buffer.format
        if converter == nil || sourceFormat == nil || !sourceFormat!.isEqual(format) {
            guard let c = AVAudioConverter(from: format, to: target) else {
                if !conversionErrorLogged {
                    conversionErrorLogged = true
                    log("cannot convert \(format) to \(target)")
                }
                return nil
            }
            // Aggregate/pro interfaces expose many channels; the default
            // downmix of >2 channels is undefined, so take the first one.
            if format.channelCount > 2 && target.channelCount == 1 {
                c.channelMap = [0]
            }
            converter = c
            sourceFormat = format
            conversionErrorLogged = false
            log("capture format: \(Int(format.sampleRate)) Hz, \(format.channelCount) ch -> analyzer \(Int(target.sampleRate)) Hz, \(target.channelCount) ch")
        }
        guard let converter else { return nil }
        let ratio = target.sampleRate / format.sampleRate
        let capacity = AVAudioFrameCount(Double(buffer.frameLength) * ratio) + 64
        guard let out = AVAudioPCMBuffer(pcmFormat: target, frameCapacity: capacity) else { return nil }
        var consumed = false
        var error: NSError?
        let status = converter.convert(to: out, error: &error) { _, inputStatus in
            if consumed {
                inputStatus.pointee = .noDataNow
                return nil
            }
            consumed = true
            inputStatus.pointee = .haveData
            return buffer
        }
        if status == .error {
            if !conversionErrorLogged {
                conversionErrorLogged = true
                log("audio conversion failed: \(error.map(describe) ?? "unknown")")
            }
            return nil
        }
        return out.frameLength > 0 ? out : nil
    }

    // MARK: - Level meter (lock held)

    /// Splits the buffer into fixed windows and returns one normalised level
    /// per completed window, with the delay at which it should be shown so
    /// that several windows inside one buffer do not arrive as a burst.
    private func meter(_ buffer: AVAudioPCMBuffer) -> [(Float, Double)] {
        let frames = Int(buffer.frameLength)
        let rate = buffer.format.sampleRate
        guard frames > 0, rate > 0 else { return [] }
        let stride = buffer.stride

        // Get channel 0 as float samples.
        var base: UnsafePointer<Float>?
        if let f = buffer.floatChannelData {
            base = UnsafePointer(f[0])
        } else if let i16 = buffer.int16ChannelData {
            if scratch.count < frames { scratch = [Float](repeating: 0, count: frames) }
            vDSP_vflt16(i16[0], vDSP_Stride(stride), &scratch, 1, vDSP_Length(frames))
            var scale: Float = 1.0 / 32768.0
            vDSP_vsmul(scratch, 1, &scale, &scratch, 1, vDSP_Length(frames))
        } else {
            return []
        }

        let window = max(64, Int(rate / Self.levelRate))
        var out: [(Float, Double)] = []
        var offset = 0
        while offset < frames {
            let take = min(window - windowFrames, frames - offset)
            var sum: Float = 0
            if let base {
                vDSP_svesq(base + offset * stride, vDSP_Stride(stride), &sum, vDSP_Length(take))
            } else {
                scratch.withUnsafeBufferPointer { p in
                    vDSP_svesq(p.baseAddress! + offset, 1, &sum, vDSP_Length(take))
                }
            }
            windowSumSquares += sum
            windowFrames += take
            offset += take
            if windowFrames >= window {
                let meanSquare = windowSumSquares / Float(windowFrames)
                if meanSquare > peakMeanSquare { peakMeanSquare = meanSquare }
                meteredWindows += 1
                // This window ended (frames - offset) frames before the end of
                // the buffer; the first window is shown now, later ones later.
                let delay = out.isEmpty ? 0 : Double(out.count) * Double(window) / rate
                out.append((Self.normalise(meanSquare: meanSquare), delay))
                windowSumSquares = 0
                windowFrames = 0
            }
        }
        return out
    }

    /// Maps mean-square power to 0...1 on a dB scale: -55 dBFS (room noise)
    /// is 0, -10 dBFS (loud, close speech) is 1. Ordinary speech into a laptop
    /// microphone lands around 0.4-0.8.
    static func normalise(meanSquare: Float) -> Float {
        guard meanSquare.isFinite, meanSquare > 0 else { return 0 }
        let db = 10 * log10(meanSquare)
        let v = (db + 55) / 45
        return min(1, max(0, v))
    }
}
