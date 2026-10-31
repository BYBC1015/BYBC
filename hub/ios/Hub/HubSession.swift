import Foundation
import Combine
import WebKit
import Network
import UniformTypeIdentifiers

struct HubDownloadedFile: Identifiable { let id = UUID(); let url: URL }
struct HubOK: Decodable { let ok: Bool? }

@MainActor
final class HubSession: ObservableObject {
    let policy: HubPolicy
    let transport: HubTransport
    let disk: HubDisk
    @Published private(set) var identity: HubIdentity?
    @Published private(set) var projects: [HubProject] = []
    @Published private(set) var workspace: HubWorkspace?
    @Published private(set) var tools: [HubTool] = []
    @Published private(set) var selectedProject: HubProject?
    @Published private(set) var selectedConversation: Int?
    @Published private(set) var olderMessages: [HubMessage] = []
    @Published private(set) var olderConversations: [HubConversation] = []
    @Published private(set) var olderMessageCursor: Int?
    @Published private(set) var olderRoomCursor: Int?
    @Published private(set) var savedAt: Date?
    @Published var draft = HubDraft() { didSet { scheduleDraftSave() } }
    @Published private(set) var pending: HubDraft?
    @Published var errorMessage: String?
    @Published var statusMessage: String?
    @Published private(set) var offline = false
    @Published private(set) var refreshing = false
    @Published private(set) var sending = false
    @Published private(set) var authenticating = false
    @Published private(set) var downloading = false
    @Published var previewFile: HubDownloadedFile?
    private var generation = 0, accountGeneration = 0
    private var fileGeneration = 0
    private var pagedMessages = false, pagedRooms = false
    private var resolvingLatest = false
    private var foreground = true, started = false
    private var refreshTask: Task<Void, Never>?, pollTask: Task<Void, Never>?, saveTask: Task<Void, Never>?
    private var sendTask: Task<Void, Never>?, fileTask: Task<Void, Never>?
    private let monitor = NWPathMonitor()
    private var scope: String { HubNativeRules.scope(project: selectedProject?.slug ?? "", conversation: selectedConversation) }
    var selectionScope: String { scope }
    var selectionGeneration: Int { generation }
    var accountScope: String { sessionKey }
    var isForeground: Bool { foreground }
    var conversations: [HubConversation] {
        var seen = Set<Int>(); return ((workspace?.conversations ?? []) + olderConversations).filter { seen.insert($0.id).inserted }
    }
#if DEBUG
    private var fixtureOffline = false
#endif
    private var sessionKey: String { identity?.sessionKey ?? "" }
    private func cacheKey(_ value: String) -> String { "\(identity?.user.id ?? 0)/\(sessionKey)/\(value)" }
    private func userKey(_ value: String) -> String { "user/\(identity?.user.id ?? 0)/\(value)" }
    var messages: [HubMessage] {
        var ids = Set<Int>()
        return (olderMessages + (workspace?.messages ?? [])).filter { ids.insert($0.id).inserted }
    }

    init(baseURL: URL, connectAutomatically: Bool = true, configuration: URLSessionConfiguration = .default, storageRoot: URL? = nil) {
        guard let policy = HubPolicy(baseURL: baseURL) else { fatalError("Invalid Hub address") }
        self.policy = policy; self.transport = HubTransport(policy: policy, configuration: configuration); self.disk = HubDisk(baseURL: baseURL, root: storageRoot)
        if connectAutomatically {
            monitor.pathUpdateHandler = { [weak self] path in
                Task { @MainActor [weak self] in
                    guard let self else { return }
                    if path.status == .satisfied { if self.started && self.foreground { self.refresh() } }
                    else { self.offline = true }
                }
            }
            monitor.start(queue: DispatchQueue(label: "hub.native.network"))
            Task { await bootstrap() }
        }
    }
    private var cookieDigest: String {
        let cookie = HTTPCookieStorage.shared.cookies(for: policy.baseURL)?.first { $0.name == "codex_hub_session" }
        return cookie.map { HubDisk.digest($0.value) } ?? ""
    }
    private func migrateLegacyCookie() async {
        let token = accountGeneration
        let key = "hub.native.cookie-migrated." + HubDisk.digest(policy.baseURL.absoluteString)
        guard !UserDefaults.standard.bool(forKey: key) else { return }
        // Cookie migration only. No web view, HTML page or script is created.
        let cookies: [HTTPCookie] = await withCheckedContinuation { continuation in
            WKWebsiteDataStore.default().httpCookieStore.getAllCookies { continuation.resume(returning: $0) }
        }
        guard token == accountGeneration, !authenticating else { return }
        if cookieDigest.isEmpty {
            for cookie in cookies where cookie.name == "codex_hub_session"
                && cookie.domain.trimmingCharacters(in: CharacterSet(charactersIn: ".")).lowercased() == policy.baseURL.host?.lowercased()
                && policy.baseURL.path.hasPrefix(cookie.path)
                && (cookie.expiresDate == nil || cookie.expiresDate! > Date()) { HTTPCookieStorage.shared.setCookie(cookie) }
        }
        UserDefaults.standard.set(true, forKey: key)
    }
    private func bootstrap() async {
        let token = accountGeneration
        await migrateLegacyCookie()
        guard token == accountGeneration, !authenticating else { return }
        if let saved = disk.load(HubRememberedIdentity.self, key: "identity"), !cookieDigest.isEmpty, saved.cookieDigest == cookieDigest {
            identity = saved.identity
            projects = disk.load(HubCached<[HubProject]>.self, key: cacheKey("projects"))?.read(session: sessionKey) ?? []
            tools = disk.load(HubCached<[HubTool]>.self, key: cacheKey("tools"))?.read(session: sessionKey) ?? []
            if let first = projects.first { selectProject(first) }
        }
        started = true; await authenticateExisting(); startPolling()
    }
    func activate() { foreground = true; if started { refresh(); startPolling() } }
    func pause() { foreground = false; pollTask?.cancel(); refreshTask?.cancel(); sendTask?.cancel(); generation += 1; refreshing = false; sending = false; persistDraft() }
    private func startPolling() {
        pollTask?.cancel()
        pollTask = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(4))
                guard !Task.isCancelled, let self, self.foreground else { return }
                if !self.refreshing && !self.sending && self.identity != nil { self.refresh() }
            }
        }
    }
    func login(username: String, password: String) async {
        guard !authenticating else { return }
        accountGeneration += 1; generation += 1; transport.cancelAll(); refreshTask?.cancel(); refreshing = false
        authenticating = true; errorMessage = nil
        let token = accountGeneration
        defer { authenticating = false }
        do {
            let result: HubIdentity = try await transport.post("api/mobile/login", body: ["username":username, "password":password])
            guard token == accountGeneration else { return }
            try adoptIdentity(result); await loadProjects(); started = true; startPolling()
        } catch { if token == accountGeneration { errorMessage = error.localizedDescription } }
    }
    private func authenticateExisting() async {
        guard !authenticating else { return }
        let token = accountGeneration
        do {
            let result: HubIdentity = try await transport.get("api/mobile/session")
            guard token == accountGeneration else { return }; try adoptIdentity(result); await loadProjects()
        } catch HubNativeError.unauthorized { if token == accountGeneration { resetAccount() } }
        catch { if token == accountGeneration { offline = true; if identity == nil { errorMessage = error.localizedDescription } } }
    }
    private func adoptIdentity(_ value: HubIdentity) throws {
        guard value.apiVersion == 1, !value.sessionKey.isEmpty else { throw HubNativeError.incompatibleServer }
        if let old = identity, old.user.id != value.user.id || old.sessionKey != value.sessionKey { resetAccount(clearCookies: false) }
        identity = value; offline = false
        try disk.save(HubRememberedIdentity(identity: value, cookieDigest: cookieDigest), key: "identity")
    }
    func logout() async {
        let request = try? transport.logoutRequest(csrf: identity?.csrfToken)
        resetAccount()
        if let request { await transport.logout(using: request) }
    }
    private func resetAccount(clearCookies: Bool = true) {
        // Drafts/outbox are bound to stable user ID, not a rotating login token.
        // Expiration hides them; only verified login to that user restores them.
        persistDraft()
        accountGeneration += 1; generation += 1; fileGeneration += 1
        refreshTask?.cancel(); pollTask?.cancel(); sendTask?.cancel(); saveTask?.cancel(); fileTask?.cancel(); transport.cancelAll()
        identity = nil; selectedProject = nil; selectedConversation = nil; workspace = nil
        projects = []; tools = []; draft = HubDraft(); pending = nil; previewFile = nil; olderMessages = []; olderConversations = []
        refreshing = false; sending = false; downloading = false; disk.remove("identity")
        if clearCookies { for cookie in HTTPCookieStorage.shared.cookies(for: policy.baseURL) ?? [] { HTTPCookieStorage.shared.deleteCookie(cookie) } }
    }
    func loadProjects() async {
#if DEBUG
        if fixtureOffline { return }
#endif
        let account = accountGeneration, key = sessionKey
        do {
            let response: HubProjectList = try await transport.get("api/mobile/projects")
            guard account == accountGeneration, key == sessionKey else { return }; projects = response.projects; offline = false
            try? disk.save(HubCached(session: key, savedAt: Date(), value: projects), key: cacheKey("projects"))
            if selectedProject == nil, let first = projects.first { selectProject(first) }
            let toolbox: HubToolbox = try await transport.get("api/mobile/toolbox")
            guard account == accountGeneration, key == sessionKey else { return }; tools = toolbox.tools
            try? disk.save(HubCached(session: key, savedAt: Date(), value: tools), key: cacheKey("tools"))
        } catch HubNativeError.unauthorized { if account == accountGeneration { resetAccount() } }
        catch { if account == accountGeneration { offline = true } }
    }
    func selectProject(_ project: HubProject) {
        persistDraft(); selectedProject = project
        let selection = disk.load(Int.self, key: userKey("selection/" + project.slug))
        selectedConversation = selection.flatMap { $0 > 0 ? $0 : nil }; resolvingLatest = selection == nil
        loadSelection()
    }
    func openConversation(_ id: Int?) {
        persistDraft(); selectedConversation = id; resolvingLatest = false
        if let project = selectedProject {
            try? disk.save(id ?? 0, key: userKey("selection/" + project.slug))
        }
        loadSelection()
    }
    private func loadSelection() {
        generation += 1; refreshTask?.cancel(); fileTask?.cancel(); fileGeneration += 1; downloading = false; sendTask?.cancel(); sending = false
        statusMessage = nil; errorMessage = nil; olderMessages = []; olderConversations = []; pagedMessages = false; pagedRooms = false
        let cached = disk.load(HubCached<HubWorkspace>.self, key: cacheKey("workspace/" + scope))
        workspace = cached?.read(session: sessionKey); savedAt = cached?.savedAt
        if workspace == nil, let project = selectedProject {
            workspace = disk.load(HubCached<HubWorkspace>.self, key: cacheKey("project/" + project.slug))?.read(session: sessionKey)?.shell(conversation: selectedConversation)
        }
        olderMessageCursor = workspace?.olderEntriesBefore; olderRoomCursor = workspace?.olderRoomsBefore
        let composer = disk.load(HubComposerState.self, key: userKey("composer/" + scope))
        draft = composer?.draft ?? disk.load(HubDraft.self, key: userKey("draft/" + scope)) ?? HubDraft()
        pending = composer?.pending ?? disk.load(HubDraft.self, key: userKey("pending/" + scope))
        if draft.awaitingReceipt && pending == nil { pending = draft; draft = HubDraft(agentIDs: draft.agentIDs) }
        if draft.hasContent || pending != nil { resolvingLatest = false }
        refreshing = false; refresh()
    }
    func refresh() {
#if DEBUG
        if fixtureOffline { return }
#endif
        guard identity != nil else { if started { Task { await authenticateExisting() } }; return }
        guard let project = selectedProject, !refreshing else { return }
        let token = generation, key = sessionKey, capturedScope = scope, conversation = selectedConversation, latest = resolvingLatest
        refreshing = true
        refreshTask = Task { [weak self] in
            guard let self else { return }; defer { if token == self.generation { self.refreshing = false } }
            do {
                let query = latest ? [] : [URLQueryItem(name: "conversation_id", value: String(conversation ?? 0))]
                let response: HubWorkspace = try await self.transport.get("api/mobile/projects/\(project.slug)/workspace", query: query)
                guard !Task.isCancelled, self.isCurrent(token, key, capturedScope), response.sessionKey == key else { return }
                if latest {
                    self.resolvingLatest = false
                    if !self.draft.hasContent, self.pending == nil, let room = response.conversation?.id {
                        let selected = HubNativeRules.scope(project: project.slug, conversation: room)
                        try? self.disk.save(HubCached(session: key, savedAt: Date(), value: response), key: self.cacheKey("workspace/" + selected))
                        self.openConversation(room); return
                    }
                    if response.conversation != nil {
                        self.workspace = response.shell(conversation: nil); self.offline = false; return
                    }
                }
                if self.workspace?.revision != response.revision || self.workspace == nil { self.workspace = response }
                self.offline = false; self.savedAt = Date()
                if !self.pagedMessages { self.olderMessageCursor = response.olderEntriesBefore }
                if !self.pagedRooms { self.olderRoomCursor = response.olderRoomsBefore }
                if self.draft.agentIDs.isEmpty, let agent = response.defaultAgentId { self.draft.agentIDs = [agent] }
                try? self.disk.save(HubCached(session: key, savedAt: Date(), value: response), key: self.cacheKey("workspace/" + capturedScope))
                try? self.disk.save(HubCached(session: key, savedAt: Date(), value: response), key: self.cacheKey("project/" + project.slug))
                if self.pending != nil && !self.sending { await self.checkReceipt() }
            } catch HubNativeError.unauthorized { if self.isCurrent(token, key, capturedScope) { self.resetAccount() } }
            catch { if !Task.isCancelled && self.isCurrent(token, key, capturedScope) { self.offline = true } }
        }
    }
    private func isCurrent(_ token: Int, _ key: String, _ capturedScope: String) -> Bool {
        HubNativeRules.requestStillCurrent(generation: generation, expected: token, session: sessionKey, expectedSession: key, scope: scope, expectedScope: capturedScope)
    }
    func loadOlderMessages() async {
        guard let project = selectedProject, let room = selectedConversation,
              let before = olderMessageCursor else { return }
        let token = generation, key = sessionKey, selected = scope
        do {
            let response: HubWorkspace = try await transport.get("api/mobile/projects/\(project.slug)/workspace", query: [URLQueryItem(name: "conversation_id", value: String(room)), URLQueryItem(name: "before_entry_id", value: String(before))])
            guard isCurrent(token, key, selected), response.sessionKey == key else { return }
            olderMessages = response.messages + olderMessages
            pagedMessages = true; olderMessageCursor = response.olderEntriesBefore
        } catch { if isCurrent(token, key, selected) { errorMessage = error.localizedDescription } }
    }
    func loadOlderConversations() async {
        guard let project = selectedProject, let before = olderRoomCursor else { return }
        let token = generation, key = sessionKey, selected = scope
        do {
            let value: HubWorkspace = try await transport.get("api/mobile/projects/\(project.slug)/workspace", query: [URLQueryItem(name: "conversation_id", value: String(selectedConversation ?? 0)), URLQueryItem(name: "rooms_before", value: String(before))])
            guard isCurrent(token, key, selected), value.sessionKey == key else { return }
            olderConversations += value.conversations; pagedRooms = true; olderRoomCursor = value.olderRoomsBefore
        } catch { if isCurrent(token, key, selected) { errorMessage = error.localizedDescription } }
    }
    private func scheduleDraftSave() {
        saveTask?.cancel(); guard identity != nil, selectedProject != nil else { return }
        saveTask = Task { [weak self] in try? await Task.sleep(for: .milliseconds(250)); guard !Task.isCancelled else { return }; self?.persistDraft() }
    }
    @discardableResult
    private func persistDraft() -> Bool {
        saveTask?.cancel(); guard identity != nil, selectedProject != nil else { return false }
        do {
            try disk.save(HubComposerState(draft: draft, pending: pending), key: userKey("composer/" + scope))
            disk.remove(userKey("draft/" + scope)); disk.remove(userKey("pending/" + scope))
            return true
        }
        catch { errorMessage = "草稿暂时无法保存到本机，请保留当前页面。"; return false }
    }
    func addFile(data: Data, name: String, mime: String) {
        do { draft.files.append(try disk.importFile(data: data, name: name, mime: mime, existing: draft.files)); persistDraft() }
        catch { errorMessage = error.localizedDescription }
    }
    func importURLs(_ urls: [URL]) {
        for url in urls {
            let accessed = url.startAccessingSecurityScopedResource(); defer { if accessed { url.stopAccessingSecurityScopedResource() } }
            do {
                let values = try url.resourceValues(forKeys: [.fileSizeKey, .contentTypeKey])
                guard HubNativeRules.acceptsFiles(existing: draft.files, newBytes: values.fileSize ?? 0) else { throw HubNativeError.oversizedFile }
                addFile(data: try Data(contentsOf: url), name: url.lastPathComponent, mime: values.contentType?.preferredMIMEType ?? "application/octet-stream")
            } catch { errorMessage = error.localizedDescription }
        }
    }
    func removeFile(_ file: HubDraftFile) {
        draft.files.removeAll { $0.id == file.id }; try? FileManager.default.removeItem(at: disk.fileURL(file.id)); persistDraft()
    }
    func send() {
        guard !sending, pending == nil, draft.hasContent, selectedProject != nil, identity != nil else { return }
        let original = draft
        var payload = draft; payload.awaitingReceipt = true; pending = payload
        draft = HubDraft(agentIDs: payload.agentIDs)
        guard persistDraft() else { draft = original; pending = nil; return }
        resolvingLatest = false; transmitPending()
    }
    func retryPending() { guard !sending, pending != nil, persistDraft() else { return }; transmitPending() }
    private func transmitPending() {
        guard let project = selectedProject, let identity, let payload = pending else { return }
        let token = generation, key = sessionKey, capturedScope = scope, conversation = selectedConversation
        sending = true; errorMessage = nil
        statusMessage = "正在发送，收到后会直接显示在这里。"
        sendTask = Task { [weak self] in
            guard let self else { return }; defer { if self.isCurrent(token, key, capturedScope) { self.sending = false } }
            do {
                let receipt = try await self.transport.upload(project: project.slug, conversation: conversation, draft: payload, csrf: self.workspace?.csrfToken ?? identity.csrfToken, disk: self.disk)
                guard !Task.isCancelled, self.isCurrent(token, key, capturedScope) else { return }
                self.finishSend(receipt, payload: payload, originalScope: capturedScope)
            } catch HubNativeError.unauthorized { if self.isCurrent(token, key, capturedScope) { self.resetAccount() } }
            catch {
                guard self.isCurrent(token, key, capturedScope) else { return }
                self.statusMessage = "正在核对发送结果。文字和附件已保留；联网后自动查收据，不会重复派单。"
                self.offline = true
            }
        }
    }
    func checkReceipt() async {
        guard let payload = pending, let project = selectedProject else { return }
        let token = generation, key = sessionKey, selected = scope
        do {
            let receipt: HubReceipt = try await transport.get("api/mobile/projects/\(project.slug)/receipts/\(payload.requestKey)")
            guard isCurrent(token, key, selected), pending?.requestKey == payload.requestKey else { return }
            if receipt.found == true { finishSend(receipt, payload: payload, originalScope: selected) }
            else { statusMessage = "服务器尚未收到。可以重试发送，同一条消息不会重复创建工作。" }
        } catch { /* Keep the exact request key and immutable pending draft. */ }
    }
    private func finishSend(_ receipt: HubReceipt, payload: HubDraft, originalScope: String) {
        guard let room = receipt.conversationId, room > 0, receipt.taskIds?.isEmpty == false,
              selectedConversation == nil || selectedConversation == room else { errorMessage = "服务器回复不完整，请检查发送结果。"; return }
        guard pending?.requestKey == payload.requestKey else { return }
        let target = HubNativeRules.scope(project: selectedProject?.slug ?? "", conversation: room)
        do { try disk.save(HubComposerState(draft: draft, pending: nil), key: userKey("composer/" + target)) }
        catch { errorMessage = "消息已送达，但下一条草稿尚未安全保存，请保留当前页面。"; return }
        disk.remove(userKey("pending/" + originalScope)); for file in payload.files { try? FileManager.default.removeItem(at: disk.fileURL(file.id)) }
        pending = nil; sending = false; statusMessage = "已收到，智能体会在这场对话中继续回复。"
        if selectedConversation != receipt.conversationId { disk.remove(userKey("composer/" + originalScope)) }
        if selectedConversation != receipt.conversationId { selectedConversation = receipt.conversationId; generation += 1; refreshTask?.cancel(); refreshing = false; workspace = nil }
        if let project = selectedProject, let room = selectedConversation { try? disk.save(room, key: userKey("selection/" + project.slug)) }
        persistDraft(); refresh()
    }
    func cancelWaiting() {
        sendTask?.cancel(); sending = false; statusMessage = "已停止等待。服务器可能已收到，下一步先检查发送结果。"; Task { await checkReceipt() }
    }
    func taskDetail(_ task: HubTask, before: Int? = nil) async throws -> HubTaskDetail {
        guard let project = selectedProject else { throw HubNativeError.invalidResponse }
        return try await cachedRead("api/mobile/projects/\(project.slug)/tasks/\(task.id)" + (before.map { "?before=\($0)" } ?? ""))
    }
    func cachedRead<T: Codable>(_ path: String) async throws -> T {
        let key = cacheKey(path), account = sessionKey, token = accountGeneration, selection = scope
        guard !account.isEmpty else { throw HubNativeError.unauthorized }
        if offline, let cached = disk.load(HubCached<T>.self, key: key)?.read(session: account) { return cached }
#if DEBUG
        if fixtureOffline { throw URLError(.notConnectedToInternet) }
#endif
        let value: T = try await transport.get(path)
        guard sessionKey == account, accountGeneration == token, scope == selection else { throw CancellationError() }
        try? disk.save(HubCached(session: account, savedAt: Date(), value: value), key: key); return value
    }
    func openAttachment(_ attachment: HubAttachment) {
        guard !downloading else { return }; let account = accountGeneration; fileGeneration += 1; let token = fileGeneration; downloading = true
        fileTask = Task { [weak self] in
            guard let self else { return }; defer { if self.fileGeneration == token { self.downloading = false } }
            do {
                let url = try await self.transport.download(attachment, disk: self.disk)
                guard !Task.isCancelled, self.accountGeneration == account, self.fileGeneration == token else { try? FileManager.default.removeItem(at: url.deletingLastPathComponent()); return }
                self.previewFile = HubDownloadedFile(url: url)
            } catch { if !Task.isCancelled && self.accountGeneration == account { self.errorMessage = error.localizedDescription } }
        }
    }
    func closePreview() { if let file = previewFile { try? FileManager.default.removeItem(at: file.url.deletingLastPathComponent()) }; previewFile = nil }
    func thumbnail(_ file: HubAttachment) async throws -> URL {
        guard let preview = file.previewUrl, (file.mimeType ?? "").hasPrefix("image/"), identity != nil else { throw HubNativeError.invalidResponse }
        let account = accountGeneration, token = generation, key = sessionKey, selected = scope
        let cache = cacheKey("thumbnail/\(file.id)/" + HubDisk.digest(preview))
        if let saved = disk.load(HubCached<String>.self, key: cache)?.read(session: key), FileManager.default.fileExists(atPath: saved) { return URL(fileURLWithPath: saved) }
        let copy = HubAttachment(id: file.id, name: "预览-\(file.id).webp", mimeType: "image/webp", byteSize: nil, downloadUrl: preview, previewUrl: nil)
        let url = try await transport.download(copy, disk: disk)
        guard account == accountGeneration, isCurrent(token, key, selected), !Task.isCancelled else { try? FileManager.default.removeItem(at: url.deletingLastPathComponent()); throw CancellationError() }
        try? disk.save(HubCached(session: key, savedAt: Date(), value: url.path), key: cache)
        return url
    }
    func feedback(title: String, body: String, kind: String, key: String) async throws {
        guard let project = selectedProject, let identity else { throw HubNativeError.unauthorized }
        let _: HubFeedbackResponse = try await transport.post("api/mobile/projects/\(project.slug)/feedback", body: ["request_key":key,"kind":kind,"title":title,"body":body,"platform":"ios-native-0.2.0","conversation_id":selectedConversation ?? 0], csrf: workspace?.csrfToken ?? identity.csrfToken)
    }
#if DEBUG
    // Simulator-only native interaction fixture. Transport is also replaced by
    // HubNoNetworkProtocol in the testing app, so no path can reach production.
    func installFixture(reset: Bool = false, userID: Int = 1, login: String = "fixture-session", composerDetails: Bool = false) throws {
        if reset { disk.clear() }
        fixtureOffline = true
        let value = HubIdentity(apiVersion: 1, release: "2.12.0", user: HubUser(id: userID, username: "native-fixture"), csrfToken: "fixture-csrf", sessionKey: login)
        try adoptIdentity(value)
        var object = try JSONSerialization.jsonObject(with: HubFixtures.workspace) as! [String: Any]
        object["session_key"] = login; object["csrf_token"] = "fixture-csrf"
        let fixture = try HubTransport.decoder.decode(HubWorkspace.self, from: JSONSerialization.data(withJSONObject: object))
        projects = try HubTransport.decoder.decode(HubProjectList.self, from: HubFixtures.projects).projects
        tools = try HubTransport.decoder.decode(HubToolbox.self, from: HubFixtures.toolbox).tools
        let scope = HubNativeRules.scope(project: fixture.project.slug, conversation: fixture.conversation?.id)
        try disk.save(HubCached(session: login, savedAt: Date(), value: fixture), key: cacheKey("workspace/" + scope))
        try disk.save(HubCached(session: login, savedAt: Date(), value: fixture), key: cacheKey("project/" + fixture.project.slug))
        if disk.load(Int.self, key: userKey("selection/" + fixture.project.slug)) == nil, let room = fixture.conversation?.id { try disk.save(room, key: userKey("selection/" + fixture.project.slug)) }
        let detail = try HubTransport.decoder.decode(HubTaskDetail.self, from: HubFixtures.task)
        try disk.save(HubCached(session: login, savedAt: Date(), value: detail), key: cacheKey("api/mobile/projects/\(fixture.project.slug)/tasks/\(detail.task.id)"))
        let devices = try HubTransport.decoder.decode(HubDevices.self, from: HubFixtures.devices)
        try disk.save(HubCached(session: login, savedAt: Date(), value: devices), key: cacheKey("api/mobile/projects/\(fixture.project.slug)/devices"))
        let library = try HubTransport.decoder.decode(HubLibrary.self, from: HubFixtures.library)
        try disk.save(HubCached(session: login, savedAt: Date(), value: library), key: cacheKey("api/mobile/projects/\(fixture.project.slug)/library"))
        let releases = try HubTransport.decoder.decode(HubReleases.self, from: HubFixtures.releases)
        try disk.save(HubCached(session: login, savedAt: Date(), value: releases), key: cacheKey("api/releases"))
        offline = true; selectProject(fixture.project)
        if composerDetails {
            draft.text = "Attachment and pending draft"
            draft.files = [try disk.importFile(data: Data("Native UI fixture attachment".utf8), name: "参考说明.txt", mime: "text/plain", existing: [])]
            pending = HubDraft(text: "上一条消息已经送出，等待确认回执。", awaitingReceipt: true)
            statusMessage = "网络暂未恢复。\n上一条消息等待确认。\n本条文字和附件已经保存在本机。"
            persistDraft()
        }
    }
    func expireFixtureSession() { resetAccount() }
#endif
}
