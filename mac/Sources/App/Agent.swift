// The monitor's background service: a plain LaunchAgent in ~/Library/LaunchAgents that runs
// this app's arm-launcher. It starts at login, restarts if it stops (KeepAlive) and runs
// whether or not the menu is open.
//
// Why not SMAppService.agent: macOS ties an app-registered agent to the signature of the build
// that registered it, so a rebuilt or updated app (new signature) is refused with
// OS_REASON_CODESIGNING until the record is reset by hand. A plain LaunchAgent has no such tie,
// and the app rewrites it whenever the app has moved or been updated.

import Foundation

enum Agent {
    static let label = "io.github.astro-rig-monitor.agent"
    static var plist: URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/LaunchAgents/\(label).plist")
    }
    static var domain: String { "gui/\(getuid())" }
    static var launcher: String { Bundle.main.bundleURL.appendingPathComponent("Contents/MacOS/arm-launcher").path }
    static var build: String { Bundle.main.object(forInfoDictionaryKey: "CFBundleVersion") as? String ?? "0" }

    /// Is the service installed (whether or not it's running right now)?
    static var installed: Bool { FileManager.default.fileExists(atPath: plist.path) }

    static func contents() -> [String: Any] {
        [
            "Label": label,
            "ProgramArguments": [launcher],
            "RunAtLoad": true,
            "KeepAlive": true,
            "ThrottleInterval": 10,
            "ProcessType": "Standard",
            "AssociatedBundleIdentifiers": [Bundle.main.bundleIdentifier ?? "io.github.astro-rig-monitor.app"],
            // Lets a later launch of the app see that the service runs an older build.
            "EnvironmentVariables": ["ARM_APP_BUILD": build],
        ]
    }

    /// Write the plist and (re)load it. Safe to call when it's already installed.
    static func install() throws {
        let data = try PropertyListSerialization.data(fromPropertyList: contents(), format: .xml, options: 0)
        try FileManager.default.createDirectory(at: plist.deletingLastPathComponent(), withIntermediateDirectories: true)
        try data.write(to: plist, options: .atomic)
        _ = launchctl(["bootout", "\(domain)/\(label)"])
        let (code, out) = launchctl(["bootstrap", domain, plist.path])
        if code != 0 && code != 37 {        // 37: already loaded
            throw NSError(domain: "Agent", code: Int(code),
                          userInfo: [NSLocalizedDescriptionKey: "launchctl bootstrap failed (\(code)): \(out)"])
        }
    }

    static func remove() {
        _ = launchctl(["bootout", "\(domain)/\(label)"])
        try? FileManager.default.removeItem(at: plist)
    }

    static func restart() {
        _ = launchctl(["kickstart", "-k", "\(domain)/\(label)"])
    }

    /// Installed, but for another copy or build of the app (moved, or updated in place)?
    static var outOfDate: Bool {
        guard let d = NSDictionary(contentsOf: plist) as? [String: Any] else { return false }
        let args = d["ProgramArguments"] as? [String] ?? []
        let env = d["EnvironmentVariables"] as? [String: String] ?? [:]
        return args.first != launcher || env["ARM_APP_BUILD"] != build
    }

    @discardableResult
    static func launchctl(_ args: [String]) -> (Int32, String) {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/launchctl")
        p.arguments = args
        let pipe = Pipe()
        p.standardOutput = pipe
        p.standardError = pipe
        do { try p.run() } catch { return (-1, "\(error)") }
        let out = String(data: pipe.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
        p.waitUntilExit()
        return (p.terminationStatus, out.trimmingCharacters(in: .whitespacesAndNewlines))
    }
}
