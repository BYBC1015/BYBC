import Foundation
import Combine
import WebKit
import UIKit
import Network

struct HubDownloadedFile: Identifiable {
    let id = UUID()
    let url: URL
}

@MainActor
final class HubSession: NSObject, ObservableObject, WKNavigationDelegate, WKUIDelegate, WKDownloadDelegate {
    let webView: WKWebView
    let policy: HubPolicy
    @Published var isLoading = false
    @Published var progress = 0.0
    @Published var connectionError: String?
    @Published var downloadMessage: String?
    @Published var downloadedFile: HubDownloadedFile?
    @Published var localRecord: HubLocalRecord?
    @Published var localRecordMessage: String?
    private var recoveryURL: URL
    private var progressObservation: NSKeyValueObservation?
    private let networkMonitor = NWPathMonitor()
    private var networkAvailable = true
    private var retry: Task<Void, Never>?
    private var retryCount = 0
    private var navigationGeneration = 0
    private var failedGeneration: Int?
    private var activeNavigation: WKNavigation?
    private var presentedDownloadURL: URL?
    private var downloads: [ObjectIdentifier: (WKDownload, URL?)] = [:]
    private let lastLocationKey = "hub.last-workspace"
    private let lastSessionKey = "hub.last-workspace-session-v1"
    private let recordKey = "hub.native-current-record-v1"
    private var recordTask: Task<Void, Never>?
    private var nativeHistory: [URL] = []
    private var requestedLocation: URL?
    private var authenticatedPageSession = ""
    private var recordPresentationGeneration = 0
    private var foreground = true

    init(baseURL: URL, connectAutomatically: Bool = true) {
        guard let policy = HubPolicy(baseURL: baseURL) else { fatalError("HubBaseURL must be a trusted HTTPS base URL") }
        self.policy = policy
        let config = WKWebViewConfiguration()
        config.websiteDataStore = .default()
        config.defaultWebpagePreferences.allowsContentJavaScript = true
        config.applicationNameForUserAgent = "BYBCHubIOS/0.1.1"
        self.webView = WKWebView(frame: .zero, configuration: config)
        recoveryURL = baseURL
        super.init()
        webView.navigationDelegate = self
        webView.uiDelegate = self
        webView.isOpaque = false
        webView.backgroundColor = UIColor(red: 0.07, green: 0.08, blue: 0.09, alpha: 1)
        webView.scrollView.backgroundColor = webView.backgroundColor
        webView.scrollView.contentInsetAdjustmentBehavior = .never
        // Back uses a fresh GET; WebKit's gesture snapshot can resurrect old HTML.
        webView.allowsBackForwardNavigationGestures = false
        webView.isInspectable = false
        progressObservation = webView.observe(\.estimatedProgress, options: [.new]) { [weak self] _, change in
            Task { @MainActor [weak self] in self?.progress = change.newValue ?? 0 }
        }
        networkMonitor.pathUpdateHandler = { [weak self] path in
            let available = path.status == .satisfied
            Task { @MainActor [weak self] in
                guard let self else { return }
                self.networkAvailable = available
                // Wi-Fi/cellular/VPN route changes can remain "satisfied" throughout.
                // Ask the shared workspace to verify its release on every usable path.
                if available { self.resume() }
            }
        }
        if connectAutomatically {
            networkMonitor.start(queue: DispatchQueue(label: "hub.network-state"))
            Task { [weak self] in
                guard let self else { return }
                let generation = self.navigationGeneration
                let identity = await self.currentSession()
                guard generation == self.navigationGeneration else { return }
                if !identity.isEmpty, identity == UserDefaults.standard.string(forKey: self.lastSessionKey),
                   let saved = UserDefaults.standard.string(forKey: self.lastLocationKey), let url = URL(string: saved), self.policy.isWorkspace(url) { self.recoveryURL = url }
                self.loadRecovery()
            }
        }
    }

    func resume() {
        guard foreground, networkAvailable else { return }
        if connectionError != nil && !isLoading { retryNow() }
        else if let url = webView.url, policy.isInternal(url) {
            // This starts the web app's own fresh-data check; it does not assert synchronization.
            webView.evaluateJavaScript("window.dispatchEvent(new Event('online'))", completionHandler: nil)
            startRecordCapture()
        }
    }

    func retryNow() {
        retry?.cancel()
        guard networkAvailable else { connectionError = "网络暂时不可用，联网后会恢复。"; return }
        guard !isLoading else { return }
        retryCount = 0
        loadRecovery()
    }

    private func loadRecovery() {
        retry?.cancel()
        // Only GET recovery is automatic. A failed form submission is never replayed here.
        webView.load(policy.pageRequest(recoveryURL))
    }

    func goBack() {
        guard let url = webView.url, policy.isInternal(url) else { loadRecovery(); return }
        let generation = navigationGeneration
        webView.evaluateJavaScript(Self.backScript) { [weak self] result, _ in
            guard let self, generation == self.navigationGeneration, url == self.webView.url,
                  self.foreground, (result as? Bool) != true else { return }
            if self.nativeHistory.count > 1 {
                self.nativeHistory.removeLast()
                self.webView.load(self.policy.pageRequest(self.nativeHistory.last!))
            }
        }
    }

    func openHome() { webView.load(policy.pageRequest(policy.baseURL)) }

    func activate() { foreground = true; resume() }

    func pause() {
        foreground = false
        recordPresentationGeneration += 1
        recordTask?.cancel()
        localRecord = nil
    }

    private func currentSession() async -> String {
        let cookies: [HTTPCookie] = await withCheckedContinuation { continuation in
            webView.configuration.websiteDataStore.httpCookieStore.getAllCookies { continuation.resume(returning: $0) }
        }
        let host = policy.baseURL.host?.lowercased() ?? ""
        guard let cookie = cookies.first(where: {
            $0.name == "codex_hub_session" && $0.domain.trimmingCharacters(in: CharacterSet(charactersIn: ".")).lowercased() == host
                && policy.baseURL.path.hasPrefix($0.path) && ($0.expiresDate == nil || $0.expiresDate! > Date())
        }) else { return "" }
        return HubLocalRecord.sessionDigest(cookie.value)
    }

    private func clearLocalRecord() {
        recordPresentationGeneration += 1
        recordTask?.cancel(); localRecord = nil
        for key in [recordKey, lastLocationKey, lastSessionKey] { UserDefaults.standard.removeObject(forKey: key) }
        nativeHistory.removeAll()
    }

    private func startRecordCapture() {
        guard foreground, !isLoading, connectionError == nil, let url = webView.url, policy.isWorkspace(url) else { return }
        recordTask?.cancel()
        let generation = navigationGeneration
        recordTask = Task { [weak self] in
            while !Task.isCancelled {
                guard let self, self.navigationGeneration == generation, self.webView.url == url,
                      !self.isLoading, self.connectionError == nil else { return }
                let identity = await self.currentSession()
                guard !Task.isCancelled, generation == self.navigationGeneration, self.webView.url == url else { return }
                guard !identity.isEmpty, identity == self.authenticatedPageSession else { self.clearLocalRecord(); return }
                do {
                    let result = try await self.webView.evaluateJavaScript(Self.recordScript)
                    guard !Task.isCancelled, generation == self.navigationGeneration, self.webView.url == url,
                          let values = result as? [String: Any], let user = values["user"] as? String,
                          let project = values["project"] as? String, let conversation = values["conversation"] as? String else { return }
                    let rows = (values["messages"] as? [[String: String]] ?? []).suffix(30).map {
                        HubLocalMessage(author: String(($0["author"] ?? "").prefix(100)), text: String(($0["text"] ?? "").prefix(1800)))
                    }
                    let record = HubLocalRecord(url: url, session: identity, user: user, project: project, conversation: conversation,
                        title: String((values["title"] as? String ?? "本机对话记录").prefix(160)), messages: Array(rows), savedAt: Date())
                    let finalIdentity = await self.currentSession()
                    guard !Task.isCancelled, generation == self.navigationGeneration, self.webView.url == url,
                          !self.isLoading, self.connectionError == nil,
                          record.readable(policy: self.policy, location: url, session: finalIdentity) else { return }
                    let data = try JSONEncoder().encode(record)
                    if data.count <= 240000 {
                        UserDefaults.standard.set(data, forKey: self.recordKey)
                        UserDefaults.standard.set(url.absoluteString, forKey: self.lastLocationKey)
                        UserDefaults.standard.set(identity, forKey: self.lastSessionKey)
                    }
                    try await Task.sleep(nanoseconds: 15_000_000_000)
                } catch { return }
            }
        }
    }

    func showLocalRecord() {
        guard foreground else { return }
        recordPresentationGeneration += 1
        let presentationGeneration = recordPresentationGeneration
        let generation = navigationGeneration
        let location = requestedLocation ?? webView.url ?? recoveryURL
        Task { [weak self] in
            guard let self else { return }
            let identity = await self.currentSession()
            guard generation == self.navigationGeneration, self.foreground,
                  presentationGeneration == self.recordPresentationGeneration else { return }
            guard let data = UserDefaults.standard.data(forKey: self.recordKey), data.count <= 240000,
                  let record = try? JSONDecoder().decode(HubLocalRecord.self, from: data),
                  record.readable(policy: self.policy, location: location, session: identity) else {
                self.localRecordMessage = "当前对话尚无可读取的本机记录。"
                return
            }
            self.localRecordMessage = nil
            self.localRecord = record
        }
    }

    static let recordScript = """
    (() => {
      const s=document.querySelector('[data-ws-shell]');
      if(!s?.dataset.wsRelease||!s.dataset.user||!s.dataset.project||!s.dataset.conversation)return null;
      const messages=Array.from(s.querySelectorAll('[data-ws-live] > .ws-message[data-ws-entry]')).slice(-30).map(n=>({
        author:(n.querySelector('.ws-message-meta strong')?.textContent||'').trim().slice(0,100),
        text:(n.querySelector('[data-ws-message-body]')?.innerText||'').trim().slice(0,1800)
      })).filter(n=>n.text);
      return {user:s.dataset.user,project:s.dataset.project,conversation:s.dataset.conversation,
        title:(s.querySelector('.ws-chat-title h1')?.textContent||document.title).trim().slice(0,160),messages};
    })()
    """

    static let backScript = """
    (() => {
      if (window.CodexHubMediaViewer?.back?.()) return true;
      const dialog = document.querySelector('dialog[open]');
      if (dialog) { if(dialog.dispatchEvent(new Event('cancel',{cancelable:true})) && dialog.open) dialog.close(); return true; }
      const shell = document.querySelector('[data-ws-shell]');
      if(shell?.classList.contains('is-sidebar-open')) { shell.querySelector('[data-ws-sidebar-close]')?.click(); return true; }
      if(shell?.classList.contains('is-inspector-open')) { shell.querySelector('[data-ws-inspector-close]')?.click(); return true; }
      return false;
    })()
    """

    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = navigationAction.request.url else { decisionHandler(.cancel); return }
        if navigationAction.targetFrame?.isMainFrame == true, policy.isAuthentication(url) { clearLocalRecord() }
        if policy.isInternal(url) {
            if navigationAction.targetFrame?.isMainFrame == true, navigationAction.request.httpMethod == "GET", policy.canRecover(url) {
                requestedLocation = url
                recoveryURL = url
            }
            if policy.isDownload(url) && (navigationAction.shouldPerformDownload || navigationAction.navigationType == .linkActivated || navigationAction.targetFrame == nil) { decisionHandler(.download) }
            else if navigationAction.targetFrame?.isMainFrame == true,
                    navigationAction.request.httpMethod == "GET", policy.canRecover(url),
                    navigationAction.request.cachePolicy != .reloadIgnoringLocalCacheData,
                    !policy.sameDocument(webView.url, url) {
                decisionHandler(.cancel)
                webView.load(policy.pageRequest(url))
            }
            else { decisionHandler(.allow) }
            return
        }
        decisionHandler(.cancel)
        if navigationAction.targetFrame?.isMainFrame != false && navigationAction.navigationType == .linkActivated && ["https", "http", "mailto", "tel"].contains(url.scheme?.lowercased() ?? "") {
            UIApplication.shared.open(url)
        }
    }

    func webView(_ webView: WKWebView, decidePolicyFor navigationResponse: WKNavigationResponse,
                 decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void) {
        guard let url = navigationResponse.response.url, policy.isInternal(url) else { decisionHandler(.cancel); return }
        if let response = navigationResponse.response as? HTTPURLResponse, response.statusCode >= 400,
           navigationResponse.isForMainFrame {
            failedGeneration = navigationGeneration
            fail("页面暂时无法连接（HTTP \(response.statusCode)）。已保存的草稿仍会保留。")
            decisionHandler(.cancel)
            return
        }
        let disposition = (navigationResponse.response as? HTTPURLResponse)?.value(forHTTPHeaderField: "Content-Disposition") ?? ""
        if policy.isDownload(url) && (!navigationResponse.canShowMIMEType || disposition.lowercased().hasPrefix("attachment")) {
            decisionHandler(.download)
        } else { decisionHandler(.allow) }
    }

    func webView(_ webView: WKWebView, didStartProvisionalNavigation navigation: WKNavigation!) {
        activeNavigation = navigation
        navigationGeneration += 1
        recordPresentationGeneration += 1
        authenticatedPageSession = ""
        recordTask?.cancel()
        localRecord = nil
        if let url = webView.url, policy.isAuthentication(url) { clearLocalRecord() }
        retry?.cancel()
        failedGeneration = nil
        isLoading = true
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        guard navigation === activeNavigation, failedGeneration != navigationGeneration else { return }
        guard let url = webView.url, policy.isInternal(url) else { return }
        let generation = navigationGeneration
        webView.evaluateJavaScript("(()=>{const s=document.querySelector('[data-ws-shell]');return {workspace:!!s,release:s?.dataset.wsRelease||document.documentElement.dataset.hubRelease||''};})()") { [weak self] modern, _ in
            guard let self, generation == self.navigationGeneration, url == self.webView.url,
                  self.failedGeneration != generation else { return }
            let state = modern as? [String: Any] ?? [:]
            let release = state["release"] as? String ?? ""
            if self.policy.isWorkspace(url) && ((state["workspace"] as? Bool) != true || !HubPolicy.currentRelease(release))
                || !release.isEmpty && !HubPolicy.currentRelease(release) {
                self.fail("正在连接新版工作区，稍后重试。可以查看本机只读记录。")
                return
            }
            self.isLoading = false; self.connectionError = nil; self.retryCount = 0
            if self.policy.canRecover(url) { self.recoveryURL = url }
            if self.policy.isAuthentication(url) { self.clearLocalRecord() }
            else if self.policy.canRecover(url), self.nativeHistory.last != url {
                self.nativeHistory.append(url)
                if self.nativeHistory.count > 40 { self.nativeHistory.removeFirst() }
            }
            Task { [weak self] in
                guard let self else { return }
                let identity = await self.currentSession()
                guard generation == self.navigationGeneration, url == self.webView.url else { return }
                self.authenticatedPageSession = identity
                self.resume()
            }
        }
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        guard navigation === activeNavigation else { return }
        navigationFailed(error)
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        guard navigation === activeNavigation else { return }
        navigationFailed(error)
    }

    private func navigationFailed(_ error: Error) {
        let issue = error as NSError
        if issue.domain == NSURLErrorDomain && issue.code == NSURLErrorCancelled { return }
        failedGeneration = navigationGeneration
        fail(networkAvailable ? "连接中断，正在尝试恢复。消息是否发送成功由 Hub 核对，不会自动重复提交。" : "网络暂时不可用，联网后会恢复。")
    }

    private func fail(_ message: String) {
        isLoading = false; connectionError = message
        retry?.cancel()
        guard networkAvailable && retryCount < 3 else { return }
        retryCount += 1
        let generation = navigationGeneration
        let seconds = UInt64(min(8, 1 << retryCount))
        retry = Task { [weak self] in
            do { try await Task.sleep(nanoseconds: seconds * 1_000_000_000) }
            catch { return }
            guard let self, !Task.isCancelled, self.navigationGeneration == generation,
                  !self.isLoading, self.networkAvailable, self.connectionError != nil else { return }
            self.loadRecovery()
        }
    }

    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        isLoading = false
        fail("页面进程已中断，正在恢复。已保存的草稿会在恢复后显示。")
    }

    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for navigationAction: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        guard navigationAction.targetFrame == nil, let url = navigationAction.request.url else { return nil }
        if policy.isInternal(url) {
            webView.load(navigationAction.request.httpMethod == "GET" ? policy.pageRequest(url) : navigationAction.request)
        }
        else if navigationAction.navigationType == .linkActivated && ["https", "http"].contains(url.scheme ?? "") { UIApplication.shared.open(url) }
        return nil
    }

    func webView(_ webView: WKWebView, navigationAction: WKNavigationAction, didBecome download: WKDownload) { attach(download) }
    func webView(_ webView: WKWebView, navigationResponse: WKNavigationResponse, didBecome download: WKDownload) { attach(download) }

    private func attach(_ download: WKDownload) {
        guard downloads.count < 3 else { download.cancel { _ in }; downloadMessage = "请等待当前文件下载完成。"; return }
        downloads[ObjectIdentifier(download)] = (download, nil)
        download.delegate = self
        isLoading = false
        downloadMessage = "正在下载文件，当前对话会保持打开。"
    }

    func download(_ download: WKDownload, decideDestinationUsing response: URLResponse,
                  suggestedFilename: String, completionHandler: @escaping (URL?) -> Void) {
        guard let url = response.url, policy.isDownload(url), response.expectedContentLength <= 1_073_741_824 else {
            completionHandler(nil); downloadMessage = "文件地址或大小不符合下载条件。"; return
        }
        do {
            let directory = FileManager.default.temporaryDirectory.appendingPathComponent("HubDownloads", isDirectory: true)
                .appendingPathComponent(UUID().uuidString, isDirectory: true)
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
            let file = directory.appendingPathComponent(HubPolicy.safeFilename(suggestedFilename))
            downloads[ObjectIdentifier(download)] = (download, file)
            completionHandler(file)
        } catch { completionHandler(nil); downloadMessage = "无法准备文件保存位置。" }
    }

    func download(_ download: WKDownload, willPerformHTTPRedirection response: HTTPURLResponse,
                  newRequest request: URLRequest, decisionHandler: @escaping (WKDownload.RedirectPolicy) -> Void) {
        decisionHandler(request.url.map(policy.isDownload) == true ? .allow : .cancel)
    }

    func downloadDidFinish(_ download: WKDownload) {
        guard let (_, path) = downloads.removeValue(forKey: ObjectIdentifier(download)), let path else { return }
        downloadMessage = "下载完成，选择“存储到文件”保存。"
        if presentedDownloadURL == nil { presentDownload(HubDownloadedFile(url: path)) }
        else { completedDownloads.append(HubDownloadedFile(url: path)) }
    }

    private var completedDownloads: [HubDownloadedFile] = []
    private func presentDownload(_ file: HubDownloadedFile) {
        presentedDownloadURL = file.url
        downloadedFile = file
    }

    func closeDownloadSheet() {
        // SwiftUI clears the sheet binding before onDismiss. Keep its owned URL
        // separately, or the cache and queued presentation would be lost.
        if let current = presentedDownloadURL { try? FileManager.default.removeItem(at: current.deletingLastPathComponent()) }
        presentedDownloadURL = nil
        downloadedFile = nil
        if !completedDownloads.isEmpty {
            let next = completedDownloads.removeFirst()
            presentedDownloadURL = next.url
            DispatchQueue.main.async { [weak self] in self?.downloadedFile = next }
        }
        else { downloadMessage = nil }
    }

    func download(_ download: WKDownload, didFailWithError error: Error, resumeData: Data?) {
        if let (_, path) = downloads.removeValue(forKey: ObjectIdentifier(download)), let path {
            try? FileManager.default.removeItem(at: path.deletingLastPathComponent())
        }
        downloadMessage = "文件未下载完成，请在原对话重试；没有提交新的任务。"
    }
}
