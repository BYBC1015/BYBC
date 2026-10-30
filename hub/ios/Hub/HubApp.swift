import SwiftUI
import WebKit

@main
struct HubApp: App {
    @Environment(\.scenePhase) private var scenePhase
    @StateObject private var session: HubSession

    init() {
        let address = Bundle.main.object(forInfoDictionaryKey: "HubBaseURL") as? String ?? ""
        guard let url = URL(string: address), HubPolicy(baseURL: url) != nil else { fatalError("Invalid HubBaseURL configuration") }
        _session = StateObject(wrappedValue: HubSession(baseURL: url))
    }

    var body: some Scene {
        WindowGroup {
            HubScreen(session: session)
                .preferredColorScheme(.dark)
                .onChange(of: scenePhase) { _, state in if state == .active { session.resume() } }
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
            HubWebView(webView: session.webView)
        }
        .background(Color(red: 0.07, green: 0.08, blue: 0.09))
        .sheet(item: $session.downloadedFile, onDismiss: session.closeDownloadSheet) { file in
            HubShareSheet(file: file.url)
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
