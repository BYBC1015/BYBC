import Foundation

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
        return parts.count == 2 || (parts.count == 4 && parts[2] == "conversations" && (Int(parts[3]) ?? 0) > 0)
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
