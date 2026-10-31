import SwiftUI
import WebKit

@main
struct HubApp: App {
    @Environment(\.scenePhase) private var scenePhase
    @StateObject private var session: HubSession

    init() {
        // Hosted XCTest must not contact the user's live Hub during CI.
        let verificationOnly = ProcessInfo.processInfo.environment["HUB_TESTING"] == "1"
        let address = verificationOnly ? "https://example.invalid/hub/" : (Bundle.main.object(forInfoDictionaryKey: "HubBaseURL") as? String ?? "")
        guard let url = URL(string: address), HubPolicy(baseURL: url) != nil else { fatalError("Invalid HubBaseURL configuration") }
        _session = StateObject(wrappedValue: HubSession(baseURL: url, connectAutomatically: !verificationOnly))
    }

    var body: some Scene {
        WindowGroup {
            HubScreen(session: session)
                .preferredColorScheme(.dark)
                .onChange(of: scenePhase) { _, state in if state == .active { session.activate() } else { session.pause() } }
        }
    }
}

struct HubScreen: View {
    @ObservedObject var session: HubSession
    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 16) {
                Button(action: session.goBack) { Image(systemName: "chevron.left").frame(width: 44, height: 44) }
                    .accessibilityLabel("返回，先关闭当前面板")
                Image("Owl").resizable().scaledToFit().frame(width: 28, height: 28).accessibilityHidden(true)
                Text("BYBC Hub").font(.system(.headline, design: .default).weight(.semibold))
                Spacer(minLength: 0)
                Menu {
                    Button("回到项目", systemImage: "square.grid.2x2", action: session.openHome)
                    Button("重新连接", systemImage: "arrow.clockwise", action: session.retryNow)
                    Button("本机对话记录", systemImage: "clock", action: session.showLocalRecord)
                } label: { Image(systemName: "ellipsis").frame(width: 44, height: 44) }
                .accessibilityLabel("Hub 操作")
            }
            .padding(.horizontal, 10)
            .foregroundStyle(Color(red: 0.86, green: 0.81, blue: 0.72))
            if session.isLoading {
                ProgressView(value: session.progress).tint(Color(red: 0.82, green: 0.75, blue: 0.65))
                    .accessibilityLabel("页面载入进度")
            }
            if let message = session.connectionError {
                HStack(alignment: .center, spacing: 12) {
                    Text(message).font(.subheadline).fixedSize(horizontal: false, vertical: true)
                    Button("重试", action: session.retryNow).buttonStyle(.bordered).disabled(session.isLoading)
                }.padding(12).background(Color.orange.opacity(0.14)).accessibilityElement(children: .combine)
            }
            if let message = session.downloadMessage {
                Text(message).font(.footnote).frame(maxWidth: .infinity, alignment: .leading)
                    .padding(10).background(Color.white.opacity(0.06))
            }
            ZStack {
                HubWebView(webView: session.webView)
                    .opacity(session.connectionError == nil ? 1 : 0)
                    .allowsHitTesting(session.connectionError == nil)
                if session.connectionError != nil {
                    ScrollView {
                        VStack(spacing: 20) {
                            Image("Owl").resizable().scaledToFit().frame(width: 64, height: 64)
                            Text("工作区仍在这里").font(.title2.weight(.semibold))
                            Text("连接恢复后将核对最新消息。草稿与待发送附件仍保留在本机。")
                                .font(.body).foregroundStyle(.secondary).multilineTextAlignment(.center)
                            Button("查看本机对话记录", action: session.showLocalRecord).buttonStyle(.borderedProminent)
                            if let message = session.localRecordMessage { Text(message).font(.footnote).foregroundStyle(.secondary) }
                        }.padding(24).frame(maxWidth: .infinity)
                    }
                }
            }
        }
        .background(Color(red: 0.07, green: 0.08, blue: 0.09))
        .sheet(item: $session.downloadedFile, onDismiss: session.closeDownloadSheet) { file in
            HubShareSheet(file: file.url)
        }
        .sheet(item: $session.localRecord) { record in
            NavigationStack {
                ScrollView {
                    VStack(alignment: .leading, spacing: 22) {
                        Text("本机只读记录 · \(record.savedAt.formatted())\n不是实时状态，联网后将核对最新消息。")
                            .font(.footnote).foregroundStyle(.secondary)
                        ForEach(Array(record.messages.enumerated()), id: \.offset) { _, message in
                            VStack(alignment: .leading, spacing: 8) {
                                Text(message.author).font(.subheadline.weight(.semibold))
                                Text(message.text).font(.body).textSelection(.enabled)
                            }.frame(maxWidth: .infinity, alignment: .leading)
                        }
                    }.padding(20)
                }.navigationTitle(record.title).navigationBarTitleDisplayMode(.inline)
                    .toolbar { ToolbarItem(placement: .confirmationAction) { Button("返回") { session.localRecord = nil } } }
            }.preferredColorScheme(.dark)
        }
    }
}

struct HubWebView: UIViewRepresentable {
    let webView: WKWebView
    func makeUIView(context: Context) -> WKWebView { webView }
    func updateUIView(_ uiView: WKWebView, context: Context) { }
}

struct HubShareSheet: UIViewControllerRepresentable {
    let file: URL
    func makeUIViewController(context: Context) -> UIActivityViewController {
        UIActivityViewController(activityItems: [file], applicationActivities: nil)
    }
    func updateUIViewController(_ uiViewController: UIActivityViewController, context: Context) { }
}
