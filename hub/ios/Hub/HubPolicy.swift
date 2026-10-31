import Foundation
import CryptoKit

struct HubPolicy {
    let baseURL: URL

    init?(baseURL: URL) {
        guard let parts = URLComponents(url: baseURL, resolvingAgainstBaseURL: false),
              parts.scheme?.lowercased() == "https", parts.host?.isEmpty == false,
              parts.user == nil, parts.password == nil, parts.query == nil, parts.fragment == nil,
              parts.port == nil || (1...65535).contains(parts.port!),
              Self.cleanPath(parts.percentEncodedPath) != nil else { return nil }
        self.baseURL = baseURL
    }

    func isInternal(_ url: URL) -> Bool {
        guard let root = URLComponents(url: baseURL, resolvingAgainstBaseURL: false),
              let parts = URLComponents(url: url, resolvingAgainstBaseURL: false),
              parts.scheme?.lowercased() == "https", parts.user == nil, parts.password == nil,
              parts.host?.lowercased() == root.host?.lowercased(),
              (parts.port ?? 443) == (root.port ?? 443),
              let path = Self.cleanPath(parts.percentEncodedPath),
              let prefix = Self.cleanPath(root.percentEncodedPath) else { return false }
        let normalized = prefix.hasSuffix("/") ? prefix : prefix + "/"
        return path == String(normalized.dropLast()) || path.hasPrefix(normalized)
    }

    func components(_ url: URL) -> [String]? {
        guard isInternal(url) else { return nil }
        let prefix = baseURL.path.split(separator: "/")
        return url.path.split(separator: "/").dropFirst(prefix.count).map(String.init)
    }

    func isWorkspace(_ url: URL) -> Bool {
        guard let parts = components(url), parts.count >= 2, parts[0] == "projects" else { return false }
        // Archive is a real maintenance screen, not a conversation to snapshot or restore.
        if URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems?.contains(where: { $0.name == "view" && $0.value == "archive" }) == true { return false }
        return parts.count == 2 || (parts.count == 4 && parts[2] == "conversations" && (Int(parts[3]) ?? 0) > 0)
    }

    func isAuthentication(_ url: URL) -> Bool { components(url).map { $0.count == 1 && ["login", "logout"].contains($0[0]) } ?? false }

    func sameDocument(_ current: URL?, _ next: URL) -> Bool {
        guard let current, next.fragment != nil,
              var previous = URLComponents(url: current, resolvingAgainstBaseURL: false),
              var target = URLComponents(url: next, resolvingAgainstBaseURL: false) else { return false }
        previous.fragment = nil; target.fragment = nil
        return previous == target
    }

    static func currentRelease(_ release: String) -> Bool {
        let parts = release.split(separator: ".", omittingEmptySubsequences: false)
        let values = parts.compactMap { Int($0) }
        return parts.count == 3 && values.count == 3 && values.allSatisfy { $0 >= 0 }
            && (values[0] > 2 || values[0] == 2 && values[1] >= 11)
    }

    func isDownload(_ url: URL) -> Bool {
        guard let parts = components(url) else { return false }
        return parts.count == 2 && parts[0] == "downloads"
            || parts.count == 4 && parts[0] == "projects" && parts[2] == "attachments" && (Int(parts[3]) ?? 0) > 0
    }

    func canRecover(_ url: URL) -> Bool {
        guard let parts = components(url) else { return false }
        return !isDownload(url) && !["api", "static", "logout"].contains(parts.first ?? "")
    }

    func pageRequest(_ url: URL) -> URLRequest {
        // A page recovery is a new GET, never a replay of a submitted form.
        // Keep the website data store and content-addressed resource cache intact.
        var request = URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 30)
        request.setValue("no-cache", forHTTPHeaderField: "Cache-Control")
        return request
    }

    static func safeFilename(_ value: String) -> String {
        let forbidden = CharacterSet.controlCharacters.union(CharacterSet(charactersIn: "/\\:"))
        let sanitized = String(String.UnicodeScalarView(value.unicodeScalars.map { forbidden.contains($0) ? Unicode.Scalar(95)! : $0 }))
            .trimmingCharacters(in: .whitespacesAndNewlines)
        return sanitized.isEmpty || [".", ".."].contains(sanitized) ? "Hub 文件" : String(sanitized.prefix(160))
    }

    private static func cleanPath(_ encoded: String) -> String? {
        guard let path = encoded.removingPercentEncoding,
              !path.contains("\\"), !path.unicodeScalars.contains(where: CharacterSet.controlCharacters.contains),
              !path.split(separator: "/", omittingEmptySubsequences: false).contains(where: { $0 == "." || $0 == ".." }) else { return nil }
        return path.isEmpty ? "/" : path
    }
}

struct HubLocalMessage: Codable { let author: String; let text: String }
struct HubLocalRecord: Codable, Identifiable {
    var id: String { url.absoluteString }
    let url: URL
    let session: String
    let user: String
    let project: String
    let conversation: String
    let title: String
    let messages: [HubLocalMessage]
    let savedAt: Date

    func readable(policy: HubPolicy, location: URL, session current: String, now: Date = Date()) -> Bool {
        guard !current.isEmpty, current == session, location == url, policy.isWorkspace(location),
              savedAt <= now, now.timeIntervalSince(savedAt) <= 7 * 24 * 60 * 60,
              let parts = policy.components(location), (Int(user) ?? 0) > 0,
              parts[1] == project else { return false }
        return parts.count == 2 ? conversation == "new" : parts[3] == conversation
    }

    static func sessionDigest(_ value: String) -> String {
        guard !value.isEmpty else { return "" }
        return SHA256.hash(data: Data(value.utf8)).map { String(format: "%02x", $0) }.joined()
    }
}
