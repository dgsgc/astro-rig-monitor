// arm-launcher: what the app's background item runs (launchd starts it at login and again
// if it stops). It keeps the Mac from idle-sleeping while the monitor runs (the screen can
// still sleep), then runs the bundled Python:
//
//     python3 -m arm --config "~/Library/Application Support/Astro Rig Monitor/config.toml"
//
// in that folder, so arm.log, nights/ and reports/ land there too. With no config.toml yet,
// the monitor serves its setup page on http://localhost:8080/setup until it's saved.

import Foundation
import IOKit.pwr_mgt

let fm = FileManager.default
let support = fm.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
let data = support.appendingPathComponent("Astro Rig Monitor", isDirectory: true)
try? fm.createDirectory(at: data, withIntermediateDirectories: true)

let resources = Bundle.main.resourceURL!
let python = resources.appendingPathComponent("python/bin/python3")

// Console output (Python errors before logging starts, the first-run setup message).
let console = data.appendingPathComponent("console.log")
if let size = (try? fm.attributesOfItem(atPath: console.path))?[.size] as? Int, size > 1_000_000 {
    try? fm.removeItem(at: console)
}
if !fm.fileExists(atPath: console.path) { fm.createFile(atPath: console.path, contents: nil) }
let out = try FileHandle(forWritingTo: console)
out.seekToEndOfFile()

func note(_ s: String) {
    let line = ISO8601DateFormatter().string(from: Date()) + " launcher: " + s + "\n"
    out.write(line.data(using: .utf8)!)
}

// Keep the Mac awake (not the screen) while the monitor runs.
var assertion: IOPMAssertionID = 0
IOPMAssertionCreateWithName(kIOPMAssertionTypePreventUserIdleSystemSleep as CFString,
                            IOPMAssertionLevel(kIOPMAssertionLevelOn),
                            "Astro Rig Monitor is watching your rigs" as CFString, &assertion)

/// True if nothing is listening on this port on this Mac.
func portFree(_ port: Int) -> Bool {
    let s = socket(AF_INET, SOCK_STREAM, 0)
    guard s >= 0 else { return false }
    defer { close(s) }
    var addr = sockaddr_in()
    addr.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
    addr.sin_family = sa_family_t(AF_INET)
    addr.sin_port = in_port_t(UInt16(port).bigEndian)
    addr.sin_addr.s_addr = INADDR_ANY
    return withUnsafePointer(to: &addr) {
        $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { bind(s, $0, socklen_t(MemoryLayout<sockaddr_in>.size)) == 0 }
    }
}

// First run (no config yet): the setup page uses 8080, or the next free port if something else
// has it. The menu app reads the port from the same setting ("SetupPort").
let config = data.appendingPathComponent("config.toml")
var setupPort = 8080
if !fm.fileExists(atPath: config.path) {
    setupPort = (8080...8099).first(where: portFree) ?? 8080
    UserDefaults.standard.set(setupPort, forKey: "SetupPort")
    UserDefaults.standard.synchronize()
}

let p = Process()
p.executableURL = python
p.arguments = ["-m", "arm", "--config", config.path, "--setup-port", String(setupPort)]
p.currentDirectoryURL = data
var env = ProcessInfo.processInfo.environment
env["PYTHONPATH"] = resources.path              // the arm package
env["PYTHONNOUSERSITE"] = "1"                    // ignore any Python packages the user installed
env["PYTHONDONTWRITEBYTECODE"] = "1"             // the app is signed: never write inside it
env["PYTHONUNBUFFERED"] = "1"
env["ARM_APP"] = Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "?"
p.environment = env
p.standardOutput = out
p.standardError = out

// Pass a stop from launchd (or the app) on to Python, so it shuts down cleanly.
for sig in [SIGTERM, SIGINT] {
    signal(sig, SIG_IGN)
    let src = DispatchSource.makeSignalSource(signal: sig, queue: .main)
    src.setEventHandler { if p.isRunning { p.interrupt(); kill(p.processIdentifier, SIGTERM) } }
    src.resume()
    _ = Unmanaged.passRetained(src as AnyObject)
}

p.terminationHandler = { proc in
    note("monitor stopped (status \(proc.terminationStatus))")
    IOPMAssertionRelease(assertion)
    exit(proc.terminationStatus)                // launchd starts it again if it should be running
}

do {
    note("starting the monitor")
    try p.run()
} catch {
    note("couldn't start Python: \(error)")
    exit(1)
}
dispatchMain()
