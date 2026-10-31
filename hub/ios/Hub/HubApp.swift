import SwiftUI

@main
@MainActor
struct HubApp: App {
    @Environment(\.scenePhase) private var scenePhase
    @StateObject private var session: HubSession
    init() {
        let testing = ProcessInfo.processInfo.environment["HUB_TESTING"] == "1"
        let address = testing ? "https://example.invalid/hub/" : (Bundle.main.object(forInfoDictionaryKey: "HubBaseURL") as? String ?? "")
        guard let url = URL(string: address), HubPolicy(baseURL: url) != nil else { fatalError("Invalid Hub address") }
        let configuration = URLSessionConfiguration.default
#if DEBUG
        if testing { configuration.protocolClasses = [HubNoNetworkProtocol.self] }
#endif
        let model = HubSession(baseURL: url, connectAutomatically: !testing, configuration: configuration)
#if DEBUG
        if testing && ProcessInfo.processInfo.environment["HUB_UI_TESTING"] == "1" {
            try? model.installFixture(reset: ProcessInfo.processInfo.environment["HUB_RESET_FIXTURE"] == "1")
        }
#endif
        _session = StateObject(wrappedValue: model)
    }
    var body: some Scene {
        WindowGroup {
            HubNativeRoot(session: session).preferredColorScheme(.dark)
                .onChange(of: scenePhase) { _, state in if state == .active { session.activate() } else { session.pause() } }
        }
    }
}
