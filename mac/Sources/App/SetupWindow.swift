// The setup page in a window of the app's own, instead of the browser. It's the same page the
// monitor serves at /setup (one setup page for every platform); this only frames it. Links that
// leave the monitor (ntfy, the docs) open in the browser. After "Save and start" the page moves
// on to the monitor's own page, which then shows in this window until it's closed.

import AppKit
import WebKit

@MainActor
final class SetupWindow: NSObject, NSWindowDelegate, WKNavigationDelegate, WKUIDelegate {
    static let shared = SetupWindow()

    private var window: NSWindow?
    private var web: WKWebView?
    private var titleWatch: NSKeyValueObservation?
    private var lastURL: URL?

    /// Show `url` (a page of the monitor on this Mac) in the window, opening the window if needed.
    func show(_ url: URL) {
        lastURL = url
        if window == nil { make() }
        web?.load(URLRequest(url: url))
        NSApp.setActivationPolicy(.regular)           // a Dock icon and Cmd-Tab while it's open
        installMenu()
        NSApp.activate(ignoringOtherApps: true)
        window?.makeKeyAndOrderFront(nil)
    }

    private func make() {
        let config = WKWebViewConfiguration()
        config.websiteDataStore = .default()
        let web = WKWebView(frame: .zero, configuration: config)
        web.navigationDelegate = self
        web.uiDelegate = self
        web.allowsBackForwardNavigationGestures = false

        let w = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 780, height: 860),
                         styleMask: [.titled, .closable, .miniaturizable, .resizable],
                         backing: .buffered, defer: false)
        w.title = "Astro Rig Monitor Setup"
        w.contentView = web
        w.contentMinSize = NSSize(width: 420, height: 480)
        w.isReleasedWhenClosed = false
        w.delegate = self
        w.setFrameAutosaveName("SetupWindow")
        if !w.setFrameUsingName("SetupWindow") { w.center() }
        titleWatch = web.observe(\.title, options: [.new]) { [weak w] view, _ in
            let t = view.title ?? ""
            Task { @MainActor in
                w?.title = t.lowercased().contains("setup") || t.isEmpty ? "Astro Rig Monitor Setup" : "Astro Rig Monitor"
            }
        }
        self.window = w
        self.web = web
    }

    func windowWillClose(_ note: Notification) {
        web?.stopLoading()
        web?.loadHTMLString("", baseURL: nil)       // stop the page's polling while it's hidden
        NSApp.setActivationPolicy(.accessory)        // back to a menu-bar-only app
    }

    // MARK: navigation

    private static func isMonitor(_ url: URL?) -> Bool {
        guard let url, let host = url.host else { return url?.scheme == "about" }
        return ["localhost", "127.0.0.1", "::1"].contains(host)
    }

    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping @MainActor (WKNavigationActionPolicy) -> Void) {
        if Self.isMonitor(action.request.url) || action.request.url?.scheme == "about" {
            decisionHandler(.allow)
        } else {
            if let url = action.request.url { NSWorkspace.shared.open(url) }
            decisionHandler(.cancel)
        }
    }

    /// target="_blank" links and window.open: the browser.
    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = action.request.url { NSWorkspace.shared.open(url) }
        return nil
    }

    func webViewDidClose(_ webView: WKWebView) {
        window?.close()
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation nav: WKNavigation!, withError error: Error) {
        let err = error as NSError
        if err.code == NSURLErrorCancelled { return }
        let retry = lastURL?.absoluteString ?? ""
        webView.loadHTMLString("""
            <!doctype html><meta charset="utf-8"><style>
            body{font:15px -apple-system,sans-serif;background:#111;color:#ddd;padding:40px;line-height:1.5}
            @media (prefers-color-scheme: light){body{background:#f6f6f6;color:#222}}
            a{color:#6ab0ff}</style>
            <h2>The monitor isn't answering yet</h2>
            <p>It may still be starting. <a href="\(retry)">Try again</a> in a few seconds.</p>
            <p>If it keeps happening, check that <b>Run Monitor</b> is ticked in the menu, and look at
            <b>More → Show Log</b>.</p>
            """, baseURL: URL(string: "about:blank"))
    }

    // MARK: JavaScript dialogs (the page doesn't use them today; this keeps them working if it does)

    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping @MainActor () -> Void) {
        let a = NSAlert()
        a.messageText = message
        if let window { a.beginSheetModal(for: window) { _ in completionHandler() } } else { a.runModal(); completionHandler() }
    }

    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping @MainActor (Bool) -> Void) {
        let a = NSAlert()
        a.messageText = message
        a.addButton(withTitle: "OK")
        a.addButton(withTitle: "Cancel")
        if let window {
            a.beginSheetModal(for: window) { completionHandler($0 == .alertFirstButtonReturn) }
        } else {
            completionHandler(a.runModal() == .alertFirstButtonReturn)
        }
    }

    // MARK: menu

    /// A menu-bar app has no menus, so without these Cmd-C, Cmd-V and Cmd-W do nothing in the
    /// window's text fields (pasting an ntfy topic or a Dropbox link needs them).
    private func installMenu() {
        let main = NSMenu()

        let appItem = NSMenuItem()
        let app = NSMenu()
        app.addItem(withTitle: "About Astro Rig Monitor", action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
        app.addItem(.separator())
        app.addItem(withTitle: "Hide", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        appItem.submenu = app
        main.addItem(appItem)

        let editItem = NSMenuItem()
        let edit = NSMenu(title: "Edit")
        edit.addItem(withTitle: "Undo", action: Selector(("undo:")), keyEquivalent: "z")
        let redo = edit.addItem(withTitle: "Redo", action: Selector(("redo:")), keyEquivalent: "z")
        redo.keyEquivalentModifierMask = [.command, .shift]
        edit.addItem(.separator())
        edit.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        edit.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        edit.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        edit.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editItem.submenu = edit
        main.addItem(editItem)

        let viewItem = NSMenuItem()
        let view = NSMenu(title: "View")
        let reload = view.addItem(withTitle: "Reload Page", action: #selector(reloadPage), keyEquivalent: "r")
        reload.target = self
        viewItem.submenu = view
        main.addItem(viewItem)

        let windowItem = NSMenuItem()
        let win = NSMenu(title: "Window")
        win.addItem(withTitle: "Close", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
        win.addItem(withTitle: "Minimize", action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m")
        windowItem.submenu = win
        main.addItem(windowItem)

        NSApp.mainMenu = main
    }

    @objc private func reloadPage() {
        web?.reload()
    }
}
