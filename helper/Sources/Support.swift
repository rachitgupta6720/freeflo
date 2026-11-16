// Shared plumbing: version, logging, JSON-lines output, timeouts.

import Foundation

/// Helper/protocol version. Bump when the JSON protocol changes.
let helperVersion = "1.2.0"

/// Exit code used when the helper kills itself because CoreAudio stopped
/// answering. The app treats any exit as "respawn"; this one is just easy to
/// spot in logs.
let exitCodeAudioWedged: Int32 = 75

// MARK: - Logging (stderr only; stdout belongs to the protocol)

private let logQueue = DispatchQueue(label: "freeflo.helper.log", qos: .utility)
private let logLock = NSLock()
private var logPending = 0
private let logStamp: DateFormatter = {
    let f = DateFormatter()
    f.locale = Locale(identifier: "en_US_POSIX")
    f.dateFormat = "HH:mm:ss.SSS"
    return f
}()

/// Writes one line to stderr. Asynchronous and bounded, so a parent that is
/// slow to drain stderr can delay log lines but never the dictation path.
func log(_ message: @autoclosure () -> String) {
    logLock.lock()
    if logPending > 400 { logLock.unlock(); return }
    logPending += 1
    logLock.unlock()
    let text = message()
    let now = Date()
    logQueue.async {
        let line = "freeflo-helper \(logStamp.string(from: now)) \(text)\n"
        line.withCString { p in _ = write(STDERR_FILENO, p, strlen(p)) }
        logLock.lock(); logPending -= 1; logLock.unlock()
    }
}

func flushLog(timeout: Double = 0.3) {
    let sem = DispatchSemaphore(value: 0)
    logQueue.async { sem.signal() }
    _ = sem.wait(timeout: .now() + timeout)
}

// MARK: - JSON encoding

/// A pre-encoded JSON fragment (used to echo numbers exactly as received).
struct RawJSON { let text: String }

/// Minimal JSON writer. Hand-rolled so key order is stable ("event" first) and
/// floats print as short decimals rather than 17-digit doubles.
enum JSON {
    static func string(_ s: String) -> String {
        var out = "\""
        out.reserveCapacity(s.utf8.count + 2)
        for u in s.unicodeScalars {
            switch u {
            case "\"": out += "\\\""
            case "\\": out += "\\\\"
            case "\n": out += "\\n"
            case "\r": out += "\\r"
            case "\t": out += "\\t"
            default:
                if u.value < 0x20 || u.value == 0x2028 || u.value == 0x2029 || u.value == 0x7F {
                    out += String(format: "\\u%04x", u.value)
                } else {
                    out.unicodeScalars.append(u)
                }
            }
        }
        return out + "\""
    }

    static func number(_ d: Double, places: Int = 3) -> String {
        guard d.isFinite else { return "0" }
        var s = String(format: "%.\(places)f", d)
        if s.contains(".") {
            while s.hasSuffix("0") { s.removeLast() }
            if s.hasSuffix(".") { s += "0" }
        }
        return s
    }

    static func value(_ v: Any?) -> String {
        switch v {
        case nil: return "null"
        case let r as RawJSON: return r.text
        case let s as String: return string(s)
        case let b as Bool: return b ? "true" : "false"
        case let i as Int: return String(i)
        case let d as Double: return number(d)
        case let f as Float: return number(Double(f))
        case let a as [String]: return "[" + a.map(string).joined(separator: ",") + "]"
        case let a as [[String]]: return "[" + a.map { value($0) }.joined(separator: ",") + "]"
        case is NSNull: return "null"
        default: return string(String(describing: v!))
        }
    }

    static func object(_ pairs: KeyValuePairs<String, Any?>) -> String {
        object(pairs.map { ($0.key, $0.value) })
    }

    static func object(_ pairs: [(String, Any?)]) -> String {
        var out = "{"
        var first = true
        for (k, v) in pairs {
            if !first { out += "," }
            first = false
            out += string(k) + ":" + value(v)
        }
        return out + "}"
    }

    /// Anything JSONSerialization can encode, as a fragment (used for nested
    /// objects such as `decisions`).
    static func fragment(_ any: Any) -> RawJSON {
        guard JSONSerialization.isValidJSONObject(["v": any]),
              let data = try? JSONSerialization.data(withJSONObject: any, options: [.fragmentsAllowed, .withoutEscapingSlashes]),
              let text = String(data: data, encoding: .utf8) else { return RawJSON(text: "null") }
        return RawJSON(text: text)
    }

    /// Re-encodes a number parsed by JSONSerialization (integers stay integers).
    static func echoNumber(_ any: Any?) -> RawJSON? {
        guard let n = any as? NSNumber, CFGetTypeID(n) != CFBooleanGetTypeID() else { return nil }
        let d = n.doubleValue
        if d == d.rounded(), abs(d) < 9.0e15 { return RawJSON(text: String(n.int64Value)) }
        return RawJSON(text: number(d, places: 6))
    }

    static func int(_ any: Any?) -> Int? {
        guard let n = any as? NSNumber, CFGetTypeID(n) != CFBooleanGetTypeID() else { return nil }
        let d = n.doubleValue
        guard d == d.rounded(), abs(d) < 9.0e15 else { return nil }
        return n.intValue
    }
}

// MARK: - Event output

/// Serialises every protocol event onto stdout, one JSON object per line.
///
/// Writes happen on a private serial queue, so no caller (least of all the
/// audio tap) ever blocks on the pipe. `level` events are droppable: if the
/// parent stops reading they are discarded instead of piling up.
final class Emitter: @unchecked Sendable {
    static let shared = Emitter()

    private let queue = DispatchQueue(label: "freeflo.helper.stdout", qos: .userInitiated)
    private let lock = NSLock()
    private var droppablePending = 0
    private var levelGate: Int?   // session id whose `level` events may be written

    func send(_ pairs: KeyValuePairs<String, Any?>) {
        let line = JSON.object(pairs)
        queue.async { self.writeLine(line) }
    }

    func send(pairs: [(String, Any?)]) {
        let line = JSON.object(pairs)
        queue.async { self.writeLine(line) }
    }

    /// Emits a `level` event, optionally a little later (used to space out the
    /// several meter windows that arrive inside one audio buffer).
    func level(id: Int, rms: Float, after delay: Double) {
        lock.lock()
        if droppablePending > 64 { lock.unlock(); return }
        droppablePending += 1
        lock.unlock()
        let line = "{\"event\":\"level\",\"id\":\(id),\"rms\":\(JSON.number(Double(rms)))}"
        let work = {
            self.lock.lock()
            self.droppablePending -= 1
            let open = self.levelGate == id
            self.lock.unlock()
            if open { self.writeLine(line) }
        }
        if delay > 0.001 {
            queue.asyncAfter(deadline: .now() + delay, execute: work)
        } else {
            queue.async(execute: work)
        }
    }

    /// `level` events are only written between `openLevels` and `closeLevels`
    /// for their session, so none can trail in after `final`/`cancelled` or
    /// sneak out before `started` - even ones already queued with a delay.
    func openLevels(for id: Int) {
        lock.lock(); levelGate = id; lock.unlock()
    }

    func closeLevels(for id: Int) {
        lock.lock()
        if levelGate == id { levelGate = nil }
        lock.unlock()
    }

    /// Waits (bounded) until everything queued so far has been written.
    func flush(timeout: Double = 1.0) {
        let sem = DispatchSemaphore(value: 0)
        queue.async { sem.signal() }
        _ = sem.wait(timeout: .now() + timeout)
    }

    private func writeLine(_ line: String) {
        var bytes = Array(line.utf8)
        bytes.append(0x0A)
        var offset = 0
        while offset < bytes.count {
            let n = bytes.withUnsafeBytes { raw in
                write(STDOUT_FILENO, raw.baseAddress! + offset, bytes.count - offset)
            }
            if n > 0 {
                offset += n
            } else if n < 0 && errno == EINTR {
                continue
            } else if n < 0 && errno == EAGAIN {
                usleep(2000)
            } else {
                // EPIPE or similar: the parent is gone. Nothing left to serve.
                exit(0)
            }
        }
    }
}

// MARK: - Errors

/// An error that maps directly onto a protocol `error` event.
struct HelperError: Error, CustomStringConvertible {
    let code: String
    let message: String
    var description: String { "\(code): \(message)" }

    static func internalError(_ message: String) -> HelperError { .init(code: "internal", message: message) }
}

struct TimeoutError: DescribedError {
    let what: String
    let seconds: Double
    var description: String { "\(what) timed out after \(JSON.number(seconds, places: 1))s" }
}

/// Errors defined by the helper itself; `describe` prints their own text
/// instead of the opaque NSError bridge ("The operation couldn't be completed").
protocol DescribedError: Error, CustomStringConvertible {}

func describe(_ error: Error) -> String {
    if let h = error as? HelperError { return h.message }
    if let d = error as? DescribedError { return d.description }
    let ns = error as NSError
    return "\(ns.localizedDescription) [\(ns.domain) \(ns.code)]"
}

// MARK: - Timeouts

/// Resumes a continuation exactly once, from whichever of several racers
/// gets there first.
final class ResumeOnce<T>: @unchecked Sendable {
    private let lock = NSLock()
    private var continuation: CheckedContinuation<T, Never>?

    init(_ continuation: CheckedContinuation<T, Never>) { self.continuation = continuation }

    func resume(_ value: T) {
        lock.lock()
        let c = continuation
        continuation = nil
        lock.unlock()
        c?.resume(returning: value)
    }
}

/// Runs `operation` but stops *waiting* for it after `seconds`. The operation
/// itself is not interrupted (the Speech APIs offer no way to), it is simply
/// abandoned - which is what lets the helper always answer.
func withDeadline<T>(
    _ seconds: Double,
    _ what: String,
    _ operation: @escaping () async throws -> T
) async -> Result<T, Error> {
    await withCheckedContinuation { (cont: CheckedContinuation<Result<T, Error>, Never>) in
        let once = ResumeOnce(cont)
        Task {
            do { once.resume(.success(try await operation())) } catch { once.resume(.failure(error)) }
        }
        DispatchQueue.global().asyncAfter(deadline: .now() + seconds) {
            once.resume(.failure(TimeoutError(what: what, seconds: seconds)))
        }
    }
}

/// One-shot signal that any number of main-actor tasks can await.
@MainActor
final class Latch {
    private var fired = false
    private var waiters: [ResumeOnce<Void>] = []

    var isFired: Bool { fired }

    func fire() {
        guard !fired else { return }
        fired = true
        let w = waiters
        waiters.removeAll()
        for c in w { c.resume(()) }
    }

    /// Returns when fired, or after `timeout` seconds - never hangs.
    func wait(timeout: Double) async {
        if fired { return }
        await withCheckedContinuation { (cont: CheckedContinuation<Void, Never>) in
            let once = ResumeOnce(cont)
            waiters.append(once)
            DispatchQueue.global().asyncAfter(deadline: .now() + timeout) { once.resume(()) }
        }
    }
}

func millis(since start: DispatchTime) -> Double {
    Double(DispatchTime.now().uptimeNanoseconds &- start.uptimeNanoseconds) / 1_000_000
}
