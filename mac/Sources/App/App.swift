// Astro Rig Monitor for Mac: a menu-bar app that installs and controls the monitor.
//
// The monitor itself runs as a LaunchAgent (Agent.swift → arm-launcher → the bundled Python
// running `python -m arm`), so it starts at login, restarts if it stops and keeps running when
// this menu is quit. This app only shows its status and opens things:
// it never talks to the rigs.

import AppKit
import CoreImage
import ServiceManagement
import SwiftUI

let releasesPage = URL(string: "https://github.com/dgsgc/astro-rig-monitor/releases")!
let releasesAPI = URL(string: "https://api.github.com/repos/dgsgc/astro-rig-monitor/releases?per_page=20")!

@main
struct MonitorApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var delegate
    @StateObject private var model = Model.shared

    var body: some Scene {
        MenuBarExtra {
            MenuContent().environmentObject(model)
        } label: {
            Image(systemName: "scope")
        }
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ note: Notification) {
        // "Astro Rig Monitor.app/Contents/MacOS/Astro Rig Monitor" --uninstall: stop the monitor and
        // remove its background item (and the menu's login item), then quit. Settings stay.
        if CommandLine.arguments.contains("--uninstall") {
            Agent.remove()
            try? SMAppService.mainApp.unregister()
            print("Stopped the monitor and removed its background item. Settings and logs are still in \(Paths.data.path)")
            exit(0)
        }
        // --setup-window <url>: only show the setup window for that page (for testing the window
        // against a throwaway monitor). Leaves the background service alone.
        if let i = CommandLine.arguments.firstIndex(of: "--setup-window"), i + 1 < CommandLine.arguments.count,
           let url = URL(string: CommandLine.arguments[i + 1]) {
            SetupWindow.shared.show(url)
            return
        }
        Model.shared.firstLaunch()
    }
}

// MARK: - What the menu shows

struct MenuContent: View {
    @EnvironmentObject var model: Model

    var body: some View {
        Text(model.statusLine)
        Divider()
        Button("Open in Browser on This Mac") { model.openPage("/") }
            .keyboardShortcut("o")
            .disabled(!model.hasConfig)
        Button(model.hasConfig && model.phoneAddress == nil ? "Open on Your Phone (No Network)" : "Open on Your Phone…") {
            model.showPhone()
        }
            .disabled(model.phoneAddress == nil)
        Divider()
        Button(model.hasConfig ? "Add Rigs or Change Settings…" : "Set Up…") { model.openPage("/setup") }
        Button("Send Test Notification") { model.testNotify() }
            .disabled(!model.hasConfig)
        Divider()
        Menu("More") {
            Button("Edit Settings File…") { model.editConfig() }.disabled(!model.hasConfig)
            Button("Import Settings File…") { model.importConfig() }
            Button("Show Log") { model.showLog() }
            Button("Open Data Folder") { NSWorkspace.shared.open(Paths.data) }
            Button("Restart Monitor") { model.restart() }.disabled(!model.running)
        }
        Toggle("Run Monitor", isOn: Binding(get: { model.running }, set: { model.setRunning($0) }))
        Toggle("Show This Menu at Login", isOn: Binding(get: { model.menuAtLogin }, set: { model.setMenuAtLogin($0) }))
        Divider()
        Button("Check for Updates…") { model.checkForUpdates() }
        Button("About Astro Rig Monitor") { model.about() }
        Button("Quit Menu (the Monitor Keeps Running)") { NSApp.terminate(nil) }
            .keyboardShortcut("q")
    }
}

// MARK: - Files

enum Paths {
    static let data: URL = {
        let u = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("Astro Rig Monitor", isDirectory: true)
        try? FileManager.default.createDirectory(at: u, withIntermediateDirectories: true)
        return u
    }()
    static var config: URL { data.appendingPathComponent("config.toml") }
    static var log: URL { data.appendingPathComponent("arm.log") }
    static var console: URL { data.appendingPathComponent("console.log") }
    static let resources = Bundle.main.resourceURL!
    static var python: URL { resources.appendingPathComponent("python/bin/python3") }
}

// MARK: - The model

@MainActor
final class Model: ObservableObject {
    static let shared = Model()

    @Published var running = false
    @Published var menuAtLogin = false
    @Published var hasConfig = false
    @Published var answering = false            // the monitor's web page answers
    @Published var rigCount = 0
    @Published var phoneAddress: String?

    private var timer: Timer?

    var appVersion: String { Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "?" }
    var monitorVersion: String { Bundle.main.object(forInfoDictionaryKey: "ARMVersion") as? String ?? "?" }

    var statusLine: String {
        if !running { return "Monitor stopped" }
        if !hasConfig { return answering ? "Not set up yet: choose Set Up…" : "Starting…" }
        if answering { return "Monitor running · \(rigCount) rig\(rigCount == 1 ? "" : "s")" }
        return webPort == nil ? "Monitor running (no web page)" : "Starting, or not answering…"
    }

    /// The web page's port from config.toml ([web] port), 8080 if not set, nil if there's no [web].
    var webPort: Int? {
        guard let text = try? String(contentsOf: Paths.config, encoding: .utf8) else {   // the setup page
            UserDefaults.standard.synchronize()
            let p = UserDefaults.standard.integer(forKey: "SetupPort")
            return p > 0 ? p : 8080
        }
        var inWeb = false, hasWeb = false
        for raw in text.split(separator: "\n") {
            let line = raw.trimmingCharacters(in: .whitespaces)
            if line.hasPrefix("[") { inWeb = line == "[web]"; hasWeb = hasWeb || inWeb; continue }
            if inWeb, line.hasPrefix("port"), let eq = line.firstIndex(of: "=") {
                let v = line[line.index(after: eq)...].split(separator: "#").first?.trimmingCharacters(in: .whitespaces)
                if let v, let n = Int(v) { return n }
            }
        }
        return hasWeb ? 8080 : nil
    }

    func firstLaunch() {
        if Bundle.main.bundleURL.path.hasPrefix("/Volumes/") || Bundle.main.bundleURL.path.contains("/AppTranslocation/") {
            alert("Move the app to Applications first",
                  "Drag Astro Rig Monitor into your Applications folder and open it from there. "
                  + "The monitor runs from where the app is, so it must stay in one place.")
        }
        warnIfTranslated()
        refresh()
        if Agent.installed && Agent.outOfDate {
            // The app was moved or updated: point the service at this copy and restart it.
            setRunning(true)
        } else if !Agent.installed && !UserDefaults.standard.bool(forKey: "MonitorOff") {
            setRunning(true)
            if !hasConfig {
                // Open the setup page as soon as the monitor answers.
                Task { @MainActor in
                    for _ in 0..<30 {
                        try? await Task.sleep(nanoseconds: 1_000_000_000)
                        await poll()
                        if answering { openPage("/setup"); return }
                    }
                    alert("The setup page didn't open",
                          "The monitor didn't start its setup page. Choose More → Show Log for details.")
                }
            }
        }
        timer = Timer.scheduledTimer(withTimeInterval: 5, repeats: true) { _ in
            Task { @MainActor in self.refresh(); await self.poll() }
        }
        Task { await poll() }
    }

    func refresh() {
        running = Agent.installed
        menuAtLogin = SMAppService.mainApp.status == .enabled
        hasConfig = FileManager.default.fileExists(atPath: Paths.config.path)
        phoneAddress = hasConfig ? localAddress().flatMap { ip in webPort.map { "http://\(ip):\($0)" } } : nil
    }

    func poll() async {
        guard running, let port = webPort else { answering = false; return }
        let path = hasConfig ? "/state.json" : "/setup/api/state"
        var req = URLRequest(url: URL(string: "http://127.0.0.1:\(port)\(path)")!)
        req.timeoutInterval = 3
        do {
            let (data, resp) = try await URLSession.shared.data(for: req)
            answering = (resp as? HTTPURLResponse)?.statusCode == 200
            if hasConfig, let j = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
               let rigs = j["rigs"] as? [Any] {
                rigCount = rigs.count
            }
        } catch {
            answering = false
        }
    }

    // MARK: actions

    func setRunning(_ on: Bool) {
        UserDefaults.standard.set(!on, forKey: "MonitorOff")     // remember a deliberate stop
        if on {
            do {
                try Agent.install()
            } catch {
                alert("Couldn't start the monitor", error.localizedDescription)
            }
        } else {
            Agent.remove()
        }
        refresh()
    }

    func setMenuAtLogin(_ on: Bool) {
        do {
            if on { try SMAppService.mainApp.register() } else { try SMAppService.mainApp.unregister() }
        } catch {
            alert("Couldn't change that", error.localizedDescription)
        }
        refresh()
    }

    func restart() {
        Agent.restart()
    }

    func openPage(_ path: String) {
        guard let port = webPort else {
            alert("There's no web page", "The settings have no [web] section, so there's no page to open. "
                  + "Use More → Edit Settings File… to add one.")
            return
        }
        let url = URL(string: "http://localhost:\(port)\(path)")!
        if path.hasPrefix("/setup") {
            SetupWindow.shared.show(url)          // its own window, not the browser
        } else {
            NSWorkspace.shared.open(url)
        }
    }

    func copy(_ s: String) {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(s, forType: .string)
    }

    /// A QR code for the phone's camera, with the address to type or copy.
    func showPhone() {
        guard let address = phoneAddress else { return }
        NSApp.activate(ignoringOtherApps: true)
        let a = NSAlert()
        a.messageText = "Open the monitor on your phone"
        a.informativeText = "Point your phone's camera at the code and tap the link, or type this address "
            + "into its browser:\n\n\(address)\n\nYour phone must be on the same WiFi as this Mac. "
            + "To keep it handy, add the page to your Home Screen (Share → Add to Home Screen)."
        if let qr = qrCode(address) {
            let view = NSImageView(frame: NSRect(x: 0, y: 0, width: 200, height: 200))
            view.image = qr
            a.accessoryView = view
        }
        a.addButton(withTitle: "Done")
        a.addButton(withTitle: "Copy Address")
        if a.runModal() == .alertSecondButtonReturn { copy(address) }
    }

    func qrCode(_ text: String) -> NSImage? {
        guard let f = CIFilter(name: "CIQRCodeGenerator") else { return nil }
        f.setValue(Data(text.utf8), forKey: "inputMessage")
        f.setValue("M", forKey: "inputCorrectionLevel")
        guard let out = f.outputImage else { return nil }
        // Scale up with hard edges, on a white border so phones read it in dark mode too.
        let scaled = out.transformed(by: CGAffineTransform(scaleX: 8, y: 8))
        let rep = NSCIImageRep(ciImage: scaled)
        let side: CGFloat = 200, pad: CGFloat = 12
        let img = NSImage(size: NSSize(width: side, height: side))
        img.lockFocus()
        NSColor.white.setFill()
        NSRect(x: 0, y: 0, width: side, height: side).fill()
        NSGraphicsContext.current?.imageInterpolation = .none
        rep.draw(in: NSRect(x: pad, y: pad, width: side - 2 * pad, height: side - 2 * pad))
        img.unlockFocus()
        return img
    }

    func editConfig() {
        let textEdit = URL(fileURLWithPath: "/System/Applications/TextEdit.app")
        NSWorkspace.shared.open([Paths.config], withApplicationAt: textEdit, configuration: NSWorkspace.OpenConfiguration())
        alert("After saving your changes", "Choose More → Restart Monitor so the monitor uses them.")
    }

    func showLog() {
        let file = FileManager.default.fileExists(atPath: Paths.log.path) ? Paths.log : Paths.console
        let console = URL(fileURLWithPath: "/System/Applications/Utilities/Console.app")
        NSWorkspace.shared.open([file], withApplicationAt: console, configuration: NSWorkspace.OpenConfiguration())
    }

    func importConfig() {
        let panel = NSOpenPanel()
        panel.message = "Choose a config.toml from an earlier Astro Rig Monitor install"
        panel.allowedContentTypes = []
        panel.allowsOtherFileTypes = true
        panel.canChooseDirectories = false
        NSApp.activate(ignoringOtherApps: true)
        guard panel.runModal() == .OK, let src = panel.url else { return }
        guard let text = try? String(contentsOf: src, encoding: .utf8), text.contains("[rigs.") else {
            alert("That doesn't look like a settings file", "A config.toml has at least one [rigs.<name>] section.")
            return
        }
        let fm = FileManager.default
        if fm.fileExists(atPath: Paths.config.path) {
            let stamp = ISO8601DateFormatter().string(from: Date()).replacingOccurrences(of: ":", with: "")
            try? fm.moveItem(at: Paths.config, to: Paths.data.appendingPathComponent("config.toml.bak-\(stamp)"))
        }
        do {
            try fm.copyItem(at: src, to: Paths.config)
            try fm.setAttributes([.posixPermissions: 0o600], ofItemAtPath: Paths.config.path)
        } catch {
            alert("Couldn't import it", error.localizedDescription)
            return
        }
        let relative = text.contains("nights_dir") || text.contains("reports_dir") || text.contains("archive_dir")
        alert("Imported",
              "The monitor is restarting with these settings. Night recordings and reports now go to "
              + "\(Paths.data.path)." + (relative ? " Check any folders named in the file (nights_dir, reports_dir, archive_dir)." : ""))
        restart()
        refresh()
    }

    func testNotify() {
        let p = Process()
        p.executableURL = Paths.python
        p.arguments = ["-m", "arm", "test-notify", "--config", Paths.config.path]
        p.currentDirectoryURL = Paths.data
        p.environment = ["PYTHONPATH": Paths.resources.path, "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1"]
        let pipe = Pipe()
        p.standardOutput = pipe
        p.standardError = pipe
        do { try p.run() } catch { alert("Couldn't run the test", error.localizedDescription); return }
        Task.detached {
            let out = String(data: pipe.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
            p.waitUntilExit()
            let ok = p.terminationStatus == 0
            await MainActor.run {
                self.alert(ok ? "Test notification sent" : "The test didn't send",
                           out.split(separator: "\n").suffix(4).joined(separator: "\n"))
            }
        }
    }

    func checkForUpdates() {
        Task {
            do {
                var req = URLRequest(url: releasesAPI)
                req.setValue("application/vnd.github+json", forHTTPHeaderField: "Accept")
                let (data, _) = try await URLSession.shared.data(for: req)
                let list = (try JSONSerialization.jsonObject(with: data) as? [[String: Any]]) ?? []
                let tags = list.compactMap { $0["tag_name"] as? String }.filter { $0.hasPrefix("mac-v") }
                guard let newest = tags.max(by: { versionLess($0.dropFirst(5), $1.dropFirst(5)) }) else {
                    alert("No Mac releases found", "Have a look at the releases page instead.")
                    NSWorkspace.shared.open(releasesPage)
                    return
                }
                let v = String(newest.dropFirst(5))
                if versionLess(Substring(appVersion), Substring(v)) {
                    if confirm("Version \(v) is available", "You have \(appVersion). Open the download page?") {
                        NSWorkspace.shared.open(releasesPage.appendingPathComponent("tag/\(newest)"))
                    }
                } else {
                    alert("You're up to date", "Astro Rig Monitor \(appVersion) is the newest Mac version.")
                }
            } catch {
                alert("Couldn't check for updates", error.localizedDescription)
            }
        }
    }

    func about() {
        NSApp.activate(ignoringOtherApps: true)
        NSApp.orderFrontStandardAboutPanel(options: [
            .applicationVersion: "\(appVersion) (monitor \(monitorVersion))",
            .credits: NSAttributedString(string: "Watches your guiding and tells you when it goes wrong. "
                                         + "It never sends commands to your rigs.\n\nSettings and logs: \(Paths.data.path)"),
        ])
    }

    /// The Intel app works on Apple Silicon through Rosetta, but the Apple Silicon one is better.
    func warnIfTranslated() {
        var translated: Int32 = 0
        var size = MemoryLayout<Int32>.size
        guard sysctlbyname("sysctl.proc_translated", &translated, &size, nil, 0) == 0, translated == 1,
              !UserDefaults.standard.bool(forKey: "toldAboutAppleSilicon") else { return }
        UserDefaults.standard.set(true, forKey: "toldAboutAppleSilicon")
        if confirm("There's an Apple Silicon version",
                   "This is the Intel version, running through Rosetta. The Apple Silicon version uses less power. "
                   + "Open the download page?") {
            NSWorkspace.shared.open(releasesPage)
        }
    }

    // MARK: helpers

    func alert(_ title: String, _ text: String) {
        NSApp.activate(ignoringOtherApps: true)
        let a = NSAlert()
        a.messageText = title
        a.informativeText = text
        a.runModal()
    }

    func confirm(_ title: String, _ text: String) -> Bool {
        NSApp.activate(ignoringOtherApps: true)
        let a = NSAlert()
        a.messageText = title
        a.informativeText = text
        a.addButton(withTitle: "Open")
        a.addButton(withTitle: "Not Now")
        return a.runModal() == .alertFirstButtonReturn
    }
}

/// "0.10.0" > "0.9.1", comparing numbers part by part.
func versionLess(_ a: Substring, _ b: Substring) -> Bool {
    let x = a.split(separator: ".").map { Int($0) ?? 0 }, y = b.split(separator: ".").map { Int($0) ?? 0 }
    for i in 0..<max(x.count, y.count) {
        let p = i < x.count ? x[i] : 0, q = i < y.count ? y[i] : 0
        if p != q { return p < q }
    }
    return false
}

/// This Mac's address on the home network (the first private IPv4 address), for the phone link.
func localAddress() -> String? {
    var ifaddr: UnsafeMutablePointer<ifaddrs>?
    guard getifaddrs(&ifaddr) == 0, let first = ifaddr else { return nil }
    defer { freeifaddrs(ifaddr) }
    var found: [String] = []
    for p in sequence(first: first, next: { $0.pointee.ifa_next }) {
        let a = p.pointee
        guard let sa = a.ifa_addr, sa.pointee.sa_family == UInt8(AF_INET),
              (a.ifa_flags & UInt32(IFF_UP)) != 0, (a.ifa_flags & UInt32(IFF_LOOPBACK)) == 0 else { continue }
        var host = [CChar](repeating: 0, count: Int(NI_MAXHOST))
        if getnameinfo(sa, socklen_t(sa.pointee.sa_len), &host, socklen_t(host.count), nil, 0, NI_NUMERICHOST) == 0 {
            let ip = String(cString: host)
            if ip.hasPrefix("192.168.") || ip.hasPrefix("10.") || ip.range(of: #"^172\.(1[6-9]|2\d|3[01])\."#, options: .regularExpression) != nil {
                found.append(ip)
            }
        }
    }
    return found.first
}
