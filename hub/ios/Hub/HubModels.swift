import Foundation

struct HubUser: Codable, Equatable { let id: Int; let username: String }
struct HubIdentity: Codable, Equatable {
    let apiVersion: Int
    let release: String
    let user: HubUser
    let csrfToken: String
    let sessionKey: String
}
struct HubProject: Codable, Identifiable, Hashable {
    let id: Int
    let slug: String
    let name: String
    let description: String?
}
struct HubProjectList: Codable { let projects: [HubProject] }
struct HubConversation: Codable, Identifiable, Hashable { let id: Int; let title: String }
struct HubAgent: Codable, Identifiable, Hashable {
    let id: Int
    let name: String
    let job: String?
    let instructions: String?
    let kind: String?
    let active: Bool
    let canDispatch: Bool?
    let connectionLabel: String?
    let connectionState: String?
}
struct HubAttachment: Codable, Identifiable, Hashable {
    let id: Int
    let name: String
    let mimeType: String?
    let byteSize: Int?
    let downloadUrl: String
    let previewUrl: String?
    var sha256: String? = nil
}
struct HubMessage: Codable, Identifiable, Hashable {
    let id: Int
    let author: String
    let role: String?
    let text: String
    let createdAt: String?
    let taskId: Int?
    let receiptLabel: String?
    let attachments: [HubAttachment]
}
struct HubTask: Codable, Identifiable, Hashable {
    let id: Int
    let title: String
    let description: String?
    let status: String
    let resultSummary: String?
    let assignedAgentId: Int?
    let assignedAgentName: String?
    var statusLabel: String { ["open":"等待开始", "claimed":"已接手", "in_progress":"正在处理", "blocked":"需要处理", "review":"等待验收", "done":"已完成", "cancelled":"已取消"][status] ?? status }
}
struct HubWorkspace: Codable {
    let project: HubProject
    let conversation: HubConversation?
    let conversations: [HubConversation]
    let agents: [HubAgent]
    let tasks: [HubTask]
    let messages: [HubMessage]
    let defaultAgentId: Int?
    let olderEntriesBefore: Int?
    let olderRoomsBefore: Int?
    let sessionKey: String
    let csrfToken: String
    let progress: [HubProgress]
    let devices: [HubDevice]
    let revision: String?
    func shell(conversation id: Int?) -> HubWorkspace {
        HubWorkspace(project: project, conversation: conversations.first { $0.id == id }, conversations: conversations,
            agents: agents, tasks: tasks, messages: [], defaultAgentId: defaultAgentId, olderEntriesBefore: nil,
            olderRoomsBefore: olderRoomsBefore, sessionKey: sessionKey, csrfToken: csrfToken, progress: [], devices: devices, revision: nil)
    }
}
struct HubProgress: Codable, Identifiable {
    var id: Int { taskId }
    let taskId: Int
    let agentName: String?
    let statusText: String?
    let assistantText: String?
    let sequence: Int?
    let executionState: String?
    let executionContactValid: Bool?
    let deviceContactRecent: Bool?
    let reportAgeLabel: String?
    let runAgeLabel: String?
}
struct HubTaskDetail: Codable { let task: HubTask; let messages: [HubMessage]; let progress: [HubProgress]?; let olderEntriesBefore: Int? }
struct HubReceipt: Codable { let found: Bool?; let created: Bool?; let requestKey: String?; let conversationId: Int?; let taskIds: [Int]? }
struct HubTool: Codable, Identifiable, Hashable {
    let id: String
    let title: String
    let description: String?
    let action: String?
    let available: Bool?
}
struct HubToolbox: Codable { let tools: [HubTool] }
struct HubDevice: Codable, Identifiable {
    var id: Int { deviceId }
    let deviceId: Int
    let deviceLabel: String
    let isOnline: Bool
    let isAgentOnline: Bool
    let presenceLabel: String?
    let lastSeenAt: String?
}
struct HubDevices: Codable { let devices: [HubDevice] }
struct HubTasks: Codable { let tasks: [HubTask]; let olderBefore: Int? }
struct HubLibrary: Codable { let attachments: [HubAttachment]; let olderBefore: Int? }
struct HubRelease: Codable, Identifiable {
    var id: String { platform }
    let platform: String
    let platformLabel: String
    let version: String
    let filename: String
    let byteSize: Int
    let downloadUrl: String
    let sha256: String
}
struct HubReleases: Codable { let releases: [HubRelease] }
struct HubFeedbackResponse: Decodable { let feedback: HubFeedback }
struct HubFeedback: Decodable { let id: Int; let status: String; let title: String }

struct HubDraftFile: Codable, Identifiable, Hashable {
    let id: UUID
    let name: String
    let mimeType: String
    let bytes: Int
}
struct HubDraft: Codable, Equatable {
    var requestKey = UUID().uuidString
    var text = ""
    var files: [HubDraftFile] = []
    var agentIDs: [Int] = []
    // A failed transport can still have committed on the server. Only a
    // receipt, or an explicit same-key retry, may resolve that uncertainty.
    var awaitingReceipt = false
    var hasContent: Bool { !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || !files.isEmpty }
}
struct HubComposerState: Codable { let draft: HubDraft; let pending: HubDraft? }

enum HubNativeError: LocalizedError {
    case unauthorized, untrustedURL, incompatibleServer, invalidResponse, oversizedFile, missingFile, rejected(String)
    var errorDescription: String? {
        switch self {
        case .unauthorized: return "登录已失效，请重新登录。"
        case .untrustedURL: return "这个文件地址不属于当前 Hub。"
        case .incompatibleServer: return "服务器尚未提供此版本的原生接口，请更新 Hub 服务。"
        case .invalidResponse: return "服务器未返回可识别的数据，请稍后重试。"
        case .oversizedFile: return "附件最多 12 个，单个不超过 50 MB，总计不超过 100 MB。"
        case .missingFile: return "本机附件已不可用，请重新选择。"
        case .rejected(let message): return message
        }
    }
}

enum HubNativeRules {
    static func scope(project: String, conversation: Int?) -> String { "\(project)/\(conversation ?? 0)" }
    static func acceptsFiles(existing: [HubDraftFile], newBytes: Int) -> Bool {
        existing.count < 12 && newBytes >= 0 && newBytes <= 50 * 1024 * 1024
            && existing.reduce(newBytes, { $0 + $1.bytes }) <= 100 * 1024 * 1024
    }
    static func requestStillCurrent(generation: Int, expected: Int, session: String, expectedSession: String, scope: String, expectedScope: String) -> Bool {
        generation == expected && !session.isEmpty && session == expectedSession && scope == expectedScope
    }
}
