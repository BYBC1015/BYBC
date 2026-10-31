import SwiftUI
import PhotosUI
import UniformTypeIdentifiers
import QuickLook
import CoreTransferable

private let hubAccent = Color(red: 0.84, green: 0.77, blue: 0.66)
private let hubCanvas = Color(red: 0.08, green: 0.09, blue: 0.10)

private struct HubPickedFile: Transferable {
    let data: Data
    static var transferRepresentation: some TransferRepresentation {
        FileRepresentation(importedContentType: .data) { received in
            let size = try received.file.resourceValues(forKeys: [.fileSizeKey]).fileSize ?? 0
            guard size <= 50 * 1024 * 1024 else { throw HubNativeError.oversizedFile }
            return HubPickedFile(data: try Data(contentsOf: received.file, options: .mappedIfSafe))
        }
    }
}

struct HubNativeRoot: View {
    @ObservedObject var session: HubSession
    @State private var tab = 0
    var body: some View {
        Group {
            if session.identity == nil { HubLoginView(session: session) }
            else {
                TabView(selection: $tab) {
                    NavigationStack { HubChatView(session: session) }.tabItem { Label("聊天", systemImage: "bubble.left.and.bubble.right") }.tag(0)
                    NavigationStack { HubProjectsView(session: session, chat: { tab = 0 }) }.tabItem { Label("项目", systemImage: "square.grid.2x2") }.tag(1)
                    NavigationStack { HubAgentsView(session: session, chat: { tab = 0 }) }.tabItem { Label("智能体", systemImage: "person.2") }.tag(2)
                    NavigationStack { HubTasksView(session: session) }.tabItem { Label("工作", systemImage: "checklist") }.tag(3)
                    NavigationStack { HubToolsView(session: session) }.tabItem { Label("工具箱", systemImage: "briefcase") }.tag(4)
                }
                .id(session.accountScope + "/" + session.selectionScope)
            }
        }
        .tint(hubAccent).background(hubCanvas)
        .alert("Hub 提示", isPresented: Binding(get: { session.errorMessage != nil }, set: { if !$0 { session.errorMessage = nil } })) {
            Button("知道了", role: .cancel) { session.errorMessage = nil }
        } message: { Text(session.errorMessage ?? "") }
        .sheet(item: $session.previewFile) { file in
            NavigationStack {
                HubQuickLook(url: file.url).navigationTitle(file.url.lastPathComponent).navigationBarTitleDisplayMode(.inline)
                    .toolbar {
                        ToolbarItem(placement: .cancellationAction) { Button("关闭") { session.closePreview() } }
                        ToolbarItem(placement: .primaryAction) { ShareLink(item: file.url) { Label("保存或分享", systemImage: "square.and.arrow.up") } }
                    }
            }
        }
    }
}

struct HubLoginView: View {
    @ObservedObject var session: HubSession
    @State private var username = ""
    @State private var password = ""
    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 24) {
                Image("Owl").resizable().scaledToFit().frame(width: 74, height: 74)
                Text("你的团队，随时在身边。").font(.largeTitle.weight(.semibold))
                Text("登录 BYBC Hub，继续项目与对话。").foregroundStyle(.secondary)
                TextField("用户名", text: $username).textContentType(.username).textInputAutocapitalization(.never).autocorrectionDisabled().padding(16).background(.white.opacity(0.06), in: RoundedRectangle(cornerRadius: 16)).accessibilityIdentifier("login.username")
                SecureField("密码", text: $password).textContentType(.password).padding(16).background(.white.opacity(0.06), in: RoundedRectangle(cornerRadius: 16)).accessibilityIdentifier("login.password")
                Button {
                    Task { await session.login(username: username, password: password); password = "" }
                } label: { HStack { Text(session.authenticating ? "正在登录…" : "进入工作区"); Spacer(); Image(systemName: "arrow.right") }.padding(12) }
                .buttonStyle(.borderedProminent).disabled(session.authenticating || username.isEmpty || password.isEmpty)
                Text("此客户端使用本机界面与导航，连接仅用于同步消息和文件。").font(.footnote).foregroundStyle(.secondary)
            }.padding(28).frame(maxWidth: 540)
        }.frame(maxWidth: .infinity).background(hubCanvas)
    }
}

struct HubConnectionBanner: View {
    @ObservedObject var session: HubSession
    var body: some View {
        if session.offline {
            HStack(alignment: .top) {
                Image(systemName: "wifi.slash")
                VStack(alignment: .leading, spacing: 3) {
                    Text("离线 · 可继续查看和写草稿").font(.subheadline)
                    if let date = session.savedAt { Text("记录保存于 \(date.formatted())").font(.caption).foregroundStyle(.secondary) }
                }
                Spacer(); Button("重试") { session.refresh() }.frame(minHeight: 44)
            }.padding(.horizontal, 16).padding(.vertical, 8).background(.orange.opacity(0.12))
        }
    }
}

struct HubChatView: View {
    @ObservedObject var session: HubSession
    @Environment(\.verticalSizeClass) private var verticalSizeClass
    @State private var showRooms = false
    @State private var showAgents = false
    @State private var chooseFiles = false
    @State private var photos: [PhotosPickerItem] = []
    @State private var followLatest = true
    @FocusState private var composerFocused: Bool
    @State private var filePickerScope = ""
    @State private var filePickerAccount = ""
    @State private var filePickerGeneration = 0
    var body: some View {
        VStack(spacing: 0) {
            HubConnectionBanner(session: session)
            if session.selectedProject == nil { ContentUnavailableView("选择一个项目", systemImage: "square.grid.2x2", description: Text("在项目页打开工作区，消息会留在对应项目中。")) }
            else {
                ScrollViewReader { proxy in
                    ScrollView {
                        LazyVStack(alignment: .leading, spacing: 22) {
                            if session.olderMessageCursor != nil { Button("查看更早消息") { Task { await session.loadOlderMessages() } }.frame(maxWidth: .infinity, minHeight: 44) }
                            if session.messages.isEmpty && session.workspace?.progress.isEmpty != false {
                                VStack(alignment: .leading, spacing: 12) { Text("想让团队做点什么？").font(.title2.weight(.semibold)); Text("直接写下目标，或添加图片与文件。").foregroundStyle(.secondary) }.padding(.vertical, 40)
                            }
                            ForEach(session.messages) { message in HubMessageRow(message: message, session: session).id(message.id) }
                            ForEach(session.workspace?.progress ?? []) { progress in HubProgressRow(progress: progress) }
                            Color.clear.frame(height: 1).id("latest").onAppear { followLatest = true }.onDisappear { followLatest = false }
                        }.padding(18)
                    }
                    .scrollDismissesKeyboard(.interactively)
                    .accessibilityIdentifier("chat.messages")
                    .refreshable { session.refresh() }
                    .onChange(of: session.messages.last?.id) { _, _ in if followLatest { withAnimation(.easeOut(duration: 0.18)) { proxy.scrollTo("latest", anchor: .bottom) } } }
                    .overlay(alignment: .bottomTrailing) {
                        if !followLatest { Button { proxy.scrollTo("latest", anchor: .bottom) } label: { Label("最新消息", systemImage: "arrow.down") }.buttonStyle(.borderedProminent).padding(12) }
                    }
                }
            }
        }
        .background(hubCanvas)
        .navigationTitle(session.workspace?.conversation?.title ?? "新对话")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .topBarLeading) { Button { showRooms = true } label: { Image(systemName: "bubble.left.and.text.bubble.right").frame(width: 44, height: 44) }.accessibilityLabel("切换对话") }
            ToolbarItem(placement: .topBarTrailing) { Image("Owl").resizable().scaledToFit().frame(width: 30, height: 30).accessibilityHidden(true) }
            ToolbarItem(placement: .topBarTrailing) {
                if composerFocused {
                    Button { composerFocused = false } label: { Image(systemName: "keyboard.chevron.compact.down").frame(width: 44, height: 44) }
                        .accessibilityLabel("收起键盘").accessibilityIdentifier("chat.dismiss-keyboard")
                }
            }
        }
        .safeAreaInset(edge: .bottom, spacing: 0) { if session.selectedProject != nil { composer } }
        .sheet(isPresented: $showRooms) { HubConversationPicker(session: session, close: { showRooms = false }) }
        .sheet(isPresented: $showAgents) { recipientPicker }
        .fileImporter(isPresented: $chooseFiles, allowedContentTypes: [.item], allowsMultipleSelection: true) { result in
            guard filePickerScope == session.selectionScope, filePickerAccount == session.accountScope, filePickerGeneration == session.selectionGeneration else { return }
            switch result { case .success(let urls): session.importURLs(urls); case .failure(let error): session.errorMessage = error.localizedDescription }
        }
        .onChange(of: photos) { _, items in
            let scope = session.selectionScope, account = session.accountScope, generation = session.selectionGeneration
            Task {
                for item in items {
                    do { if let imported = try await item.loadTransferable(type: HubPickedFile.self) {
                        guard account == session.accountScope, scope == session.selectionScope, generation == session.selectionGeneration else { return }
                        let type = item.supportedContentTypes.first ?? .image; session.addFile(data: imported.data, name: "照片-\(UUID().uuidString.prefix(6)).\(type.preferredFilenameExtension ?? "jpg")", mime: type.preferredMIMEType ?? "image/jpeg")
                    } }
                    catch { session.errorMessage = error.localizedDescription }
                }
                photos = []
            }
        }
    }
    private var compactComposer: Bool { verticalSizeClass == .compact }
    private var composerLayout: AnyLayout {
        compactComposer ? AnyLayout(HStackLayout(alignment: .center, spacing: 8)) : AnyLayout(VStackLayout(alignment: .leading, spacing: 9))
    }
    private var composer: some View {
        VStack(alignment: .leading, spacing: 9) {
            if let status = session.statusMessage { Text(status).font(.caption).foregroundStyle(.secondary).lineLimit(3) }
            if let pending = session.pending {
                HStack {
                    Text(pending.text.isEmpty ? "上一条附件消息" : pending.text).font(.caption).lineLimit(2)
                    Spacer()
                    if session.sending { Button("停止等待", action: session.cancelWaiting) }
                    else { Button("查收") { Task { await session.checkReceipt() } }; Button("重试", action: session.retryPending) }
                }.padding(9).background(.white.opacity(0.06), in: RoundedRectangle(cornerRadius: 12))
            }
            if !session.draft.files.isEmpty {
                ScrollView(.horizontal) {
                    HStack { ForEach(session.draft.files) { file in
                        HStack(spacing: 8) { Image(systemName: "doc"); Text(file.name).font(.caption).lineLimit(1).frame(maxWidth: 170); Button { session.removeFile(file) } label: { Image(systemName: "xmark").frame(width: 44, height: 44) }.accessibilityLabel("移除 \(file.name)").disabled(session.draft.awaitingReceipt) }.padding(.leading, 10).background(.white.opacity(0.07), in: RoundedRectangle(cornerRadius: 12))
                    } }
                }
            }
            // AnyLayout keeps the same TextField and FocusState when rotating.
            composerLayout {
                Button { showAgents = true } label: {
                    Label("交给 " + recipientNames, systemImage: "person.crop.circle").font(.subheadline).lineLimit(1).frame(minHeight: 44)
                }.frame(maxWidth: compactComposer ? 120 : nil, alignment: .leading)
                TextField("写下目标，或添加图片和文件…", text: $session.draft.text, axis: .vertical)
                    .lineLimit(compactComposer ? 1...2 : 1...5).font(.body).padding(.vertical, 5).frame(maxWidth: .infinity)
                    .disabled(session.draft.awaitingReceipt).focused($composerFocused).accessibilityIdentifier("chat.composer")
                HStack(spacing: compactComposer ? 4 : 12) {
                    PhotosPicker(selection: $photos, maxSelectionCount: max(1, 12 - session.draft.files.count), matching: .any(of: [.images, .videos])) { Image(systemName: "photo").frame(width: 44, height: 44) }.disabled(session.draft.awaitingReceipt).accessibilityLabel("添加图片或视频")
                    Button { filePickerScope = session.selectionScope; filePickerAccount = session.accountScope; filePickerGeneration = session.selectionGeneration; chooseFiles = true } label: { Image(systemName: "paperclip").frame(width: 44, height: 44) }.accessibilityLabel("添加文件")
                    if !compactComposer { Spacer() }
                    Button(action: session.send) { Image(systemName: "arrow.up").frame(width: 44, height: 44) }.buttonStyle(.borderedProminent).clipShape(Circle()).disabled(!session.draft.hasContent || session.pending != nil).accessibilityLabel("发送消息")
                }.fixedSize(horizontal: compactComposer, vertical: false)
            }
        }.padding(compactComposer ? 8 : 14).background(Color.white.opacity(0.07), in: RoundedRectangle(cornerRadius: 24)).padding(.horizontal, 10).padding(.vertical, 8).background(hubCanvas)
    }
    private var recipientNames: String { (session.workspace?.agents.filter { session.draft.agentIDs.contains($0.id) }.map(\.name) ?? []).joined(separator: "、").isEmpty ? "总控" : (session.workspace?.agents.filter { session.draft.agentIDs.contains($0.id) }.map(\.name) ?? []).joined(separator: "、") }
    private var recipientPicker: some View {
        NavigationStack {
            List(session.workspace?.agents ?? []) { agent in
                Button {
                    if session.draft.agentIDs.contains(agent.id) { session.draft.agentIDs.removeAll { $0 == agent.id } } else { session.draft.agentIDs.append(agent.id) }
                } label: { HStack { VStack(alignment: .leading) { Text(agent.name); Text(agent.job ?? "").font(.caption).foregroundStyle(.secondary) }; Spacer(); if session.draft.agentIDs.contains(agent.id) { Image(systemName: "checkmark.circle.fill") } } }.disabled(!agent.active || agent.canDispatch == false)
            }.navigationTitle("这次交给谁").toolbar { ToolbarItem(placement: .confirmationAction) { Button("完成") { showAgents = false } } }
        }.presentationDetents([.medium, .large])
    }
}

struct HubMessageRow: View {
    let message: HubMessage
    @ObservedObject var session: HubSession
    var body: some View {
        VStack(alignment: .leading, spacing: 9) {
            HStack { Text(message.author).font(.subheadline.weight(.semibold)); Spacer(); if let date = message.createdAt { Text(date.replacingOccurrences(of: "T", with: " ").prefix(16)).font(.caption2).foregroundStyle(.secondary) } }
            Text(message.text).font(.body).textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
            ForEach(message.attachments) { attachment in
                HubAttachmentRow(file: attachment, session: session)
            }
            if let receipt = message.receiptLabel, !receipt.isEmpty { Text(receipt).font(.caption).foregroundStyle(.secondary) }
        }.padding(16).frame(maxWidth: .infinity, alignment: .leading).background(message.role == "user" ? hubAccent.opacity(0.13) : Color.white.opacity(0.045), in: RoundedRectangle(cornerRadius: 20))
    }
}
struct HubAttachmentRow: View {
    let file: HubAttachment
    @ObservedObject var session: HubSession
    @State private var thumbnail: UIImage?
    var body: some View {
        Button { session.openAttachment(file) } label: {
            VStack(alignment: .leading, spacing: 8) {
                if let thumbnail { Image(uiImage: thumbnail).resizable().scaledToFit().frame(maxHeight: 190).clipShape(RoundedRectangle(cornerRadius: 12)) }
                Label(file.name, systemImage: (file.mimeType ?? "").hasPrefix("image/") ? "photo" : "doc").font(.subheadline).lineLimit(2).frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
            }
        }.disabled(session.downloading).task(id: file.previewUrl) {
            if let url = try? await session.thumbnail(file), !Task.isCancelled { thumbnail = UIImage(contentsOfFile: url.path) }
        }
    }
}
struct HubProgressRow: View {
    let progress: HubProgress
    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Label(progress.agentName ?? "智能体", systemImage: "circle.dotted").font(.subheadline.weight(.semibold))
            if let text = progress.assistantText, !text.isEmpty { Text(text).textSelection(.enabled) }
            if let status = progress.statusText, !status.isEmpty { Text(status).font(.caption).foregroundStyle(.secondary) }
            if progress.executionContactValid == false { Label("执行连接中断，等待恢复", systemImage: "wifi.exclamationmark").font(.caption).foregroundStyle(.orange) }
            if let age = progress.reportAgeLabel, !age.isEmpty { Text(age).font(.caption).foregroundStyle(.secondary) }
        }.frame(maxWidth: .infinity, alignment: .leading).padding(16).background(.white.opacity(0.05), in: RoundedRectangle(cornerRadius: 20))
    }
}

struct HubConversationPicker: View {
    @ObservedObject var session: HubSession
    var close: () -> Void
    var body: some View {
        NavigationStack {
            List {
                Button("发起新对话", systemImage: "square.and.pencil") { session.openConversation(nil); close() }
                ForEach(session.conversations) { room in Button { session.openConversation(room.id); close() } label: { Label(room.title, systemImage: "bubble.left") }.frame(minHeight: 44).accessibilityIdentifier("room.\(room.id)") }
                if session.olderRoomCursor != nil { Button("更早的对话") { Task { await session.loadOlderConversations() } } }
            }.navigationTitle("工作对话").toolbar { ToolbarItem(placement: .confirmationAction) { Button("完成", action: close) } }
        }
    }
}

struct HubProjectsView: View {
    @ObservedObject var session: HubSession
    var chat: () -> Void
    var body: some View {
        List {
            Section { HubConnectionBanner(session: session) }
            Section("我的项目") { ForEach(session.projects) { project in
                Button { session.selectProject(project); chat() } label: { VStack(alignment: .leading, spacing: 8) { Text(project.name).font(.headline); if let text = project.description, !text.isEmpty { Text(text).font(.subheadline).foregroundStyle(.secondary).lineLimit(3) } }.padding(.vertical, 8) }.accessibilityIdentifier("project.\(project.slug)")
            } }
            if session.projects.isEmpty { Text("暂无项目记录。联网后下拉更新。").foregroundStyle(.secondary) }
        }.navigationTitle("项目").refreshable { await session.loadProjects() }
    }
}

struct HubAgentsView: View {
    @ObservedObject var session: HubSession
    var chat: () -> Void
    var body: some View {
        List(session.workspace?.agents ?? []) { agent in
            NavigationLink { HubAgentDetail(agent: agent, session: session, chat: chat) } label: {
                HStack(spacing: 14) { Text(String(agent.name.prefix(1))).font(.headline).frame(width: 48, height: 48).background(hubAccent.opacity(0.2), in: Circle()); VStack(alignment: .leading, spacing: 5) { Text(agent.name).font(.headline); Text(agent.job ?? "").font(.subheadline).foregroundStyle(.secondary); Text(agent.connectionLabel ?? "连接状态待核对").font(.caption).foregroundStyle(.secondary) } }.padding(.vertical, 6)
            }.accessibilityIdentifier("agent.\(agent.id)")
        }.navigationTitle("智能体").overlay { if session.workspace?.agents.isEmpty != false { ContentUnavailableView("先选择项目", systemImage: "person.2", description: Text("这里显示项目内的长期执行者。")) } }
    }
}
struct HubAgentDetail: View {
    let agent: HubAgent
    @ObservedObject var session: HubSession
    var chat: () -> Void
    var body: some View {
        List {
            Section("职责") { Text(agent.job ?? "暂无职责说明"); Text(agent.instructions ?? "").textSelection(.enabled) }
            Section("执行连接") { Text(agent.connectionLabel ?? "等待核对"); Text(agent.active ? "身份已启用" : "身份已暂停") }
            Button("交给他") { if !session.draft.awaitingReceipt { session.draft.agentIDs = [agent.id]; chat() } }.disabled(!agent.active || agent.canDispatch == false || session.draft.awaitingReceipt)
        }.navigationTitle(agent.name).navigationBarTitleDisplayMode(.inline)
    }
}

struct HubTasksView: View {
    @ObservedObject var session: HubSession
    @State private var history: [HubTask] = []
    @State private var before: Int?
    @State private var loadedHistory = false
    @State private var historyError: String?
    var body: some View {
        List {
            Section("当前工作") { ForEach(session.workspace?.tasks ?? []) { task in
            NavigationLink { HubTaskScreen(task: task, session: session) } label: { VStack(alignment: .leading, spacing: 7) { Text(task.title).font(.headline); Text(task.statusLabel).font(.caption).foregroundStyle(hubAccent); if let text = task.resultSummary, !text.isEmpty { Text(text).font(.subheadline).foregroundStyle(.secondary).lineLimit(3) } }.padding(.vertical, 6) }.accessibilityIdentifier("task.\(task.id)")
            } }
            if session.workspace?.tasks.isEmpty != false { Text("当前没有待办").foregroundStyle(.secondary) }
            Section("历史工作") {
                ForEach(history) { task in NavigationLink { HubTaskScreen(task: task, session: session) } label: { VStack(alignment: .leading) { Text(task.title); Text(task.statusLabel).font(.caption).foregroundStyle(.secondary) } } }
                if !loadedHistory || before != nil { Button(loadedHistory ? "更早的工作" : "查看历史工作") { Task { await loadHistory() } } }
                if loadedHistory && history.isEmpty { Text("暂无历史工作").foregroundStyle(.secondary) }
                if let historyError { Text(historyError).font(.caption).foregroundStyle(.secondary) }
            }
        }.navigationTitle("工作进展").refreshable { session.refresh() }
    }
    private func loadHistory() async {
        guard let project = session.selectedProject else { return }
        do {
            let path = "api/mobile/projects/\(project.slug)/tasks?history=1" + (before.map { "&before=\($0)" } ?? "")
            let response: HubTasks = try await session.cachedRead(path)
            var ids = Set(history.map(\.id)); history += response.tasks.filter { ids.insert($0.id).inserted }
            before = response.olderBefore; loadedHistory = true; historyError = nil
        } catch { historyError = error.localizedDescription }
    }
}
struct HubTaskScreen: View {
    let task: HubTask
    @ObservedObject var session: HubSession
    @State private var detail: HubTaskDetail?
    @State private var error: String?
    @State private var older: [HubMessage] = []
    @State private var before: Int?
    @State private var paged = false
    var body: some View {
        ScrollView { VStack(alignment: .leading, spacing: 20) {
            Text(task.title).font(.title2.weight(.semibold)); Text((detail?.task ?? task).statusLabel).foregroundStyle(hubAccent)
            Text((detail?.task ?? task).description ?? "").textSelection(.enabled)
            if let result = (detail?.task ?? task).resultSummary, !result.isEmpty { Text("当前结果").font(.headline); Text(result).textSelection(.enabled) }
            if before != nil { Button("更早的记录") { Task { await load(more: true) } } }
            ForEach(older + (detail?.messages ?? [])) { message in HubMessageRow(message: message, session: session) }
            ForEach(detail?.progress ?? []) { progress in HubProgressRow(progress: progress) }
            if let error { Text(error).foregroundStyle(.secondary); Button("重试") { Task { await load() } } }
        }.padding(20).frame(maxWidth: .infinity, alignment: .leading) }.navigationTitle("工作详情").navigationBarTitleDisplayMode(.inline).task {
            while !Task.isCancelled { if session.isForeground { await load() }; try? await Task.sleep(for: .seconds(4)) }
        }
    }
    private func load(more: Bool = false) async {
        do {
            let value = try await session.taskDetail(task, before: more ? before : nil)
            guard !Task.isCancelled else { return }
            if more { older = value.messages + older; paged = true; before = value.olderEntriesBefore }
            else { detail = value; if !paged { before = value.olderEntriesBefore } }
            error = nil
        } catch { if !Task.isCancelled { self.error = error.localizedDescription } }
    }
}

struct HubToolsView: View {
    @ObservedObject var session: HubSession
    var body: some View {
        List {
            Section("项目工具") { ForEach(session.tools) { tool in NavigationLink { HubToolScreen(tool: tool, session: session) } label: { VStack(alignment: .leading, spacing: 6) { Text(tool.title).font(.headline); Text(tool.description ?? "").font(.subheadline).foregroundStyle(.secondary) }.padding(.vertical, 7) }.accessibilityIdentifier("tool.\(tool.id)") } }
            Section("本机") { Text("iOS 0.2.0 · 本机界面"); Text("大模型与制作任务由在线电脑执行。手机离线时仍可查看已同步内容、保存草稿。").font(.footnote).foregroundStyle(.secondary) }
            Button("退出登录", role: .destructive) { Task { await session.logout() } }
        }.navigationTitle("工具箱")
    }
}
struct HubToolScreen: View {
    let tool: HubTool
    @ObservedObject var session: HubSession
    var body: some View {
        Group {
            switch tool.action ?? tool.id {
            case "library": HubLibraryScreen(session: session)
            case "devices": HubDeviceScreen(session: session)
            case "feedback": HubFeedbackScreen(session: session)
            case "downloads": HubDownloadsScreen(session: session)
            default: ContentUnavailableView("此工具尚未接入手机", systemImage: "wrench.and.screwdriver", description: Text("当前安装版没有此功能的本机页面。"))
            }
        }.navigationTitle(tool.title).navigationBarTitleDisplayMode(.inline)
    }
}
struct HubLibraryScreen: View {
    @ObservedObject var session: HubSession
    @State private var attachments: [HubAttachment] = []
    @State private var before: Int?
    @State private var error: String?
    var body: some View {
        List { ForEach(attachments) { file in Button { session.openAttachment(file) } label: { Label(file.name, systemImage: (file.mimeType ?? "").hasPrefix("image/") ? "photo" : "doc").frame(minHeight: 44) }.disabled(session.downloading) }
            if before != nil { Button("加载更多") { Task { await load(more: true) } } }
            if let error { Text(error).foregroundStyle(.secondary) }
        }.overlay { if session.downloading { ProgressView("正在下载文件…").padding().background(.regularMaterial, in: RoundedRectangle(cornerRadius: 16)) } }.task { await load() }.refreshable { await load() }
    }
    private func load(more: Bool = false) async {
        guard let project = session.selectedProject else { error = "先选择项目，再查看项目文件。"; return }
        do {
            let suffix = more ? "?before=\(before ?? 0)" : ""
            let result: HubLibrary = try await session.cachedRead("api/mobile/projects/\(project.slug)/library" + suffix)
            attachments = more ? attachments + result.attachments : result.attachments; before = result.olderBefore; error = nil
        } catch { self.error = error.localizedDescription }
    }
}
struct HubDeviceScreen: View {
    @ObservedObject var session: HubSession
    @State private var devices: [HubDevice] = []
    @State private var error: String?
    var body: some View {
        List { ForEach(devices) { device in VStack(alignment: .leading, spacing: 8) { Label(device.deviceLabel, systemImage: "desktopcomputer").font(.headline); Text(device.presenceLabel ?? (device.isOnline ? "设备在线" : "设备离线")); Text(device.isAgentOnline ? "执行连接可用" : "执行连接尚未就绪").font(.caption).foregroundStyle(.secondary) }.padding(.vertical, 7) }; if let error { Text(error) } }.task { await load() }.refreshable { await load() }
    }
    private func load() async { guard let project = session.selectedProject else { error = "请先选择项目。"; return }; do { let value: HubDevices = try await session.cachedRead("api/mobile/projects/\(project.slug)/devices"); devices = value.devices; error = nil } catch { self.error = error.localizedDescription } }
}
struct HubFeedbackScreen: View {
    @ObservedObject var session: HubSession
    @State private var title = ""
    @State private var bodyText = ""
    @State private var kind = "bug"
    @State private var key = UUID().uuidString
    @State private var result: String?
    @State private var saving = false
    var body: some View {
        Form {
            Picker("类型", selection: $kind) { Text("遇到问题").tag("bug"); Text("改进想法").tag("idea") }
            TextField("简要说明", text: $title)
            TextField("发生了什么，或者希望怎样改进…", text: $bodyText, axis: .vertical).lineLimit(5...12)
            Button(saving ? "正在提交…" : "提交反馈") { Task { saving = true; defer { saving = false }; do { try await session.feedback(title: title, body: bodyText, kind: kind, key: key); result = "已记录，工程师会结合设备与运行记录处理。" } catch { result = error.localizedDescription } } }.disabled(saving || title.isEmpty || bodyText.isEmpty)
            if let result { Text(result).foregroundStyle(.secondary) }
        }.disabled(saving).onChange(of: title) { _, _ in key = UUID().uuidString }.onChange(of: bodyText) { _, _ in key = UUID().uuidString }.onChange(of: kind) { _, _ in key = UUID().uuidString }
    }
}
struct HubDownloadsScreen: View {
    @ObservedObject var session: HubSession
    @State private var releases: [HubRelease] = []
    @State private var error: String?
    var body: some View {
        List { ForEach(releases) { release in VStack(alignment: .leading, spacing: 9) {
            Text("\(release.platformLabel) · \(release.version)").font(.headline)
            Text(release.filename).font(.caption).foregroundStyle(.secondary)
            Button("下载并保存") { session.openAttachment(HubAttachment(id: 0, name: release.filename, mimeType: nil, byteSize: release.byteSize, downloadUrl: release.downloadUrl, previewUrl: nil, sha256: release.sha256)) }.disabled(session.downloading)
        }.padding(.vertical, 8) }; if let error { Text(error) } }.task { do { let value: HubReleases = try await session.cachedRead("api/releases"); releases = value.releases } catch { self.error = error.localizedDescription } }
    }
}
struct HubQuickLook: UIViewControllerRepresentable {
    let url: URL
    func makeCoordinator() -> Coordinator { Coordinator(url: url) }
    func makeUIViewController(context: Context) -> QLPreviewController { let controller = QLPreviewController(); controller.dataSource = context.coordinator; return controller }
    func updateUIViewController(_ controller: QLPreviewController, context: Context) {}
    final class Coordinator: NSObject, QLPreviewControllerDataSource {
        let url: URL; init(url: URL) { self.url = url }
        func numberOfPreviewItems(in controller: QLPreviewController) -> Int { 1 }
        func previewController(_ controller: QLPreviewController, previewItemAt index: Int) -> QLPreviewItem { url as NSURL }
    }
}
